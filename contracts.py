"""contracts.py — 冻结的共享契约。

规则：
1. 所有 worker 只读本文件，**不得修改**。
2. 确需新增字段 → 写到文件末尾「EXTENSIONS」区，不改动已有签名。
3. 测试统一用 stdlib unittest（本机无 pytest），不引入第三方依赖。
"""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Protocol, Sequence, runtime_checkable

try:                                  # 可选依赖：没装 jsonschema 也要能 import 本模块
    import jsonschema
except ImportError:                   # pragma: no cover —— 取决于运行环境
    jsonschema = None                 # type: ignore[assignment]


# ══════════════════════════════════════════════════════════════
# 枚举
# ══════════════════════════════════════════════════════════════
class Phase(str, Enum):
    RECEIVED = "RECEIVED"
    ROUTING = "ROUTING"
    PLANNING = "PLANNING"
    EXECUTING = "EXECUTING"
    REPLANNING = "REPLANNING"
    WAITING_USER = "WAITING_USER"
    FINALIZING = "FINALIZING"
    CLARIFY = "CLARIFY"
    DONE = "DONE"
    FAILED = "FAILED"
    ABORTED = "ABORTED"


class Owner(str, Enum):
    GATEWAY = "GATEWAY"
    ROUTER = "ROUTER"
    PLANNER = "PLANNER"
    LOOP = "LOOP"
    MEMORY = "MEMORY"
    KNOWLEDGE = "KNOWLEDGE"
    SKILL = "SKILL"
    DISPATCHER = "DISPATCHER"
    NORMALIZER = "NORMALIZER"
    #: Workflow（DAG）执行事实的权威源。
    #:
    #: 为什么需要一个**新** owner：`DAGRunner` 原先把 `parent_ctx` 一路透传给
    #: `dispatch`，自己**一个字都不写**（实测 `ctx.apply` 计数为 0）。
    #: 后果是"DAG 跑到第几层、哪个节点被跳过、为什么跳过"没有任何地方记录 ——
    #: 出事后无法从 Context 复盘。但又不能让它写 `observations`（那是 LOOP 的），
    #: 否则与回合内编排（C）抢同一个字段。
    WORKFLOW = "WORKFLOW"


class TermReason(str, Enum):
    DONE = "DONE"
    ABORTED = "ABORTED"
    DEADLINE = "DEADLINE"
    BUDGET = "BUDGET"
    MAX_STEPS = "MAX_STEPS"
    NO_PROGRESS = "NO_PROGRESS"
    UNRECOVERABLE = "UNRECOVERABLE"
    NEEDS_CONFIRMATION = "NEEDS_CONFIRMATION"


class SideEffect(str, Enum):
    READ_ONLY = "read_only"
    MUTATING = "mutating"
    IRREVERSIBLE = "irreversible"


class RiskTier(int, Enum):
    T0_SAFE = 0
    T1_LOW = 1
    T2_HIGH = 2
    T3_CRITICAL = 3


# ══════════════════════════════════════════════════════════════
# 错误
# ══════════════════════════════════════════════════════════════
class ContextError(Exception):
    """Context 操作失败的基类。"""


class OwnershipViolation(ContextError):
    """写入者与字段 owner 不符（INV-3）。"""


class BudgetExceeded(ContextError):
    """预算不足。"""


# ══════════════════════════════════════════════════════════════
# Context：patch 协议
# ══════════════════════════════════════════════════════════════
@dataclass(frozen=True)
class Patch:
    op: str           # set | append | incr | merge
    path: str
    by: Owner
    value: Any = None

    def to_dict(self) -> dict:
        return {"op": self.op, "path": self.path, "by": self.by.value, "value": self.value}


@dataclass(frozen=True)
class ContextSnapshot:
    """不可变视图。Builder 只吃这个。"""
    version: int
    data: dict

    def get(self, path: str, default: Any = None) -> Any:
        cur: Any = self.data
        for seg in path.split("."):
            if not isinstance(cur, dict) or seg not in cur:
                return default
            cur = cur[seg]
        return cur


# 字段 → 唯一 writer（INV-3）。前缀匹配，未声明的字段一律拒绝写入。
OWNER_TABLE: dict[str, Owner] = {
    "principal": Owner.GATEWAY,
    "session_id": Owner.GATEWAY,
    "turn_id": Owner.GATEWAY,
    "task_id": Owner.GATEWAY,
    "agent_state": Owner.ROUTER,
    "route_confidence": Owner.ROUTER,
    "plan": Owner.PLANNER,
    "current_step": Owner.PLANNER,
    "step_status": Owner.PLANNER,
    #: DAG 执行事实：层/节点/状态/跳过原因。
    #: **与 observations 分开**：observations 是回合内的观测事实（LOOP 的），
    #: 这里是工作流的编排事实（WORKFLOW 的）。混在一起会让 C 与 B 互相覆盖。
    "workflow_state": Owner.WORKFLOW,
    "observations": Owner.LOOP,          # 观测事实的权威源是 Loop，Planner 只读
    "phase": Owner.LOOP,
    "loop_count": Owner.LOOP,
    "deadline": Owner.LOOP,
    "error": Owner.LOOP,
    "pending_confirmation": Owner.LOOP,
    "history": Owner.LOOP,
    "summary": Owner.LOOP,
    "tool_results": Owner.LOOP,
    "usage": Owner.LOOP,
    "budget": Owner.LOOP,
    "memory_hits": Owner.MEMORY,
    "knowledge_hits": Owner.KNOWLEDGE,
    "skill_state": Owner.SKILL,
    "retry_count": Owner.DISPATCHER,
    "denied_log": Owner.DISPATCHER,
    "artifacts": Owner.NORMALIZER,
}


# ══════════════════════════════════════════════════════════════
# 预算与执行上下文
# ══════════════════════════════════════════════════════════════
@dataclass
class Budget:
    token_total: int = 20000
    token_used: int = 0
    cost_total_usd: float = 0.50
    cost_used_usd: float = 0.0
    tool_calls_total: int = 20
    tool_calls_used: int = 0
    max_steps: int = 8

    def token_left(self) -> int:
        return self.token_total - self.token_used

    def cost_left(self) -> float:
        return round(self.cost_total_usd - self.cost_used_usd, 6)

    def tool_calls_left(self) -> int:
        return self.tool_calls_total - self.tool_calls_used

    def exhausted(self) -> str | None:
        if self.token_left() < 0:
            return "token"
        if self.cost_left() < 0:
            return "cost"
        if self.tool_calls_left() < 0:
            return "tool_calls"
        return None

    def to_dict(self) -> dict:
        return {
            "token": {"total": self.token_total, "used": self.token_used},
            "cost_usd": {"total": self.cost_total_usd, "used": self.cost_used_usd},
            "tool_calls": {"total": self.tool_calls_total, "used": self.tool_calls_used},
            "max_steps": self.max_steps,
        }


@dataclass(frozen=True)
class Principal:
    user_id: str = "u_demo"
    roles: tuple[str, ...] = ("engineer",)
    tenant: str = "acme"
    scopes: frozenset[str] = frozenset({"tool.read", "tool.write"})

    def can(self, scope: str) -> bool:
        return scope in self.scopes

    def to_dict(self) -> dict:
        return {"user_id": self.user_id, "roles": list(self.roles),
                "tenant": self.tenant, "scopes": sorted(self.scopes)}


@dataclass(frozen=True)
class Approval:
    tool: str
    args_hash: str
    approved: bool
    approver: str = "human"


@dataclass
class ExecCtx:
    """主动脉：principal / 预算 / 截止 / 追踪 / 幂等 一次带到任何地方。"""
    principal: Principal = field(default_factory=Principal)
    task_id: str = field(default_factory=lambda: f"task_{uuid.uuid4().hex[:8]}")
    turn_id: str = field(default_factory=lambda: f"turn_{uuid.uuid4().hex[:8]}")
    trace_id: str = field(default_factory=lambda: uuid.uuid4().hex[:16])
    deadline: float = 0.0            # epoch ms；0 表示不限
    budget: Budget = field(default_factory=Budget)
    idempotency_key: str = ""
    approvals: tuple[Approval, ...] = ()
    depth: int = 0
    _aborted: bool = False
    #: **外部取消信号**（跨线程）：HTTP 层超时后由另一个线程 set()。
    #:
    #: 为什么需要它：`abort()` 只能从"跑回合的那个线程"里调，而 HTTP 超时发生在
    #: 事件循环线程 —— 拿不到 ExecCtx 的引用，只能预先塞一个 `threading.Event` 进来。
    #: 有了它，阻塞中的调用方（adapter 的每次重试、工具执行前后）能在**下一个检查点**
    #: 立刻退出，而不是把整轮跑完。
    #:
    #: 注意这仍是**协作式**的：CPython 无法安全强杀线程。真正让"卡在 socket 上"的
    #: 线程退出的是**读超时**（见 adapters/openai_compat.py 的 per-request timeout）。
    cancel_event: Any = None
    #: **副作用落盘回调**：`(ctx, e, info) -> None`。`None` = 不落盘（默认）。
    #:
    #: 为什么需要它：现有的 checkpoint **只在 `WAITING_USER` 挂起时**落盘。
    #: 长回合（真模型实测 4-6 步 / 26 次工具 / 40 秒）如果中途进程崩了，
    #: 前几步的工具调用**全丢** —— 而其中可能有"已经改了生产"的操作。
    #:
    #: 触发点刻意选在**「非只读工具成功之后」**而不是每步末：
    #:   * 只读取证丢了重跑就行（便宜，且幂等）
    #:   * 已经发生的写操作丢了才是灾难 —— **你不知道它到底做没做**
    #: 所以这里只付"真正需要"的写入成本，不是每步全量序列化。
    #:
    #: **子 Agent 不继承**（见 `child()`）：子 Context 是 fork 出来的另一个
    #: Context，用它爹的 `ctx_id` 落盘会把两份状态搅在一起。
    checkpoint: Any = None
    #: 进度侧通道（见 `ProgressSink`）。`None` = 不播报（默认）。
    #:
    #: 谁写：`SubagentRunner` 在 start/step/finish 三个点 emit。
    #: 谁读：`StreamingGateway` 每次 tick 清空并转成 `step` 事件。
    #: **不进 Context** —— 那是状态，这是事件。
    progress: Any = None

    @property
    def aborted(self) -> bool:
        if self._aborted:
            return True
        ev = self.cancel_event
        return bool(ev is not None and getattr(ev, "is_set", lambda: False)())

    def abort(self) -> None:
        self._aborted = True
        # 同步置位外部信号：让**其他线程**里正在跑的检查也能看到
        ev = self.cancel_event
        if ev is not None:
            try:
                ev.set()
            except Exception:                                  # noqa: BLE001
                pass

    def deadline_passed(self) -> bool:
        return self.deadline > 0 and now_ms() > self.deadline

    def approved(self, tool: str, args_hash: str) -> bool:
        return any(a.approved and a.tool == tool and a.args_hash == args_hash
                   for a in self.approvals)

    def child(self, max_depth: int = 2) -> "ExecCtx":
        """子 Agent：principal 降权 + 预算切分 + depth+1（INV-18）。"""
        if self.depth + 1 > max_depth:
            raise ContextError(f"depth {self.depth + 1} 超过 max_depth={max_depth}")
        half = Budget(
            token_total=max(1, self.budget.token_left() // 2),
            cost_total_usd=max(0.0, self.budget.cost_left() / 2),
            tool_calls_total=max(1, self.budget.tool_calls_left() // 2),
            max_steps=max(1, self.budget.max_steps // 2),
        )
        return ExecCtx(
            principal=Principal(
                user_id=self.principal.user_id,
                roles=self.principal.roles,
                tenant=self.principal.tenant,
                scopes=frozenset(s for s in self.principal.scopes if s.endswith(".read")),
            ),
            task_id=self.task_id,
            trace_id=self.trace_id,
            deadline=self.deadline,
            budget=half,
            depth=self.depth + 1,
            # **继承取消信号与进度通道**：否则
            #   * 超时只停住父层，子 Agent 继续烧 token；
            #   * 层叠子 Agent（子再派子）的进度永远传不到客户端。
            cancel_event=self.cancel_event,
            progress=self.progress,
            # `checkpoint` **刻意不继承**：子 Context 是 fork 出来的另一个
            # Context，用父的 `ctx_id` 落盘会把两份状态搅在一起
            # （恢复时读到的是"父的骨架 + 子的局部"，两边都不对）。
        )


# ══════════════════════════════════════════════════════════════
# Tool
# ══════════════════════════════════════════════════════════════
@dataclass(frozen=True)
class ToolManifest:
    name: str
    side_effect: SideEffect
    risk_tier: RiskTier = RiskTier.T0_SAFE
    idempotent: bool = True
    timeout_ms: int = 5000
    max_result_bytes: int = 65536
    required_scope: str = "tool.read"
    secrets: tuple[str, ...] = ()
    per_turn_limit: int = 10
    description: str = ""
    #: 入参 JSON Schema（子集）。**没有它就只能靠模型猜参数名** ——
    #: 实测模型会把 `lines` 写成 `limit`，而 Dispatcher 静默接受，
    #: 工具收到空结果后模型还会基于空结果一本正经地分析。
    #: 支持的键：type / properties / required / additionalProperties /
    #: items / enum / minimum / maximum / minLength / maxLength。
    input_schema: dict | None = None


@dataclass(frozen=True)
class ToolResult:
    tool: str
    payload: Any
    bytes_in: int = 0
    bytes_out: int = 0
    truncated: bool = False
    deduped: bool = False
    redacted: int = 0
    duration_ms: float = 0.0

    def to_dict(self) -> dict:
        return {"tool": self.tool, "payload": self.payload,
                "bytes_in": self.bytes_in, "bytes_out": self.bytes_out,
                "truncated": self.truncated, "deduped": self.deduped,
                "redacted": self.redacted, "duration_ms": self.duration_ms}


@dataclass(frozen=True)
class ToolOutcome:
    kind: str                       # ok | denied | needs_confirmation | failed | timeout
    result: ToolResult | None = None
    reason: str = ""
    retryable: bool = False

    @staticmethod
    def ok(r: ToolResult) -> "ToolOutcome":
        return ToolOutcome(kind="ok", result=r)

    @staticmethod
    def denied(reason: str) -> "ToolOutcome":
        return ToolOutcome(kind="denied", reason=reason)

    @staticmethod
    def needs_confirmation(reason: str) -> "ToolOutcome":
        return ToolOutcome(kind="needs_confirmation", reason=reason)

    @staticmethod
    def failed(reason: str, retryable: bool = False) -> "ToolOutcome":
        return ToolOutcome(kind="failed", reason=reason, retryable=retryable)

    @staticmethod
    def timeout(ms: float) -> "ToolOutcome":
        return ToolOutcome(kind="timeout", reason=f"timeout after {ms:.0f}ms", retryable=True)


# ══════════════════════════════════════════════════════════════
# LLM
# ══════════════════════════════════════════════════════════════
@dataclass(frozen=True)
class NormalizedToolCall:
    id: str
    name: str
    args: dict


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_hit_tokens: int = 0
    model: str = "mock-1"

    @property
    def total(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass(frozen=True)
class LLMResponse:
    text: str = ""
    tool_calls: tuple[NormalizedToolCall, ...] = ()
    usage: Usage = field(default_factory=Usage)
    finish_reason: str = "stop"


# ══════════════════════════════════════════════════════════════
# 结果
# ══════════════════════════════════════════════════════════════
@dataclass(frozen=True)
class Result:
    phase: Phase
    term_reason: TermReason
    text: str = ""
    partial: bool = False
    steps: int = 0
    artifacts: tuple[dict, ...] = ()

    def to_dict(self) -> dict:
        return {"phase": self.phase.value, "term_reason": self.term_reason.value,
                "text": self.text, "partial": self.partial, "steps": self.steps,
                "artifacts": list(self.artifacts)}


# ══════════════════════════════════════════════════════════════
# 协议（structural typing，worker 只需形状一致）
# ══════════════════════════════════════════════════════════════
class BuilderProtocol(Protocol):
    def build(self, snap: ContextSnapshot, tools: Sequence[ToolManifest]) -> str: ...


class LLMProtocol(Protocol):
    def complete(self, prompt: str, tools: Sequence[ToolManifest],
                 e: ExecCtx) -> LLMResponse: ...


class DispatcherProtocol(Protocol):
    def invoke(self, name: str, args: dict, e: ExecCtx) -> ToolOutcome: ...


class TracerProtocol(Protocol):
    def span(self, name: str, attrs: dict | None = None): ...   # contextmanager
    def finish(self) -> dict: ...


_TYPE_MAP: dict[str, tuple[type, ...]] = {
    "string": (str,),
    "integer": (int,),
    "number": (int, float),
    "boolean": (bool,),
    "object": (dict,),
    "array": (list,),
}


#: 编译后的 Validator 缓存。键是 schema **内容**的指纹，不是 id() ——
#: id 会被 CPython 回收复用，指向另一份 schema 时就是静默错判（比缓存未命中危险得多）。
#: 工具清单在一次进程里是稳定的，所以命中率接近 100%；设上限只为防动态造 schema 的场景
#: 把内存吃穿（插入序淘汰，够用且无需额外依赖）。
_VALIDATOR_CACHE: dict[str, Any] = {}
_VALIDATOR_CACHE_LIMIT = 256
#: FormatChecker 无状态，进程内单例即可（formats 注册表是模块级的）。
_FORMAT_CHECKER: Any = None

#: 这些关键字的值是「子 schema」——归一化时要递归进去摘掉读不懂的片段。
_SUBSCHEMA_MAP_KEYS = ("properties", "patternProperties", "dependentSchemas",
                       "$defs", "definitions")
_SUBSCHEMA_LIST_KEYS = ("allOf", "anyOf", "oneOf", "prefixItems")
_SUBSCHEMA_ONE_KEYS = ("not", "if", "then", "else", "contains", "propertyNames",
                       "additionalProperties", "additionalItems", "unevaluatedItems",
                       "unevaluatedProperties", "items")
_HANDLED_SUBSCHEMA_KEYS = frozenset(
    _SUBSCHEMA_MAP_KEYS + _SUBSCHEMA_LIST_KEYS + _SUBSCHEMA_ONE_KEYS)


def _sanitize_schema(schema: dict) -> dict:
    """摘掉「读不懂的片段」，其余原样保留，再交给 jsonschema 编译。

    为什么要有这一步（而不是直接把原 schema 丢给 check_schema）：
    老实现的口径是「读不懂就跳过」（``tests/test_input_schema.py`` 第 7 例
    明确锁住：``properties`` 里写成 ``"x": "not-a-dict"`` 不许让整次校验 fail-closed，
    因为它本来也没约束什么）。降级规则只作用于**子 schema 槽位里不是 schema 的东西**；
    结构性的错（``{"type": "not-a-type"}``）原样保留，交给 check_schema 抓出来。

    注意这里刻意用 ``.get()`` 读键：会抛异常的 mapping（schema 写坏/代理对象）
    在第一句话就暴露，由调用方（Dispatcher）按 fail-closed 处理，而不是被当成空 schema。
    """
    out: dict[str, Any] = {k: v for k, v in schema.items()
                           if k not in _HANDLED_SUBSCHEMA_KEYS}
    for key in _SUBSCHEMA_MAP_KEYS:
        raw = schema.get(key)
        if isinstance(raw, dict):
            out[key] = {name: _sanitize_schema(sub) for name, sub in raw.items()
                        if isinstance(sub, dict)}
    for key in _SUBSCHEMA_LIST_KEYS:
        raw = schema.get(key)
        if isinstance(raw, list):
            out[key] = [_sanitize_schema(sub) for sub in raw if isinstance(sub, dict)]
    for key in _SUBSCHEMA_ONE_KEYS:
        raw = schema.get(key)
        if isinstance(raw, bool):
            out[key] = raw
        elif isinstance(raw, dict):
            out[key] = _sanitize_schema(raw)
        elif isinstance(raw, list) and key == "items":      # draft-04 的元组式 items
            out[key] = [_sanitize_schema(sub) for sub in raw if isinstance(sub, dict)]
    return out


def _schema_fingerprint(schema: dict) -> str:
    try:
        return json.dumps(schema, sort_keys=True, ensure_ascii=False, default=repr)
    except (TypeError, ValueError):     # 理论不可达（已归一化），兜底不炸
        return repr(schema)


def _rfc3339_datetime(value: Any) -> bool:
    """RFC 3339 date-time 的最小实现（stdlib 够用，不必为此拉 rfc3339-validator）。"""
    import datetime
    if not isinstance(value, str):
        return False
    text = value.strip()
    if "T" not in text and "t" not in text:
        return False
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        datetime.datetime.fromisoformat(text)
    except ValueError:
        return False
    return True


def _format_checker() -> Any:
    """FormatChecker 单例：jsonschema 默认注册表 + 我们自己补的 date-time。

    为什么补：jsonschema 4.26 的 ``FormatChecker()`` 只注册了能用 stdlib 实现的格式
    （email / ipv4 / ipv6 / uuid / date / time / regex / idn-*），``date-time`` / ``uri``
    要装 ``jsonschema[format]`` 才有。而工具入参里最常见的时间戳正是 date-time，所以
    用 stdlib 补这一条；其余格式保持「没装扩展就只当注解」的本来语义，不假装校验过。
    """
    global _FORMAT_CHECKER
    if _FORMAT_CHECKER is None:
        checker = jsonschema.FormatChecker()
        checker.checkers.setdefault("date-time", (_rfc3339_datetime, ValueError))
        _FORMAT_CHECKER = checker
    return _FORMAT_CHECKER


def _compile_validator(schema: dict) -> Any:
    """取（必要时编译）Draft 2020-12 的 Validator；schema 非法时抛 SchemaError。"""
    key = _schema_fingerprint(schema)
    cached = _VALIDATOR_CACHE.get(key)
    if cached is not None:
        return cached
    # 按 schema 自己声明的 $schema 选草案（默认 2020-12）：声明 draft-07 / draft-04 的
    # schema 用 2020-12 去校验会误判（典型：元组式 items / 布尔式 exclusiveMaximum），
    # 甚至会因为 metaschema 不认识而把合法 schema 判成非法。
    validator_cls = jsonschema.validators.validator_for(
        schema, default=jsonschema.Draft202012Validator)
    validator_cls.check_schema(schema)          # 非法 schema → jsonschema.SchemaError
    validator = validator_cls(schema, format_checker=_format_checker())
    if len(_VALIDATOR_CACHE) >= _VALIDATOR_CACHE_LIMIT:
        _VALIDATOR_CACHE.pop(next(iter(_VALIDATOR_CACHE)), None)
    _VALIDATOR_CACHE[key] = validator
    return validator


def _format_error_path(base: str, parts: Sequence[Any]) -> str:
    """把 jsonschema 的 absolute_path 还原成 ``args.rows[1].id`` 这种形状。"""
    out = base
    for part in parts:
        if isinstance(part, int) and not isinstance(part, bool):
            out += f"[{part}]"
        else:
            out += f".{part}"
    return out


def _json_type_name(value: Any) -> str:
    """报错里的「收到什么」：用 Python 类型名（str/dict/list），布尔与 null 用 JSON 名。"""
    if isinstance(value, bool):
        return "boolean"
    if value is None:
        return "null"
    return type(value).__name__


def _matches_any_pattern(name: str, patterns: dict) -> bool:
    import re
    for pattern in patterns:
        try:
            if re.search(pattern, name):
                return True
        except re.error:                 # 坏正则：不算匹配，check_schema 会另报
            continue
    return False


def _first_duplicate(items: Sequence[Any]) -> Any:
    seen: set[str] = set()
    for item in items:
        key = json.dumps(item, sort_keys=True, default=repr, ensure_ascii=False)
        if key in seen:
            return item
        seen.add(key)
    return None


def _translate_errors(err: Any, base: str) -> list[str]:
    """一条 ``jsonschema.ValidationError`` → 一条或多条老格式的中文错误。

    老格式是 ``plane/tool.py`` 与既有 37 个用例的依赖面，所以这里是「翻译」不是「重写」：
    路径用 absolute_path，措辞沿用 ``{path}: <老措辞>``。
    """
    parts = list(err.absolute_path)
    where = _format_error_path(base, parts)
    validator = err.validator
    limit = err.validator_value
    instance = err.instance

    if validator == "type":
        expected = limit if isinstance(limit, str) else "|".join(str(x) for x in limit)
        return [f"{where}: 期望 {expected}，收到 {_json_type_name(instance)}"]

    if validator == "enum":
        values = list(limit) if not isinstance(limit, (str, bytes)) else [limit]
        return [f"{where}: 值 {instance!r} 不在枚举 {values} 内"]

    if validator == "const":
        return [f"{where}: 值 {instance!r} 不等于常量 {limit!r}"]

    if validator in ("minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum"):
        container = err.schema if isinstance(err.schema, dict) else {}
        bound = limit
        exclusive = validator.startswith("exclusive")
        if isinstance(limit, bool):             # draft-04：exclusive* 是布尔修饰符
            bound = container.get("minimum" if validator.endswith("Minimum") else "maximum")
        elif validator == "minimum" and container.get("exclusiveMinimum") is True:
            exclusive = True                    # draft-04 的另一种写法：minimum + 修饰符
        elif validator == "maximum" and container.get("exclusiveMaximum") is True:
            exclusive = True
        is_lower = validator.endswith("Minimum") or validator == "minimum"
        if exclusive:
            symbol = "<= exclusiveMinimum" if is_lower else ">= exclusiveMaximum"
        else:
            symbol = "< minimum" if is_lower else "> maximum"
        return [f"{where}: {instance} {symbol} {bound}"]

    if validator in ("minLength", "maxLength"):
        symbol = "minLength" if validator == "minLength" else "maxLength"
        op = "<" if validator == "minLength" else ">"
        return [f"{where}: 长度 {len(instance)} {op} {symbol} {limit}"]

    if validator in ("minItems", "maxItems"):
        symbol = "minItems" if validator == "minItems" else "maxItems"
        op = "<" if validator == "minItems" else ">"
        return [f"{where}: 元素数 {len(instance)} {op} {symbol} {limit}"]

    if validator == "required":
        missing = [k for k in (limit or ()) if not isinstance(instance, dict) or k not in instance]
        if not missing:                  # 兜底：理论不可达
            return [f"{where}: 缺少必填字段（{err.message}）"]
        return [f"{_format_error_path(base, parts + [k])}: 缺少必填字段" for k in missing]

    if validator == "dependentRequired":
        deps = limit if isinstance(limit, dict) else {}
        out: list[str] = []
        for trigger, needed in deps.items():
            if not isinstance(instance, dict) or trigger not in instance:
                continue
            for name in needed:
                if name not in instance:
                    out.append(
                        f"{_format_error_path(base, parts + [name])}: 缺少必填字段"
                        f"（{trigger!r} 存在时依赖 {name!r}）")
        return out or [f"{where}: 依赖字段缺失（dependentRequired）"]

    if validator in ("additionalProperties", "unevaluatedProperties"):
        extra: list[Any] = []
        if isinstance(instance, dict):
            container = err.schema if isinstance(err.schema, dict) else {}
            props = container.get("properties") or {}
            patterns = container.get("patternProperties") or {}
            extra = [k for k in instance
                     if k not in props and not _matches_any_pattern(k, patterns)]
        if not extra:                    # 子 schema 形状的 additionalProperties：靠子错误报
            return [f"{where}: 未声明的字段（schema 禁止额外属性）"]
        return [f"{_format_error_path(base, parts + [k])}: 未声明的字段（schema 禁止额外属性）"
                for k in extra]

    if validator == "pattern":
        return [f"{where}: 值 {instance!r} 不匹配 pattern {limit!r}"]

    if validator == "format":
        return [f"{where}: 值 {instance!r} 不符合 format {limit!r}"]

    if validator == "multipleOf":
        return [f"{where}: {instance} 不是 {limit} 的整数倍（multipleOf）"]

    if validator == "uniqueItems":
        dup = _first_duplicate(instance) if isinstance(instance, list) else None
        tail = f"，重复元素 {dup!r}" if dup is not None else ""
        return [f"{where}: 数组元素必须唯一（uniqueItems）{tail}"]

    if validator == "oneOf":
        # "is valid under each of" → 匹配了多种；否则一种都没匹配上
        if "under each of" in (err.message or ""):
            return [f"{where}: 值同时满足 oneOf 的多种形态（必须恰好一种）"]
        return [f"{where}: 值不满足 oneOf 的恰好一种形态（一种都没匹配上）"]

    if validator == "anyOf":
        # **必须告诉模型缺哪个字段**，否则它只看到"不满足任何一种形态"，
        # 无从下手（实测：模型会原样重发一次，白烧一轮）。
        # jsonschema 的 anyOf 错误本身不含"每条分支缺什么"，这里从分支里
        # 提取各自声明的 `required`，列出候选键名。
        hints: list[str] = []
        for branch in (limit if isinstance(limit, list) else []):
            if isinstance(branch, dict):
                req = branch.get("required")
                if isinstance(req, list) and req:
                    hints.append("/".join(str(x) for x in req))
        if hints:
            return [f"{where}: 值不满足 anyOf 中的任何一种形态"
                    f"（需要以下之一：{' 或 '.join(hints)}）"]
        return [f"{where}: 值不满足 anyOf 中的任何一种形态"]

    if validator == "allOf":
        return [f"{where}: 值不满足 allOf 中的某一形态（{err.message}）"]

    if validator == "not":
        return [f"{where}: 值命中了 not 禁止的形态"]

    if validator == "contains":
        return [f"{where}: 数组中没有元素满足 contains 声明的形态"]

    if validator == "propertyNames":
        return [f"{where}: 字段名不满足 propertyNames 约束（{err.message}）"]

    # 兜底：保留 jsonschema 原文（英文），但路径与中文前缀与老格式一致
    return [f"{where}: 校验失败（{validator}）：{err.message}"]


def validate_against_schema(schema: dict | None, args: Any,
                            path: str = "args") -> list[str]:
    """按 JSON Schema 校验入参，返回错误列表（空列表＝通过）。

    实现是 ``jsonschema``（Draft 2020-12）的完整语义：oneOf/anyOf/allOf/not、
    pattern/patternProperties、dependentRequired、multipleOf、uniqueItems、
    format、$ref+$defs …… 手写子集漏判的每一条，都是「模型瞎传参数、工具静默
    返回空结果」的入口。

    三条兼容承诺：
      * ``schema=None`` → 不校验（没有声明契约就不假装有）；
      * 错误消息沿用老格式 ``{path}: <措辞>``（``plane/tool.py`` 与 37 个既有用例依赖它）；
      * ``jsonschema`` 没装 → 退化到 ``_validate_subset``（原子集实现），import 不会炸。

    schema 本身非法（``jsonschema.SchemaError``）→ 抛 ``ValueError``：宁可 fail-closed
    也不把「schema 写错了」静默当成「校验通过」。
    """
    if schema is None:
        return []
    if not isinstance(schema, dict):
        return [f"{path}: schema 必须是 dict，收到 {type(schema).__name__}"]

    # 每次调用都读模块级名字：测试与本机部署都可能把它置 None 来验证降级路径
    library = jsonschema
    if library is None:
        return _validate_subset(schema, args, path)

    clean = _sanitize_schema(schema)
    try:
        validator = _compile_validator(clean)
    except library.SchemaError as exc:
        message = getattr(exc, "message", None) or str(exc)
        raise ValueError(f"{path}: schema 本身不是合法 JSON Schema：{message}") from exc

    errs: list[str] = []
    for err in validator.iter_errors(args):
        errs.extend(_translate_errors(err, path))
    return errs


def _validate_subset(schema: dict | None, args: Any,
                     path: str = "args") -> list[str]:
    """手写子集校验器：只在 ``jsonschema`` 不可用时使用（也是老行为的精确保留）。

    支持：type / properties / required / additionalProperties / items /
          enum / minimum / maximum / minLength / maxLength。
    """
    if schema is None:
        return []
    errs: list[str] = []
    if not isinstance(schema, dict):
        return [f"{path}: schema 必须是 dict，收到 {type(schema).__name__}"]

    t = schema.get("type")
    if isinstance(t, str):
        allowed = _TYPE_MAP.get(t)
        if allowed is None:
            errs.append(f"{path}: 未知 type {t!r}")
        else:
            if t in ("integer", "number") and isinstance(args, bool):
                errs.append(f"{path}: 期望 {t}，收到 boolean")
            elif not isinstance(args, allowed):
                errs.append(f"{path}: 期望 {t}，收到 {type(args).__name__}")

    if "enum" in schema and args not in schema["enum"]:
        errs.append(f"{path}: 值 {args!r} 不在枚举 {schema['enum']} 内")

    if isinstance(args, (int, float)) and not isinstance(args, bool):
        if "minimum" in schema and args < schema["minimum"]:
            errs.append(f"{path}: {args} < minimum {schema['minimum']}")
        if "maximum" in schema and args > schema["maximum"]:
            errs.append(f"{path}: {args} > maximum {schema['maximum']}")

    if isinstance(args, str):
        if "minLength" in schema and len(args) < schema["minLength"]:
            errs.append(f"{path}: 长度 {len(args)} < minLength {schema['minLength']}")
        if "maxLength" in schema and len(args) > schema["maxLength"]:
            errs.append(f"{path}: 长度 {len(args)} > maxLength {schema['maxLength']}")

    if isinstance(args, dict):
        props = schema.get("properties") or {}
        for key in schema.get("required") or ():
            if key not in args:
                errs.append(f"{path}.{key}: 缺少必填字段")
        if schema.get("additionalProperties") is False:
            for key in args:
                if key not in props:
                    errs.append(f"{path}.{key}: 未声明的字段（schema 禁止额外属性）")
        for key, sub in props.items():
            if key in args and isinstance(sub, dict):
                errs.extend(_validate_subset(sub, args[key], f"{path}.{key}"))

    if isinstance(args, list) and isinstance(schema.get("items"), dict):
        for i, item in enumerate(args):
            errs.extend(_validate_subset(schema["items"], item, f"{path}[{i}]"))

    return errs


def now_ms() -> float:
    return time.time() * 1000.0


def args_hash(args: dict) -> str:
    import hashlib
    return hashlib.sha256(
        json.dumps(args, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()[:16]


def dump(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2, default=str)


# ══════════════════════════════════════════════════════════════
# EXTENSIONS（v2：Memory Plane / Knowledge Plane / Governance Plane）
# 只追加，不动上面任何签名。
# ══════════════════════════════════════════════════════════════
class MemoryKind(str, Enum):
    """三层长期记忆。Procedural 不在这里——它的内容归 Skill 库，见架构文档 §3。"""
    PROFILE = "profile"      # 永久，免疫衰减，最高注入优先级
    SEMANTIC = "semantic"    # 长期演进，版本覆写
    EPISODIC = "episodic"    # 中短期，TTL 淘汰


@dataclass(frozen=True)
class Subject:
    """记忆的归属主体。INV-12：无主记忆拒绝入库。"""
    tenant: str
    user_id: str = ""
    team: str = ""
    project: str = ""

    def key(self) -> str:
        return f"{self.tenant}|{self.team}|{self.user_id}|{self.project}"

    def to_dict(self) -> dict:
        return {"tenant": self.tenant, "user_id": self.user_id,
                "team": self.team, "project": self.project}

    def shares_scope_with(self, other: "Subject") -> bool:
        """同租户才可能互相可见（多租户强隔离的底线）。"""
        return bool(self.tenant) and self.tenant == other.tenant


@dataclass
class MemoryRecord:
    """一条长期记忆。不是日志，是「删除它会改变 Agent 未来行为」的那种东西。"""
    id: str
    kind: MemoryKind
    text: str
    subject: Subject
    importance: float = 0.5          # 0..1，重要度打分
    confidence: float = 0.5          # 0..1，置信度（反思可提升）
    created_at: float = 0.0          # epoch ms
    last_hit_at: float = 0.0         # 最近一次被召回激活
    hits: int = 0                    # 被召回次数，衰减的"续命"依据
    version: int = 1
    superseded_by: str | None = None  # 被新事实覆写时指向新记录
    ttl_days: float | None = None     # None = 永久（仅 Profile 合法）
    source: str = ""                  # 溯源：哪个 turn / 哪个工具回包
    #: 事实归一化键：**由 Extractor 决定「什么算同一事实」**（Agent 的语义判断）。
    #: 为空时 Runtime 退化为 (kind, text) 哈希 —— 只能抓完全相同的文本。
    fact_key: str = ""

    def is_alive(self) -> bool:
        return self.superseded_by is None

    def to_dict(self) -> dict:
        return {"id": self.id, "kind": self.kind.value, "text": self.text,
                "subject": self.subject.to_dict(), "importance": self.importance,
                "confidence": self.confidence, "created_at": self.created_at,
                "last_hit_at": self.last_hit_at, "hits": self.hits,
                "version": self.version, "superseded_by": self.superseded_by,
                "ttl_days": self.ttl_days, "source": self.source,
                "fact_key": self.fact_key}


@dataclass(frozen=True)
class Chunk:
    """检索命中的统一形状。Memory 和 Knowledge 都返回它，所以 Builder 只需认一种。"""
    doc_id: str
    text: str
    score: float = 0.0
    span: str = ""
    version: str = ""
    source: str = "memory"     # memory | knowledge
    kind: str = ""             # memory 时填 profile/semantic/episodic


@dataclass(frozen=True)
class GuardVerdict:
    """护栏的判定结果。allowed=False 表示必须阻断。"""
    allowed: bool
    reason: str = ""
    payload: Any = None        # 处理后的文本（包裹/脱敏后）
    hits: tuple[str, ...] = ()  # 命中的规则名

    @staticmethod
    def ok(payload: Any = None, hits: tuple[str, ...] = ()) -> "GuardVerdict":
        return GuardVerdict(allowed=True, payload=payload, hits=hits)

    @staticmethod
    def block(reason: str, payload: Any = None, hits: tuple[str, ...] = ()) -> "GuardVerdict":
        return GuardVerdict(allowed=False, reason=reason, payload=payload, hits=hits)


@dataclass(frozen=True)
class RetentionRule:
    """保留矩阵的一行。INV-13：每一次淘汰都必须能落到某一条规则上。"""
    kind: MemoryKind
    ttl_days: float | None            # None = 永不按时间淘汰
    min_importance: float = 0.0       # 低于此重要度且到期 → 淘汰
    action: str = "archive"           # archive | delete
    reason: str = ""


#: 企业级确定性保留矩阵（替代概率衰减公式）
#: 阈值必须**高于**抽取阶段的默认重要度，否则规则永远不会触发 ——
#: 「有规则但规则不生效」比没有规则更危险，因为它让人误以为有代谢。
#: 抽取默认：Profile 0.9 / Semantic 0.6 / Episodic 0.4。
RETENTION_MATRIX: tuple[RetentionRule, ...] = (
    RetentionRule(MemoryKind.PROFILE, None, 0.0, "keep", "永久保留，仅允许主体显式删除"),
    RetentionRule(MemoryKind.SEMANTIC, 180.0, 0.70, "archive",
                  "180 天未激活且未达高重要度 → 归档冷存（可召回留痕，不再常态注入）"),
    RetentionRule(MemoryKind.EPISODIC, 60.0, 0.50, "delete",
                  "超 60 天 TTL 且未达高重要度 → 物理删除"),
)


# ══════════════════════════════════════════════════════════════
# EXTENSIONS v3：Workflow Plane（多 Agent 协同）
# ══════════════════════════════════════════════════════════════
class MessageType(str, Enum):
    """Agent 间信元类型。**严禁自然语言群聊** —— 契约先于礼貌。"""
    REQUEST = "REQUEST"
    RESPONSE = "RESPONSE"
    EVENT = "EVENT"
    ERROR = "ERROR"
    CANCEL = "CANCEL"


class Partition(str, Enum):
    """黑板三区：私有探索不污染全局。"""
    GLOBAL = "global"      # 全局规范，只读为主
    WORKING = "working"    # 协同工作区，CAS 管控
    PRIVATE = "private"    # Agent 私有暂存，别人看不见


@dataclass(frozen=True)
class AgentEnvelope:
    """强类型信元。自然语言是 Agent 与人的界面，结构化协议才是 Agent 之间的底座。"""
    trace_id: str
    message_type: MessageType
    sender: str
    recipient: str
    payload: dict = field(default_factory=dict)
    task_id: str = ""
    correlation_id: str = ""
    seq: int = 0
    created_at: float = 0.0

    def to_dict(self) -> dict:
        return {"trace_id": self.trace_id, "message_type": self.message_type.value,
                "sender": self.sender, "recipient": self.recipient,
                "payload": self.payload, "task_id": self.task_id,
                "correlation_id": self.correlation_id, "seq": self.seq,
                "created_at": self.created_at}


@dataclass
class BlackboardEntry:
    """黑板上的一个条目。**每一次写都必须带期望版本号**（CAS）。"""
    key: str
    value: Any
    partition: Partition = Partition.WORKING
    version: int = 1
    owner: str = ""            # 最后写入者
    updated_at: float = 0.0        # epoch ms
    writers: tuple[str, ...] = ()  # 写入历史（审计）

    def to_dict(self) -> dict:
        return {"key": self.key, "value": self.value, "partition": self.partition.value,
                "version": self.version, "owner": self.owner,
                "updated_at": self.updated_at, "writers": list(self.writers)}


@dataclass(frozen=True)
class CASResult:
    """乐观并发控制结果。冲突不是错误，是「你要基于新版本重新合并」的信号。"""
    ok: bool
    version: int = 0
    reason: str = ""
    current_version: int = 0

    @staticmethod
    def success(version: int) -> "CASResult":
        return CASResult(ok=True, version=version)

    @staticmethod
    def conflict(expected: int, current: int) -> "CASResult":
        return CASResult(ok=False, current_version=current,
                         reason=f"CAS conflict: expected v{expected}, current v{current}")


@dataclass
class SagaStep:
    """补偿事务的一步。forward 失败 → 逆序执行已完成步骤的 compensate。"""
    name: str
    action: Callable[[], Any]
    compensate: Callable[[], Any] | None = None
    done: bool = False
    result: Any = None
    error: str = ""


@dataclass(frozen=True)
class SubagentSpec:
    """子 Agent 的派发规格。权限只能比父更窄（INV-18）。"""
    name: str
    goal: str
    allowed_tools: tuple[str, ...] = ()   # 空 = 只允许 read_only
    max_steps: int = 4
    token_budget: int = 4000


@dataclass(frozen=True)
class SubagentResult:
    name: str
    ok: bool
    summary: str = ""
    artifacts: tuple[dict, ...] = ()
    steps: int = 0
    term_reason: str = ""
    error: str = ""
    #: 子 Agent 卡在「需要人工批准」时带出的**结构化请求**（C3a）。
    #:
    #: 为什么不能只是 `ok=False`：那和"子 Agent 跑挂了"无法区分，
    #: 父级只能整体重跑 —— 而重跑会重复执行已经做完的只读取证（白花钱），
    #: 更糟的是人不知道该批什么，只能去翻日志。
    #:
    #: 形状：`{"tool": str, "args_hash": str, "reason": str, "step": int}`
    needs_approval: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        out = {"name": self.name, "ok": self.ok, "summary": self.summary,
               "artifacts": list(self.artifacts), "steps": self.steps,
               "term_reason": self.term_reason, "error": self.error}
        if self.needs_approval:
            out["needs_approval"] = dict(self.needs_approval)
        return out


class ContextStore(Protocol):
    """Context 持久化 SPI —— **ADR-7 的物理兑现**。

    光能 `serialize()` 不算持久化：进程一死，内存里的字符串一起没。
    「挂起等人工审批 4 小时」是常态场景，不是边缘情况，所以落盘是必需能力，
    不是优化项。

    实现约定：
      * `save` 必须是**原子**的（临时文件 + rename），否则崩溃会留下半个 Context；
      * `load` 必须校验 `schema_version`，不认识的版本要抛错而不是猜；
      * 同一 `task_id` 后写覆盖先写（Context 是状态，不是流水账）。
    """
    def save(self, ctx_id: str, payload: str) -> None: ...
    def load(self, ctx_id: str) -> str | None: ...
    def delete(self, ctx_id: str) -> bool: ...
    def list_pending(self) -> list[str]: ...


@dataclass(frozen=True)
class PolicyPack:
    """治理规则包：**默认是本 Runtime 的协议事实，可被企业覆盖扩展**。

    `sk-` / `AKIA` / `ghp_` 这些不是业务判断，是厂商公开的凭据格式，
    所以默认清单留在 Runtime；但企业内部 token（xoxb-、私有 JWT）必须能加进来。
    这就是「留默认 + 可覆盖」，不是二选一。
    """
    name: str = "default"
    credential_patterns: tuple[tuple[str, str], ...] = ()
    jailbreak_patterns: tuple[tuple[str, str], ...] = ()
    exfil_host_allowlist: tuple[str, ...] = ()
    notes: str = ""

    def extend(self, **kw: Any) -> "PolicyPack":
        """返回一个合并后的新包（不就地修改，保持不可变）。"""
        data = {
            "name": kw.get("name", self.name),
            "credential_patterns": tuple(self.credential_patterns) + tuple(kw.get("credential_patterns", ())),
            "jailbreak_patterns": tuple(self.jailbreak_patterns) + tuple(kw.get("jailbreak_patterns", ())),
            "exfil_host_allowlist": tuple(self.exfil_host_allowlist) + tuple(kw.get("exfil_host_allowlist", ())),
            "notes": kw.get("notes", self.notes),
        }
        return PolicyPack(**data)


# ══════════════════════════════════════════════════════════════
# EXTENSIONS v4：Gateway / Router / Skill / MCP Resources / DAG / Summary
# ══════════════════════════════════════════════════════════════
@dataclass(frozen=True)
class AgentSpec:
    """一个可被 Router 选中的 Agent。**描述属于 Agent 层，选择逻辑属于 Runtime。**"""
    name: str
    description: str = ""
    tags: tuple[str, ...] = ()
    required_scopes: tuple[str, ...] = ()
    tools: tuple[str, ...] = ()
    skills: tuple[str, ...] = ()
    priority: int = 0


@dataclass(frozen=True)
class RouteDecision:
    """Router 的判定结果。confidence 低于阈值时必须回澄清，不许硬选。"""
    agent: str
    confidence: float
    reason: str = ""
    candidates: tuple[tuple[str, float], ...] = ()

    def to_dict(self) -> dict:
        return {"agent": self.agent, "confidence": round(self.confidence, 4),
                "reason": self.reason,
                "candidates": [[n, round(s, 4)] for n, s in self.candidates]}


@dataclass(frozen=True)
class SkillSpec:
    """技能 = 含 SOP 的复合经验包。

    **索引常驻、规程按需装载**：`index_text` 进 Prompt，`body` 只在命中后才装。
    这就是「索引常驻保持敏捷，规程按需装载控制预算」的物理形态。
    """
    name: str
    title: str = ""
    description: str = ""
    trigger: tuple[str, ...] = ()        # 触发词/场景
    steps: tuple[str, ...] = ()          # SOP 步骤
    pitfalls: tuple[str, ...] = ()       # 避坑指南
    verification: tuple[str, ...] = ()   # 验收标准
    tools: tuple[str, ...] = ()          # 引用的工具
    version: str = "1"
    source: str = ""                     # 来源：手工 / memory-consolidate

    @property
    def index_text(self) -> str:
        """常驻索引：一行，短。"""
        trig = "/".join(self.trigger) or "-"
        return f"{self.name} [{trig}]: {self.description or self.title}"

    def body_text(self) -> str:
        """规程全文：只在命中后装载。"""
        parts = [f"# {self.title or self.name}"]
        if self.steps:
            parts.append("## 步骤\n" + "\n".join(f"{i}. {x}" for i, x in enumerate(self.steps, 1)))
        if self.pitfalls:
            parts.append("## 避坑\n" + "\n".join(f"- {x}" for x in self.pitfalls))
        if self.verification:
            parts.append("## 验收\n" + "\n".join(f"- {x}" for x in self.verification))
        if self.tools:
            parts.append("## 工具\n" + ", ".join(self.tools))
        return "\n\n".join(parts)


@dataclass(frozen=True)
class SkillMatch:
    name: str
    score: float
    provider: str = ""


@dataclass(frozen=True)
class ResourceRef:
    """MCP Resources：**只读数据，URI 寻址**。

    这是 v1 的 `read_only` 的标准载体 —— 有 URI 就能寻址、缓存、授权，
    不需要每个工具自己约定参数形状。
    """
    uri: str
    name: str = ""
    mime: str = "text/plain"
    description: str = ""
    max_bytes: int = 262144

    def scheme(self) -> str:
        return self.uri.split("://", 1)[0] if "://" in self.uri else ""


@dataclass(frozen=True)
class NamedPrompt:
    """MCP Prompts：工程化模板，版本化、可注入参数。"""
    name: str
    template: str
    arguments: tuple[str, ...] = ()
    description: str = ""

    def render(self, **kw: Any) -> str:
        out = self.template
        for k in self.arguments:
            out = out.replace("{" + k + "}", str(kw.get(k, "")))
        return out


@dataclass(frozen=True)
class DAGNode:
    name: str
    agent: str = ""
    depends_on: tuple[str, ...] = ()
    goal: str = ""


@dataclass(frozen=True)
class DAGSpec:
    """静态 DAG：拓扑在代码里固化，保证确定性流水线。"""
    name: str
    nodes: tuple[DAGNode, ...] = ()

    def topo_order(self) -> list[str]:
        raise NotImplementedError("由 plane/dag.py 实现（这里只放形状）")


# ══════════════════════════════════════════════════════════════
# EXTENSIONS v5：Embedding / Sandbox 后端 / 指标
# ══════════════════════════════════════════════════════════════
class EmbeddingProvider(Protocol):
    """语义向量 SPI。

    Memory / Knowledge 的相似度目前是字符 bigram（同义改写召回不了）。
    接真 embedding 后：`"连接池耗尽"` 与 `"数据库连接数打满"` 的余弦是 0.81，
    而 bigram 几乎为 0 —— 这个差距直接决定召回质量。
    """
    name: str
    dim: int

    def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


class VectorBackend(Protocol):
    """向量库 SPI。**qdrant / milvus / 内存三选一，运行时可配**。

    为什么要有这层：本地开发用内存（零依赖）、单机部署用 qdrant（本地模式不用起服务）、
    生产集群用 milvus（水平扩展）。**换库不该改业务代码**，只该改配置。

    约定：
      * 所有实现都必须支持 payload filter（跨租户隔离在库内完成，不能取回来再过滤）
      * `put` 的 payload 必须把 `tenant/user_id/team/project` **摊平到顶层**
        （Qdrant 与 Milvus 的 filter 都只作用于顶层字段）
    """

    name: str

    def upsert(self, point_id: str, vector: Sequence[float], payload: dict) -> None: ...
    def delete(self, point_id: str) -> bool: ...
    def search(self, vector: Sequence[float], flt: dict | None,
               limit: int) -> list[tuple[str, float, dict]]: ...
    def scroll(self, flt: dict | None, limit: int = 256) -> list[tuple[str, dict]]: ...
    def count(self) -> int: ...
    def close(self) -> None: ...


@dataclass(frozen=True)
class VectorBackendInfo:
    """后端自述：**"配了却没生效"是本项目反复踩的坑**，所以必须能自报状态。"""
    kind: str = "memory"
    available: bool = False
    shared: bool = False          # 是否跨进程/跨副本
    ann: bool = False             # 是否有 ANN 索引（而非暴力扫描）
    dim: int = 0
    location: str = ""
    reason: str = ""              # 不可用时的原因（可直接行动）


class DocumentSplitter(Protocol):
    """文档切分 SPI。LlamaIndex 是可选实现之一，不是唯一选择。

    默认实现是本项目自己的按标题/空行切分（零依赖、确定性）。
    装了 `llama-index-core` 可切到 `SentenceSplitter`（更聪明的句子边界、
    重叠窗口、token 感知），但**不是必须** —— 分块策略该是配置项。
    """
    name: str

    def split(self, text: str, *, max_chars: int = 600) -> list[str]: ...


class SandboxBackend(Protocol):
    """执行隔离后端 SPI。**隔离强度是能力，不是实现细节。**

    subprocess 只能挡手滑，挡不住逃逸；企业环境要 docker / nsjail / gVisor。
    后端必须自报可用性，上层据此降级 —— 不能"配置了却静默不生效"。
    """
    name: str
    strength: str        # "none" | "process" | "container" | "kernel"

    def available(self) -> bool: ...
    def run(self, argv: Sequence[str], *, cwd: str | None = None,
            timeout_ms: int = 8000, env: dict | None = None) -> dict: ...


@dataclass(frozen=True)
class SandboxPolicy:
    """沙箱策略：允许的命令、资源上限、网络是否放行。"""
    allowed_commands: tuple[str, ...] = ("echo", "ls", "cat", "python3", "git", "grep")
    max_output_bytes: int = 8192
    timeout_ms: int = 8000
    max_cpu_seconds: int = 10
    max_memory_mb: int = 512
    allow_network: bool = False
    require_backend: str = ""      # 非空时：拿不到该强度的后端就拒绝执行


# ══════════════════════════════════════════════════════════════
# EXTENSIONS v6：分布式一致性 / 可观测性
# ══════════════════════════════════════════════════════════════
class KV(Protocol):
    """原子键值 SPI。**每个方法都必须是原子的**，这是跨副本一致性的全部前提。

    为什么不用"读-改-写"拼出来：单进程里 `RLock` 够用，跨副本里那是竞态。
    所以接口只暴露服务端能原子完成的动作：CAS、INCR、SETNX。

    实现方：`InMemoryKV`（单进程测试）/ `RESPKV`（真 Redis，基于官方 `redis` 客户端）。
    """

    def get(self, key: str) -> str | None: ...
    def set(self, key: str, value: str, *, ttl_s: float | None = None) -> bool: ...
    def set_nx(self, key: str, value: str, *, ttl_s: float | None = None) -> bool: ...
    def compare_and_set(self, key: str, expected: str, value: str,
                        *, ttl_s: float | None = None) -> bool: ...
    def incr(self, key: str, delta: int = 1, *, ttl_s: float | None = None) -> int: ...
    def delete(self, key: str) -> bool: ...
    def expire(self, key: str, ttl_s: float) -> bool: ...


@dataclass(frozen=True)
class DistributedInfo:
    """分布式能力自述：**不能"配了却没生效"**。"""
    backend: str = "in-memory"
    atomic: bool = False      # CAS/INCR 是否跨进程原子
    shared: bool = False      # 是否跨副本可见
    note: str = ""


class Extractor(Protocol):
    """抽取策略 SPI —— **Agent/领域层的能力，不是 Runtime 的**。

    Runtime 判断「形状」：有哪几类记忆、代谢管道几步、什么时候淘汰。
    Agent 判断「内容」：这句话是偏好还是事件、算几分重要、「什么算同一事实」。

    换一个业务领域（法务 / 代码 / 客服），只需换一个 Extractor，
    Memory Plane 的管道、存储、保留矩阵全部不动。
    """
    def extract(self, turn: dict, subject: Subject) -> list[MemoryRecord]: ...


@dataclass(frozen=True)
class MemoryMetrics:
    """Memory 五维评估（架构文档 §4.2）。"""
    recall_precision: float = 0.0     # Recall Precision@K
    profile_consistency: float = 0.0  # 输出未违背硬偏好的比例
    token_overhead: float = 0.0       # 记忆注入占 Prompt 的比例
    recency_correctness: float = 0.0  # 未误用过期记忆的比例
    conflict_resolution: float = 0.0  # 多版本事实取对数

    def to_dict(self) -> dict:
        return {"recall_precision": round(self.recall_precision, 4),
                "profile_consistency": round(self.profile_consistency, 4),
                "token_overhead": round(self.token_overhead, 4),
                "recency_correctness": round(self.recency_correctness, 4),
                "conflict_resolution": round(self.conflict_resolution, 4)}



# ══════════════════════════════════════════════════════════════
# EXTENSIONS v7：Agent 层的三个扩展点（Runtime 只定契约，不实现）
# ══════════════════════════════════════════════════════════════
#
# 为什么要有这一节：
# Runtime 的职责是「提供 SPI，让上层照着实现」——但在此之前，Planner /
# Router / Skill 执行这三处只是**鸭子类型**：文档里写着"有 replan(...) 即可"
# 或某个具体类恰好有同名方法。上层要独立实现时，没有任何可编译的契约可依，
# 只能去读 Runtime 源码猜签名。**这不是 SPI，是隐式约定。**
#
# 这三者的共同点：**它们是 Agent 层的能力，Runtime 只负责调用与校验。**
# 所以契约里只写"Runtime 会怎么调你"，不写"你该做什么内容判断"。


@runtime_checkable
class ProgressSink(Protocol):
    """进度侧通道 —— **只播报，不改 Context**。

    为什么必须单独开一条通道，而不是往 Context 写 patch：

      1. `OWNER_TABLE` 规定每个字段有唯一 owner。子 Agent 对父 Context 的字段
         没有任何写权，"写进去"必被 `apply()` 拒绝（只留一条 warning）。
      2. 进度是**瞬时事件**，不是状态。进 `patch_log` 会污染轨迹与回放 ——
         回放时会看到一堆"当时播报过什么"，那不是执行事实。

    线程安全：`emit` 会被工具线程池的 N 个 worker 同时调用，实现方必须自己保证。
    推荐 `queue.Queue`（线程安全）；**满了就丢弃并计数**，绝不能阻塞调用方 ——
    播报是观测面，不能把主链路拖死（fail-open）。

    实现方：`core.observability.QueueProgressSink`。测试可用列表替代。
    """

    def emit(self, kind: str, data: "Mapping[str, Any]") -> None: ...


class PlannerProtocol(Protocol):
    """规划 SPI —— **Agent 层能力，Runtime 不实现也不默认提供**。

    Runtime 什么时候调它：Loop 检测到死锁（连续无进展）时，**给一次机会**换方案。
    这不是"每步都规划"——每步做什么由 LLM 在当轮自行决定（ReAct）；
    Planner 只在"沿用当前方案已经不收敛"时介入。

    契约要点（为什么是返回 `Patch` 而不是让 Planner 直接操作 Context）：

      * **Planner 输出数据，Runtime 决定怎么应用。** Planner 返回 Patch，
        Runtime 校验 owner 后 `ctx.apply()`；不合法就记 warning 并丢弃。
        如果 Planner 能直接写 Context，它就能绕开 OWNER_TABLE 与审计。
      * **可写字段以 `OWNER_TABLE` 为唯一权威**：`plan` 归 PLANNER，
        `observations` 归 LOOP（Planner 只读）。写后者会被拒且**只记 warning**，
        必须靠 OWNER_TABLE 自查，不能靠"我觉得该能写"。
      * **返回 None 表示"我也想不出新方案"**：Loop 据此判定 NO_PROGRESS，
        而不是被空规划拖进无限重试。
      * **抛异常不算致命**：Runtime 记 warnign + 写一条系统消息，继续跑。
        规划失败不该让整个回合失败（它本来就是在补救路径上）。
      * **`snapshot` 是只读快照**，不是 Context 句柄——Planner 改不到真实状态。

    实现方：`agents/` 下（业务内容），例如基于 LLM 的分解器或静态 DAG 选择器
    （`plane/dag.py` 提供了静态 DAG 的执行能力，可作为实现素材）。
    """

    def replan(self, snapshot: Mapping[str, Any], reason: str) -> "Patch | None":
        """返回一条要应用的 Patch，或 None。

        **只能写 `plan`**（以及 Planner 自己拥有的字段）。特别注意
        `observations` 的 owner 是 `Owner.LOOP` —— Planner 写它会被
        `ctx.apply()` 以 `OwnershipViolation` 拒绝，Loop 只记一条 warning，
        表现是"规划器跑了但什么也没生效"（典型静默故障）。
        以 `OWNER_TABLE` 为准，不要以调用方直觉为准。
        """
        ...


class RouterProtocol(Protocol):
    """路由 SPI —— 选哪个 Agent。

    Runtime 提供的是**机制**（`plane/router.py.AgentRouter` 是默认实现：
    goal 匹配 + 描述相似 + scope 过滤 + tag 命中 + priority，加权打分），
    Agent 层提供的是**数据**（`AgentSpec` 的 description / tags / priority）。

    上层什么时候要实现它：需要完全不同的选择策略时（比如按 embedding 相似度、
    按 A/B 实验分流、按租户白名单硬指定）。

    实现方**必须**遵守的语义（Runtime 与客户端都依赖它）：
      * 回澄清的约定是 `RouteDecision.agent == "__clarify__"`（哨兵值，不是空串）。
        **用哨兵而不是空串**：空串容易和"字段没填"混淆，而哨兵去查 Agent 表
        必然查不到 → 立刻炸，不存在"忘了处理然后拿它当 Agent 名"的可能。
      * 没有候选时同样返回 `__clarify__` 且 `confidence=0.0`，**不许抛**
        （抛了调用方只能吞异常，信息全丢）
      * `explain()` 返回**每个候选的得分拆解**（含四项分项与是否被权限剔除），
        线上排查"为什么选 A 不选 B"全靠它
    """

    def route(self, goal: str, principal: "Principal") -> "RouteDecision": ...
    def explain(self, goal: str, principal: "Principal") -> list[dict]: ...


class SkillHandlerProtocol(Protocol):
    """技能执行 SPI —— 业务级能力编排。

    `SkillSpec` 描述"这个技能是什么"（属于数据契约，已在 v4 冻结）；
    本协议描述"Runtime 会怎么执行它"。

    与 `ToolHandler` 的区别（这是最容易混的一处）：
      * **Tool** 是原子能力（一次 HTTP、一次 SQL），Runtime 管权限/审批/归一化
      * **Skill** 是多 Tool 编排（"发布一次变更"= 查状态 → 备份 → 执行 → 验证），
        Runtime 管配额/审计/深度限制，**不拆解步骤**（那是 Skill 自己的事）

    所以 handler 拿到的是 `ExecCtx`——它需要预算与截止时间来做多步调用，
    但**拿不到 Dispatcher 之外的写权限**（想调工具必须经由 ctx 暴露的入口）。
    """

    def __call__(self, args: Mapping[str, Any], ctx: "ExecCtx") -> Any: ...
