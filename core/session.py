"""core/session.py — 会话层：把「一次任务」串成「一段对话」。

================================================================================
它补的是哪条裂缝
================================================================================

`core/gateway.py` 的 `handle(request)` 里，`ctx_id` 有值 → 从 store 恢复同一次任务；
没有 → **新建一个空 Context**（`history=[]`、`loop_count=0`）。于是「同一个用户聊 5 轮」
在系统里等于 5 个互不相干的 Context —— 用户说「那它呢？」，Loop 完全不知道「它」指谁。

    Gateway             = 一次请求 → 一次任务（有界、可续跑）
    Session（本模块）   = 一段对话 → 多轮任务（跨轮承接）       ← 这里

`contracts.OWNER_TABLE` 里 `session_id` 归 GATEWAY，架构文档也一直有 `Context.session_id`，
但从来没人做「承接」这件事。本模块只补承接，不改任何既有契约。

================================================================================
三条设计裁决（为什么长这样）
================================================================================

① **只承接对话骨架，不承接工具回包**（本文件最重要的一条）
   每轮只往 `history` 追加 `{"role", "text"}` 两条（user / assistant）。
   `tool_results` / `observations` / `usage` / `pending_confirmation` 一律**不带**：
     - 它们是**单次任务**的事实（哪次调用、哪个工具、什么参数、谁的审批），
       只对产生它的那次任务有意义；
     - 跨轮带过来会让 Context **无限膨胀**：一轮 8 步、每步 3 个工具回包，
       5 轮就是 120 条原始 payload，而模型真正需要的往往只是「用户问过什么、
       我上次怎么答的」；
     - 更硬的理由：工具回包是最容易夹带**过期/越权事实**的地方。把上一轮
       （可能是另一个租户上下文里）的工具结果带进本轮 Prompt，等于让模型
       拿旧观测当新证据 —— 上下文污染比上下文缺失危险得多。
   所以 `Session.history` 的每个 dict 在落库前都会被 `_skeleton()` 压成
   恰好两个键（`role` / `text`），多出来的键在构造与反序列化两侧都被丢弃。

② **存储与 ContextStore 分开**（不同的键空间、不同的生命周期）
   Context 是「一次任务」的状态（可能挂起 4 小时等人审批），Session 是「一段对话」
   的轨迹（TTL 7 天）。混在一起，`list_pending()` 会把会话当成待续跑任务捞出来，
   `resume_from_store()` 会拿会话 payload 去当 Context 反序列化。所以本模块用
   `session-<id>` 这个**前缀键空间**复用同一个底层 store 的 save/load/delete，
   但语义完全独立 —— 底层还是 `ContextStore` 协议那四个方法，互换实现不用改一行。

③ **超长滚动压缩 + TTL 惰性清理**
   - `len(history) > max_history` → 调 `summarizer`（`core/summary.HistoryCompressor`）
     把溢出部分压成 `summary`，history 只留最近 `max_history` 条；
   - 过期会话在 `get()` 里**顺手删掉**（惰性清理）。不做后台线程/定时任务：
     一个需要 cron 才能正确的东西，部署时第一个被忘掉。

依赖纪律：只读 `contracts.py`（本文件其实连它都不用），不 import `core/gateway.py`、
不改 `core/store.py` / `core/sqlite_store.py` / `core/summary.py`。底层 store 通过
鸭子类型（`save/load/delete/list_pending`）注入，`None` 时退化为进程内内存。

跑法::

    cd /Users/supre/Documents/tmp/agent-runtime
    .venv/bin/python -m unittest tests.test_session -v
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, replace
from typing import Any, Callable, Mapping, Sequence

__all__ = [
    "Session",
    "SessionStore",
    "SessionError",
    "SessionCorrupt",
    "SessionOwnershipError",
    "ROLE_USER",
    "ROLE_ASSISTANT",
    "SKELETON_KEYS",
    "new_session_id",
]

log = logging.getLogger(__name__)

# ── 常量 ──────────────────────────────────────────────────────
#: 骨架里只允许存在这两个角色（`system` 不进会话：它是单次任务的重规划产物）
ROLE_USER = "user"
ROLE_ASSISTANT = "assistant"
#: 「对话骨架」的全部键。多一个键都不进 Session（见模块 docstring 裁决①）。
SKELETON_KEYS: tuple[str, ...] = ("role", "text")

#: 会话键空间前缀：与 Context 的 ctx_id 共用一个底层 store，但不共用一个语义
_KEY_PREFIX = "session-"
#: 落库信封版本。不认识的版本**不猜**（与 core/store.py 同一纪律）
_SCHEMA_VERSION = 1
#: 会话 id 当文件名用，规则与 `core.store.validate_ctx_id` 对齐（但更严：
#: 只允许 `[A-Za-z0-9._-]`，且必须以字母数字/下划线开头）
_RE_SESSION_ID = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]{0,127}$")
#: 记住多少个「最近过期」的 id（供上层区分「过期」与「从未存在」）
_EXPIRED_MEMO = 256


class SessionError(Exception):
    """会话层失败的基类（坏信封 / 归属不符 / 参数非法）。"""


class SessionCorrupt(SessionError):
    """落库内容读不回来（非 JSON / 版本不认识 / 结构不符）。"""


class SessionOwnershipError(SessionError):
    """会话归属（tenant / user）与调用方不一致。

    上层 `SessionGateway` 会先把这种情况变成 403；store 层再兜一道 —— 因为
    「谁的会话」是数据完整性约束，不能只靠调用方自觉。
    """


# ══════════════════════════════════════════════════════════════
# 纯函数：骨架化 / 校验 / id 生成
# ══════════════════════════════════════════════════════════════
def new_session_id() -> str:
    """生成一个新会话 id。带 `sess_` 前缀，肉眼可辨形态。"""
    return f"sess_{uuid.uuid4().hex[:12]}"


def _check_session_id(session_id: Any) -> str:
    """校验会话 id 可安全地当键名（同时也是文件名）。非法 → ``ValueError``。"""
    if not isinstance(session_id, str):
        raise ValueError(f"session_id 必须是 str，收到 {type(session_id).__name__}")
    sid = session_id.strip()
    if not sid:
        raise ValueError("session_id 不能为空")
    if not _RE_SESSION_ID.match(sid):
        raise ValueError(
            f"session_id 只允许 [A-Za-z0-9._-] 且以字母数字/下划线开头：{session_id!r}")
    return sid


def _skeleton(entry: Any) -> dict:
    """把任意一条消息压成**恰好** `{"role","text"}`。

    这是裁决①的物理兑现点：不管调用方塞进来的是 LLM 原始回包（带 `tool_calls`）、
    Loop 记账消息（带 `step` / `finish_reason`）还是旧的序列化残留（带
    `tool_results`），出门时只剩 role/text。**静默丢弃**是刻意的：会话层的职责
    边界不该由上游数据形状决定。
    """
    if isinstance(entry, Mapping):
        role = entry.get("role")
        text = entry.get("text")
    elif isinstance(entry, str):
        role, text = ROLE_USER, entry
    else:                                   # 怪对象：按用户发言收编，绝不让它炸
        role, text = ROLE_USER, entry
    role_s = role.strip() if isinstance(role, str) else ""
    if role_s not in (ROLE_USER, ROLE_ASSISTANT):
        # 非问答角色（system / tool / 空）不属于「对话骨架」——统一降级成 user，
        # 保证下游按 role 分支的渲染器（builder._render_history）不会看到陌生角色。
        role_s = ROLE_USER
    try:
        text_s = text if isinstance(text, str) else str(text)
    except Exception:                       # `__str__` 炸了也不能让会话存不进去
        text_s = ""
    return {"role": role_s, "text": text_s}


def _merge_summary(old: str, extra: str) -> str:
    """把调用方补给的摘要并进已有摘要（换行拼接，去重，空值不产生空行）。"""
    parts: list[str] = []
    for chunk in (old, extra):
        if isinstance(chunk, str):
            text = chunk.strip()
            if text and text not in parts:
                parts.append(text)
    return "\n".join(parts)


# ══════════════════════════════════════════════════════════════
# Session：一段对话的不可变快照
# ══════════════════════════════════════════════════════════════
@dataclass(frozen=True)
class Session:
    """一段对话的只读快照。

    刻意 `frozen`：会话是**跨请求共享**的状态，任何就地修改都等于给并发写
    （两个请求同一 session）开一个丢失更新的洞。改会话 = 产出一个新对象。

    `history` 与 `summary` 是跨轮承接的全部内容：
      * `history`：`(user, assistant)` 成对，每轮两条，只含 role/text；
      * `summary`：被压缩掉的那部分历史的滚动摘要（`HistoryCompressor` 的产物）。
    """
    session_id: str
    tenant: str
    user_id: str
    created_at: float
    updated_at: float
    turns: int = 0
    #: 跨轮承接的东西：**只带「对话骨架」，不带工具回包全量**
    history: tuple[dict, ...] = ()
    summary: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "session_id", _check_session_id(self.session_id))
        object.__setattr__(self, "history",
                           tuple(_skeleton(m) for m in (self.history or ())))
        object.__setattr__(self, "summary",
                           self.summary if isinstance(self.summary, str)
                           else str(self.summary or ""))
        object.__setattr__(self, "turns", int(self.turns or 0))

    # ── 序列化 ───────────────────────────────────────────────
    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "tenant": self.tenant,
            "user_id": self.user_id,
            "created_at": float(self.created_at),
            "updated_at": float(self.updated_at),
            "turns": int(self.turns),
            "history": [dict(m) for m in self.history],
            "summary": self.summary,
        }

    @classmethod
    def from_dict(cls, raw: Any) -> "Session":
        """反序列化。结构不对 → `SessionCorrupt`（不猜、不静默补默认值）。"""
        if not isinstance(raw, Mapping):
            raise SessionCorrupt(f"session 必须是 dict，收到 {type(raw).__name__}")
        required = ("session_id", "tenant", "user_id", "created_at", "updated_at")
        missing = [k for k in required if k not in raw]
        if missing:
            raise SessionCorrupt(f"session 缺字段 {missing}")
        try:
            return cls(
                session_id=raw["session_id"],
                tenant=str(raw.get("tenant") or ""),
                user_id=str(raw.get("user_id") or ""),
                created_at=float(raw["created_at"]),
                updated_at=float(raw["updated_at"]),
                turns=int(raw.get("turns") or 0),
                history=tuple(raw.get("history") or ()),
                summary=raw.get("summary") or "",
            )
        except (TypeError, ValueError) as exc:
            raise SessionCorrupt(f"session 字段类型不对：{exc}") from exc

    # ── 派生量 ───────────────────────────────────────────────
    def is_expired(self, now: float, ttl_s: float | None) -> bool:
        """`ttl_s=None` = 永不过期（调用方显式选择「不做 TTL」）。"""
        if ttl_s is None:
            return False
        return (float(now) - float(self.updated_at)) > float(ttl_s)

    @property
    def skeleton(self) -> tuple[dict, ...]:
        """直接可塞进 Context `history` 的骨架（深拷贝一层，防止调用方改到内部）。"""
        return tuple(dict(m) for m in self.history)

    def __repr__(self) -> str:              # pragma: no cover - 调试用
        return (f"<Session {self.session_id} tenant={self.tenant!r} "
                f"user={self.user_id!r} turns={self.turns} "
                f"history={len(self.history)} summary={len(self.summary)}>")


# ══════════════════════════════════════════════════════════════
# SessionStore
# ══════════════════════════════════════════════════════════════
class SessionStore:
    """会话存储。与 ContextStore 分开：Context 是「一次任务」，Session 是「一段对话」。

    混在一起会导致两个具体故障：
      1. `list_pending()` 把会话当成待续跑任务捞出来 → 运维对着不存在的 ctx_id 找任务；
      2. 会话越长越接近 Context 的形状，最终有人拿 `load_checkpoint()` 去读它。

    构造::

        store = SessionStore()                                    # 进程内
        store = SessionStore(core.store.MemoryStore())            # 复用底层 store
        store = SessionStore(FileStore("sessions"), max_history=20,
                             summarizer=HistoryCompressor(max_keep=8), ttl_s=86400)

    参数：
      * `store`       底层 store（`save/load/delete/list_pending`）；None = 进程内内存。
                      **键空间**是 `session-<session_id>`，与 ctx_id 不冲突。
      * `max_history` 保留的消息条数上限（每轮 2 条）。超限触发压缩 + 裁剪。
      * `summarizer`  压缩器（`compress(history, existing_summary=...)` 或普通可调用）。
                      None = 只裁剪 + 记 warning（不静默丢信息，至少让日志说出丢了什么）。
      * `ttl_s`       会话过期秒数；None = 不过期。默认 7 天。
      * `clock`       秒级时钟（测试推假时钟）。
    """

    def __init__(self, store: Any = None, *, max_history: int = 20,
                 summarizer: Any = None, ttl_s: float | None = 7 * 86400,
                 clock: Callable[[], float] | None = None) -> None:
        if isinstance(max_history, bool) or not isinstance(max_history, int):
            raise TypeError(
                f"max_history 必须是 int，收到 {type(max_history).__name__}")
        if max_history < 0:
            raise ValueError(f"max_history 不能为负：{max_history}")
        if ttl_s is not None:
            if isinstance(ttl_s, bool) or not isinstance(ttl_s, (int, float)):
                raise TypeError(f"ttl_s 必须是数字或 None，收到 {type(ttl_s).__name__}")
            if ttl_s < 0:
                raise ValueError(f"ttl_s 不能为负：{ttl_s}")
        if summarizer is not None and not (
                callable(summarizer) or callable(getattr(summarizer, "compress", None))):
            raise TypeError("summarizer 必须可调用，或带 compress(history, existing_summary)")
        if store is not None:
            # 形状校验：把「时钟 / 摘要器 / 别的东西」误传成 store 是很容易犯的错，
            # 而且症状会延迟到第一次写入才炸（那时回合已经跑完了）。
            missing = [m for m in ("save", "load", "delete")
                       if not callable(getattr(store, m, None))]
            if missing:
                raise TypeError(
                    f"store 必须实现 save/load/delete（缺 {missing}），"
                    f"收到 {type(store).__name__}；只想用进程内存储就传 None")

        self._store = store
        self.max_history = int(max_history)
        self.summarizer = summarizer
        self.ttl_s = None if ttl_s is None else float(ttl_s)
        self._clock = clock or time.time
        self._lock = threading.RLock()
        #: store 为 None 时的进程内后备（键 = session_id）
        self._mem: dict[str, Session] = {}
        #: 最近被惰性清理掉的 id（供上层区分「过期」与「从未存在」）
        self._expired: deque[str] = deque(maxlen=_EXPIRED_MEMO)
        #: 观测计数（运维看得到会话是不是在漏）
        self.gets = 0
        self.puts = 0
        self.deletes = 0
        self.expired_total = 0
        self.compressed_total = 0

    # ── 键空间 ──────────────────────────────────────────────
    @staticmethod
    def _key(session_id: str) -> str:
        return f"{_KEY_PREFIX}{session_id}"

    # ── 协议四件套（get / put / delete）+ 轮次写入 ────────────
    def get(self, session_id: str) -> Session | None:
        """读会话。**不存在 → None；过期 → None 且顺手删除**（惰性清理）。

        「过期」与「从未存在」在协议上都是 None —— 上层若要区分，用 `was_expired()`。
        这样 `get()` 的语义保持单一：能用的会话才返回。
        """
        sid = _check_session_id(session_id)
        with self._lock:
            self.gets += 1
            session = self._load(sid)
            if session is None:
                return None
            if session.is_expired(self._now(), self.ttl_s):
                # 惰性清理：不做后台任务。代价是「过期后第一次访问」多一次写，
                # 收益是部署里少一个必须被记住的定时器。
                self._delete_key(sid)
                self._expired.append(sid)
                self.expired_total += 1
                log.info("会话 %s 已过期（updated_at=%s），惰性删除",
                         sid, session.updated_at)
                return None
            return session

    def put(self, session: Session) -> None:
        """整段覆盖写（`Session` 是不可变对象，put 就是「换上这一份」）。"""
        if not isinstance(session, Session):
            raise TypeError(f"put 只接受 Session，收到 {type(session).__name__}")
        with self._lock:
            self._save(session)

    def upsert_turn(self, *, session_id: str, tenant: str, user_id: str,
                    goal: str, answer: str, summary: str = "") -> Session:
        """把**一轮**问答追加进会话，返回写入后的新快照。

        - 会话不存在 → 按首次创建（`created_at = updated_at = now`, `turns=0`）；
        - 会话存在但归属不同 → `SessionOwnershipError`（跨租户/跨用户串用会话）；
        - `summary` 参数：调用方额外想带进会话的摘要（与既有 summary 换行合并），
          不是「覆盖」—— 覆盖会让早先压出来的结论凭空消失。
        """
        sid = _check_session_id(session_id)
        tenant_s = tenant if isinstance(tenant, str) else str(tenant or "")
        user_s = user_id if isinstance(user_id, str) else str(user_id or "")
        now = self._now()
        with self._lock:
            existing = self.get(sid)             # RLock：可重入，同时管 TTL
            if existing is None:
                base = Session(session_id=sid, tenant=tenant_s, user_id=user_s,
                               created_at=now, updated_at=now, turns=0)
            else:
                if existing.tenant != tenant_s or existing.user_id != user_s:
                    raise SessionOwnershipError(
                        f"会话 {sid} 属于 tenant={existing.tenant!r}/"
                        f"user={existing.user_id!r}，拒绝 "
                        f"tenant={tenant_s!r}/user={user_s!r} 的写入")
                base = existing

            messages = list(base.history)
            messages.append({"role": ROLE_USER, "text": goal})
            messages.append({"role": ROLE_ASSISTANT, "text": answer})
            kept, merged_summary = self._roll(messages,
                                              _merge_summary(base.summary, summary))
            session = replace(base, updated_at=now, turns=base.turns + 1,
                              history=tuple(kept), summary=merged_summary)
            self._save(session)
            return session

    def delete(self, session_id: str) -> bool:
        """删除；返回是否真的删掉了（幂等清理不抛错）。"""
        sid = _check_session_id(session_id)
        with self._lock:
            return self._delete_key(sid)

    # ── 查询 ────────────────────────────────────────────────
    def list_by_user(self, tenant: str, user_id: str, limit: int = 20) -> list[Session]:
        """某租户某用户的会话，`updated_at` 降序。

        **tenant 是必填的**：跨租户查不到别人的会话。少一个参数就少一道隔离，
        所以这里不提供「只按 user_id 查」的重载。
        """
        if not isinstance(tenant, str) or not tenant.strip():
            raise ValueError("list_by_user 必须带非空 tenant（租户隔离是硬约束）")
        if not isinstance(user_id, str) or not user_id.strip():
            raise ValueError("list_by_user 必须带非空 user_id")
        if isinstance(limit, bool) or not isinstance(limit, int):
            raise TypeError(f"limit 必须是 int，收到 {type(limit).__name__}")
        if limit < 0:
            raise ValueError(f"limit 不能为负：{limit}")

        found: list[Session] = []
        for sid in self._iter_ids():
            session = self.get(sid)              # 顺带做 TTL 清理
            if session is None:
                continue
            if session.tenant == tenant and session.user_id == user_id:
                found.append(session)
        found.sort(key=lambda s: (s.updated_at, s.session_id), reverse=True)
        return found[:limit]

    def was_expired(self, session_id: str) -> bool:
        """这个 id 是不是**刚刚被 TTL 清理掉**的（区别于「从未存在」）。

        上层用它决定要不要给客户端打 `session_expired=True`：用户看到「会话过期，
        已按新会话处理」才不会以为自己的历史被吞了。
        """
        try:
            sid = _check_session_id(session_id)
        except ValueError:
            return False
        return sid in self._expired

    def list_ids(self) -> list[str]:
        """当前所有会话 id（调试/运维用，不保证顺序稳定）。"""
        return self._iter_ids()

    # ══════════════════════════════════════════════════════
    # 内部
    # ══════════════════════════════════════════════════════
    def _now(self) -> float:
        try:
            return float(self._clock())
        except Exception:                    # 时钟坏了也不能让存储层崩
            return time.time()

    def _load(self, sid: str) -> Session | None:
        if self._store is None:
            return self._mem.get(sid)
        try:
            payload = self._store.load(self._key(sid))
        except Exception as exc:             # 底层 store 坏了：当读不到处理，但要出声
            log.warning("会话 %s 读取失败（按不存在处理）：%s: %s",
                        sid, type(exc).__name__, exc)
            return None
        if payload is None:
            return None
        return _decode_payload(payload, sid)

    def _save(self, session: Session) -> None:
        if self._store is None:
            self._mem[session.session_id] = session
        else:
            payload = json.dumps(
                {"schema_version": _SCHEMA_VERSION, "session": session.to_dict()},
                ensure_ascii=False, sort_keys=True)
            self._store.save(self._key(session.session_id), payload)
        self.puts += 1

    def _delete_key(self, sid: str) -> bool:
        if self._store is None:
            removed = self._mem.pop(sid, None) is not None
        else:
            try:
                removed = bool(self._store.delete(self._key(sid)))
            except Exception as exc:
                log.warning("会话 %s 删除失败：%s: %s", sid, type(exc).__name__, exc)
                removed = False
        if removed:
            self.deletes += 1
        return removed

    def _iter_ids(self) -> list[str]:
        """枚举会话 id。

        优先问底层 store 的 `list_pending()`（既有的 ContextStore 协议就有它，
        不用改任何别人的文件），只收 `session-` 前缀的键 —— 这是「共用 store、
        不共用语义」的落点。
        """
        if self._store is None:
            with self._lock:
                return list(self._mem)
        lister = getattr(self._store, "list_pending", None)
        if not callable(lister):
            log.warning("底层 store 不支持 list_pending()，无法枚举会话")
            return []
        try:
            keys = lister()
        except Exception as exc:
            log.warning("枚举会话失败：%s: %s", type(exc).__name__, exc)
            return []
        ids: list[str] = []
        for key in keys or ():
            if not isinstance(key, str) or not key.startswith(_KEY_PREFIX):
                continue
            sid = key[len(_KEY_PREFIX):]
            if _RE_SESSION_ID.match(sid):
                ids.append(sid)
        return ids

    def _roll(self, history: Sequence[dict],
              summary: str) -> tuple[list[dict], str]:
        """超长滚动：压溢出部分 → 只留最近 `max_history` 条。

        顺序刻意是「先摘要，再裁剪」：反过来的话被裁掉的那段**永远没机会**
        进摘要，等于静默丢失早期决议 —— 模型接着聊就会自相矛盾。
        """
        limit = self.max_history
        if len(history) <= limit:
            return list(history), summary

        overflow = len(history) - limit
        if self.summarizer is None:
            log.warning(
                "会话历史超限（%d > %d）：未配置 summarizer，只裁剪不摘要 —— "
                "最旧的 %d 条问答不会被记进 summary（这是显式降级，不是静默丢失）",
                len(history), limit, overflow)
            return list(history[-limit:]) if limit else [], summary
        try:
            result = self._summarize(list(history), summary)
        except Exception as exc:
            log.warning("summarizer 失败（本次只裁剪不摘要）：%s: %s",
                        type(exc).__name__, exc)
            return list(history[-limit:]) if limit else [], summary

        new_summary = result if isinstance(result, str) else \
            (getattr(result, "summary", "") or "")
        compressed = int(getattr(result, "compressed_count", 0) or 0)
        self.compressed_total += 1
        if not compressed:
            # 压缩器自己的 max_keep 比溢出条数大 → 它认为「没什么可压」。
            # 这里不能装作压过了：被裁掉的部分确实没进摘要，必须留痕。
            log.warning("summarizer 未压缩任何条目（溢出 %d 条被裁掉且未摘要）", overflow)
        return (list(history[-limit:]) if limit else []), (new_summary or summary)

    def _summarize(self, history: list[dict], summary: str) -> Any:
        """兼容两种压缩器形状：`compress(history, existing_summary=...)` 与普通可调用。"""
        compress = getattr(self.summarizer, "compress", None)
        if callable(compress):
            try:
                return compress(history, existing_summary=summary)
            except TypeError:                 # 老签名：不带关键字
                return compress(history, summary)
        return self.summarizer(history, summary)


def _decode_payload(payload: Any, sid: str) -> Session:
    """解析落库信封。版本不认识 / 不是 JSON → `SessionCorrupt`（不猜）。"""
    if not isinstance(payload, str):
        raise SessionCorrupt(
            f"会话 {sid} 的 payload 不是字符串（{type(payload).__name__}）")
    try:
        doc = json.loads(payload)
    except (ValueError, TypeError) as exc:
        raise SessionCorrupt(f"会话 {sid} 的 payload 不是 JSON：{exc}") from exc
    if not isinstance(doc, Mapping):
        raise SessionCorrupt(f"会话 {sid} 的 payload 顶层不是 object")
    version = doc.get("schema_version")
    if version != _SCHEMA_VERSION:
        raise SessionCorrupt(
            f"会话 {sid} 的 schema_version 不认识：期望 {_SCHEMA_VERSION}，"
            f"收到 {version!r}（不猜旧版本怎么读）")
    session = Session.from_dict(doc.get("session"))
    if session.session_id != sid:
        raise SessionCorrupt(
            f"会话 {sid} 的 payload 里 session_id={session.session_id!r} 与键不一致")
    return session
