"""core/context.py — Context 内核（W1）。

`ContextHandle` 是整个运行时的**唯一可变状态容器**。其他模块只能通过
`Patch` 写、通过 `ContextSnapshot` 读，因此上下文变更天然可审计、可回放。

不变量（本模块负责的部分）
────────────────────────────────────────────────────────────
- INV-3  单写者：每个根字段在 `OWNER_TABLE` 里只有一个 owner。未声明的根字段
         一律拒绝写入（补字段 = 改契约 = 一次显式评审，而不是顺手 set）。
- INV-2  纯读视图：`snapshot()` 返回深拷贝，调用方随便改都不会污染内部状态；
         同一个 snapshot 可以被 Builder 反复消费而字节级一致。
- INV-9  追加式日志：`patch_log` 只 append 不修改，Quality Plane 直接当轨迹吃。
- 预算   累加型预算字段（token_used / cost_used_usd / tool_calls_used）一旦会
         越过上限就抛 `BudgetExceeded`，绝不"先超了再说"。

`apply()` 的四道校验，顺序固定，任何一道失败都抛异常（绝不静默吞掉）：

    1. op ∈ {set, append, incr, merge}      → ContextError
    2. by == OWNER_TABLE[path 根字段]        → OwnershipViolation（期望/实际/path 全给）
    3. path 根字段已声明                     → OwnershipViolation（INV-3）
    4. budget.* 不得超额                     → BudgetExceeded

实现细节：第 3 道校验在第 2 道里一并完成——根字段未声明时"期望 owner"不存在，
这本身就是一次归属判断失败，报错信息里显式标注 `expected=<未声明>`。

`merge()` 是唯一有权绕过第 2 道校验的通道（LOOP 特权，跨分支迁移），
但其写入的 patch 在日志里 `by=Owner.LOOP`；需要重放历史时用
`ContextHandle.replay()`，它对历史 patch 不再校验 owner（历史是既成事实）。
"""
from __future__ import annotations

import copy
import dataclasses
import io
import json
import types
from enum import Enum
from typing import Any, Iterable, Sequence

from contracts import (
    OWNER_TABLE,
    Budget,
    BudgetExceeded,
    ContextError,
    ContextSnapshot,
    Owner,
    OwnershipViolation,
    Patch,
    Principal,
)

SCHEMA_VERSION = 1

#: 允许的 patch 操作
_OPS: tuple[str, ...] = ("set", "append", "incr", "merge")

#: Budget 数据类的全部标量字段（序列化用扁平结构，round-trip 无损）
_BUDGET_FIELDS: tuple[str, ...] = (
    "token_total",
    "token_used",
    "cost_total_usd",
    "cost_used_usd",
    "tool_calls_total",
    "tool_calls_used",
    "max_steps",
)

#: 累加型预算字段 → 对应的上限字段
_USED_TO_TOTAL: dict[str, str] = {
    "token_used": "token_total",
    "cost_used_usd": "cost_total_usd",
    "tool_calls_used": "tool_calls_total",
}

#: 金额类字段，写入时保留 6 位小数避免浮点噪声
_MONEY_FIELDS = frozenset({"cost_total_usd", "cost_used_usd"})


# ══════════════════════════════════════════════════════════════
# 路径 / 归属工具
# ══════════════════════════════════════════════════════════════
def _root_of(path: str) -> str:
    """取点号路径的根字段。"""
    return path.split(".", 1)[0]


def _owner_for(path: str) -> Owner | None:
    """根字段 → owner。支持 OWNER_TABLE 里出现带点键的"最长前缀"命中。"""
    root = _root_of(path)
    if root in OWNER_TABLE:
        return OWNER_TABLE[root]
    best: str | None = None
    for key in OWNER_TABLE:
        if path == key or path.startswith(key + "."):
            if best is None or len(key) > len(best):
                best = key
    return OWNER_TABLE[best] if best is not None else None


def _read_path(data: Any, path: str) -> Any:
    """按点号路径取值，缺失返回 ``_MISSING``。"""
    cur = data
    for seg in path.split("."):
        if not isinstance(cur, dict) or seg not in cur:
            return _MISSING
        cur = cur[seg]
    return cur


_MISSING = object()


def _is_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _validate_path(path: str) -> None:
    if not isinstance(path, str):
        raise ContextError(f"path 必须是 str，收到 {type(path).__name__}")
    if not path or path.startswith(".") or path.endswith(".") or ".." in path:
        raise ContextError(f"非法 path={path!r}：不允许空段")


def _budget_flat(budget: Budget) -> dict:
    return {f: getattr(budget, f) for f in _BUDGET_FIELDS}


def _budget_from_mapping(mapping: dict, fallback: Budget | None) -> Budget:
    """把扁平或嵌套（Budget.to_dict 那种）预算映射还原成 Budget。"""
    base = copy.deepcopy(fallback) if fallback is not None else Budget()
    flat = dict(_budget_flat(base))

    for field in _BUDGET_FIELDS:
        if field in mapping:
            flat[field] = mapping[field]

    for key, total_field, used_field in (
        ("token", "token_total", "token_used"),
        ("cost_usd", "cost_total_usd", "cost_used_usd"),
        ("tool_calls", "tool_calls_total", "tool_calls_used"),
    ):
        nested = mapping.get(key)
        if isinstance(nested, dict):
            if "total" in nested:
                flat[total_field] = nested["total"]
            if "used" in nested:
                flat[used_field] = nested["used"]

    if "max_steps" in mapping:
        flat["max_steps"] = mapping["max_steps"]

    kwargs: dict[str, Any] = {}
    for field in _BUDGET_FIELDS:
        value = flat[field]
        if field in _MONEY_FIELDS:
            kwargs[field] = float(value)
        else:
            kwargs[field] = int(value)
    return Budget(**kwargs)


# ══════════════════════════════════════════════════════════════
# 序列化白名单（显式拒绝不可序列化对象，不靠 json 的默认报错）
# ══════════════════════════════════════════════════════════════
def _jsonable(obj: Any, where: str = "value") -> Any:
    """把上下文值转成 JSON 原生结构；转不动就抛带位置的 ContextError。"""
    if obj is None or isinstance(obj, (bool, int, str)):
        return obj
    if isinstance(obj, float):
        if obj != obj or obj in (float("inf"), float("-inf")):
            raise ContextError(f"{where} 含非有限浮点 {obj!r}，JSON 无法表达")
        return obj
    if isinstance(obj, Enum):
        return _jsonable(obj.value, where)
    if isinstance(obj, (bytes, bytearray, memoryview)):
        raise ContextError(
            f"{where} 含二进制数据（{type(obj).__name__}），序列化前请自行 base64/str 化"
        )
    if isinstance(obj, dict):
        out: dict[str, Any] = {}
        for k, v in obj.items():
            key = k if isinstance(k, str) else str(k)
            out[key] = _jsonable(v, f"{where}.{key}")
        return out
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v, f"{where}[{i}]") for i, v in enumerate(obj)]
    if isinstance(obj, (set, frozenset)):
        return [_jsonable(v, where) for v in sorted(obj, key=repr)]
    if isinstance(obj, (types.ModuleType, type)):
        raise ContextError(
            f"{where} 是不可序列化对象（{type(obj).__name__} {getattr(obj, '__name__', '?')}）"
        )
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        to_dict = getattr(obj, "to_dict", None)
        if callable(to_dict):
            return _jsonable(to_dict(), where)
        return _jsonable(dataclasses.asdict(obj), where)
    if callable(obj):
        name = getattr(obj, "__name__", type(obj).__name__)
        raise ContextError(f"{where} 是不可序列化对象（可调用/函数 {name}）")
    if isinstance(obj, io.IOBase) or any(
        hasattr(obj, attr) for attr in ("fileno", "send", "recv", "sendall", "recvfrom")
    ):
        raise ContextError(
            f"{where} 是不可序列化对象（连接/文件句柄 {type(obj).__name__}），不允许进上下文"
        )
    raise ContextError(
        f"{where} 是不可序列化对象（{type(obj).__name__}），请先转成 dict/list/str"
    )


# ══════════════════════════════════════════════════════════════
# ContextHandle
# ══════════════════════════════════════════════════════════════
class ContextHandle:
    """唯一可变状态容器。所有写操作都必须走 `Patch`。"""

    __slots__ = ("_data", "_budget", "_version", "_log", "_parent_version",
                 "_engine")

    def __init__(self, initial: dict | None = None, budget: Budget | None = None) -> None:
        #: 当前绑定的编排引擎（"loop" / "dag" / ""）。
        #: 一个 Context 同时只能挂一个：`observations` 的 owner 是 LOOP，
        #: 而 DAG 的 `_merge_back` 也会写它 —— 两条路径并发写同一字段，
        #: 顺序与归属就不确定了。
        self._engine = ""
        if initial is not None and not isinstance(initial, dict):
            raise ContextError(
                f"initial 必须是 dict 或 None，收到 {type(initial).__name__}"
            )
        self._data: dict = copy.deepcopy(dict(initial)) if initial else {}
        self._version: int = 0
        self._log: list[Patch] = []
        self._parent_version: int | None = None

        # principal 若给了 dataclass，落库成 dict（Builder 按 dict 消费）
        principal = self._data.get("principal")
        if isinstance(principal, Principal):
            self._data["principal"] = principal.to_dict()

        # 预算来源优先级：显式参数 > data["budget"] > 默认
        if budget is not None:
            if not isinstance(budget, Budget):
                raise ContextError(
                    f"budget 必须是 Budget，收到 {type(budget).__name__}"
                )
            self._budget = copy.deepcopy(budget)
        elif isinstance(self._data.get("budget"), dict):
            self._budget = _budget_from_mapping(self._data["budget"], None)
        else:
            self._budget = Budget()
        # data 里的 budget 段与内存预算保持同一份扁平面（快照可见、可序列化）
        self._data["budget"] = _budget_flat(self._budget)

    # ── 只读视图 ─────────────────────────────────────────────
    @property
    def version(self) -> int:
        return self._version

    @property
    def patch_log(self) -> tuple[Patch, ...]:
        """全部历史 patch（只追加，供 Quality Plane 当轨迹重放）。"""
        return tuple(self._log)

    @property
    def parent_version(self) -> int | None:
        """fork 出来的分支记录的父版本；主线为 None。"""
        return self._parent_version

    @property
    def budget(self) -> Budget:
        """预算的深拷贝（调用方改这份不会影响内部）。"""
        return copy.deepcopy(self._budget)

    @property
    def principal(self) -> Any:
        """当前主体：dict（由 GATEWAY 写入）或 None。"""
        return self._data.get("principal")

    def bind_engine(self, engine: str) -> None:
        """绑定编排引擎。**一个 Context 只允许一个**，冲突即 fail-closed 拒绝。

        为什么需要它：工作流（B，`DAGRunner`）与回合内编排（C，`SubagentRunner`）
        都会把结果回写到父 Context 的 `observations`。两条路径并发写同一字段时，
        顺序与归属都不确定 —— 而 `OWNER_TABLE` 存在的全部意义就是防这个。

        重复绑定**同一个**引擎是幂等的（`Loop` 每轮可能会调）。
        """
        name = str(engine or "").strip().lower()
        if not name:
            return
        current = self._engine
        if current and current != name:
            raise ContextError(
                f"Context 已绑定编排引擎 {current!r}，不能再绑定 {name!r}"
                f"（同一 Context 只能有一个引擎：两者都会写 observations，"
                f"并发写会让顺序与归属不确定）")
        self._engine = name

    @property
    def engine(self) -> str:
        """当前绑定的编排引擎（`""` = 未绑定）。"""
        return self._engine

    def snapshot(self) -> ContextSnapshot:
        """不可变深拷贝视图。改返回值绝不影响内部状态。"""
        return ContextSnapshot(version=self._version, data=copy.deepcopy(self._data))

    def get(self, path: str, default: Any = None) -> Any:
        """便捷读：等价于 `snapshot().get(path, default)`（也是深拷贝）。"""
        value = _read_path(self._data, path)
        return default if value is _MISSING else copy.deepcopy(value)

    # ── 写入口 ───────────────────────────────────────────────
    def apply(self, patch: Patch) -> Patch:
        """校验后落盘。成功返回 patch，失败抛 ContextError 家族异常。"""
        if not isinstance(patch, Patch):
            raise ContextError(f"apply 只接受 Patch，收到 {type(patch).__name__}")
        return self._commit(
            patch.op, patch.path, patch.by, patch.value, check_owner=True
        )

    def _commit(
        self, op: str, path: str, by: Any, value: Any, check_owner: bool = True
    ) -> Patch:
        # ── 校验 1：op 白名单 ───────────────────────────────
        if not isinstance(op, str):
            raise ContextError(f"op 必须是 str，收到 {type(op).__name__}")
        op = op.strip().lower()
        if op not in _OPS:
            raise ContextError(f"非法 op={op!r}，只允许 {list(_OPS)}")

        _validate_path(path)

        if isinstance(by, Owner):
            owner = by
        else:
            try:
                owner = Owner(by)
            except ValueError as exc:
                raise ContextError(f"by={by!r} 不是合法 Owner") from exc

        # ── 校验 2 + 3：归属 / 已声明（INV-3）───────────────
        expected = _owner_for(path)
        if expected is None:
            raise OwnershipViolation(
                f"字段未声明，拒绝写入（INV-3）：expected=<未声明> "
                f"actual={owner.value} path={path}"
            )
        if check_owner and owner is not expected:
            raise OwnershipViolation(
                f"写入者不是该字段的 owner：expected={expected.value} "
                f"actual={owner.value} path={path}"
            )

        # ── 校验 4：预算（在预算是原子事务里做）─────────────
        if path == "budget" or path.startswith("budget."):
            self._commit_budget(op, path, value)
        else:
            self._write(op, path, value)

        self._version += 1
        stored = Patch(op=op, path=path, by=owner, value=copy.deepcopy(value))
        self._log.append(stored)
        return stored

    # ── 普通字段写入 ────────────────────────────────────────
    def _write(self, op: str, path: str, value: Any) -> None:
        segs = path.split(".")
        cur: dict = self._data
        for seg in segs[:-1]:
            nxt = cur.get(seg)
            if nxt is None:
                cur[seg] = {}
                nxt = cur[seg]
            if not isinstance(nxt, dict):
                raise ContextError(
                    f"path={path} 的中间段 {seg!r} 不是 dict（{type(nxt).__name__}）"
                )
            cur = nxt

        leaf = segs[-1]

        if op == "set":
            if path == "principal" and isinstance(value, Principal):
                value = value.to_dict()
            cur[leaf] = copy.deepcopy(value)
            return

        if op == "append":
            existing = cur.get(leaf)
            if existing is None:
                existing = []
                cur[leaf] = existing
            if not isinstance(existing, list):
                raise ContextError(
                    f"append 目标不是 list：path={path} "
                    f"实际 {type(existing).__name__}"
                )
            existing.append(copy.deepcopy(value))
            return

        if op == "incr":
            delta = 1 if value is None else value
            existing = cur.get(leaf, 0)
            if not _is_number(existing) or not _is_number(delta):
                raise ContextError(
                    f"incr 需要数值：path={path} 目标={existing!r} 增量={delta!r}"
                )
            cur[leaf] = existing + delta
            return

        if op == "merge":
            if not isinstance(value, dict):
                raise ContextError(
                    f"merge 的 value 必须是 dict，收到 {type(value).__name__}"
                )
            existing = cur.get(leaf)
            if existing is None:
                existing = {}
                cur[leaf] = existing
            if not isinstance(existing, dict):
                raise ContextError(
                    f"merge 目标不是 dict：path={path} "
                    f"实际 {type(existing).__name__}"
                )
            existing.update(copy.deepcopy(value))
            return

        raise ContextError(f"非法 op={op!r}")  # 理论上不可达

    # ── 预算写入（原子）──────────────────────────────────────
    def _commit_budget(self, op: str, path: str, value: Any) -> None:
        staged = copy.deepcopy(self._budget)

        if path == "budget":
            if op not in ("set", "merge"):
                raise ContextError(f"budget 整体只支持 set/merge，收到 op={op!r}")
            if not isinstance(value, dict):
                raise ContextError(
                    f"budget 整体写入必须是 dict，收到 {type(value).__name__}"
                )
            nested = _budget_from_mapping(value, staged)
            # 逐个字段过一遍校验，保持与单字段写入同样的约束
            for field in _BUDGET_FIELDS:
                _budget_set(staged, field, getattr(nested, field), path)
        else:
            field = path[len("budget."):]
            if "." in field:
                raise ContextError(f"未知预算字段 path={path}（预算字段是扁平的）")
            if field not in _BUDGET_FIELDS:
                raise ContextError(f"未知预算字段 budget.{field}，path={path}")

            if op == "append":
                raise ContextError(f"预算字段不支持 append：path={path}")
            if op == "merge":
                if not isinstance(value, dict):
                    raise ContextError(
                        f"预算字段 merge 的 value 必须是 dict，收到 {type(value).__name__}"
                    )
                _budget_set(staged, field, value, path)
            elif op == "incr":
                delta = 1 if value is None else value
                if not _is_number(delta):
                    raise ContextError(
                        f"incr 需要数值：path={path} 增量={delta!r}"
                    )
                _budget_set(staged, field, getattr(staged, field) + delta, path)
            else:  # set
                if not _is_number(value):
                    raise ContextError(f"set 预算字段需要数值：path={path} 值={value!r}")
                _budget_set(staged, field, value, path)

        self._budget = staged
        self._data["budget"] = _budget_flat(staged)

    # ── fork / merge ────────────────────────────────────────
    def fork(self) -> "ContextHandle":
        """子 Agent 分支：深拷贝 data，继承 principal/budget，日志归零。"""
        child = ContextHandle.__new__(ContextHandle)
        child._data = copy.deepcopy(self._data)
        child._budget = copy.deepcopy(self._budget)
        child._version = self._version
        child._log = []
        child._parent_version = self._version
        # 子 Context 是**另一个执行单元**，不继承引擎归属
        # （否则子 Agent 内部想跑 DAG 会被父的 "loop" 绑定挡住）
        child._engine = ""
        return child

    def merge(self, child: "ContextHandle", fields: Sequence[str]) -> list[Patch]:
        """把 child 上白名单 `fields` 里的路径拉回 self（by=LOOP，特权通道）。

        白名单以外的路径一律不读；白名单里出现未声明字段则越权失败。
        child 上不存在的路径直接跳过（不产生 patch）。
        """
        if not isinstance(child, ContextHandle):
            raise ContextError(
                f"merge 的目标必须是 ContextHandle，收到 {type(child).__name__}"
            )
        if child is self:
            raise ContextError("不能 merge 自身")
        if isinstance(fields, str):
            raise ContextError("fields 必须是路径序列（list/tuple），不是单个字符串")

        applied: list[Patch] = []
        for path in fields:
            _validate_path(path)
            if _owner_for(path) is None:
                raise OwnershipViolation(
                    f"merge 拒绝未声明字段（INV-3）：expected=<未声明> "
                    f"actual={Owner.LOOP.value} path={path}"
                )
            value = _read_path(child._data, path)
            if value is _MISSING:
                continue
            applied.append(
                self._commit(
                    "set", path, Owner.LOOP, copy.deepcopy(value), check_owner=False
                )
            )
        return applied

    # ── 序列化 / 恢复 ───────────────────────────────────────
    def serialize(self) -> str:
        """JSON + schema_version。不可序列化对象 → ContextError（带位置）。"""
        payload = {
            "schema_version": SCHEMA_VERSION,
            "version": self._version,
            "parent_version": self._parent_version,
            "data": _jsonable(self._data, "data"),
            "budget": _budget_flat(self._budget),
            "patch_log": [
                {
                    "op": p.op,
                    "path": p.path,
                    "by": p.by.value if isinstance(p.by, Owner) else str(p.by),
                    "value": _jsonable(p.value, f"patch_log[{i}].value"),
                }
                for i, p in enumerate(self._log)
            ],
        }
        try:
            return json.dumps(payload, ensure_ascii=False)
        except (TypeError, ValueError) as exc:  # 兜底：理论上 _jsonable 已经拦住
            raise ContextError(f"Context 序列化失败：{exc}") from exc

    @staticmethod
    def restore(s: str, budget: Budget | None = None) -> "ContextHandle":
        """从 serialize() 的输出恢复；data 与 version 必须逐一相等。"""
        if not isinstance(s, str):
            raise ContextError(f"restore 需要 str，收到 {type(s).__name__}")
        try:
            payload = json.loads(s)
        except (json.JSONDecodeError, ValueError) as exc:
            raise ContextError(f"Context 反序列化失败（非法 JSON）：{exc}") from exc
        if not isinstance(payload, dict):
            raise ContextError("Context 反序列化失败：顶层不是 JSON object")

        schema = payload.get("schema_version")
        if schema != SCHEMA_VERSION:
            raise ContextError(
                f"schema_version 不兼容：期望 {SCHEMA_VERSION}，收到 {schema!r}"
            )
        data = payload.get("data")
        if not isinstance(data, dict):
            raise ContextError("Context 反序列化失败：data 不是 object")
        version = payload.get("version")
        if not isinstance(version, int) or isinstance(version, bool) or version < 0:
            raise ContextError(f"Context 反序列化失败：version 非法 {version!r}")

        raw_budget = payload.get("budget")
        if budget is not None:
            restored_budget = copy.deepcopy(budget)
        elif isinstance(raw_budget, dict):
            restored_budget = _budget_from_mapping(raw_budget, None)
        else:
            restored_budget = Budget()

        handle = ContextHandle(initial=data, budget=restored_budget)
        # data 里原本有 budget 段 → 保持镜像；原本没有则不要凭空加一份
        if "budget" not in data:
            handle._data.pop("budget", None)
        handle._version = version
        parent = payload.get("parent_version")
        handle._parent_version = parent if isinstance(parent, int) else None

        log: list[Patch] = []
        for i, entry in enumerate(payload.get("patch_log") or []):
            if not isinstance(entry, dict):
                raise ContextError(f"patch_log[{i}] 不是 object")
            try:
                by = Owner(entry["by"])
            except (KeyError, ValueError) as exc:
                raise ContextError(
                    f"patch_log[{i}].by 非法：{entry.get('by')!r}"
                ) from exc
            op = entry.get("op")
            if op not in _OPS:
                raise ContextError(f"patch_log[{i}].op 非法：{op!r}")
            log.append(
                Patch(op=op, path=entry.get("path", ""), by=by, value=entry.get("value"))
            )
        handle._log = log
        return handle

    @classmethod
    def replay(
        cls,
        patches: Iterable[Patch],
        initial: dict | None = None,
        budget: Budget | None = None,
        version: int | None = None,
    ) -> "ContextHandle":
        """重放 patch 序列得到等价状态（Quality Plane 用）。

        历史 patch 是既成事实，因此不再校验 owner（否则 merge 产生的
        by=LOOP 补丁无法重放）；op、路径、预算约束仍然强制。
        """
        handle = cls(initial=initial, budget=budget)
        for patch in patches:
            if not isinstance(patch, Patch):
                raise ContextError(f"replay 只接受 Patch，收到 {type(patch).__name__}")
            handle._commit(
                patch.op, patch.path, patch.by, patch.value, check_owner=False
            )
        if version is not None:
            handle._version = int(version)
        return handle

    # ── 其它 ────────────────────────────────────────────────
    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return (
            f"<ContextHandle v{self._version} fields={len(self._data)} "
            f"patches={len(self._log)} parent={self._parent_version}>"
        )


# ══════════════════════════════════════════════════════════════
# 预算单字段写入 + 校验（供 _commit_budget 复用）
# ══════════════════════════════════════════════════════════════
def _budget_set(budget: Budget, field: str, new_value: Any, path: str) -> None:
    if not _is_number(new_value):
        if isinstance(new_value, dict):
            raise ContextError(f"预算字段需要数值：path={path} 收到 dict")
        raise ContextError(f"预算字段需要数值：path={path} 值={new_value!r}")
    value = float(new_value) if field in _MONEY_FIELDS else int(new_value)
    if field in _MONEY_FIELDS:
        value = round(value, 6)
    if value < 0:
        raise ContextError(f"预算字段不能为负：path={path} 值={value}")

    total_field = _USED_TO_TOTAL.get(field)
    if total_field is not None:
        total = getattr(budget, total_field)
        if value > total:
            raise BudgetExceeded(
                f"预算不足：path={path} {getattr(budget, field)} → {value} "
                f"超过 {total_field}={total}"
            )
    else:
        # 上限字段：不允许下调到低于已用量
        for used_field, bound_field in _USED_TO_TOTAL.items():
            if bound_field == field and getattr(budget, used_field) > value:
                raise BudgetExceeded(
                    f"预算上限不能低于已用量：path={path} {used_field}="
                    f"{getattr(budget, used_field)} > 新上限 {value}"
                )

    setattr(budget, field, value)
