"""core/store.py — ADR-7 的物理兑现：Context 落盘 + 崩溃续跑（W14）。

`ContextHandle.serialize()` 只把状态变成一个内存字符串：进程一死全没。
「T2 工具挂起等人工审批 4 小时」是常态场景，因此这一层不是优化项，
而是把 `WAITING_USER` 从「扛一次 GC」升级成「扛一次重启」。

两层结构（职责分离，别混）
────────────────────────────────────────────────────────────
1) **Store 层（本模块）** 只管字节：把 `payload`（Context 序列化出来的**原样字符串**）
   连同信封头 `{ctx_id, saved_at, schema_version, payload}` 原子地写进磁盘 / 内存。
   它**不理解** payload 里的内容 —— 不解析、不改写、不猜版本。
2) **Context 层（core/context.py）** 只管语义：`serialize()` / `restore()` 校验
   `schema_version`、`version`、`patch_log`。两层各自有独立的 `schema_version`，
   所以信封头里的版本号是 **Store 的**，payload 里的是 **Context 的**。

原子性（本模块的核心承诺）
────────────────────────────────────────────────────────────
`save` 的写入路径固定是：

    {ctx_id}.tmp.<pid>.<random>   ← 写完 + flush + fsync
                ↓ os.replace      ← 同目录 rename，POSIX 上是原子的
    {ctx_id}.json                 ← 读者永远只会看到完整文件

崩溃可能发生在任何位置：
- 崩在写临时文件途中 → 目的文件根本没被动过，旧的完整版本还在，残留的 `.tmp`
  因为**不以 `.json` 结尾**而不被 `list_pending()` 认（见 `_FILE_SUFFIX` 白名单）；
- 崩在 `os.replace` 前后 → rename 本身原子，只可能是「旧文件」或「新文件」，
  不存在半个；
- 崩在 `fsync` 之后 → 字节已落盘，最坏是目录项没刷新，重启后要么有要么没有。

`load` 只对 `.json` 文件做校验：JSON 能解析、`schema_version` **认识**、
`payload` 是 str。版本不认识就抛 `StoreSchemaMismatch` —— **不猜**，因为
「猜一个旧版本怎么读」等价于让运行时在别人写的状态上瞎跑。

崩溃续跑编排（`save_checkpoint` / `load_checkpoint` / `list_resumable` /
`resume_from_store`）在本模块底部：`resume_from_store` 是唯一入口，
一行完成「读盘 → 按 phase 分流 resume/run → 收尾（删除或再次落盘）」。

边界（诚实写在这里）
────────────────────────────────────────────────────────────
- `MemoryStore` **不**做信封校验：它不跨进程，没有"半写"可言，把它当成
  一个 dict 用就对了。要证明持久化，用 `FileStore`。
- 单机文件锁**没有**做：同一 `ctx_id` 被两个进程同时 save 时，最后 rename 的那个赢
  （last-writer-wins，符合契约里「Context 是状态不是流水账」）。要串行化请在外面
  加锁；本模块不替调用方猜并发模型。
- `delete` 是**不可恢复**的：本模块不保留历史版本（要审计就靠 `patch_log` 与 Tracer）。
"""
from __future__ import annotations

import json
import os
import re
import uuid
from pathlib import Path
from typing import Any

from contracts import (
    Budget,
    ContextError,
    Owner,
    Patch,
    Phase,
    Result,
    now_ms,
)
from core.context import ContextHandle

__all__ = [
    "STORE_SCHEMA_VERSION",
    "DEFAULT_ROOT",
    "StoreError",
    "ContextNotFound",
    "StoreCorrupt",
    "StoreSchemaMismatch",
    "validate_ctx_id",
    "MemoryStore",
    "FileStore",
    "CrashStore",
    "save_checkpoint",
    "load_checkpoint",
    "list_resumable",
    "resume_from_store",
]

#: Store 信封格式版本。与 core.context.SCHEMA_VERSION 是**两个独立**的版本号。
STORE_SCHEMA_VERSION = 1

DEFAULT_ROOT = "/tmp/agent-runtime-store"

#: 只有这个后缀的文件才被当成"落盘的 Context"。临时文件故意不用它。
_FILE_SUFFIX = ".json"

_MAX_CTX_ID_LEN = 128

#: ctx_id 的字符白名单：字母/数字/`_`/`-`/`.`，且不得以 `.` 开头。
#: 一条正则同时掐掉：目录穿越（`/`、`\`）、隐藏文件（`.`、`..`）、
#: 以及 glob 元字符（`*?[]`）—— 后者会被 `list_pending()` 的 glob 当成通配符。
_RE_CTX_ID = re.compile(r"^[A-Za-z0-9_-][A-Za-z0-9._-]*$")


# ══════════════════════════════════════════════════════════════
# 错误
# ══════════════════════════════════════════════════════════════
class StoreError(Exception):
    """Store 层失败的基类（磁盘/信封/找不到）。"""


class ContextNotFound(StoreError):
    """该 ctx_id 没有落盘记录。"""


class StoreCorrupt(StoreError):
    """信封结构坏了：不是 JSON、字段缺失、payload 不是字符串、ctx_id 对不上。"""


class StoreSchemaMismatch(StoreError):
    """信封里的 `schema_version` 不认识 —— 拒绝猜。"""


# ══════════════════════════════════════════════════════════════
# ctx_id 校验（路径安全的第一道闸，两种 store 共用）
# ══════════════════════════════════════════════════════════════
def validate_ctx_id(ctx_id: str) -> str:
    """校验 ctx_id 可安全地当文件名用；非法 → ``ValueError``。

    逐条报错而不是统一一句话，因为调用方需要知道是"空"还是"穿越"。
    """
    if not isinstance(ctx_id, str):
        raise ValueError(f"ctx_id 必须是 str，收到 {type(ctx_id).__name__}")
    if not ctx_id:
        raise ValueError("ctx_id 不能是空串")
    if ".." in ctx_id:
        raise ValueError(f"ctx_id 不允许包含 '..'（防目录穿越）：{ctx_id!r}")
    if "/" in ctx_id or "\\" in ctx_id:
        raise ValueError(f"ctx_id 不允许包含路径分隔符：{ctx_id!r}")
    if "\x00" in ctx_id:
        raise ValueError(f"ctx_id 不允许包含 NUL 字节：{ctx_id!r}")
    if len(ctx_id) > _MAX_CTX_ID_LEN:
        raise ValueError(
            f"ctx_id 过长（{len(ctx_id)} > {_MAX_CTX_ID_LEN}）：{ctx_id[:32]!r}…"
        )
    if not _RE_CTX_ID.match(ctx_id):
        raise ValueError(
            f"ctx_id 只允许字母/数字/'_'/'-'/'.'，且不能以 '.' 开头：{ctx_id!r}"
        )
    return ctx_id


def _require_payload(payload: Any) -> str:
    if not isinstance(payload, str):
        raise StoreError(
            f"payload 必须是 str（Context.serialize() 的输出），"
            f"收到 {type(payload).__name__}"
        )
    return payload


# ══════════════════════════════════════════════════════════════
# 信封（envelope）编解码
# ══════════════════════════════════════════════════════════════
def _envelope(ctx_id: str, payload: str, saved_at: float) -> dict:
    return {
        "ctx_id": ctx_id,
        "saved_at": saved_at,
        "schema_version": STORE_SCHEMA_VERSION,
        "payload": payload,
    }


def _decode_envelope(raw: str, where: str) -> dict:
    """解析并校验信封；任何不合规都抛 StoreError 家族（绝不返回半信半疑的东西）。"""
    try:
        env = json.loads(raw)
    except (json.JSONDecodeError, ValueError) as exc:
        raise StoreCorrupt(f"{where} 不是合法 JSON：{exc}") from exc
    if not isinstance(env, dict):
        raise StoreCorrupt(f"{where} 顶层不是 JSON object（{type(env).__name__}）")

    if "schema_version" not in env:
        raise StoreCorrupt(f"{where} 缺少 schema_version 字段")
    schema = env.get("schema_version")
    if not isinstance(schema, int) or isinstance(schema, bool):
        raise StoreSchemaMismatch(
            f"{where} 的 schema_version 不是整数：{schema!r}"
        )
    if schema != STORE_SCHEMA_VERSION:
        raise StoreSchemaMismatch(
            f"{where} 的 schema_version 不认识：期望 {STORE_SCHEMA_VERSION}，"
            f"收到 {schema}（不猜旧版本怎么读）"
        )

    ctx_id = env.get("ctx_id")
    if not isinstance(ctx_id, str) or not ctx_id:
        raise StoreCorrupt(f"{where} 的 ctx_id 缺失或不是字符串：{ctx_id!r}")

    saved_at = env.get("saved_at")
    if not isinstance(saved_at, (int, float)) or isinstance(saved_at, bool):
        raise StoreCorrupt(f"{where} 的 saved_at 不是数值：{saved_at!r}")

    payload = env.get("payload")
    if not isinstance(payload, str):
        raise StoreCorrupt(
            f"{where} 的 payload 不是字符串（{type(payload).__name__}）"
        )
    return env


# ══════════════════════════════════════════════════════════════
# MemoryStore：单进程 / 测试用
# ══════════════════════════════════════════════════════════════
class MemoryStore:
    """进程内 store。**不跨进程、不跨崩溃** —— 它的价值是给测试和单进程场景
    一个零 IO 的替身，形状与 `FileStore` 完全一致（同 4 个方法）。

    与 `FileStore` 的差别只有一个：payload 不经过 JSON 边界，所以这里
    **不做信封校验**（没有"半写"可防，也没有版本可猜）。
    """

    def __init__(self) -> None:
        self._entries: dict[str, dict] = {}
        #: save 调用计数（观测用；CrashStore 的父类同名字段）
        self.saves = 0

    # ── 协议四件套 ─────────────────────────────────────────
    def save(self, ctx_id: str, payload: str) -> None:
        validate_ctx_id(ctx_id)
        _require_payload(payload)
        self.saves += 1
        self._entries[ctx_id] = _envelope(ctx_id, payload, now_ms())

    def load(self, ctx_id: str) -> str | None:
        validate_ctx_id(ctx_id)
        entry = self._entries.get(ctx_id)
        return None if entry is None else entry["payload"]

    def delete(self, ctx_id: str) -> bool:
        validate_ctx_id(ctx_id)
        return self._entries.pop(ctx_id, None) is not None

    def list_pending(self) -> list[str]:
        """按 saved_at 升序（同一毫秒按 ctx_id 兜底，保证确定性）。"""
        items = sorted(
            self._entries.items(),
            key=lambda kv: (kv[1]["saved_at"], kv[0]),
        )
        return [ctx_id for ctx_id, _ in items]

    # ── 附加查询（不在 Protocol 里，编排层按需用）────────────
    def saved_at(self, ctx_id: str) -> float | None:
        validate_ctx_id(ctx_id)
        entry = self._entries.get(ctx_id)
        return None if entry is None else float(entry["saved_at"])

    def exists(self, ctx_id: str) -> bool:
        validate_ctx_id(ctx_id)
        return ctx_id in self._entries

    def __len__(self) -> int:
        return len(self._entries)

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"<MemoryStore entries={len(self._entries)} saves={self.saves}>"


# ══════════════════════════════════════════════════════════════
# FileStore：真落盘，零依赖
# ══════════════════════════════════════════════════════════════
class FileStore:
    """`{root}/{ctx_id}.json` 一文件一 Context。

    原子写入的三段式：写临时文件 → flush+fsync → `os.replace`。
    任何一步抛异常都会**尽力**清理临时文件；进程被 SIGKILL 时清理代码不会执行，
    但残留的 `.tmp.*` 不会被 `list_pending()` 认，也不会被 `load()` 读到，
    所以"崩溃留下半个文件"在语义上不成立。
    """

    def __init__(self, root: str | Path = DEFAULT_ROOT) -> None:
        path = Path(root).expanduser()
        if path.exists() and not path.is_dir():
            raise StoreError(f"store root 已存在且不是目录：{path}")
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise StoreError(f"无法创建 store root {path}：{exc}") from exc
        self.root: Path = path
        #: save 调用计数（观测用）
        self.saves = 0

    # ── 协议四件套 ─────────────────────────────────────────
    def save(self, ctx_id: str, payload: str) -> None:
        validate_ctx_id(ctx_id)
        _require_payload(payload)
        self.saves += 1

        target = self._path(ctx_id)
        blob = json.dumps(_envelope(ctx_id, payload, now_ms()), ensure_ascii=False)
        tmp = self._tmp_path(ctx_id)
        try:
            self._write_temp(tmp, blob)
            os.replace(tmp, target)          # 同目录 rename：原子替换
            self._fsync_dir()
        except BaseException:
            # 尽力清理；OSError 也不能盖掉原始异常
            try:
                tmp.unlink()
            except OSError:
                pass
            raise

    def load(self, ctx_id: str) -> str | None:
        path = self._path(ctx_id)
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise StoreError(f"读取落盘文件失败：{path}（{exc}）") from exc

        env = _decode_envelope(raw, str(path))
        if env["ctx_id"] != ctx_id:
            raise StoreCorrupt(
                f"{path} 里的 ctx_id={env['ctx_id']!r} 与文件名 {ctx_id!r} 不一致"
            )
        return env["payload"]

    def delete(self, ctx_id: str) -> bool:
        path = self._path(ctx_id)
        try:
            path.unlink()
        except FileNotFoundError:
            removed = False
        except OSError as exc:
            raise StoreError(f"删除落盘文件失败：{path}（{exc}）") from exc
        else:
            removed = True
            self._fsync_dir()

        # 顺手清掉这个 ctx_id 的孤儿临时文件（崩溃残留），让 delete 成为收敛点
        for orphan in self.root.glob(f"{ctx_id}.tmp.*"):
            try:
                orphan.unlink()
            except OSError:
                pass
        return removed

    def list_pending(self) -> list[str]:
        """按 saved_at 升序返回所有 ctx_id。

        只认 `*.json`。坏文件**照样列出来**（它的 id 是一个待处理项，会不会读到
        是 `load()` 的事），排序时读不出 saved_at 就用 mtime 兜底 —— 一个坏文件
        不该让整次扫描失败。
        """
        entries: list[tuple[float, str]] = []
        for path in self.root.glob(f"*{_FILE_SUFFIX}"):
            ctx_id = path.name[: -len(_FILE_SUFFIX)]
            if not _RE_CTX_ID.match(ctx_id):     # 非法命名的垃圾文件，不认
                continue
            entries.append((self._saved_at_or_mtime(path), ctx_id))
        entries.sort(key=lambda item: (item[0], item[1]))
        return [ctx_id for _, ctx_id in entries]

    # ── 附加查询 ───────────────────────────────────────────
    def saved_at(self, ctx_id: str) -> float | None:
        path = self._path(ctx_id)
        if not path.exists():
            return None
        return self._saved_at_or_mtime(path)

    def exists(self, ctx_id: str) -> bool:
        return self._path(ctx_id).exists()

    def stats(self) -> dict:
        """落盘现状快照（运维/验收用，不参与续跑逻辑）。"""
        pending = self.list_pending()
        return {
            "root": str(self.root),
            "pending": len(pending),
            "temps": len(list(self.root.glob("*.tmp.*"))),
            "saves": self.saves,
        }

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"<FileStore root={self.root} saves={self.saves}>"

    # ── 内部：路径与写入原语（CrashStore 覆盖 _write_temp 来模拟断电）──
    def _path(self, ctx_id: str) -> Path:
        validate_ctx_id(ctx_id)
        return self.root / f"{ctx_id}{_FILE_SUFFIX}"

    def _tmp_path(self, ctx_id: str) -> Path:
        validate_ctx_id(ctx_id)
        return self.root / f"{ctx_id}.tmp.{os.getpid()}.{uuid.uuid4().hex[:8]}"

    def _write_temp(self, tmp: Path, blob: str) -> None:
        """把字节写进临时文件并落盘。**这是唯一会被打断的写窗口。**"""
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(blob)
            fh.flush()
            os.fsync(fh.fileno())

    def _fsync_dir(self) -> None:
        """让 rename 这件事本身也落盘（目录项）。失败不致命，故只吞 OSError。"""
        try:
            fd = os.open(self.root, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(fd)
        except OSError:
            pass
        finally:
            os.close(fd)

    def _saved_at_or_mtime(self, path: Path) -> float:
        try:
            env = _decode_envelope(path.read_text(encoding="utf-8"), str(path))
        except StoreError:
            try:
                return path.stat().st_mtime * 1000.0
            except OSError:
                return 0.0
        except OSError:
            return 0.0
        return float(env["saved_at"])


# ══════════════════════════════════════════════════════════════
# CrashStore：验收用的"断电"
# ══════════════════════════════════════════════════════════════
class CrashStore(FileStore):
    """在第 N 次**写临时文件**时假装断电（只断这一次，之后的 save 恢复正常）。

    关键点：崩溃发生在 `_write_temp` 里、`os.replace` **之前** —— 也就是真的
    "写到一半就死"。此刻磁盘上是：
      * 完整的旧 `{ctx_id}.json`（旧版本还在，字节级未变，可读）
      * 一个残缺的 `{ctx_id}.tmp.*`（半截 JSON）
    而 `list_pending()` 只认 `*.json`，所以不会有半成品混进待处理清单。

    与真实 SIGKILL 的唯一差别：异常会沿 `save` 上抛，`save` 的清理分支会**尽力**
    删掉那份残缺 temp（SIGKILL 时这行代码根本不会执行，见 test_28）。所以
    "残留 temp 也能被忽略"由 test_27 单独验证，不依赖本类的清理行为。

    `crashes` 记下每次断电的现场（写入字节数 / 半截内容），供测试断言
    "真的写了一半才抛"，而不是"什么都没写就抛"。
    """

    def __init__(self, root: str | Path = DEFAULT_ROOT, fail_at: int = 2) -> None:
        super().__init__(root)
        if int(fail_at) < 1:
            raise ValueError(f"fail_at 必须 >= 1，收到 {fail_at!r}")
        self.fail_at = int(fail_at)
        self.attempts = 0
        #: 每次"断电"的现场证据：写到多少字节、内容是什么、目标临时文件名。
        #: 这是验收要用的证物（证明"真的写了一半才死"，而不是写都没写就抛）。
        self.crashes: list[dict] = []

    def _write_temp(self, tmp: Path, blob: str) -> None:
        self.attempts += 1
        if self.attempts == self.fail_at:      # 只断一次电：之后的 save 应当恢复正常
            half = max(1, len(blob) // 2)
            prefix = blob[:half]
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write(prefix)
                fh.flush()
                os.fsync(fh.fileno())
            self.crashes.append({
                "tmp": str(tmp),
                "written": len(prefix),
                "total": len(blob),
                "prefix": prefix,
            })
            raise RuntimeError("simulated crash")
        super()._write_temp(tmp, blob)


# ══════════════════════════════════════════════════════════════
# 续跑编排
# ══════════════════════════════════════════════════════════════
def save_checkpoint(ctx: ContextHandle, store: Any, ctx_id: str, *,
                    reason: str = "") -> str:
    """把 Context 落盘，返回 ctx_id（下次续跑用的把手）。

    - 落盘内容 = `ctx.serialize()` 的**原样输出**，因此 `restore` 后
      `version` / `patch_log` / `data` / `budget` 全部一致，能接着跑；
    - `reason` 是给**人**看的（"挂起等审批""崩溃前快照"），写进
      `history` 里的一条 system 标记，而不是往存储信封里塞额外字段 ——
      信封格式越薄，跨版本读旧文件的成本越低；
    - `ContextError`（不可序列化）原样上抛，不做降级：写不下去就说写不下去。
    """
    validate_ctx_id(ctx_id)
    serialize = getattr(ctx, "serialize", None)
    if not callable(serialize):
        raise StoreError(
            f"save_checkpoint 需要可 serialize() 的 Context，"
            f"收到 {type(ctx).__name__}"
        )
    if reason:
        _mark_checkpoint(ctx, ctx_id, reason)
    payload = _require_payload(serialize())
    store.save(ctx_id, payload)
    return ctx_id


def _mark_checkpoint(ctx: ContextHandle, ctx_id: str, reason: str) -> None:
    """在 history 留一条 checkpoint 标记（审计用）。

    次要信息：`ctx` 若不是可写的 ContextHandle（只读视图/别家实现），
    写不进去也绝不能让落盘失败。
    """
    apply = getattr(ctx, "apply", None)
    if not callable(apply):
        return
    try:
        apply(Patch("append", "history", Owner.LOOP, {
            "role": "system",
            "at": now_ms(),
            "ctx_id": ctx_id,
            "text": f"checkpoint：{reason}",
        }))
    except Exception:  # noqa: BLE001 —— 审计标记失败不得影响持久化主路径
        pass


def load_checkpoint(store: Any, ctx_id: str,
                    budget: Budget | None = None) -> ContextHandle:
    """从 store 恢复 `ContextHandle`；找不到 → `ContextNotFound`，读坏 → `StoreCorrupt`。"""
    validate_ctx_id(ctx_id)
    payload = store.load(ctx_id)
    if payload is None:
        raise ContextNotFound(
            f"没有找到落盘的 Context：ctx_id={ctx_id!r}（store={type(store).__name__}）"
        )
    if not isinstance(payload, str):
        raise StoreCorrupt(
            f"ctx_id={ctx_id!r} 的 payload 不是字符串（{type(payload).__name__}）"
        )
    try:
        return ContextHandle.restore(payload, budget)
    except ContextError as exc:
        # Context 层拒绝的状态 = Store 层存了不可用的东西，统一按"读不出来"报
        raise StoreCorrupt(
            f"ctx_id={ctx_id!r} 的 payload 无法恢复成 Context：{exc}"
        ) from exc


def _phase_of(ctx: ContextHandle) -> Phase | None:
    """把 Context 里的 phase 归一成枚举；认不出就 None（不猜）。"""
    raw = ctx.get("phase")
    if isinstance(raw, Phase):
        return raw
    if isinstance(raw, str):
        try:
            return Phase(raw)
        except ValueError:
            return None
    return None


def _phase_text(ctx: ContextHandle) -> str | None:
    phase = _phase_of(ctx)
    if phase is not None:
        return phase.value
    raw = ctx.get("phase")
    return None if raw is None else str(raw)


def _saved_at_of(store: Any, ctx_id: str) -> float:
    reader = getattr(store, "saved_at", None)
    if callable(reader):
        try:
            value = reader(ctx_id)
        except StoreError:
            return 0.0
        return float(value) if isinstance(value, (int, float)) else 0.0
    return 0.0


def list_resumable(store: Any) -> list[dict]:
    """扫描所有落盘的 Context，产出可续跑清单（按 saved_at 升序）。

    每条形如::

        {"ctx_id": "...", "phase": "WAITING_USER", "saved_at": 1700000000000.0,
         "task_id": "task_x", "pending_confirmation": {...}}

    单条解析失败**不会**让整次扫描失败 —— 记 `{"ctx_id", "saved_at", "error"}` 继续，
    因为"一个坏文件"和"整个集群不可续跑"是两种完全不同的事故等级。
    """
    items: list[dict] = []
    for ctx_id in store.list_pending():
        saved_at = _saved_at_of(store, ctx_id)
        try:
            ctx = load_checkpoint(store, ctx_id)
            items.append({
                "ctx_id": ctx_id,
                "phase": _phase_text(ctx),
                "saved_at": saved_at,
                "task_id": ctx.get("task_id"),
                "pending_confirmation": ctx.get("pending_confirmation"),
            })
        except Exception as exc:  # noqa: BLE001 —— 坏条目降级成报告项，不中断扫描
            items.append({
                "ctx_id": ctx_id,
                "saved_at": saved_at,
                "error": f"{type(exc).__name__}: {exc}",
            })
    items.sort(key=lambda item: (item["saved_at"], item["ctx_id"]))
    return items


def resume_from_store(loop: Any, store: Any, ctx_id: str, e: Any,
                      decision: bool = True, reason: str = "") -> Result:
    """一行搞定崩溃续跑。

    1) `load_checkpoint` 拿到 Context；
    2) `phase == WAITING_USER` → `loop.resume(ctx, e, decision, reason)`
       否则 → `loop.run(goal, ctx, e)`，goal 取 `Context.data["goal"]`，没有就 `"继续"`；
    3) 跑完（非挂起）→ `delete(ctx_id)`：正常结束就不该还留在 pending 里；
    4) 又挂起（结果仍是 WAITING_USER）→ 重新 `save_checkpoint`，把手保持不变。

    返回 `Result`。找不到续跑点时抛 `ContextNotFound`（明确、可分辨）。
    """
    ctx = load_checkpoint(store, ctx_id)
    phase = _phase_of(ctx)

    if phase is Phase.WAITING_USER:
        result = loop.resume(ctx, e, decision, reason)
    else:
        goal = ctx.get("goal")
        if not isinstance(goal, str) or not goal.strip():
            goal = "继续"
        result = loop.run(goal, ctx, e)

    if getattr(result, "phase", None) is Phase.WAITING_USER:
        save_checkpoint(
            ctx, store, ctx_id,
            reason=reason or "续跑后仍挂起，等待人工确认",
        )
    elif _should_keep_checkpoint(result):
        # 预算耗尽 / 步数上限这类失败是**可续跑**的：追加大预算就能接着干。
        # 把落盘点删掉等于替调用方做了「放弃」的决定 —— 那不该由 Store 层决定。
        save_checkpoint(
            ctx, store, ctx_id,
            reason=reason or f"未完成（{getattr(result, 'term_reason', '?')}），保留续跑点",
        )
    else:
        store.delete(ctx_id)
    return result


def _should_keep_checkpoint(result: Any) -> bool:
    """只有「真的还能接着跑」的终态才保留检查点。

    DONE / ABORTED → 删（任务结束或用户明确取消）
    其他（BUDGET / MAX_STEPS / DEADLINE / NO_PROGRESS / FAILED 且 partial）→ 留
    """
    phase = getattr(result, "phase", None)
    if phase in (Phase.DONE, Phase.ABORTED):
        return False
    return bool(getattr(result, "partial", False)) or phase is Phase.FAILED


