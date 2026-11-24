"""core/session_gateway.py — 在 Gateway 外面包一层：把「一次请求」变成「一段会话里的一轮」。

================================================================================
职责只有三件
================================================================================

    1. 按 session_id 取出会话历史，塞进**新建的** Context（续跑时绝不塞）
    2. 跑完把本轮问答追加回 Session
    3. 跨轮不变的东西（tenant / user）持续校验 —— 不一致就 403

    客户端请求                         SessionGateway                       Gateway
    ────────────────────────────────────────────────────────────────────────────
    {goal, principal}          →  生成 session_id，history=()          →  handle()
    {goal, principal, session} →  取 history 预填进新 Context           →  handle()
                                 跑完 upsert_turn（只记 role/text）
                                 回 TurnResult，vertex 补 session_id/turn_index

**为什么是「包一层」而不是改 `core/gateway.py`**：Gateway 的 11 步顺序本身就是安全
语义（见那个模块的 docstring），在它内部插一步「会话搬运」会把「形状校验 → 身份 →
RBAC → 配额 → 护栏 → Context」这条链的因果搞乱。会话属于**接入层的上层**：它拿到
的是已经过鉴权的请求、交出去的是已经过输出护栏的 `TurnResult`。包装层天然满足这个
顺序，而且 `gateway.py` 一行都不用动。

================================================================================
历史怎么塞进 Context（唯一需要的注入点）
================================================================================

`Gateway._new_context()` 的写法是 `factory(initial=initial, budget=e.budget)`，而
`_check_shape()` **只读已知键**（未知键直接忽略）。于是有两条可用的注入通道：

① **`context_factory`**（首选，只对真 Gateway 有效）
   `Gateway` 把 `context_factory` 存在 `self._context_factory`。本模块在
   `__init__` 里把它**包起来**（不改文件，只在实例上换掉那个可调用对象）：
   新建 Context 时把会话骨架并进 `initial["history"]`，再交给原来的 factory。
   因为是「包装」而不是「替换」，调用方自己传的 factory（比如带观测的）照常生效。

   通道是线程局部的栈：`handle()` 在调用 Gateway 之前 push 本轮要带的历史，
   factory 读栈顶，`finally` pop。用栈而不是单值，是为了容忍同一线程上
   「流式生成器还没迭代完就又进来一个请求」这种情况。

② **请求字段 `request["session"]`**（兜底，任何 Gateway 都收）
   `Gateway._check_shape()` 忽略未知键，所以多塞一个 `session` 字段是安全的。
   对**假 Gateway / 第三方接入点**（没有 `_context_factory`）来说，这就是全部
   注入手段：它自己决定要不要读 `request["session"]["history"]`。

两条通道**同时**生效：真 Gateway 走 ①，替身走 ②。`history_injected` 属性告诉你
① 是否装上了 —— 没装上不是错误（②永远在），但值得知道。

================================================================================
几条不能含糊的细节
================================================================================

* **续跑（带 `ctx_id`）不预填历史**：那次任务的 history 已经在落盘的 Context 里，
  再塞一遍就是同一段对话被记两次（模型会看到重复的问答）。同理，续跑轮次也
  **不**再追加进 Session —— 否则同一轮的 goal 会出现两次。
* **失败轮次不入会话历史**（`ok=False`）**：失败的那一轮往往带着被护栏拦下的原文、
  或者 Loop 炸掉的半截事实。把它写进会话，下一轮 Prompt 里就会出现一段
  「用户问了、助手没答」的悬空对话，模型会以为那是个未完成的任务。
* **会话写回失败不回滚本轮**：`upsert_turn` 抛异常时本轮答案照样返回，只在 vertex
  打 `session_persisted=False`。这与 Context 检查点（fail-closed）方向相反，因为
  两者的后果不同：检查点丢了 → 挂起的任务永远批不了（必须拒绝）；会话丢了 →
  下一轮少一点上下文（降级可接受，用户还有本轮答案）。
* **403 不是 "bad request"**：tenant/user 与会话不一致意味着 token 串用，必须与
  参数写错区分开 —— 客户端据此决定是「修参数重试」还是「别再试了」。

================================================================================
已知边界（写在代码里，不靠口头传）
================================================================================

* **同一会话的并发轮次没有序号互斥**：`turn_index` 在 `_prepare` 里按「读到几轮」算，
  两个请求同时进同一会话时会拿到相同的 `turn_index`（历史本身不会丢：`upsert_turn`
  在 store 的锁内做完 read-modify-write，两条问答都会落进去）。要严格顺序，需要
  客户端按 session 串行、或给 store 加乐观锁（`version` 比较）。本模块没做，
  因为「同一会话并发提问」在真实产品里本来就要被串行化到队列里。
* **续跑轮次（带 `ctx_id`）不写回会话**：那次任务是上一轮的继续，写回会让同一轮
  的 goal 在 history 里出现两次。它的最终答案因此只体现在本轮 `TurnResult` 上，
  而不是历史里 —— 这是取舍，不是遗漏。
* **`context_factory` 通道依赖实例属性 `_context_factory`**：真 `Gateway` 有；
  第三方/替身可能没有。没有的时候本模块自动退回请求字段通道，不会失败。
"""
from __future__ import annotations

import contextlib
import dataclasses
import logging
import threading
from dataclasses import dataclass
from typing import Any, Callable, Iterator, Mapping

from contracts import Principal
from core.gateway import TurnResult
from core.session import Session, SessionError, SessionStore, new_session_id

__all__ = [
    "SessionGateway",
    "SessionTurn",
    "SessionReject",
    "SESSION_STATUS_BAD_REQUEST",
    "SESSION_STATUS_FORBIDDEN",
]

log = logging.getLogger(__name__)

#: 会话归属不符（token 串用）→ 403
SESSION_STATUS_FORBIDDEN = 403
#: 会话相关参数写错 → 400
SESSION_STATUS_BAD_REQUEST = 400

#: 请求里承载会话载荷的字段名（Gateway 忽略未知键，所以是安全通道）
SESSION_REQUEST_FIELD = "session"
#: 打包在 `context_factory` 上的自省标记（防止重复包装）
_SHIM_MARK = "__session_carry_shim__"
_SHIM_INNER = "__session_carry_inner__"


# ══════════════════════════════════════════════════════════════
# 对外形状
# ══════════════════════════════════════════════════════════════
@dataclass(frozen=True)
class SessionTurn:
    """本轮在会话里的位置 + 本轮要带进 Context 的骨架。"""
    session_id: str
    tenant: str
    user_id: str
    goal: str
    #: 本轮是会话里的第几轮（0 基：0 = 首轮）
    turn_index: int
    #: 前几轮的问答骨架（只含 role/text）
    history: tuple[dict, ...] = ()
    summary: str = ""
    #: 会话已不可用（被 TTL 清理，或落库内容读不回来）→ 本轮按新会话处理，
    #: 但必须在 vertex 上告诉客户端「你的历史没了」，否则用户会以为被静默吞掉
    expired: bool = False
    #: 这个会话是本轮创建的（首次请求 / 过期后重建）
    created: bool = False
    #: 本轮是「续跑同一次任务」（带 ctx_id）→ 不预填、也不回写
    resumed: bool = False

    def payload(self) -> dict:
        """要塞进请求字段的载荷（也是 `context_factory` 要并进 `initial` 的东西）。"""
        return {
            "session_id": self.session_id,
            "turn_index": self.turn_index,
            "tenant": self.tenant,
            "user_id": self.user_id,
            "history": [dict(m) for m in self.history],
            "summary": self.summary,
            "expired": self.expired,
            "resumed": self.resumed,
        }


@dataclass(frozen=True)
class SessionReject:
    """会话层自己的拒绝（在 Gateway 之前就挡住的情况）。"""
    code: str
    detail: str = ""
    session_id: str = ""
    turn_index: int = -1

    @property
    def error(self) -> str:
        return f"{self.code}: {self.detail}" if self.detail else self.code

    @property
    def http_status(self) -> int:
        return SESSION_STATUS_FORBIDDEN if self.code == "forbidden" \
            else SESSION_STATUS_BAD_REQUEST

    def result(self) -> TurnResult:
        return TurnResult(
            ok=False, text="", error=self.error, phase="", term_reason="",
            vertex={
                "session_id": self.session_id,
                "session_rejected": self.code,
                "http_status": self.http_status,
            })


# ══════════════════════════════════════════════════════════════
# SessionGateway
# ══════════════════════════════════════════════════════════════
class SessionGateway:
    """会话感知的接入层包装器。

    用法::

        gw = Gateway(loop_factory, store=ctx_store)
        sgw = SessionGateway(gw, SessionStore(store, summarizer=HistoryCompressor()))

        r1 = sgw.handle({"goal": "连接池超时怎么办", "principal": {...}})
        r2 = sgw.handle({"goal": "那它上限多少", "principal": {...},
                         "session_id": r1.vertex["session_id"]})

    参数：
      * `gateway`  任意有 `handle(request)` 的对象（真 `Gateway` 或替身）
      * `sessions` `SessionStore`
      * `history_field` 请求里承载会话载荷的字段名，默认 `"session"`
    """

    def __init__(self, gateway: Any, sessions: SessionStore, *,
                 history_field: str = SESSION_REQUEST_FIELD) -> None:
        handle = getattr(gateway, "handle", None)
        if not callable(handle):
            raise TypeError(
                f"gateway 必须有 handle(request) 方法，收到 {type(gateway).__name__}")
        if not isinstance(sessions, SessionStore):
            raise TypeError(
                f"sessions 必须是 SessionStore，收到 {type(sessions).__name__}")
        if not isinstance(history_field, str) or not history_field.strip():
            raise ValueError("history_field 必须是非空 str")

        self._gateway = gateway
        self._sessions = sessions
        self.history_field = history_field
        self._lock = threading.RLock()
        #: 线程局部的「待带入历史」栈（见模块 docstring 通道①）
        self._local = threading.local()
        #: context_factory 包装是否装上（装不上不是错误，通道②永远可用）
        self.history_injected = False
        #: 实际用的注入通道：`"context_seed"` / `"context_factory"` / `""`（都没装上）。
        #: 观测用 —— "配了没生效"必须能看出来。
        self.injection_channel = ""
        self._inner_factory: Callable[..., Any] | None = None
        self._shim: Callable[..., Any] | None = None
        self._install_context_shim()

    # ══════════════════════════════════════════════════════
    # 对外：一次性返回
    # ══════════════════════════════════════════════════════
    def handle(self, request: Any) -> Any:
        """跑一轮。**永不抛异常** —— 与 Gateway 同一纪律。"""
        if not isinstance(request, Mapping):
            return self._gateway.handle(request)     # 形状校验归 Gateway

        turn, reject = self._prepare(request)
        if turn is None:
            if reject is not None:
                return reject.result()
            return self._gateway.handle(request)     # 身份不可解析：交给 Gateway 判

        forwarded = self._forward(request, turn)
        try:
            with self._carrying(self._carry_payload(turn)):
                result = self._gateway.handle(forwarded)
        except Exception as exc:                     # 包装层兜底，绝不外抛
            log.warning("Gateway 抛异常（构造失败结果）：%s: %s",
                        type(exc).__name__, exc)
            return TurnResult(
                ok=False, text="",
                error=f"session_gateway: {type(exc).__name__}: {exc}",
                vertex=self._vertex_of(turn, excepted=True))
        return self._finish(turn, result)

    # ══════════════════════════════════════════════════════
    # 对外：流式事件（形状与 Gateway 完全一致，只做加法）
    # ══════════════════════════════════════════════════════
    def handle_stream(self, request: Any) -> Iterator[dict]:
        """透传 Gateway 的事件序列，在 `completed` 上补会话字段。

        事件形状**不变**（`accepted/started/completed/rejected`，seq 0/1/2），
        只是 `completed.data` 多几个键 —— 客户端的状态机不用改。
        会话历史在 `rejected` 之后**不**追加（失败轮次不入历史）。
        """
        if not isinstance(request, Mapping):
            for event in self._gateway.handle_stream(request):
                yield event
            return

        turn, reject = self._prepare(request)
        if turn is None:
            if reject is not None:
                yield {"type": "rejected", "seq": 0, "data": {
                    "error": reject.error,
                    "session_id": reject.session_id,
                    "session_rejected": reject.code,
                    "http_status": reject.http_status,
                    "audit_id": "",
                }}
                return
            for event in self._gateway.handle_stream(request):
                yield event
            return

        forwarded = self._forward(request, turn)
        stream = self._gateway.handle_stream(forwarded)
        completed = False
        try:
            with self._carrying(self._carry_payload(turn)):
                for event in stream:
                    if not isinstance(event, Mapping):
                        yield event
                        continue
                    if event.get("type") != "completed":
                        yield event
                        continue
                    completed = True
                    data = dict(event.get("data") or {})
                    data.update(self._session_vertex(turn, event.get("data") or {}))
                    yield {"type": "completed", "seq": event.get("seq", 2),
                           "data": data}
        except Exception as exc:                 # 流中途炸了也要让客户端收口
            log.warning("Gateway 流式异常（补一条 completed）：%s: %s",
                        type(exc).__name__, exc)
            if not completed:
                yield {"type": "completed", "seq": 2, "data": {
                    "text": "", "steps": 0, "ok": False, "phase": "failed",
                    "term_reason": "unrecoverable",
                    "error": f"session_gateway: {type(exc).__name__}: {exc}",
                    "audit_id": "",
                    **self._vertex_of(turn, excepted=True),
                }}
        finally:
            close = getattr(stream, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:               # 关流失败不该反噬已经发出去的事件
                    pass

    # ══════════════════════════════════════════════════════
    # ① 会话解析（含归属校验）
    # ══════════════════════════════════════════════════════
    def _prepare(self, request: Mapping) -> tuple[SessionTurn | None, SessionReject | None]:
        """返回 `(本轮会话位置, 拒绝)`。`(None, None)` = 交给 Gateway 自己判。"""
        ident = _identity_of(request)
        if ident is None:
            # principal 缺失/不可解析：会话层不抢 Gateway 的活（unauthorized 归它）
            return None, None
        tenant, user_id = ident

        raw_sid = request.get("session_id")
        if raw_sid is not None and not isinstance(raw_sid, str):
            return None, SessionReject(
                "bad_request",
                f"session_id 必须是 str 或 None，收到 {type(raw_sid).__name__}")

        ctx_id = request.get("ctx_id")
        resumed = isinstance(ctx_id, str) and bool(ctx_id.strip())

        existing: Session | None = None
        expired = False
        if isinstance(raw_sid, str) and raw_sid.strip():
            sid = raw_sid.strip()
            try:
                existing = self._sessions.get(sid)
            except SessionError as exc:
                # 坏信封：按「没有会话」处理，但要留痕（这不是可以静默的故障）
                log.warning("会话 %s 读取异常（按新会话处理）：%s", sid, exc)
                existing = None
                expired = True
            if existing is None:
                expired = expired or self._sessions.was_expired(sid)
            else:
                if existing.tenant != tenant:
                    return None, SessionReject(
                        "forbidden",
                        f"会话 {sid} 属于租户 {existing.tenant!r}，"
                        f"当前主体租户 {tenant!r}（拒绝跨租户串用）",
                        session_id=sid, turn_index=existing.turns)
                if existing.user_id != user_id:
                    return None, SessionReject(
                        "forbidden",
                        f"会话 {sid} 属于用户 {existing.user_id!r}，"
                        f"当前主体用户 {user_id!r}（拒绝跨用户串用）",
                        session_id=sid, turn_index=existing.turns)
        else:
            sid = new_session_id()

        if existing is not None:
            return SessionTurn(
                session_id=sid, tenant=tenant, user_id=user_id,
                goal=_goal_text(request), turn_index=existing.turns,
                history=existing.skeleton, summary=existing.summary,
                expired=False, created=False, resumed=resumed), None

        return SessionTurn(
            session_id=sid, tenant=tenant, user_id=user_id,
            goal=_goal_text(request), turn_index=0,
            history=(), summary="",
            expired=expired, created=True, resumed=resumed), None

    # ══════════════════════════════════════════════════════
    # ② 两个注入通道
    # ══════════════════════════════════════════════════════
    def _forward(self, request: Mapping, turn: SessionTurn) -> dict:
        """请求字段通道：把会话载荷塞进去（Gateway 忽略未知键，替身可以读）。"""
        forwarded = dict(request)
        forwarded[self.history_field] = turn.payload()
        forwarded.setdefault("session_id", turn.session_id)
        return forwarded

    def _carry_payload(self, turn: SessionTurn) -> dict | None:
        """context_factory 通道的载荷。续跑（带 ctx_id）不预填：历史已在 Context 里。"""
        if turn.resumed:
            return None
        payload = turn.payload()
        if not payload["history"] and not payload["summary"]:
            return None
        return payload

    def _install_context_shim(self) -> None:
        """把会话历史接进新建 Context。**两条通道，优先走公开的那条。**

        通道 ①（首选）：`gateway.context_seed` —— `Gateway` 的**正式注入口**。
            拿到 initial 字典、返回改造后的字典，不依赖任何私有属性。
        通道 ②（降级）：包装 `gateway._context_factory`。
            真 Gateway 有这属性，但它是私有的：Gateway 一旦改结构（`__slots__` /
            frozen / 重命名）就会**静默降级** —— 历史不再预填而没人知道。
            所以只在前者不可用时才用它，并打 INFO 日志。
        """
        gw = self._gateway
        # ① 公开注入口
        if hasattr(gw, "_context_seed"):
            current = getattr(gw, "_context_seed", None)
            if getattr(current, _SHIM_MARK, False):
                current = getattr(current, _SHIM_INNER, None)
            self._inner_seed = current
            seed = self._make_seed(current)
            try:
                setattr(gw, "_context_seed", seed)
                self._seed = seed
                self.history_injected = True
                self.injection_channel = "context_seed"
                return
            except Exception as exc:                           # noqa: BLE001
                log.info("context_seed 注入失败（%s），降级到包装 factory",
                         type(exc).__name__)
        # ② 降级：包装私有属性
        if not hasattr(gw, "_context_factory"):
            log.info("Gateway 未暴露 _context_factory：历史只走请求字段 %r",
                     self.history_field)
            return
        current = getattr(gw, "_context_factory", None)
        if getattr(current, _SHIM_MARK, False):
            # 已经被别的 SessionGateway 包过：接上它的内层，避免包装套包装
            current = getattr(current, _SHIM_INNER, None)
        self._inner_factory = current
        shim = self._make_shim()
        try:
            setattr(gw, "_context_factory", shim)
        except Exception as exc:              # 只读/冻结的 gateway 替身
            log.info("无法包装 context_factory（%s: %s）：历史只走请求字段 %r",
                     type(exc).__name__, exc, self.history_field)
            return
        self._shim = shim
        self.history_injected = True
        self.injection_channel = "context_factory"

    def _make_seed(self, inner: Callable[..., Any] | None) -> Callable[..., Any]:
        """构造 `context_seed(initial, goal, principal, e) -> dict`。

        把会话骨架并进种子；**已有历史时不覆盖**（那是续跑出来的真实轨迹，
        用会话骨架替掉等于伪造轨迹）。
        """

        def seed(initial: Any, goal: str = "", principal: Any = None,
                 e: Any = None) -> Any:
            merged = dict(initial) if isinstance(initial, Mapping) else {}
            payload = self._current_carry()
            if payload:
                history = [dict(m) for m in payload.get("history") or ()]
                if history and not merged.get("history"):
                    merged["history"] = history
                if payload.get("summary") and not merged.get("summary"):
                    merged["summary"] = payload["summary"]
                merged.setdefault("session_id", payload.get("session_id"))
            if inner is not None:
                try:
                    out = inner(merged, goal, principal, e)
                    if isinstance(out, Mapping):
                        merged = dict(out)
                except TypeError:
                    out = inner(merged)
                    if isinstance(out, Mapping):
                        merged = dict(out)
            return merged

        setattr(seed, _SHIM_MARK, True)
        setattr(seed, _SHIM_INNER, inner)
        return seed

    def _make_shim(self) -> Callable[..., Any]:
        def shim(initial: Any = None, budget: Any = None, **kw: Any) -> Any:
            merged = dict(initial) if isinstance(initial, Mapping) else {}
            payload = self._current_carry()
            if payload:
                history = [dict(m) for m in payload.get("history") or ()]
                # 只在「还没有历史」时预填：已有历史的 Context 是续跑出来的，
                # 覆盖它等于把真实轨迹替换成会话骨架。
                if history and not merged.get("history"):
                    merged["history"] = history
                if payload.get("summary") and not merged.get("summary"):
                    merged["summary"] = payload["summary"]
                merged.setdefault("session_id", payload.get("session_id"))
            factory = self._resolve_factory()
            try:
                return factory(initial=merged, budget=budget)
            except TypeError:                 # 老签名：位置参数
                return factory(merged, budget)

        setattr(shim, _SHIM_MARK, True)
        setattr(shim, _SHIM_INNER, self._inner_factory)
        return shim

    def _resolve_factory(self) -> Callable[..., Any]:
        if self._inner_factory is not None:
            return self._inner_factory
        from core.context import ContextHandle   # 懒加载：并行施工期只读依赖
        return ContextHandle

    # ── 线程局部栈 ──────────────────────────────────────────
    def _stack(self) -> list:
        stack = getattr(self._local, "stack", None)
        if stack is None:
            stack = []
            self._local.stack = stack
        return stack

    def _current_carry(self) -> dict | None:
        stack = getattr(self._local, "stack", None)
        return stack[-1] if stack else None

    @contextlib.contextmanager
    def _carrying(self, payload: dict | None):
        if payload is None:
            yield
            return
        stack = self._stack()
        stack.append(payload)
        try:
            yield
        finally:
            if stack and stack[-1] is payload:
                stack.pop()
            elif payload in stack:            # 防御：调用方提前清栈的极端情况
                stack.remove(payload)

    # ══════════════════════════════════════════════════════
    # ③ 回写 + 结果装饰
    # ══════════════════════════════════════════════════════
    def _finish(self, turn: SessionTurn, result: Any) -> Any:
        extra = self._session_vertex(turn, getattr(result, "vertex", None) or {},
                                     result=result)
        return _with_vertex(result, extra)

    def _session_vertex(self, turn: SessionTurn, data: Mapping,
                        *, result: Any = None, excepted: bool = False) -> dict:
        """本轮结束后要补进 vertex / 事件 data 的会话字段（并顺手完成回写）。"""
        if result is None:
            # 流式路径：ok 从事件 data 里取
            ok = bool(data.get("ok"))
            text = data.get("text") or ""
        else:
            ok = bool(getattr(result, "ok", False))
            text = getattr(result, "text", "") or ""
        if excepted:
            ok = False

        session: Session | None = None
        persisted = False
        if ok and not turn.resumed:
            try:
                session = self._sessions.upsert_turn(
                    session_id=turn.session_id, tenant=turn.tenant,
                    user_id=turn.user_id, goal=turn.goal, answer=text)
                persisted = True
            except Exception as exc:
                # fail-open：会话是「下一轮的上下文」，不是本轮的交付物。
                # 为了写不进去而把已经跑完的答案判成失败，是把降级升级成事故。
                log.warning("会话回写失败（本轮答案照常返回）：%s: %s",
                            type(exc).__name__, exc)

        return {
            "session_id": turn.session_id,
            "turn_index": turn.turn_index,
            "session_turns": session.turns if session is not None else turn.turn_index,
            "session_persisted": persisted,
            "session_expired": bool(turn.expired),
            "session_new": bool(turn.created),
            "session_resumed": bool(turn.resumed),
        }

    def _vertex_of(self, turn: SessionTurn, *, excepted: bool = False) -> dict:
        """包装层兜底失败时用：不回写历史，但会话定位信息仍然要给出来。"""
        return {
            "session_id": turn.session_id,
            "turn_index": turn.turn_index,
            "session_turns": turn.turn_index,
            "session_persisted": False,
            "session_expired": bool(turn.expired),
            "session_new": bool(turn.created),
            "session_resumed": bool(turn.resumed),
            "session_error": bool(excepted),
        }

    # ── 观测 ────────────────────────────────────────────────
    @property
    def sessions(self) -> SessionStore:
        return self._sessions

    def uninstall(self) -> bool:
        """拆掉 context_factory 包装（测试/热替换用）。返回是否真的还原了。"""
        shim = self._shim
        if shim is None:
            return False
        gw = self._gateway
        if getattr(gw, "_context_factory", None) is not shim:
            return False
        setattr(gw, "_context_factory", self._inner_factory)
        self._shim = None
        self.history_injected = False
        #: 实际用的注入通道：`"context_seed"` / `"context_factory"` / `""`（都没装上）。
        #: 观测用 —— "配了没生效"必须能看出来。
        self.injection_channel = ""
        return True

    def __repr__(self) -> str:                # pragma: no cover - 调试用
        return (f"<SessionGateway gateway={type(self._gateway).__name__} "
                f"history_injected={self.history_injected} "
                f"sessions={type(self._sessions).__name__}>")


# ══════════════════════════════════════════════════════════════
# 纯函数工具
# ══════════════════════════════════════════════════════════════
def _identity_of(request: Mapping) -> tuple[str, str] | None:
    """从请求里取 `(tenant, user_id)`。取不到 → None（交给 Gateway 判 unauthorized）。"""
    raw = request.get("principal")
    if isinstance(raw, Principal):
        return raw.tenant, raw.user_id
    if isinstance(raw, Mapping):
        tenant, user_id = raw.get("tenant"), raw.get("user_id")
        if isinstance(tenant, str) and isinstance(user_id, str):
            if tenant.strip() and user_id.strip():
                return tenant.strip(), user_id.strip()
    return None


def _goal_text(request: Mapping) -> str:
    goal = request.get("goal")
    if isinstance(goal, str):
        return goal
    try:
        return str(goal) if goal is not None else ""
    except Exception:                         # 怪对象的 __str__ 炸了
        return ""


def _with_vertex(result: Any, extra: Mapping) -> Any:
    """给结果补 vertex 字段。**不就地改**原对象（它是 frozen dataclass）。"""
    vertex = dict(getattr(result, "vertex", None) or {})
    vertex.update(dict(extra))
    try:
        return dataclasses.replace(result, vertex=vertex)
    except (TypeError, ValueError):
        pass
    try:                                      # 非 dataclass 的替身：就地补
        object.__setattr__(result, "vertex", vertex)
        return result
    except Exception:
        log.warning("结果对象不可替换 vertex（%s），会话字段只存在于事件里",
                    type(result).__name__)
        return result
