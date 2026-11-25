"""core/gateway.py — Gateway 接入层：外部请求进入系统的**唯一**那道门（W19）。

================================================================================
它在主线里的位置
================================================================================

    Gateway → Router → Planner → Loop ⇄ Context → LLM

Router 之后的所有东西都已实现，Gateway 之前什么都没有 —— 所以鉴权、租户解析、
配额、输入护栏入口、审计、流式出口目前散落在 demo 里手写。本模块把它们收敛成
一个对象：**任何外部请求都必须先穿过这里**，没有旁路。

    client ──▶ Gateway.handle(request) ──▶ TurnResult
                  │
                  ├─ ① 请求形状        非法 → bad_request
                  ├─ ② 身份 / 租户解析  缺失 → unauthorized（fail-closed）
                  ├─ ③ RBAC 准入       无 runtime:run → forbidden
                  ├─ ④ 配额            超限 → rate_limited（fail-closed）
                  ├─ ⑤ 输入护栏        注入 → input_blocked
                  ├─ ⑥ 构造 ExecCtx    deadline_ms → 绝对 epoch ms
                  ├─ ⑦ 续跑判定        ctx_id → store 加载 / 否则新建 Context
                  ├─ ⑧ 跑 Loop        异常兜住 → loop_error
                  ├─ ⑨ 输出护栏        命中 → 脱敏后文本 / 护栏故障 → 不出文本
                  ├─ ⑩ 审计            只记长度，绝不记 goal 全文
                  └─ ⑪ 落盘            phase==WAITING_USER → save_checkpoint

================================================================================
为什么这 11 步的**顺序**就是安全语义（调换任何一步都会开一个洞）
================================================================================

①→② 先校验形状再解析身份。
    形状校验是纯结构判断、零副作用、最便宜。若先解析身份，畸形请求（goal 不是
    字符串）会把 KeyError 带进身份解析逻辑里 —— 那时"解析失败"和"身份非法"混成
    一个异常，而异常分支最容易被写成放行。先形状后身份，能保证 ② 只在结构确定
    的数据上工作。

②→③ 先确定"你是谁/哪个租户"，再判"你能不能进来"。
    RBAC 的输入是 `Principal`，`principal.tenant` 为空时 RBAC 内部会拒绝；但如果
    先跑 RBAC 再解析租户，就会出现"用空租户主体做判定、再用未知租户去计费"的
    错配。租户是后面 ④ 计费的分区键，它必须是②的产物，不能是③的副产物。

③→④ 鉴权在计费之前。
    顺序反过来，未准入的调用方（伪造 scope / 无 scope）就能消耗目标租户的配额窗口
    —— 等于给了外部一个"用无效身份把别人配额打满"的免费 DoS 通道。配额窗口是
    按租户的稀缺资源，只有已准入的主体能碰。

④→⑤ 配额在输入护栏之前。
    护栏要做正则扫描（并发高时是实打实的 CPU），是比形状校验贵得多的操作。配额
    在它前面，越权/超限的洪水连正则都跑不到 —— 限流的价值就在于"在昂贵操作之前
    挡住"。代价是：被 ⑤ 拒绝的请求也占了一个配额格，这是**刻意**的：拿越狱样本
    反复试探的行为本身就该被限速，而不是免费试错。

⑤→⑥→⑦ 护栏在构造 ExecCtx / Context **之前**。
    这一步是"不落脏状态"的关键。被拒绝的 goal 绝不能变成 Context 里的一条
    history、也绝不能进审计的持久化轨迹（patch log = 轨迹 = WAL）。若先建
    Context 再扫描，注入内容已经在 WAL 里留下了永久的、可被 Replay 重放出来的
    痕迹 —— 事后无法擦除。**护栏必须在任何持久化之前。**

⑥→⑦ deadline 必须先算成绝对值，再加载历史 Context。
    `ExecCtx.deadline` 是 epoch ms（0=不限），而请求给的是相对毫秒。换算必须在
    载入旧 Context 之前完成：加载可能读盘（慢、可能失败），如果换算放在加载之后，
    读盘耗时会被算进"已用时间"之外，导致同一个 deadline_ms 在冷/热缓存下实际
    宽限不同。安全上：deadline 是硬截止，它的基准点必须是**收到请求的那一刻**。

⑦→⑧ 续跑判定在跑 Loop 之前，且必须是"要么完整恢复、要么拒绝"。
    载入失败时绝不退化成"新建一个空 Context 接着跑"：那会让一个原本带着预算/审批
    记录/待确认动作的任务，凭空丢掉历史重跑一遍副作用。丢状态 = 重复执行 =
    重复扣费与重复写外部世界。所以 ⑦ 的任何失败都是拒绝（resume_failed）。

⑧→⑨ 输出护栏必须在 Loop 之后、任何"文本离开系统"的动作之前。
    Loop 的产物是唯一可能夹带模型从上下文里抄出来的密钥/外链的东西。⑨ 之前它
    只存在于内存；⑨ 之后它才会进 ⑩ 审计、进 ⑪ 落盘、进流式事件、回给客户端。
    所以 ⑨ 一旦自身故障，结果是 **ok=False 且 text 为空** —— 宁可这次不回答，
    也不能把可能带着 `sk-...` 的原文发出去。这条没有降级分支。

⑨→⑩ 审计记的是**脱敏后**的终态。
    先审计后护栏，审计里就会留下未脱敏的 phase/steps 上下文；更要紧的是审计是
    不可撤销的（外部 sink 可能已经写到别处），所以它只能记已经过 ⑨ 的事实。

⑩→⑪ 落盘与审计谁先"生效"：检查点先算、审计后写。
    任务清单把审计列在落盘之前，但**审计记录必须在知道落盘结果之后**才写 ——
    审计是 append-only 的（外部 sink 可能已经写到别处，撤不回来），如果它先记下
    `ok=true`、之后才发现检查点没写成功，那份"这个挂起任务可续跑"的记录就永久
    错了：运维照着它去找 ctx_id，会发现任务根本不存在。所以代码里的实际顺序是
    `⑨ 护栏 → ⑪ 检查点（算结果）→ ⑩ 审计（记终态）→ 返回`：审计内容因此永远是
    *最终*的 ok / phase / error，一次回合只写一条。

⑪ 只在 `phase == WAITING_USER` 时触发：DONE / FAILED 不写检查点（DONE 不该留在
    pending 里；gateway 不替调用方决定失败是否可续跑，那是 store 层
    `resume_from_store` 的语义）。

================================================================================
依赖纪律
================================================================================
- 只读 `contracts.py`；不 import `core/loop.py`（Loop 由 `loop_factory` 注入，
  Gateway 不自己 new —— 一个能被 new 出来的 Loop 就是一个能被绕过的鉴权）。
- `plane/*` 与 `core/store.py`、`core/context.py` 正被其他 worker 并行修改，
  所以对它们的接口一律用 `getattr` 容错 + 懒加载：接口对不上时降级成"拒绝"或
  "少了那一道闸"，绝不 import 期崩掉整个接入层。
"""
from __future__ import annotations

import copy
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator, Mapping, Sequence

from contracts import Budget, ExecCtx, Phase, Principal, TermReason, now_ms

__all__ = [
    "Gateway",
    "TenantContext",
    "TurnResult",
    "RUN_SCOPE",
    "TURN_QUOTA_KIND",
    "AUDIT_FIELDS",
]

# ── 常量 ──────────────────────────────────────────────────────
#: 进入运行时所需的动作。RBAC 只建模 tool:/memory:/knowledge: 三族，runtime 是
#: Gateway 自己这一族（见 `_rbac_runtime_verdict` 的兼容说明）。
RUN_SCOPE = "runtime:run"
#: 配额种类：**不能**复用 tool_call / llm_call —— 那是 Loop 记账用的窗口，
#: Gateway 计一次"回合"就把它们污染了，会让真实工具调用的限流提前击穿。
TURN_QUOTA_KIND = "runtime_turn"

#: 审计记录的字段（顺序即契约，sink 与内存列表用同一份）
AUDIT_FIELDS = ("audit_id", "ts", "tenant", "user_id", "goal_len", "ok",
                "phase", "steps", "latency_ms", "error")

#: goal 上限（字符）。超长输入本身就是一种廉价 DoS，形状校验里挡掉。
MAX_GOAL_CHARS = 100_000
#: 配额窗口（秒）
WINDOW_SECONDS = 60.0
#: 预算里允许客户端声明的字段。拼错字段（`token_totl`）必须报错而不是被静默忽略：
#: 被忽略的预算字段 = 客户端以为设了上限、实际没设 = 预算击穿。
BUDGET_KEYS = ("token_total", "cost_total_usd", "tool_calls_total", "max_steps")

_REQUIRED_PRINCIPAL_KEYS = ("user_id", "tenant")


# ══════════════════════════════════════════════════════════════
# 一、对外数据形状
# ══════════════════════════════════════════════════════════════
@dataclass(frozen=True)
class TenantContext:
    """租户解析结果：`loop_factory` 拿到它去选"这个租户该怎么跑"。

    刻意不放进 `contracts.py`（契约冻结）：它是**接入层**的产物，
    下游只需要它携带的身份三元组 + 本次回合的定位串，不需要知道请求长什么样。
    """
    tenant: str
    user_id: str
    principal: Principal
    task_id: str
    turn_id: str
    trace_id: str
    ctx_id: str = ""
    resumed: bool = False
    goal_len: int = 0

    def to_dict(self) -> dict:
        return {"tenant": self.tenant, "user_id": self.user_id,
                "task_id": self.task_id, "turn_id": self.turn_id,
                "trace_id": self.trace_id, "ctx_id": self.ctx_id,
                "resumed": self.resumed, "goal_len": self.goal_len,
                "roles": list(self.principal.roles),
                "scopes": sorted(self.principal.scopes)}


@dataclass(frozen=True)
class TurnResult:
    """一个回合的出口。所有字段都是可 JSON 序列化的基本类型 + 一个结构化 dict。"""
    ok: bool
    text: str = ""
    phase: str = ""
    term_reason: str = ""
    task_id: str = ""
    steps: int = 0
    partial: bool = False
    error: str = ""
    audit_id: str = ""
    latency_ms: float = 0.0
    vertex: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"ok": self.ok, "text": self.text, "phase": self.phase,
                "term_reason": self.term_reason, "task_id": self.task_id,
                "steps": self.steps, "partial": self.partial,
                "error": self.error, "audit_id": self.audit_id,
                "latency_ms": self.latency_ms,
                "vertex": copy.deepcopy(self.vertex)}


# ══════════════════════════════════════════════════════════════
# 二、内部控制流：拒绝 = 异常（这样"忘了 return"就等于放行是做不到的）
# ══════════════════════════════════════════════════════════════
class _Reject(Exception):
    """准入阶段的拒绝。`code` 进错误串前缀，`detail` 给人看。

    用异常而不是返回值，是刻意的：`return` 是容易被漏掉的（某个分支忘了写就
    顺着往下跑了），`raise` 漏不掉。这和 Governance 用"返回字符串=拒绝"是同一个
    方向的两种写法 —— 这里更极端的理由是：①~⑤ 里任何一步只要有一个 if 没写全，
    请求就进 Loop 了。
    """

    def __init__(self, code: str, detail: str = "",
                 vertex: Mapping[str, Any] | None = None) -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail
        self.vertex = dict(vertex or {})

    def error(self) -> str:
        return f"{self.code}: {self.detail}" if self.detail else self.code


@dataclass
class _Prepared:
    """通过 ①~⑦ 之后、进 ⑧ 之前的全部上下文。"""
    goal: str
    principal: Principal
    tenant_ctx: TenantContext
    e: ExecCtx
    ctx: Any
    ctx_id: str
    quota: dict
    resumed: bool
    suspended: bool = False


@dataclass
class _Turn:
    """一个回合的可变装配区，最后统一经 `_finalize` 出去（审计只在这里写一次）。"""
    goal_len: int
    tenant: str = ""
    user_id: str = ""
    task_id: str = ""
    ok: bool = True
    text: str = ""
    phase: str = ""
    term_reason: str = ""
    steps: int = 0
    partial: bool = False
    error: str = ""
    vertex: dict = field(default_factory=dict)


# ══════════════════════════════════════════════════════════════
# 三、纯函数工具
# ══════════════════════════════════════════════════════════════
def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _scope_allows(scopes: set[str], required: str) -> bool:
    """scope 覆盖关系：`*` > `runtime:*` / `runtime.*` > 精确 `runtime:run`。"""
    if not required:
        return True
    if "*" in scopes or required in scopes:
        return True
    if ":" in required:
        family = required.split(":", 1)[0]
        return f"{family}:*" in scopes or f"{family}.*" in scopes
    return False


def _code_only(error: str) -> str:
    """审计里的 error 只留错误码。

    理由：完整 reason 里可能夹带 goal 的片段（输入护栏的拒绝理由会引用命中的
    原文片段），而审计是 append-only 的持久轨迹 —— **绝不记录 goal 全文**这条
    规则必须连片段一起守住。细节留在 `TurnResult.error` 里给调用方。
    """
    if not error:
        return ""
    return str(error).split(":", 1)[0].strip()


def _phase_text(phase: Any) -> str:
    if isinstance(phase, Phase):
        return phase.value
    return str(phase) if phase is not None else ""


def _term_text(term: Any) -> str:
    if isinstance(term, TermReason):
        return term.value
    return str(term) if term is not None else ""


def _latency_ms(since: float) -> float:
    """耗时（毫秒）。保证严格 > 0：0 会被下游当成"没测到"。"""
    return max(round((time.perf_counter() - since) * 1000.0, 6), 1e-6)


# ══════════════════════════════════════════════════════════════
# 四、Gateway
# ══════════════════════════════════════════════════════════════
class DispatcherCapabilityError(RuntimeError):
    """Dispatcher 缺少必需保障。**启动期**抛出，不是运行期。"""

    def __init__(self, message: str, *, detail: str = "",
                 vertex: Mapping[str, Any] | None = None) -> None:
        super().__init__(f"{message}；{detail}" if detail else message)
        self.detail = detail
        self.vertex = dict(vertex or {})


class Gateway:
    """外部请求 → 运行时 的唯一入口。

    ```python
    gw = Gateway(loop_factory, governance=gov, store=store)
    res = gw.handle({"goal": "总结这份日志", "principal": {...}})
    for ev in gw.handle_stream(req):
        print(json.dumps(ev))
    ```

    `loop_factory(goal, tenant_ctx) -> Loop` 由调用方提供：Gateway **不**自己
    new Loop。理由不只是"依赖可注入" —— Loop 一旦能被 Gateway 之外的代码直接
    构造并调用，那一层鉴权/配额/审计就是可绕过的装饰，而不是闸门。

    扩展关键字参数（都有默认值，不改变上面那份签名）：
        audit_sink    Callable[[dict], None]，审计旁路（抛异常必须被吞）
        allow_hosts   输出白名单 host（传给 OutputGuard.scan）
        clock         秒级时钟（配额窗口用），测试推假时钟
        clock_ms      毫秒时钟（审计 ts / deadline 基准），默认 contracts.now_ms
        context_factory  `(initial, budget) -> Context`，默认 core.context.ContextHandle
        checkpoint_loader `(store, ctx_id, budget) -> Context`，默认 core.store.load_checkpoint
        context_seed     `(initial, goal, principal, e) -> dict`，新建 Context 前改造种子。
                         会话承接（core/session_gateway.py）用它预填历史骨架 —— 这是
                         **公开注入口**，不再依赖包装私有属性。抛异常 → fail-closed 拒绝。
    """

    def __init__(
        self,
        loop_factory: Callable[[str, TenantContext], Any],
        *,
        governance: Any = None,
        store: Any = None,
        rate_limit_per_minute: int = 60,
        trace_factory: Callable[..., Any] | None = None,
        audit_sink: Callable[[dict], None] | None = None,
        allow_hosts: Sequence[str] = (),
        clock: Callable[[], float] | None = None,
        clock_ms: Callable[[], float] | None = None,
        context_factory: Callable[..., Any] | None = None,
        checkpoint_loader: Callable[..., Any] | None = None,
        context_seed: Callable[..., Any] | None = None,
        on_context: Callable[..., Any] | None = None,
        audit_log_maxlen: int = 10_000,
        on_turn_start: Callable[..., Any] | None = None,
        on_turn_end: Callable[..., Any] | None = None,
        required_dispatch: "frozenset[str] | None" = None,
    ) -> None:
        if not callable(loop_factory):
            raise TypeError("loop_factory 必须可调用：Callable[[str, TenantContext], Loop]")
        limit = rate_limit_per_minute
        if not _is_int(limit) or limit < 0:
            raise TypeError(f"rate_limit_per_minute 必须是非负整数，收到 {limit!r}")
        self.loop_factory = loop_factory
        self.governance = governance
        self.store = store
        self.rate_limit_per_minute = int(limit)
        self.trace_factory = trace_factory
        self.audit_sink = audit_sink
        self.allow_hosts = tuple(allow_hosts or ())
        #: 期望 Dispatcher 提供的保障。`None` = 不校验（保持历史行为）。
        #:
        #: 设了就变成**启动期**的 fail-closed：`check_dispatch()` 时若实现的
        #: `capabilities()` 少了任何一项，直接抛 `DispatcherCapabilityError`，
        #: 而不是让审批/超时/预算在运行期静默消失。
        self.required_dispatch = (frozenset(required_dispatch)
                                  if required_dispatch is not None else None)
        self._clock = clock or time.monotonic
        self._clock_ms = clock_ms or now_ms
        self._context_factory = context_factory
        self._checkpoint_loader = checkpoint_loader
        #: 新建 Context 时的**正式注入口**：`seed(initial, goal, principal, e) -> dict`。
        #:
        #: 为什么需要它：会话承接（`core/session_gateway.py`）要往新 Context 里预填
        #: 历史骨架。早期靠包装 `_context_factory` 这个**私有属性**实现，Gateway 一旦
        #: 改结构（__slots__ / frozen）就会静默降级 —— 历史不再预填但没人知道。
        #: 现在给一个公开且语义明确的钩子：拿到 initial 字典，返回修改后的字典。
        #: 抛异常 → 按 internal_error 拒绝（fail-closed，不带着半截种子继续跑）。
        self._context_seed: Callable[..., Any] | None = context_seed
        #: Context 就绪、**执行之前**的回调：`on_context(ctx, e) -> None`。
        #:
        #: 用途：真增量流式（`core/streaming.py`）需要拿到正在跑的那个 Context，
        #: 才能尾随它的 patch log 把「第几步、调了什么工具」实时吐给客户端。
        #: 没有它，流式只能"跑完再发"（客户端等很久才见第一条有意义的事件）。
        #: 回调抛异常 → 忽略（观测面 fail-open，不能让流式需求拖垮主链路）。
        self._on_context: Callable[..., Any] | None = on_context
        #: 回合开始（Context 就绪、执行之前）：`on_turn_start(ctx, goal, principal) -> Any`。
        #: 用途：Memory 召回、技能装载、路由决策 —— 这些都要在**第一次调模型之前**完成，
        #: 否则模型看不到历史与技能索引。放在 `context_seed` 里不行：那时还没有 ctx。
        self._on_turn_start: Callable[..., Any] | None = on_turn_start
        #: 回合结束（执行之后，含失败）：`on_turn_end(ctx, result) -> Any`。
        #: 用途：记忆写回、历史压缩。**失败也要调** —— 失败回合同样是有信息量的事实
        #: （"这个方案行不通"本身值得记）。回调异常一律忽略（fail-open）。
        self._on_turn_end: Callable[..., Any] | None = on_turn_end
        self.window_seconds = WINDOW_SECONDS
        #: 接入层的进程内互斥（W25：并发加固，**不改语义**）。
        #:
        #: 保护三处**跨请求共享**的可变状态，理由各不相同：
        #:   1) `_windows`（配额窗口）：`读 used → 判超限 → append` 是一段
        #:      必须原子的三段式。少了锁，10 个线程同时看到 `used=24/25`
        #:      就会一起放行 —— 配额变成"上限 × 线程数"，限流形同虚设。
        #:   2) `audit_log`（审计列表）：list.append 本身是原子的，但
        #:      "条数 == 回合数"这个契约需要一次可见的写入，而不是赌 GIL。
        #:   3) `denied_total` / `denied_by_code`：`+=` 是读-改-写三步，
        #:      高并发下丢失计数。而拒绝计数是安全信号，丢一条就是丢证据。
        #:
        #: 用 RLock 而非 Lock：`_rejected()` → `_finalize_result()` → `_audit()`
        #: 存在同线程重入，且 `_charge()` 内部会再次走到受锁保护的代码路径。
        #: 这把锁**不**覆盖 Loop 执行与输出护栏（那是每个请求自己的活，
        #: 串行化它们等于把接入层变成一个单线程服务器）。
        self._lock = threading.RLock()
        #: 审计记录（内存列表；sink 是旁路，两者都写）
        #: 内存审计窗口。**必须有界**：无界列表在长跑服务里就是内存泄漏
        #: （实测 1000 回合涨 1000 条，永不回收）。
        #:
        #: 设计口径：内存里只留**最近 N 条**供排查，持久审计走 `audit_sink`
        #: 回调（外部存储）。用 deque(maxlen=N) 而不是 list：
        #: 超限自动丢最老的，`len()` / `[-1]` / 迭代语义不变。
        self.audit_log: Any = deque(maxlen=max(1, int(audit_log_maxlen)))
        #: tenant → 时间戳窗口。**按租户隔离**：一个租户打满不影响另一个。
        self._windows: dict[str, deque[float]] = {}
        #: 观测：被拒次数（按错误码），运维看得到"谁在敲门"
        self.denied_total = 0
        self.denied_by_code: dict[str, int] = {}

    # ── 对外：一次性返回 ────────────────────────────────────
    def handle(self, request: dict) -> TurnResult:
        """跑完一个回合。**永不抛异常** —— 任何内部故障都变成 `ok=False`。"""
        t0 = time.perf_counter()
        try:
            prepared = self._prepare(request)
        except _Reject as rej:
            return self._rejected(rej, request, t0)
        except Exception as exc:                      # ①~⑦ 兜底：fail-closed
            return self._rejected(
                _Reject("internal_error",
                        f"prepare fail-closed: {type(exc).__name__}: {exc}"),
                request, t0)
        return self._execute_guarded(prepared, t0)

    def _execute_guarded(self, prepared: _Prepared, t0: float) -> TurnResult:
        """最后一道保险：`_execute` 自己的兜底也炸了也不要紧，这里只构对象。

        顺带承载两个回合级钩子（Memory 召回 / 写回等）——放在这里而不是 `_execute`
        里面，是为了**失败路径也走得到** `on_turn_end`。
        """
        self._fire_turn_start(prepared)
        result: TurnResult | None = None
        try:
            result = self._execute(prepared, t0)
        except Exception as exc:                      # pragma: no cover - 双保险
            result = TurnResult(ok=False, text="",
                                task_id=prepared.e.task_id,
                                error=f"internal_error: {type(exc).__name__}: {exc}",
                                latency_ms=_latency_ms(t0),
                                vertex={"tenant": prepared.principal.tenant,
                                        "user_id": prepared.principal.user_id})
        finally:
            self._fire_turn_end(prepared, result)
        return result

    def _fire_turn_start(self, prepared: _Prepared) -> None:
        """回合开始钩子。**fail-open**：接线坏了不该让回合失败。"""
        cb = self._on_turn_start
        if cb is None:
            return
        try:
            cb(prepared.ctx, prepared.goal, prepared.principal)
        except Exception:                                      # noqa: BLE001
            pass

    def _fire_turn_end(self, prepared: _Prepared, result: TurnResult | None) -> None:
        """回合结束钩子。**失败回合也调**（失败本身是有信息量的事实）。"""
        cb = self._on_turn_end
        if cb is None:
            return
        try:
            cb(prepared.ctx, result)
        except Exception:                                      # noqa: BLE001
            pass

    # ── 对外：流式事件 ──────────────────────────────────────
    def handle_stream(self, request: dict) -> Iterator[dict]:
        """稳定事件序列（形状固定，不随实现是否真流式而变）：

            准入通过： accepted(seq=0) → started(seq=1) → completed(seq=2)
            准入拒绝： rejected(seq=0)   ← 只有这一条

        `stdout 只归事件，日志走 stderr`：本模块自己不 print 任何东西，
        事件是唯一输出通道。

        实现上"准入跑完才开始 yield started"，所以拒绝一定是单条 —— 不存在
        "先 accepted 再 rejected"这种让客户端状态机无法收口的序列。
        """
        t0 = time.perf_counter()
        try:
            prepared = self._prepare(request)
        except _Reject as rej:
            result = self._rejected(rej, request, t0)
            yield self._event("rejected", 0, {"error": result.error,
                                              "audit_id": result.audit_id})
            return
        except Exception as exc:
            result = self._rejected(
                _Reject("internal_error",
                        f"prepare fail-closed: {type(exc).__name__}: {exc}"),
                request, t0)
            yield self._event("rejected", 0, {"error": result.error,
                                              "audit_id": result.audit_id})
            return

        # seq=0：已经过 ①~⑦（含配额与输入护栏），现在确实开始跑了
        yield self._event("accepted", 0, {
            "task_id": prepared.e.task_id,
            "ctx_id": prepared.ctx_id,
            "resumed": prepared.resumed,
        })

        # seq=1：**先发 started 再跑**。
        #
        # 早期实现把 `started` 放在 `_execute_guarded` 之后 —— 语义上是错的：
        # 客户端收到 "started" 时回合其实已经跑完了。对"跑完再发"的形态看不出
        # 问题，但一旦中间插入增量事件（core/streaming.py 尾随 patch log），
        # 顺序就变成 accepted → step → step → ... → started → completed，
        # 客户端会看到"先干活再宣布开始"。
        yield self._event("started", 1, {
            "phase": _phase_text(Phase.EXECUTING),
            "task_id": prepared.e.task_id,
        })

        result = self._execute_guarded(prepared, t0)

        # seq=2：终态。失败也走 completed，客户端只需判 ok 字段
        yield self._event("completed", 2, {
            "text": result.text,
            "steps": result.steps,
            "ok": result.ok,
            "phase": result.phase,
            "term_reason": result.term_reason,
            "error": result.error,
            "audit_id": result.audit_id,
        })

    @staticmethod
    def _event(kind: str, seq: int, data: Mapping[str, Any]) -> dict:
        """事件只能装 JSON 原生类型 —— 序列化失败会炸在客户端而不是这里。"""
        return {"type": kind, "seq": int(seq), "data": dict(data)}

    # ══════════════════════════════════════════════════════
    # ① ~ ⑦ 准入
    # ══════════════════════════════════════════════════════
    def _prepare(self, request: dict) -> _Prepared:
        shape = self._check_shape(request)                       # ①
        principal = self._resolve_principal(shape["principal"])   # ②
        self._admit(principal)                                   # ③
        quota = self._charge(principal.tenant)                   # ④
        goal = shape["goal"]
        self._guard_input(goal)                                  # ⑤
        # `_cancel_event` 由接入层（serve 的超时中间件）塞进请求：
        # 它在另一个线程里 set()，本线程在下一个检查点看到 `aborted` 就退出。
        e = self._make_exec_ctx(principal, shape,
                                cancel_event=request.get("_cancel_event"))
        # 进度侧通道：接入层（serve 的流式端点）塞进来，子 Agent 往里 emit。
        # 不进 Context（那是状态，这是事件），所以单独挂 ExecCtx。
        e.progress = request.get("_progress")                # ⑥
        ctx, ctx_id, resumed, suspended = self._resolve_context(   # ⑦
            shape, principal, e)
        # **副作用落盘**：非只读工具成功之后立刻存一份，崩溃了才知道
        # "哪一步已经真做了"。没有 store 就不装（`None` = Loop 跳过）。
        e.checkpoint = self._make_side_effect_checkpoint(ctx_id)
        tenant_ctx = TenantContext(
            tenant=principal.tenant,
            user_id=principal.user_id,
            principal=principal,
            task_id=e.task_id,
            turn_id=e.turn_id,
            trace_id=e.trace_id,
            ctx_id=ctx_id,
            resumed=resumed,
            goal_len=len(goal),
        )
        return _Prepared(goal=goal, principal=principal, tenant_ctx=tenant_ctx,
                         e=e, ctx=ctx, ctx_id=ctx_id, quota=quota,
                         resumed=resumed, suspended=suspended)

    # ── ① 请求形状 ─────────────────────────────────────────
    def _check_shape(self, request: Any) -> dict:
        if not isinstance(request, Mapping):
            raise _Reject("bad_request",
                          f"request 必须是 dict，收到 {type(request).__name__}")

        goal = request.get("goal")
        if not isinstance(goal, str):
            raise _Reject("bad_request",
                          f"goal 必填且必须是 str，收到 {type(goal).__name__}")
        if not goal.strip():
            raise _Reject("bad_request", "goal 不能为空")
        if len(goal) > MAX_GOAL_CHARS:
            raise _Reject("bad_request",
                          f"goal 过长（{len(goal)} > {MAX_GOAL_CHARS} 字符）")

        if "principal" not in request:
            raise _Reject("bad_request", "principal 必填")
        raw_principal = request.get("principal")
        if not isinstance(raw_principal, (Mapping, Principal)):
            raise _Reject("bad_request",
                          f"principal 必须是 dict 或 Principal，"
                          f"收到 {type(raw_principal).__name__}")

        task_id = request.get("task_id")
        if task_id is not None and not isinstance(task_id, str):
            raise _Reject("bad_request",
                          f"task_id 必须是 str 或 None，收到 {type(task_id).__name__}")

        ctx_id = request.get("ctx_id")
        if ctx_id is not None:
            if not isinstance(ctx_id, str) or not ctx_id.strip():
                raise _Reject("bad_request", "ctx_id 必须是非空 str 或 None")
            ctx_id = ctx_id.strip()

        deadline_ms = request.get("deadline_ms")
        if deadline_ms is not None:
            if not _is_number(deadline_ms):
                raise _Reject("bad_request",
                              f"deadline_ms 必须是数字或 None，"
                              f"收到 {type(deadline_ms).__name__}")
            if deadline_ms <= 0:
                raise _Reject("bad_request", f"deadline_ms 必须 > 0，收到 {deadline_ms!r}")

        budget = self._check_budget(request.get("budget"))

        return {"goal": goal, "principal": raw_principal,
                "task_id": task_id, "ctx_id": ctx_id,
                "deadline_ms": deadline_ms, "budget": budget}

    @staticmethod
    def _check_budget(raw: Any) -> dict | None:
        if raw is None:
            return None
        if not isinstance(raw, Mapping):
            raise _Reject("bad_request",
                          f"budget 必须是 dict 或 None，收到 {type(raw).__name__}")
        unknown = sorted(str(k) for k in raw if k not in BUDGET_KEYS)
        if unknown:
            # 拼错的预算字段被静默忽略 = 客户端以为有上限、实际没有
            raise _Reject("bad_request",
                          f"budget 含未知字段 {unknown}，只允许 {list(BUDGET_KEYS)}")
        out: dict = {}
        ints = ("token_total", "tool_calls_total")
        for key in ints:
            if key in raw:
                value = raw[key]
                if not _is_int(value) or value < 0:
                    raise _Reject("bad_request",
                                  f"budget.{key} 必须是 >=0 的整数，收到 {value!r}")
                out[key] = int(value)
        if "cost_total_usd" in raw:
            value = raw["cost_total_usd"]
            if not _is_number(value) or value < 0:
                raise _Reject("bad_request",
                              f"budget.cost_total_usd 必须是 >=0 的数字，收到 {value!r}")
            out["cost_total_usd"] = float(value)
        if "max_steps" in raw:
            value = raw["max_steps"]
            if not _is_int(value) or value < 1:
                raise _Reject("bad_request",
                              f"budget.max_steps 必须是 >=1 的整数，收到 {value!r}")
            out["max_steps"] = int(value)
        return out

    # ── ② 身份 / 租户解析 ──────────────────────────────────
    def _resolve_principal(self, raw: Any) -> Principal:
        try:
            if isinstance(raw, Principal):
                principal = raw
            else:
                missing = [k for k in _REQUIRED_PRINCIPAL_KEYS if k not in raw]
                if missing:
                    raise _Reject("unauthorized",
                                  f"principal 缺少必填字段 {missing}（fail-closed）")
                user_id = raw.get("user_id")
                tenant = raw.get("tenant")
                if not isinstance(user_id, str) or not user_id.strip():
                    raise _Reject("unauthorized",
                                  "principal.user_id 必须是非空 str（fail-closed）")
                if not isinstance(tenant, str) or not tenant.strip():
                    raise _Reject("unauthorized",
                                  "principal.tenant 必须是非空 str（fail-closed）")
                roles = self._str_tuple(raw.get("roles"), "roles")
                scopes = self._str_tuple(raw.get("scopes"), "scopes")
                principal = Principal(user_id=user_id.strip(),
                                      roles=roles, tenant=tenant.strip(),
                                      scopes=frozenset(scopes))
        except _Reject:
            raise
        except Exception as exc:      # 解析过程炸了 = 身份不可确定 = 拒绝
            raise _Reject("unauthorized",
                          f"principal parse fail-closed: "
                          f"{type(exc).__name__}: {exc}") from exc

        if not isinstance(principal.tenant, str) or not principal.tenant.strip():
            raise _Reject("unauthorized", "principal.tenant 为空（fail-closed）")
        return principal

    @staticmethod
    def _str_tuple(raw: Any, field_name: str) -> tuple[str, ...]:
        if raw is None:
            return ()
        if isinstance(raw, str):
            raise _Reject("unauthorized",
                          f"principal.{field_name} 必须是字符串序列，收到裸字符串")
        if not isinstance(raw, (list, tuple, set, frozenset)):
            raise _Reject("unauthorized",
                          f"principal.{field_name} 必须是字符串序列，"
                          f"收到 {type(raw).__name__}")
        out: list[str] = []
        for item in raw:
            if not isinstance(item, str):
                raise _Reject("unauthorized",
                              f"principal.{field_name} 含非字符串元素 {item!r}")
            if item.strip():
                out.append(item.strip())
        return tuple(out)

    # ── ③ RBAC 准入 ────────────────────────────────────────
    def _admit(self, principal: Principal) -> None:
        """`runtime:run` 准入。两条判定，**拒绝优先**：

        1. Gateway 自己的 scope 覆盖判定（`*` / `runtime:*` / `runtime:run`）。
           这是权威判据：`plane/governance.RBAC` 目前只建模 tool/memory/knowledge
           三族（`SCOPE_FAMILIES`），拿 `runtime:run` 问它必然得到
           `unknown action family` —— 那是"它不认识这个动作"，不是"拒绝了"。
        2. 若 governance 的 RBAC 后端**建模了** runtime 族（并行 worker 可能加上），
           它的拒绝一并生效 —— 即"补充意见只能更严，不能更松"。
        """
        scopes = {str(s).strip() for s in (principal.scopes or ()) if str(s).strip()}
        if not scopes:
            raise _Reject("forbidden",
                          f"principal {principal.user_id} 不携带任何 scope（fail-closed）")
        if not _scope_allows(scopes, RUN_SCOPE):
            raise _Reject("forbidden",
                          f"principal {principal.user_id} 缺少 scope "
                          f"'{RUN_SCOPE}'（或 '*')，无权进入运行时")

        reason = self._rbac_runtime_verdict(principal)
        if reason:
            raise _Reject("forbidden", str(reason))

    def _rbac_runtime_verdict(self, principal: Principal) -> str | None:
        """向 RBAC 后端补问一次 `runtime:run`；返回拒绝原因或 None。"""
        rbac = getattr(self.governance, "rbac", None)
        check = getattr(rbac, "check", None)
        if not callable(check):
            return None
        try:
            reason = check(principal, RUN_SCOPE, "runtime")
        except Exception as exc:      # 后端挂了 = 判不出来 = 拒绝
            return (f"denied (fail-closed): rbac backend error: "
                    f"{type(exc).__name__}: {exc}")
        if reason is None:
            return None
        reason = str(reason)
        if "unknown action family" in reason:
            # 该 RBAC 未建模 runtime 族 → 无法判定 → 以 Gateway 自身判据为准
            return None
        return reason

    # ── ④ 配额（按租户） ───────────────────────────────────
    def _charge(self, tenant: str) -> dict:
        """计一格配额（按租户滑动窗口）。**整段临界区**（W25 并发加固）。

        为什么整段都要在锁里（包括 governance.charge）：配额的正确性依赖
        「剪枝 → 数已用 → 判超限 → 记账」四步不可分割。把锁缩小到只剩 append，
        并发下每个线程都会在「数已用」时看到同一个 `used`，于是 limit=25 的窗口
        能放进 250 个请求 —— 限流失效**不会报错**，只会静静地不算数。

        governance.charge 也在锁内：它是"另一个 kind 的配额"，快照/判定同样是
        check-then-act。代价是慢后端会拖住同进程的其他租户 —— 这是本层刻意接受的
        取舍：宁可排队，也不要静默超额。跨租户隔离靠 `_windows` 按 tenant 分桶，
        锁只是保证**每次判定看到的窗口是自洽的**。
        """
        with self._lock:
            limit = self.rate_limit_per_minute
            now = float(self._clock())
            window = self._windows.setdefault(tenant, deque())
            cutoff = now - self.window_seconds
            while window and window[0] <= cutoff:
                window.popleft()
            used = len(window)

            # **`limit <= 0` = 关闭配额**（不做任何限制），而不是"一个都不许"。
            #
            # 这是踩出来的：裸写 `used + 1 > limit` 时，`limit=0` 会拒绝**所有**
            # 请求 —— 而它在配置里长得像"关闭"（`AGENT_RUNTIME_HTTP_RATE=0` 确实是
            # 关闭）。两个名字几乎一样的开关含义相反，是必然踩的坑：
            # 运营想关掉配额，结果把整个服务关掉了，且日志只有 rate_limited。
            # limit <= 0 时**跳过**限流判定，但后面的治理面配额与记账照常走。
            if limit > 0 and used + 1 > limit:
                retry_after = max(0.0, (window[0] + self.window_seconds) - now) if window else 0.0
                raise _Reject(
                    "rate_limited",
                    f"tenant {tenant} 超过 {limit} 回合/{self.window_seconds:.0f}s"
                    f"（已用 {used}，retry_after≈{retry_after:.1f}s）",
                    vertex={"quota": {"tenant": tenant, "used": used, "limit": limit,
                                      "remaining": 0, "retry_after_s": round(retry_after, 3)},
                            "quota_remaining": 0},
                )

            # 治理面配额（独立 kind，不污染 Loop 的 tool_call / llm_call 记账）
            charge = getattr(self.governance, "charge", None)
            if callable(charge):
                try:
                    verdict = charge(tenant, TURN_QUOTA_KIND)
                except Exception as exc:      # fail-closed：算不出配额就不放行
                    raise _Reject("rate_limited",
                                  f"governance quota fail-closed: "
                                  f"{type(exc).__name__}: {exc}",
                                  vertex={"quota_remaining": 0}) from exc
                allowed = getattr(verdict, "allowed", False) if verdict is not None else True
                if not allowed:
                    reason = str(getattr(verdict, "reason", "") or "quota denied")
                    raise _Reject("rate_limited", reason,
                                  vertex={"quota_remaining": 0})

            window.append(now)
            used = len(window)
            # `limit<=0`（不限流）时 remaining 报 -1 表示"无上限"，
            # 而不是 0 —— 报 0 会让仪表盘显示"配额耗尽"，与事实相反。
            remaining = -1 if limit <= 0 else max(0, limit - used)
            return {"tenant": tenant, "used": used, "limit": limit,
                    "remaining": remaining, "window_s": self.window_seconds}

    # ── ⑤ 输入护栏 ─────────────────────────────────────────
    def _guard_input(self, goal: str) -> None:
        """用户直接输入的 goal 是 trusted=True：越狱粗筛，命中即拒。"""
        guard = getattr(self.governance, "before_input", None)
        if not callable(guard):
            return                      # 未配置治理面（调用方显式选择）
        try:
            verdict = guard(goal, trusted=True)
        except Exception as exc:        # fail-closed：护栏挂了 = 不放过
            raise _Reject("input_blocked",
                          f"input guard fail-closed: "
                          f"{type(exc).__name__}: {exc}") from exc
        if verdict is None:
            raise _Reject("input_blocked", "input guard returned None（fail-closed）")
        if not getattr(verdict, "allowed", False):
            raise _Reject("input_blocked",
                          str(getattr(verdict, "reason", "") or "input blocked"),
                          vertex={"guard_hits": list(getattr(verdict, "hits", ()) or ())})

    # ── ⑥ ExecCtx ─────────────────────────────────────────
    def check_dispatch(self, dispatcher: Any) -> frozenset[str]:
        """比对 Dispatcher 自述能力与期望，缺失即抛。返回它实际提供的能力。

        **为什么必须在启动时做**：换掉 Dispatcher 不会报错，只会少保障。
        实测：一个"只调 handler"的实现会让 T2 审批、T3 硬拒、幂等缓存、
        预算记账、工具超时全部消失，而回合照常返回 `ok`。
        这种失败在运行期没有任何信号 —— 直到某天真的需要审批时才发现没有。

        `capabilities()` 缺失（老实现 / 第三方适配器）按**空集**处理：
        不申报就是没有，宁可启动失败也不要静默降级。
        """
        getter = getattr(dispatcher, "capabilities", None)
        provided = frozenset(getter()) if callable(getter) else frozenset()
        wanted = self.required_dispatch
        if wanted is None:
            return provided
        missing = wanted - provided
        if missing:
            raise DispatcherCapabilityError(
                f"Dispatcher {type(dispatcher).__name__} 缺少保障：{sorted(missing)}",
                detail=(f"期望 {sorted(wanted)}，实际 {sorted(provided)}。"
                        f"缺的这些不会报错，只会在运行期静默失效 —— "
                        f"要么换成完整实现，要么显式把 required_dispatch 设为 None "
                        f"表示你接受这些保障缺席。"),
                vertex={"missing": sorted(missing), "provided": sorted(provided)})
        return provided

    def _make_exec_ctx(self, principal: Principal, shape: dict,
                       cancel_event: Any = None) -> ExecCtx:
        budget = Budget(**shape["budget"]) if shape["budget"] else Budget()
        deadline = 0.0
        deadline_ms = shape["deadline_ms"]
        if deadline_ms is not None:
            # 相对 → 绝对。基准点是"现在"（收到请求的时刻），不是加载完 Context 之后。
            deadline = float(self._clock_ms()) + float(deadline_ms)
        task_id = (shape["task_id"] or "").strip() or f"task_{uuid.uuid4().hex[:8]}"
        return ExecCtx(principal=principal, task_id=task_id, deadline=deadline,
                       budget=budget, cancel_event=cancel_event)

    # ── ⑦ 续跑判定 ─────────────────────────────────────────
    def _resolve_context(self, shape: dict, principal: Principal,
                         e: ExecCtx) -> tuple[Any, str, bool, bool]:
        """返回 (ctx, ctx_id, resumed, suspended)。

        `suspended` = 载入的 Context 正停在 `WAITING_USER`：这种回合必须走
        `loop.resume`（补一条人类批准）而不是 `loop.run`（重新 seed 一次历史），
        否则审批记录与待确认动作会被重新播种的 history 冲掉。
        """
        ctx_id = shape["ctx_id"]
        if not ctx_id:
            ctx = self._new_context(shape["goal"], principal, e)
            self._notify_context(ctx, e)
            return ctx, f"ctx_{uuid.uuid4().hex[:12]}", False, False

        if self.store is None:
            raise _Reject("resume_failed",
                          f"给了 ctx_id={ctx_id!r} 但没有配置 store（fail-closed）")
        loader = self._loader()
        try:
            # budget 传 None：续跑沿用**落盘时的预算**，请求里新给的 budget 不能
            # 在这里悄悄放大自己的额度（没有租户预算上限可对照，就不接受覆盖）。
            ctx = loader(self.store, ctx_id, None)
            self._notify_context(ctx, e)
        except Exception as exc:        # 找不到 / 读坏 / 结构不符 —— 一律拒绝
            raise _Reject("resume_failed",
                          f"ctx_id={ctx_id!r} 加载失败（fail-closed）："
                          f"{type(exc).__name__}: {exc}") from exc

        # 跨租户续跑 = 越权读别人任务的历史。tenant 必须一致。
        stored = getattr(ctx, "get", lambda *a, **k: None)("principal")
        stored_tenant = ""
        if isinstance(stored, Mapping):
            stored_tenant = str(stored.get("tenant") or "")
        elif isinstance(stored, Principal):
            stored_tenant = stored.tenant
        if stored_tenant and stored_tenant != principal.tenant:
            raise _Reject("forbidden",
                          f"ctx_id={ctx_id!r} 属于租户 {stored_tenant!r}，"
                          f"当前主体租户 {principal.tenant!r}（拒绝跨租户续跑）")

        stored_task = getattr(ctx, "get", lambda *a, **k: None)("task_id")
        if isinstance(stored_task, str) and stored_task.strip():
            e.task_id = stored_task          # task_id 跟着任务走，不重新生成

        suspended = _phase_text(getattr(ctx, "get", lambda *a, **k: None)("phase")) \
            == Phase.WAITING_USER.value
        return ctx, ctx_id, True, suspended

    def _notify_context(self, ctx: Any, e: ExecCtx) -> None:
        """通知观察者「Context 就绪了」。**fail-open**：观察者坏了不影响回合。"""
        cb = self._on_context
        if cb is None:
            return
        try:
            cb(ctx, e)
        except Exception:                                      # noqa: BLE001
            pass

    def _new_context(self, goal: str, principal: Principal, e: ExecCtx) -> Any:
        factory = self._context_impl()
        initial = {
            # goal 写进初始种子（OWNER_TABLE 没给 goal 定 owner，只能由建 Context
            # 的一方 seed；GATEWAY 本来就是 principal/task_id/turn_id 的 owner）
            "goal": goal,
            "principal": principal.to_dict(),
            "task_id": e.task_id,
            "turn_id": e.turn_id,
        }
        # 交给注入口改造种子（会话历史、外部配置等）。**失败即拒绝**：
        # 带着半截种子继续跑比直接报错更危险（模型会基于残缺历史推理）。
        if self._context_seed is not None:
            try:
                seeded = self._context_seed(initial, goal, principal, e)
                if isinstance(seeded, dict):
                    initial = seeded
            except Exception as exc:                           # noqa: BLE001
                raise _Reject("internal_error",
                              f"context_seed 失败（fail-closed）："
                              f"{type(exc).__name__}: {exc}") from exc
        try:
            return factory(initial=initial, budget=e.budget)
        except TypeError:
            return factory(initial, e.budget)
        except Exception as exc:
            raise _Reject("internal_error",
                          f"创建 Context 失败（fail-closed）："
                          f"{type(exc).__name__}: {exc}") from exc

    def _context_impl(self) -> Callable[..., Any]:
        if self._context_factory is not None:
            return self._context_factory
        try:
            from core.context import ContextHandle      # 懒加载：并行施工期只读依赖
        except Exception as exc:
            raise _Reject("internal_error",
                          f"core.context.ContextHandle 不可用："
                          f"{type(exc).__name__}: {exc}") from exc
        return ContextHandle

    def _loader(self) -> Callable[..., Any]:
        if self._checkpoint_loader is not None:
            return self._checkpoint_loader
        try:
            from core.store import load_checkpoint
        except Exception as exc:
            raise _Reject("resume_failed",
                          f"core.store.load_checkpoint 不可用："
                          f"{type(exc).__name__}: {exc}") from exc
        return load_checkpoint

    # ══════════════════════════════════════════════════════
    # ⑧ ~ ⑪ 执行
    # ══════════════════════════════════════════════════════
    def _execute(self, prepared: _Prepared, t0: float) -> TurnResult:
        """⑧~⑪ 的外壳：**任何**异常都必须变成一个 TurnResult，绝不上抛。

        Loop 是外部注入的，它的 `Result` 也可能是恶意/损坏对象（`text` 的
        `__str__` 会炸、`steps` 是怪对象……）。接入层是最后一道，不能假设
        "下游返回的东西一定是好的" —— 那正是 fail-closed 最容易被击穿的地方。
        """
        turn = _Turn(goal_len=len(prepared.goal),
                     tenant=prepared.principal.tenant,
                     user_id=prepared.principal.user_id,
                     task_id=prepared.e.task_id)
        try:
            turn.vertex.update({
                "tenant": prepared.principal.tenant,
                "user_id": prepared.principal.user_id,
                "task_id": prepared.e.task_id,
                "turn_id": prepared.e.turn_id,
                "trace_id": prepared.e.trace_id,
                "ctx_id": prepared.ctx_id,
                "resumed": prepared.resumed,
                "quota": dict(prepared.quota),
                "quota_remaining": int(prepared.quota.get("remaining", 0)),
                "budget": prepared.e.budget.to_dict(),
                "deadline": prepared.e.deadline,
            })
            return self._run_turn(prepared, turn, t0)
        except Exception as exc:
            turn.ok = False
            turn.text = ""
            turn.error = f"internal_error: {type(exc).__name__}: {exc}"
            try:
                return self._finalize_result(turn, t0)
            except Exception:                 # 连出口都炸了：直接构对象，不写审计
                return TurnResult(ok=False, text="", task_id=turn.task_id,
                                  error=turn.error, latency_ms=_latency_ms(t0),
                                  vertex={"tenant": turn.tenant,
                                          "user_id": turn.user_id})

    def _run_turn(self, prepared: _Prepared, turn: _Turn, t0: float) -> TurnResult:
        # ⑧ 跑 Loop ------------------------------------------------------
        loop = None
        result = None
        try:
            trace = self._start_trace(prepared)          # 观测面：fail-open
            if trace is not None:
                turn.vertex["trace"] = type(trace).__name__
            loop = self.loop_factory(prepared.goal, prepared.tenant_ctx)
            run = getattr(loop, "resume" if prepared.suspended else "run", None)
            if not callable(run):
                raise TypeError(
                    f"loop {type(loop).__name__} 缺少 "
                    f"{'resume' if prepared.suspended else 'run'}()")
            if prepared.suspended:
                result = run(prepared.ctx, prepared.e, True,
                             "gateway: 续跑已挂起的等待确认任务")
            else:
                result = run(prepared.goal, prepared.ctx, prepared.e)
        except Exception as exc:
            turn.ok = False
            turn.error = f"loop_error: {type(exc).__name__}: {exc}"
            turn.vertex["budget"] = self._budget_of(prepared.ctx, prepared.e)
            return self._finalize(turn, t0)

        if result is None or not hasattr(result, "phase"):
            turn.ok = False
            turn.error = (f"loop_error: loop 返回了不可用的结果 "
                          f"({type(result).__name__})")
            turn.vertex["budget"] = self._budget_of(prepared.ctx, prepared.e)
            return self._finalize(turn, t0)

        # 副作用落盘失败要**报出去**：吞掉它等于"没落盘还谎称落了"，
        # 崩溃后就会出现"工具做了、状态里没有"这种最难查的错。
        _ck_errors = list(getattr(loop, "checkpoint_errors", []) or [])
        if _ck_errors:
            # 写进 vertex（调用方一定能看到）+ 一条 warning 日志。
            # 不放进 `turn.error`：回合本身是成功的，把成功改成失败是撒谎。
            turn.vertex["checkpoint_errors"] = _ck_errors[:10]
            turn.vertex["checkpoint_errors_count"] = len(_ck_errors)
            logger = getattr(self, "logger", None)
            if logger is not None:
                try:
                    logger.warn("checkpoint.side_effect_failed",
                                count=len(_ck_errors), first=_ck_errors[0])
                except Exception:                          # noqa: BLE001
                    pass

        turn.phase = _phase_text(getattr(result, "phase", ""))
        turn.term_reason = _term_text(getattr(result, "term_reason", ""))
        raw_text = getattr(result, "text", "")
        turn.text = self._safe_text(raw_text)
        if not isinstance(raw_text, str):
            # 读不出文本 = 不知道这回合到底说了什么 = 不能报"成功"（fail-closed）
            turn.ok = False
            turn.error = (f"invalid_result: result.text 不是 str"
                          f"（{type(raw_text).__name__}）")
        turn.steps = self._safe_int(getattr(result, "steps", 0))
        turn.partial = bool(getattr(result, "partial", False))
        turn.vertex["budget"] = self._budget_of(prepared.ctx, prepared.e)

        # ⑨ 输出护栏 ----------------------------------------------------
        text, hits, blocked_reason = self._guard_output(turn.text)
        if blocked_reason:
            turn.ok = False
            turn.text = ""               # 宁可不出，也不带着密钥出去
            turn.error = f"output_blocked: {blocked_reason}"
            turn.vertex["guard_hits"] = hits
            return self._finalize(turn, t0)
        turn.text = text
        if hits:
            turn.vertex["guard_hits"] = hits
            turn.vertex["redacted"] = True

        # ⑩ 审计（在 _finalize 里统一写）+ ⑪ 落盘
        return self._finalize(turn, t0,
                              prepared=prepared, checkpoint=True)

    @staticmethod
    def _safe_text(value: Any) -> str:
        """Loop 的 `text` 可能是任何对象。`__str__` 炸了 → 按空文本处理。

        不能让它炸穿接入层：一个畸形的 Result 不该让整个回合连"失败"都报不出来。
        """
        if value is None:
            return ""
        if isinstance(value, str):
            return value
        try:
            return str(value)
        except Exception:
            return ""

    @staticmethod
    def _safe_int(value: Any) -> int:
        if _is_int(value):
            return int(value)
        try:
            return int(value)
        except Exception:
            return 0

    def _guard_output(self, text: str) -> tuple[str, list[str], str]:
        """返回 (脱敏后文本, hits, 阻断原因)。原因非空 = 必须放弃文本。"""
        guard = getattr(self.governance, "before_output", None)
        if not callable(guard):
            return text, [], ""
        try:
            try:
                verdict = guard(text, allow_hosts=self.allow_hosts)
            except TypeError:          # 后端签名没有 allow_hosts
                verdict = guard(text)
        except Exception as exc:       # fail-closed：护栏挂了 → 不带原文出去
            return "", ["output_guard_error"], \
                   f"output guard fail-closed: {type(exc).__name__}: {exc}"
        if verdict is None:
            return "", ["output_guard_none"], "output guard returned None（fail-closed）"
        if not getattr(verdict, "allowed", False):
            return "", list(getattr(verdict, "hits", ()) or ()), \
                   str(getattr(verdict, "reason", "") or "output blocked")
        payload = getattr(verdict, "payload", None)
        out = payload if isinstance(payload, str) else text
        return out, list(getattr(verdict, "hits", ()) or ()), ""

    @staticmethod
    def _budget_of(ctx: Any, e: ExecCtx) -> dict:
        """成本/预算归因：优先读 Context（Loop 会把真实消耗写回去），退化到 ExecCtx。

        输出形状**固定**为 `Budget.to_dict()` 的嵌套形态：
        `{"token": {"total","used"}, "cost_usd": {...}, "tool_calls": {...}, "max_steps"}`。

        为什么不能直接透传 Context 里那份：`core/context.py` 存的是**扁平**形态
        （`token_total` / `token_used` …），而 ExecCtx 侧是嵌套形态。归因数据若两种
        形状混着流到成本报表里，同一个字段会有两种读法 —— 归一化放在接入层做，
        下游只认一种。
        """
        get = getattr(ctx, "get", None)
        stored = None
        if callable(get):
            try:
                stored = get("budget")
            except Exception:
                stored = None

        budget = Budget()
        if isinstance(stored, Mapping):
            for key, caster in (("token_total", int), ("token_used", int),
                                ("tool_calls_total", int), ("tool_calls_used", int),
                                ("max_steps", int),
                                ("cost_total_usd", float), ("cost_used_usd", float)):
                value = stored.get(key)
                if _is_number(value):
                    setattr(budget, key, caster(value))
            # 也兼容有人直接把 to_dict() 的嵌套形态塞进 Context
            for key, total_field, used_field in (
                    ("token", "token_total", "token_used"),
                    ("cost_usd", "cost_total_usd", "cost_used_usd"),
                    ("tool_calls", "tool_calls_total", "tool_calls_used")):
                nested = stored.get(key)
                if isinstance(nested, Mapping):
                    if _is_number(nested.get("total")):
                        setattr(budget, total_field,
                                (float if "usd" in total_field else int)(nested["total"]))
                    if _is_number(nested.get("used")):
                        setattr(budget, used_field,
                                (float if "usd" in used_field else int)(nested["used"]))
            return budget.to_dict()
        return e.budget.to_dict()

    # ══════════════════════════════════════════════════════
    # ⑩ 审计 + ⑪ 落盘 + 统一出口
    # ══════════════════════════════════════════════════════
    def _finalize(self, turn: _Turn, t0: float, *,
                  prepared: _Prepared | None = None,
                  checkpoint: bool = False) -> TurnResult:
        # ⑪ 落盘：只在"挂起等人工确认"时写检查点。失败视为回合失败（fail-closed）：
        # 告诉用户"待审批"却没有可续跑的落盘，等于永远批不了。
        if checkpoint and prepared is not None:
            if turn.phase == Phase.WAITING_USER.value:
                err = self._save_checkpoint(prepared, turn)
                if err:
                    turn.ok = False
                    turn.text = ""
                    turn.error = err
                    turn.vertex["checkpoint_error"] = err
        return self._finalize_result(turn, t0)

    def _make_side_effect_checkpoint(self, ctx_id: str) -> Any:
        """造一个「副作用落盘」回调交给 Loop；没有 store 就返回 None。

        为什么用回调而不是让 Loop 直接拿 store：
        Loop 不该知道存储（它只管 FSM 与记账），而且 `ctx_id` 是 Gateway
        的编排概念。用回调把这两件事留在 Gateway，Loop 只负责"什么时候该落"。
        """
        store = self.store
        if store is None or not ctx_id:
            return None

        def _save(ctx: Any, _e: Any, info: Any) -> None:
            from core.store import save_checkpoint
            tool = str((info or {}).get("tool") or "")
            save_checkpoint(
                ctx, store, ctx_id,
                reason=f"副作用已生效：{tool}（step {(info or {}).get('step')}）")

        return _save

    def _save_checkpoint(self, prepared: _Prepared, turn: _Turn) -> str:
        if self.store is None:
            # 没有 store = 挂起后无法续跑。不放行成"看着成功其实丢了"。
            return ("checkpoint_failed: phase=WAITING_USER 但没有配置 store"
                    "（fail-closed）")
        try:
            from core.store import save_checkpoint
        except Exception as exc:
            return (f"checkpoint_failed: core.store.save_checkpoint 不可用："
                    f"{type(exc).__name__}: {exc}")
        try:
            save_checkpoint(prepared.ctx, self.store, prepared.ctx_id,
                            reason="gateway: WAITING_USER 挂起，等待人工确认")
        except Exception as exc:
            return (f"checkpoint_failed: {type(exc).__name__}: {exc}")
        turn.vertex["checkpoint"] = prepared.ctx_id
        return ""

    def _rejected(self, rej: _Reject, request: Any, t0: float) -> TurnResult:
        """拒绝的统一出口：也写审计（谁在敲门、被哪个码挡住，运维必须看得到）。"""
        with self._lock:                      # `+=` 是读-改-写：并发下会丢计数
            self.denied_total += 1
            self.denied_by_code[rej.code] = self.denied_by_code.get(rej.code, 0) + 1
        tenant, user_id, goal_len = self._best_effort_identity(request)
        turn = _Turn(goal_len=goal_len, tenant=tenant, user_id=user_id,
                     ok=False, error=rej.error())
        if rej.vertex:
            turn.vertex.update(copy.deepcopy(rej.vertex))
        return self._finalize_result(turn, t0)

    @staticmethod
    def _best_effort_identity(request: Any) -> tuple[str, str, int]:
        """拒绝时也要能归因 —— 但绝不为归因去解析一个坏请求（那会二次抛异常）。"""
        tenant = ""
        user_id = ""
        goal_len = 0
        try:
            if isinstance(request, Mapping):
                goal = request.get("goal")
                if isinstance(goal, str):
                    goal_len = len(goal)
                raw = request.get("principal")
                if isinstance(raw, Mapping):
                    t = raw.get("tenant")
                    u = raw.get("user_id")
                    tenant = t.strip() if isinstance(t, str) else ""
                    user_id = u.strip() if isinstance(u, str) else ""
                elif isinstance(raw, Principal):
                    tenant = raw.tenant
                    user_id = raw.user_id
        except Exception:
            pass
        return tenant, user_id, goal_len

    def _finalize_result(self, turn: _Turn, t0: float) -> TurnResult:
        latency = _latency_ms(t0)
        record = self._audit(turn, latency)          # ⑩
        return TurnResult(
            ok=turn.ok,
            text=turn.text,
            phase=turn.phase,
            term_reason=turn.term_reason,
            task_id=turn.task_id,
            steps=turn.steps,
            partial=turn.partial,
            error=turn.error,
            audit_id=record["audit_id"],
            latency_ms=latency,
            vertex=copy.deepcopy(turn.vertex),
        )

    def _audit(self, turn: _Turn, latency: float) -> dict:
        """写一条审计。**只记 goal 长度，绝不记 goal 全文**。

        sink 抛异常必须被吞：审计是旁路观测，观测面坏了不能反噬主流程
        （对照 Healthcare 面的 fail-open 纪律）。但内存列表一定写成功。
        """
        record = {
            "audit_id": f"aud_{uuid.uuid4().hex[:12]}",
            "ts": float(self._clock_ms()),
            "tenant": turn.tenant,
            "user_id": turn.user_id,
            "goal_len": int(turn.goal_len),
            "ok": bool(turn.ok),
            "phase": turn.phase,
            "steps": int(turn.steps),
            "latency_ms": latency,
            # 只留错误码：完整 reason 可能夹带 goal 片段
            "error": _code_only(turn.error),
        }
        # 字段顺序即契约：先占位再覆写，保证 sink 看到的形状与内存一致
        record = {k: record[k] for k in AUDIT_FIELDS}
        with self._lock:                      # 「审计条数 == 回合数」必须是一次可见写入
            self.audit_log.append(record)
        if self.audit_sink is not None:
            try:
                self.audit_sink(dict(record))
            except Exception:                     # 审计不得反噬主流程
                pass
        return record

    # ══════════════════════════════════════════════════════
    # 观测面（fail-open）
    # ══════════════════════════════════════════════════════
    def _start_trace(self, prepared: _Prepared) -> Any:
        """Quality 面：Trace 起不来只是少一条 span，业务照跑（fail-open）。"""
        if self.trace_factory is None:
            return None
        try:
            return self.trace_factory(prepared.tenant_ctx)
        except TypeError:
            try:
                return self.trace_factory(prepared.tenant_ctx.to_dict())
            except Exception:
                return None
        except Exception:
            return None

    def snapshot(self) -> dict:
        """只读快照：配额窗口 + 拒绝计数（运维/测试用，不抛）。

        读的时候也要拿锁：`_windows` 会被 `_charge()` 并发 `setdefault` + `append`，
        边改边迭代 `dict.items()` 会直接 `RuntimeError: dictionary changed size` ——
        一个运维查询能把服务器打崩，这是最不该有的故障面。
        """
        try:
            with self._lock:
                now = float(self._clock())
                cutoff = now - self.window_seconds
                windows = {}
                for tenant, window in self._windows.items():
                    used = sum(1 for ts in window if ts > cutoff)
                    windows[tenant] = {"used": used, "limit": self.rate_limit_per_minute,
                                       "remaining": max(0, self.rate_limit_per_minute - used)}
                return {"rate_limit_per_minute": self.rate_limit_per_minute,
                        "window_seconds": self.window_seconds,
                        "windows": windows,
                        "denied_total": self.denied_total,
                        "denied_by_code": dict(self.denied_by_code),
                        "audit_count": len(self.audit_log)}
        except Exception as exc:
            with self._lock:
                return {"error": f"snapshot failed: {exc!r}",
                        "denied_total": self.denied_total,
                        "audit_count": len(self.audit_log)}
