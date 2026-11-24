"""plane/workflow.py — W9 / Workflow Plane：多 Agent 编排。

核心立场
════════════════════════════════════════════════════════════════════════
**没有协议的多 Agent 协同是无序群聊噪音；有了状态机与补偿的 Multi-Agent 才是软件工程。**

**不信任模型自觉性 —— 必须靠外部确定性看门狗保证收敛。**
════════════════════════════════════════════════════════════════════════

这个模块只做四件事，每件都必须能被外部机械验证：

1. **强类型信元**（`EnvelopeBus`）
   Agent 之间只传 `AgentEnvelope`，不传自然语言。校验三道：`trace_id` 非空、
   `sender`/`recipient` 非空、`payload` 必须是 dict。任一不满足 → `ValueError`。
   **绝不自动修正**：自动补 trace_id 等于把「无追踪的鬼消息」洗成合法消息，
   审计轨迹从此不可信。`seq` 和 `created_at` 由总线自己发（同一 trace 内严格单调），
   调用方填了也覆盖 —— 序号是总线的账本，不是发送方的自述。

2. **子 Agent 降权**（`SubagentRunner`，对应 INV-18）
   `parent_e.child()` 已经做了 principal 降权（scopes 只剩 `*.read`）+ 预算切分 +
   depth+1，但**它不够**：
   - 它只管 scope，不管工具清单。一个 read_only 工具也可能读走租户外数据，
     所以还要按 `spec.allowed_tools` 做**清单白名单**；
   - 它不拦「声明即越权」。`allowed_tools` 里出现 mutating 工具的 spec 本身就是
     非法输入，**在执行前就拒绝**（静态检查），而不是等 Dispatcher 兜底；
   - 它不管运行时越界。模型完全可能幻觉出一个没授权的工具名，因此再套一层
     `_GuardedDispatcher`：越权调用当场记录 violation，`dispatch` 返回
     `SubagentResult(ok=False, error="subagent 越权: ...")`，且**不回写父上下文**。
   - `allowed_tools` 为空 → 默认只读（`SideEffect.READ_ONLY`）；无法取到
     manifest 时**按不可信处理 → 拒绝**（fail-closed），而不是「取不到就当能调」。

3. **补偿事务**（`Saga`）
   forward 失败 → 逆序补偿已 `done` 的步骤。补偿自己抛异常不算止损理由：
   **继续补偿其余步骤**，把异常记进 `error`。没有 compensate 的步骤记
   `no_compensate`，不阻断。一个失败就放弃剩下的补偿，等于把「半成品状态」
   永久留在系统里 —— 这比原本的失败更贵。

4. **层次主从 + 死锁看门狗**（`HierarchicalOrchestrator`）
   Leader 拆任务 → 发 REQUEST → fan-out → 收 RESPONSE → fan-in 聚合。
   看门狗是**外部确定性计数器**，不看模型脸色：plan 长度 > `max_agents`(6) 或
   累计 steps ≥ `max_total_steps`(24) → 立即停止 fan-out，返回部分结果并标记
   `aborted_by_watchdog: True`。「多 Agent 应该自己知道够了」是幻觉，
   `steps` 超限只有外部计数器认得出来。

明确不做的
════════════════════════════════════════════════════════════════════════
- 不 import `core.loop`（避免环形依赖）；`Loop` 在默认 runner 内部**延迟导入**，
  测试通过 `runner=` 注入 fake 即可完全绕开 W2 的实现。
- 不改 `contracts.py`。本模块只在末尾契约（`MessageType` / `AgentEnvelope` /
  `SagaStep` / `SubagentSpec` / `SubagentResult`）之上做组合。
- 上下文只走 `fork()` / `merge()` / `snapshot()` 三个口子，不碰 `ContextHandle`
  内部字段。回写父上下文只有白名单两项：`observations` / `artifacts`。
"""
from __future__ import annotations

import copy
import dataclasses
import uuid
from concurrent.futures import ThreadPoolExecutor
from collections.abc import Mapping
from typing import Any, Callable, Sequence

from contracts import (
    AgentEnvelope,
    Budget,
    ContextError,
    ExecCtx,
    MessageType,
    Owner,
    Patch,
    Result,
    SagaStep,
    SideEffect,
    SubagentResult,
    SubagentSpec,
    ToolManifest,
    ToolOutcome,
    now_ms,
)

__all__ = [
    "EnvelopeBus",
    "Saga",
    "SubagentRunner",
    "HierarchicalOrchestrator",
    "MERGE_WHITELIST",
]

#: 子 Agent 结束后允许回写父上下文的字段（白名单，越界一律丢弃）。
MERGE_WHITELIST: tuple[str, ...] = ("observations", "artifacts")

#: 看门狗默认阈值：外部确定性上限，与模型自我评估无关。
DEFAULT_MAX_AGENTS = 6
DEFAULT_MAX_TOTAL_STEPS = 24

_SUMMARY_LIMIT = 4000


# ══════════════════════════════════════════════════════════════
# 内部小工具
# ══════════════════════════════════════════════════════════════
def _require_text(value: Any, field: str) -> str:
    """强类型字符串字段：必须是非空（去空白后）str。**不做任何自动填充**。"""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(
            f"AgentEnvelope.{field} 必须是非空字符串，收到 {value!r}"
            f"（{type(value).__name__}）"
        )
    return value.strip()


def _enum_value(value: Any) -> str:
    """枚举 / 字符串统一成文本，便于跨 fake 比较。"""
    raw = getattr(value, "value", value)
    return raw if isinstance(raw, str) else str(raw)


def _maybe_call_hook(tracer: Any, name: str, attrs: dict | None = None):
    """tracer 是可选的；没有就返回一个空上下文管理器。"""
    import contextlib

    span = getattr(tracer, "span", None)
    if callable(span):
        try:
            return span(name, attrs or {})
        except Exception:
            return contextlib.nullcontext()
    return contextlib.nullcontext()


# ══════════════════════════════════════════════════════════════
# ① 强类型信元总线
# ══════════════════════════════════════════════════════════════
class EnvelopeBus:
    """强类型信元总线。Agent 之间只传 `AgentEnvelope`，不传自然语言。

    三条硬规矩（全部抛 `ValueError`，不静默修正）：

    - `trace_id` 非空     —— 没有追踪号的信元不可审计，等于不存在；
    - `sender`/`recipient` 非空 —— 匿名信源不可追责；
    - `payload` 必须是 dict —— 自然语言 / 裸字符串 / JSON blob 一律不收。

    `seq` 与 `created_at` 由总线赋值：同一 `trace_id` 内单调递增，跨 trace 各自
    独立计数。调用方传入的 `seq` 会被覆盖（序号是总线的账本）。
    """

    def __init__(self) -> None:
        self._boxes: dict[str, list[AgentEnvelope]] = {}
        self._seq: dict[str, int] = {}
        self._log: list[AgentEnvelope] = []

    # ── 发送 ────────────────────────────────────────────────
    def send(self, env: AgentEnvelope) -> None:
        """校验 + 定序 + 入箱 + 记账。任一校验失败即抛 `ValueError`。"""
        if not isinstance(env, AgentEnvelope):
            raise ValueError(
                f"send 只接受 AgentEnvelope，收到 {type(env).__name__}"
                "（自然语言不进总线）"
            )
        trace_id = _require_text(env.trace_id, "trace_id")
        sender = _require_text(env.sender, "sender")
        recipient = _require_text(env.recipient, "recipient")
        if not isinstance(env.payload, dict):
            raise ValueError(
                f"AgentEnvelope.payload 必须是 dict，收到 {type(env.payload).__name__}"
                f"（值={env.payload!r}）；自然语言 payload 一律拒收"
            )
        if not isinstance(env.message_type, MessageType):
            raise ValueError(
                f"AgentEnvelope.message_type 必须是 MessageType，收到 "
                f"{getattr(env.message_type, 'value', env.message_type)!r}"
            )

        seq = self._seq.get(trace_id, 0) + 1
        self._seq[trace_id] = seq
        stored = dataclasses.replace(
            env,
            trace_id=trace_id,
            sender=sender,
            recipient=recipient,
            payload=copy.deepcopy(env.payload),
            seq=seq,
            created_at=now_ms(),
        )
        self._boxes.setdefault(recipient, []).append(stored)
        self._log.append(stored)

    # ── 收件（只读，不消费）──────────────────────────────────
    def inbox(self, agent: str) -> list[AgentEnvelope]:
        """按 `(created_at, seq)` 排序返回未消费信元；**不改变**箱内内容。

        返回的是副本：收件方改自己的副本不能篡改总线账本（payload 是可变
        dict，frozen dataclass 只冻住了引用）。
        """
        name = _require_text(agent, "agent")
        box = sorted(self._boxes.get(name, ()), key=lambda e: (e.created_at, e.seq))
        return [copy.deepcopy(e) for e in box]

    # ── 收件（消费）──────────────────────────────────────────
    def drain(self, agent: str) -> list[AgentEnvelope]:
        """取走并清空该 agent 的收件箱；审计轨迹（`log`）仍然保留。"""
        name = _require_text(agent, "agent")
        box = sorted(self._boxes.pop(name, ()), key=lambda e: (e.created_at, e.seq))
        return [copy.deepcopy(e) for e in box]

    # ── 审计 ────────────────────────────────────────────────
    def log(self) -> list[dict]:
        """全量信元审计轨迹（发送顺序，含 seq / created_at）。

        深拷贝返回：审计轨迹只能追加，不能被任何调用方就地改写。
        """
        return [copy.deepcopy(env.to_dict()) for env in self._log]

    # ── 观测辅助 ────────────────────────────────────────────
    def seq_of(self, trace_id: str) -> int:
        """该 trace 已发出的最大 seq（未曾发过 → 0）。"""
        return self._seq.get(trace_id, 0)

    def count(self, agent: str, message_type: MessageType | None = None) -> int:
        """某 agent 未消费信元数量（可按类型过滤）。"""
        box = self._boxes.get(agent, ())
        if message_type is None:
            return len(box)
        return sum(1 for e in box if e.message_type is message_type)

    def __len__(self) -> int:
        return len(self._log)


# ══════════════════════════════════════════════════════════════
# ② 补偿事务
# ══════════════════════════════════════════════════════════════
class Saga:
    """补偿事务。forward 失败 → 逆序执行已完成步骤的 `compensate`。

    语义要点（全部有测试压着）：

    - 第 k 步失败 → 逆序补偿 `steps[:k]` 里 `done=True` 的那些；**失败步自己不补偿**；
    - 补偿抛异常 → 记进 `error`，**继续补偿剩下的步骤**（一个失败不放弃其余）；
    - 没有 `compensate` 的步骤 → 记 `"no_compensate:<name>"`，不阻断后续补偿；
    - `compensated` 只装**真的补偿成功**的步骤名（诚实账本，不虚报）。
    """

    def __init__(self, name: str = "") -> None:
        self.name = name or ""
        self._steps: list[SagaStep] = []
        #: 非致命备注（`no_compensate:...` 等），与 `run()` 的返回值一并可查
        self.notes: list[str] = []

    # ── 组装 ────────────────────────────────────────────────
    def add(self, step: SagaStep) -> "Saga":
        if not isinstance(step, SagaStep):
            raise ValueError(f"Saga.add 只接受 SagaStep，收到 {type(step).__name__}")
        if not isinstance(step.name, str) or not step.name.strip():
            raise ValueError(f"SagaStep.name 必须是非空字符串，收到 {step.name!r}")
        if not callable(step.action):
            raise ValueError(f"SagaStep({step.name}).action 必须可调用")
        if step.compensate is not None and not callable(step.compensate):
            raise ValueError(f"SagaStep({step.name}).compensate 必须可调用或 None")
        if any(s.name == step.name for s in self._steps):
            raise ValueError(f"Saga 步骤名重复：{step.name!r}（补偿账本必须可唯一定位）")
        self._steps.append(step)
        return self

    @property
    def steps(self) -> tuple[SagaStep, ...]:
        return tuple(self._steps)

    def reset(self) -> None:
        """清掉所有步骤的运行痕迹与备注，让同一个 Saga 可再次 `run()`。"""
        for step in self._steps:
            step.done = False
            step.result = None
            step.error = ""
        self.notes.clear()

    # ── 执行 ────────────────────────────────────────────────
    def run(self) -> dict:
        """顺序执行；失败即逆序补偿。返回结构固定四键。"""
        # 每次 run 都从干净状态开始：残留的 done 会让补偿名单说谎
        for step in self._steps:
            step.done = False
            step.result = None
            step.error = ""
        self.notes.clear()

        completed: list[str] = []
        compensated: list[str] = []
        errors: list[str] = []

        for index, step in enumerate(self._steps):
            try:
                step.result = step.action()
                step.done = True
                completed.append(step.name)
            except Exception as exc:                       # noqa: BLE001 - forward 失败一律接住
                step.error = f"{type(exc).__name__}: {exc}"
                errors.append(f"step {step.name} failed: {step.error}")
                compensated, comp_errors = self._compensate_reverse(index)
                errors.extend(comp_errors)
                return {
                    "ok": False,
                    "completed": completed,
                    "compensated": compensated,
                    "error": " | ".join(errors),
                }

        return {"ok": True, "completed": completed, "compensated": [], "error": ""}

    # ── 逆序补偿 ────────────────────────────────────────────
    def _compensate_reverse(self, failed_index: int) -> tuple[list[str], list[str]]:
        """对 `steps[:failed_index]` 里 done 的步骤逆序补偿。

        **补偿异常绝不提前 return** —— 收集完所有错误再交回去。
        """
        compensated: list[str] = []
        errors: list[str] = []
        for step in reversed(self._steps[:failed_index]):
            if not step.done:
                continue
            if step.compensate is None:
                note = f"no_compensate:{step.name}"
                self.notes.append(note)
                errors.append(note)
                continue
            try:
                step.compensate()
            except Exception as exc:                       # noqa: BLE001 - 补偿失败也要记账
                msg = (f"compensate {step.name} failed: "
                       f"{type(exc).__name__}: {exc}")
                step.error = (step.error + " | " if step.error else "") + msg
                errors.append(msg)
                continue
            step.done = False
            compensated.append(step.name)
        return compensated, errors


# ══════════════════════════════════════════════════════════════
# ③ 子 Agent 派发（INV-18）
# ══════════════════════════════════════════════════════════════
@dataclasses.dataclass
class _Attempt:
    """一次派发的全部证据：结果 + 子上下文 + 允许清单 + 违规记录。"""
    result: SubagentResult
    child_ctx: Any = None
    allowed: tuple[str, ...] = ()
    violations: tuple[str, ...] = ()

    @property
    def mergeable(self) -> bool:
        return bool(self.result.ok) and not self.violations and self.child_ctx is not None


class _GuardedDispatcher:
    """子 Agent 专用 Dispatcher 外壳：**白名单之外一律拦在这里**。

    它刻意**不转发** `registry`：子 Agent 不该有能力枚举全量工具清单
    （枚举能力本身就是信息泄漏，也是越权探测的第一步）。
    """

    def __init__(self, inner: Any, allowed: dict[str, ToolManifest]) -> None:
        self._inner = inner
        self._allowed = dict(allowed)
        self.manifests: list[ToolManifest] = list(allowed.values())
        self.violations: list[str] = []
        self.calls: list[str] = []

    # 只读观测
    def allowed_names(self) -> list[str]:
        return sorted(self._allowed)

    def invoke(self, name: str, args: dict, e: ExecCtx) -> ToolOutcome:
        manifest = self._allowed.get(name)
        if manifest is None:
            reason = (
                f"subagent 越权: 工具 {name!r} 不在允许清单 {sorted(self._allowed)} 内"
            )
            self.violations.append(reason)
            return ToolOutcome.denied(reason)
        # 白名单通过 ≠ scope 通过：read_only 工具照样要过 principal 的 scope
        scope = getattr(manifest, "required_scope", "") or ""
        can = getattr(getattr(e, "principal", None), "can", None)
        if scope and callable(can) and not can(scope):
            reason = f"subagent 越权: 工具 {name!r} 需要 scope {scope!r}，子主体不具备"
            self.violations.append(reason)
            return ToolOutcome.denied(reason)
        self.calls.append(name)
        return self._inner.invoke(name, args, e)


class SubagentRunner:
    """把一个 `SubagentSpec` 派发成子 Agent：fork Context + 降权 + 只读工具。

    降权四层（外面三层都是 `child()` 没做的）：

    1. `parent_e.child()`：principal 只剩 `*.read`、预算切半、depth+1；
    2. 再按 spec 收紧预算：`max_steps` / `token_total` 只能更小；
    3. 工具清单白名单 + 静态越权检查（声明了 mutating 工具 → 执行前就拒）；
    4. 运行期 `_GuardedDispatcher` 拦幻觉工具名，越权即 ok=False 且不回写父上下文。

    失败一律收敛成 `SubagentResult(ok=False, ...)`，**不让异常穿出编排层**
    —— 一个子 Agent 崩掉不该把 Leader 一起带走。
    """

    def __init__(
        self,
        dispatcher: Any,
        builder: Any = None,
        llm_factory: Callable[[SubagentSpec], Any] | None = None,
        tracer: Any = None,
        *,
        max_depth: int = 2,
        runner: Callable[..., Any] | None = None,
        merge_fields: Sequence[str] = MERGE_WHITELIST,
    ) -> None:
        self._dispatcher = dispatcher
        self._builder = builder
        self._llm_factory = llm_factory
        self._tracer = tracer
        self._max_depth = int(max_depth)
        self._runner = runner or self._default_runner
        self._merge_fields: tuple[str, ...] = tuple(merge_fields)
        #: 回写记录（审计用）：每次 dispatch 成功 merge 了哪些路径
        self.merge_log: list[dict] = []
        #: 降权/越权的历史（供上层做安全度量）
        self.denied: list[str] = []
        #: `_resolve_allowlist` 上一次被降权过滤掉的写工具（C3a 用）
        self._last_filtered: tuple[str, ...] = ()

    # ── 对外入口 ────────────────────────────────────────────
    def dispatch(self, spec: SubagentSpec, parent_ctx: Any, parent_e: ExecCtx) -> SubagentResult:
        """单发：派发 → 运行 → 白名单回写 → 返回强类型结果。"""
        attempt = self._attempt(spec, parent_ctx, parent_e)
        if attempt.mergeable:
            self._merge_back(parent_ctx, attempt.child_ctx, attempt.result.name)
        elif attempt.violations:
            self.denied.extend(attempt.violations)
        return attempt.result

    def dispatch_many(
        self,
        specs: Sequence[SubagentSpec],
        parent_ctx: Any,
        parent_e: ExecCtx,
        max_workers: int = 1,
    ) -> list[SubagentResult]:
        """fan-out / fan-in。**返回顺序与输入严格一致**（不按完成先后重排）。

        并发只用在「跑子 Agent」这一段；回写父上下文**串行按输入顺序**做，
        避免多个 fork 的结果互相覆盖（顺序即语义）。
        """
        spec_list = list(specs)
        if not spec_list:
            return []
        workers = max(1, min(int(max_workers), len(spec_list)))
        if workers == 1:
            attempts = [self._attempt(s, parent_ctx, parent_e) for s in spec_list]
        else:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = [pool.submit(self._attempt, s, parent_ctx, parent_e)
                           for s in spec_list]
                # 按 futures 顺序（＝输入顺序）取结果并 await，天然保序
                attempts = [f.result() for f in futures]

        results: list[SubagentResult] = []
        for attempt in attempts:
            results.append(attempt.result)
            if attempt.mergeable:
                self._merge_back(parent_ctx, attempt.child_ctx, attempt.result.name)
            elif attempt.violations:
                self.denied.extend(attempt.violations)
        return results

    # ── 单次派发内核 ────────────────────────────────────────
    def _attempt(self, spec: SubagentSpec, parent_ctx: Any, parent_e: ExecCtx) -> _Attempt:
        if not isinstance(spec, SubagentSpec):
            return _Attempt(_failed("", f"spec 必须是 SubagentSpec，收到 {type(spec).__name__}"))

        with _maybe_call_hook(self._tracer, "workflow.dispatch",
                              {"agent": spec.name, "goal_len": len(spec.goal or "")}):
            # ① 静态工具白名单（fail-closed）：越权在**执行前**就拒
            allowed, why = self._resolve_allowlist(spec)
            if allowed is None:
                self.denied.append(why)
                _emit(parent_e, "subagent.rejected",
                      {"agent": spec.name, "reason": str(why)[:200]})
                failed = _failed(spec.name, why)
                failed = dataclasses.replace(failed, needs_approval={
                    "kind": "privilege_denied",
                    "agent": spec.name,
                    "reason": str(why)[:300],
                })
                return _Attempt(failed)

            # ② 子 ExecCtx：child() 做 principal/预算/depth，我们再收紧预算
            try:
                child_e = parent_e.child(max_depth=self._max_depth)
            except ContextError as exc:
                reason = f"subagent 被拒: depth 超限（{exc}）"
                self.denied.append(reason)
                return _Attempt(_failed(spec.name, reason))
            except Exception as exc:                        # noqa: BLE001
                reason = f"subagent 被拒: 无法派生 ExecCtx（{type(exc).__name__}: {exc}）"
                self.denied.append(reason)
                return _Attempt(_failed(spec.name, reason))

            child_e.budget = _clamp_budget(child_e.budget, spec)
            child_e.idempotency_key = self._child_idempotency(parent_e, spec)

            # ③ fork Context：子写自己的副本，父永远不被直接碰
            try:
                child_ctx = parent_ctx.fork()
            except Exception as exc:                        # noqa: BLE001
                return _Attempt(_failed(spec.name,
                                        f"无法 fork 父上下文（{type(exc).__name__}: {exc}）"))
            self._stamp_principal(child_ctx, child_e)

            # ④ 带护栏执行
            guarded = _GuardedDispatcher(self._dispatcher, allowed)
            _emit(parent_e, "subagent.start", {
                "agent": spec.name, "goal_len": len(spec.goal or ""),
                "tools": len(tuple(allowed)), "depth": child_e.depth})
            try:
                raw = self._runner(spec, child_ctx, child_e, guarded, guarded.manifests)
            except Exception as exc:                        # noqa: BLE001 - 子 Agent 崩溃不传染
                result = _failed(spec.name,
                                 f"runner 异常: {type(exc).__name__}: {exc}",
                                 term_reason="UNRECOVERABLE")
                _emit(parent_e, "subagent.finish", {
                    "agent": spec.name, "ok": False, "steps": 0,
                    "term_reason": "UNRECOVERABLE", "tools": len(tuple(allowed))})
                return _Attempt(result, child_ctx, tuple(allowed), tuple(guarded.violations))

            result = _to_subagent_result(raw, spec)
            if result.needs_approval and "tool" not in result.needs_approval:
                # **C3a**：子 Agent 停在审批点 → 从子 Context 取"要批什么"。
                # 父级拿到结构化请求才能放进 `pending_confirmation`，
                # 人也才知道该批准哪一个（否则只能翻日志猜）。
                try:
                    pend = child_ctx.get("pending_confirmation") or {}
                except Exception:                          # noqa: BLE001
                    pend = {}
                if isinstance(pend, Mapping) and pend:
                    result = dataclasses.replace(result, needs_approval={
                        "tool": str(pend.get("tool") or ""),
                        "args_hash": str(pend.get("args_hash") or ""),
                        "reason": str(pend.get("reason") or ""),
                        "step": int(pend.get("step") or 0),
                        "agent": spec.name,
                    })
            if result.needs_approval:
                _emit(parent_e, "subagent.needs_approval", {
                    "agent": spec.name,
                    "tool": str(result.needs_approval.get("tool") or ""),
                })
            _emit(parent_e, "subagent.finish", {
                "agent": spec.name, "ok": bool(getattr(result, "ok", False)),
                "steps": int(getattr(result, "steps", 0) or 0),
                "term_reason": str(getattr(result, "term_reason", "") or ""),
                "tools": len(tuple(allowed))})
            if guarded.violations:
                # 运行期越权优先覆盖结果：这次任务不可信，不得回写
                result = dataclasses.replace(
                    result,
                    ok=False,
                    error=guarded.violations[0],
                    term_reason="ABORTED",
                )
            return _Attempt(result, child_ctx, tuple(allowed), tuple(guarded.violations))

    # ── 工具白名单解析（fail-closed）─────────────────────────
    def _resolve_allowlist(
        self, spec: SubagentSpec
    ) -> tuple[dict[str, ToolManifest] | None, str]:
        """返回 `(允许清单, 拒绝原因)`；清单为 None 表示拒绝派发。"""
        registry = getattr(self._dispatcher, "registry", None)
        if registry is None:
            return None, ("subagent 越权: 取不到 dispatcher.registry，工具清单不可验证"
                          " → fail-closed 拒绝派发")
        getter = getattr(registry, "get", None)
        if not callable(getter):
            return None, ("subagent 越权: registry 没有可用的 get(name)，清单不可验证"
                          " → fail-closed 拒绝派发")

        declared = bool(spec.allowed_tools)
        if declared:
            names = [str(n) for n in spec.allowed_tools]
        else:
            names = _enumerate_tool_names(registry)
            if names is None:
                return None, ("subagent 越权: allowed_tools 为空且无法枚举 registry"
                              " → 只读默认值不可验证，fail-closed 拒绝派发")

        allowed: dict[str, ToolManifest] = {}
        unknown: list[str] = []
        mutating: list[str] = []
        for name in names:
            manifest = _manifest_of(getter(name))
            if manifest is None:
                unknown.append(name)
                continue
            if manifest.side_effect == SideEffect.READ_ONLY:
                allowed[manifest.name] = manifest
            else:
                mutating.append(manifest.name)

        # **记下被降权过滤掉的写工具**（C3a 要用）：子 Agent 拿到的是只读清单，
        # 模型如果去调写工具，只会看到"越权" —— 那不是"它填错了"，是
        # "这个子 Agent 本来就没这个权限"。父级需要知道这一点才能决定
        # "要不要换个有权限的 Agent 重做"或"要不要人来做"。
        self._last_filtered = tuple(mutating)

        if declared and mutating:
            return None, (f"subagent 越权: allowed_tools 声明了非只读工具 {mutating}"
                          "（INV-18 子 Agent 权限只能更窄，mutating 一律拒绝）")
        if declared and unknown:
            return None, (f"subagent 越权: allowed_tools 含未注册工具 {unknown}"
                          " → fail-closed 拒绝派发")
        return allowed, ""

    # ── 上下文：principal 落章 + 白名单回写 ─────────────────
    def _stamp_principal(self, child_ctx: Any, child_e: ExecCtx) -> None:
        """把降权后的 principal 写进子上下文，让 builder/审计看到同一份真相。"""
        apply = getattr(child_ctx, "apply", None)
        if not callable(apply):
            return
        try:
            apply(Patch("set", "principal", Owner.GATEWAY, child_e.principal.to_dict()))
        except Exception:                                   # noqa: BLE001 - 假 Context 可能不收
            pass

    def _merge_back(self, parent_ctx: Any, child_ctx: Any, agent: str) -> list[str]:
        """只把白名单字段拉回父上下文。**子 Agent 的其它一切写入都留在分支里烂掉。**"""
        merged: list[str] = []
        merge = getattr(parent_ctx, "merge", None)
        if callable(merge):
            try:
                patches = merge(child_ctx, self._merge_fields) or []
            except Exception as exc:                        # noqa: BLE001
                self.merge_log.append({"agent": agent, "merged": [],
                                       "error": f"{type(exc).__name__}: {exc}"})
                return []
            merged = [p.path for p in patches]
        else:
            # 退化路径：没有 merge() 的假 Context —— 仍然只写白名单
            apply = getattr(parent_ctx, "apply", None)
            snapshot = getattr(child_ctx, "snapshot", None)
            if callable(apply) and callable(snapshot):
                data = snapshot().data
                for path in self._merge_fields:
                    if path in data:
                        try:
                            apply(Patch("set", path, Owner.LOOP, copy.deepcopy(data[path])))
                            merged.append(path)
                        except Exception:                   # noqa: BLE001
                            continue
        self.merge_log.append({"agent": agent, "merged": merged})
        return merged

    @staticmethod
    def _child_idempotency(parent_e: ExecCtx, spec: SubagentSpec) -> str:
        base = getattr(parent_e, "idempotency_key", "") or getattr(parent_e, "task_id", "")
        return f"{base}:sub:{spec.name}:{uuid.uuid4().hex[:8]}"

    # ── 默认 runner：延迟导入 Loop ───────────────────────────
    def _default_runner(self, spec: SubagentSpec, ctx: Any, e: ExecCtx,
                        dispatcher: Any, tools: Sequence[ToolManifest]) -> Any:
        """真实执行路径。`core.loop` 只在**这里**导入，避免模块级环形依赖。"""
        if self._llm_factory is None:
            raise RuntimeError("SubagentRunner 未注入 llm_factory，无法运行真实子 Agent")
        from core.loop import Loop                            # noqa: PLC0415 - 故意延迟

        llm = self._llm_factory(spec)
        loop = Loop(self._builder, llm, dispatcher, tracer=self._tracer, tools=tools)
        return loop.run(spec.goal, ctx, e)


# ══════════════════════════════════════════════════════════════
# ④ 层次主从 + 死锁看门狗
# ══════════════════════════════════════════════════════════════
class HierarchicalOrchestrator:
    """层次主从：Leader 拆任务 → fan-out → fan-in 聚合。

    看门狗是**外部确定性计数器**，不看模型脸色：
    `len(plan) > max_agents` 或 累计 `steps ≥ max_total_steps` → 立即停 fan-out，
    返回部分结果并标记 `aborted_by_watchdog: True`。
    """

    LEADER = "leader"
    WATCHDOG = "watchdog"

    def __init__(
        self,
        runner: SubagentRunner,
        trace_id: str = "",
        *,
        max_agents: int = DEFAULT_MAX_AGENTS,
        max_total_steps: int = DEFAULT_MAX_TOTAL_STEPS,
        bus: EnvelopeBus | None = None,
    ) -> None:
        if not isinstance(runner, SubagentRunner):
            raise ValueError(f"runner 必须是 SubagentRunner，收到 {type(runner).__name__}")
        self._runner = runner
        self._trace_id = trace_id or ""
        self.max_agents = int(max_agents)
        self.max_total_steps = int(max_total_steps)
        self.bus: EnvelopeBus = bus if bus is not None else EnvelopeBus()
        #: 最近一次聚合结果（方便外部取 watchdog 明细）
        self.last_aggregate: dict = {}

    # ── 运行 ────────────────────────────────────────────────
    def run(self, plan: Sequence[SubagentSpec], parent_ctx: Any, parent_e: ExecCtx) -> dict:
        specs = list(plan)
        trace_id = self._trace_id or getattr(parent_e, "trace_id", "") or uuid.uuid4().hex[:16]
        task_id = getattr(parent_e, "task_id", "")
        leader = self.LEADER

        watchdog = {
            "aborted_by_watchdog": False,
            "reason": "",
            "max_agents": self.max_agents,
            "max_total_steps": self.max_total_steps,
            "plan_size": len(specs),
            "agents_dispatched": 0,
            "steps_used": 0,
        }

        # ── 看门狗 A：plan 规模（fan-out 前静态判定）─────────
        capped = specs
        if len(specs) > self.max_agents:
            capped = specs[: self.max_agents]
            watchdog["aborted_by_watchdog"] = True
            watchdog["reason"] = (
                f"plan 长度 {len(specs)} 超过 max_agents={self.max_agents}，"
                f"只派发前 {self.max_agents} 个"
            )

        results: list[SubagentResult] = []
        steps_used = 0

        for index, spec in enumerate(capped):
            # ── 看门狗 B：外部确定性预算（父级取消 / 硬截止）──
            stop_reason = self._hard_stop(parent_e)
            if stop_reason:
                watchdog["aborted_by_watchdog"] = True
                watchdog["reason"] = watchdog["reason"] or stop_reason
                self._emit_event(trace_id, task_id, stop_reason)
                break

            # ── 看门狗 C：累计步数（外部计数器，不信模型自述）──
            if steps_used >= self.max_total_steps:
                reason = (f"累计 steps={steps_used} 达 max_total_steps="
                          f"{self.max_total_steps}，停止 fan-out")
                watchdog["aborted_by_watchdog"] = True
                watchdog["reason"] = watchdog["reason"] or reason
                self._emit_event(trace_id, task_id, reason)
                break

            # 非法 spec（不是 SubagentSpec）也给一个稳定身份，好让账本对得上；
            # 它会在 runner 里被判 ok=False，但那不该连带炸掉整轮编排。
            agent_name = getattr(spec, "name", "") or f"agent-{index}"

            # 看门狗的另一半：不只是**事后计数**，还要**事前裁剪**。
            # 剩余额度直接写进派发规格，异常 runner 也拿不到超额步数。
            effective = spec
            remaining = self.max_total_steps - steps_used
            if isinstance(spec, SubagentSpec) and remaining > 0:
                effective = dataclasses.replace(
                    spec, max_steps=max(1, min(int(spec.max_steps), remaining)))

            correlation_id = f"{trace_id}:{index}:{agent_name}"
            self._send(correlation_id=correlation_id, trace_id=trace_id, task_id=task_id,
                       message_type=MessageType.REQUEST, sender=leader, recipient=agent_name,
                       payload={"goal": getattr(effective, "goal", ""),
                                "allowed_tools": list(getattr(effective, "allowed_tools", ()) or ()),
                                "max_steps": getattr(effective, "max_steps", 0),
                                "token_budget": getattr(effective, "token_budget", 0),
                                "index": index})

            result = self._runner.dispatch(effective, parent_ctx, parent_e)
            steps_used += max(0, int(getattr(result, "steps", 0) or 0))
            results.append(result)

            self._send(correlation_id=correlation_id, trace_id=trace_id, task_id=task_id,
                       message_type=MessageType.RESPONSE if result.ok else MessageType.ERROR,
                       sender=agent_name, recipient=leader,
                       payload=result.to_dict())

        # 收尾：超过上限也算越界（结果已经落地，但账本要诚实）
        if steps_used > self.max_total_steps:
            watchdog["aborted_by_watchdog"] = True
            watchdog["reason"] = watchdog["reason"] or (
                f"累计 steps={steps_used} 超过 max_total_steps={self.max_total_steps}"
            )

        watchdog["agents_dispatched"] = len(results)
        watchdog["steps_used"] = steps_used

        ok = sum(1 for r in results if r.ok)
        aggregate = {
            "ok": ok,
            "failed": len(results) - ok,
            "results": [r.to_dict() for r in results],
            "watchdog": watchdog,
            "aborted_by_watchdog": watchdog["aborted_by_watchdog"],
            "trace_id": trace_id,
        }
        self.last_aggregate = aggregate
        return aggregate

    # ── 内部 ────────────────────────────────────────────────
    @staticmethod
    def _hard_stop(parent_e: ExecCtx) -> str:
        if bool(getattr(parent_e, "aborted", False)):
            return "父 ExecCtx 已取消（aborted），停止 fan-out"
        checker = getattr(parent_e, "deadline_passed", None)
        if callable(checker):
            try:
                if checker():
                    return "父 ExecCtx 已过 deadline，停止 fan-out"
            except Exception:                               # noqa: BLE001
                pass
        return ""

    def _send(self, *, correlation_id: str, trace_id: str, task_id: str,
              message_type: MessageType, sender: str, recipient: str,
              payload: dict) -> None:
        self.bus.send(AgentEnvelope(
            trace_id=trace_id,
            message_type=message_type,
            sender=sender,
            recipient=recipient,
            payload=payload,
            task_id=task_id,
            correlation_id=correlation_id,
        ))

    def _emit_event(self, trace_id: str, task_id: str, reason: str) -> None:
        """看门狗的裁决也必须进审计轨迹：谁叫停的、为什么叫停的。"""
        self._send(correlation_id=f"{trace_id}:watchdog", trace_id=trace_id,
                   task_id=task_id, message_type=MessageType.EVENT,
                   sender=self.WATCHDOG, recipient=self.LEADER,
                   payload={"reason": reason,
                            "max_agents": self.max_agents,
                            "max_total_steps": self.max_total_steps})


# ══════════════════════════════════════════════════════════════
# 模块内纯函数
# ══════════════════════════════════════════════════════════════
def _emit(e: Any, kind: str, data: dict) -> None:
    """进度播报。**fail-open**：播报坏了绝不能影响执行。

    为什么静默吞异常：`ProgressSink` 是观测面，它的实现由接入层提供
    （队列 / 日志 / 自定义）。任何观测面的异常都不该让子 Agent 失败 ——
    那是把"看得到"和"跑得通"绑死，方向反了。
    """
    sink = getattr(e, "progress", None)
    if sink is None:
        return
    try:
        sink.emit(kind, data)
    except Exception:                                          # noqa: BLE001
        pass


def _failed(name: str, error: str, term_reason: str = "ABORTED") -> SubagentResult:
    return SubagentResult(name=name, ok=False, error=error, term_reason=term_reason)


def _clamp_budget(budget: Budget, spec: SubagentSpec) -> Budget:
    """在 `child()` 切半的基础上再按 spec 收紧。**只许变小。**"""
    clamped = copy.deepcopy(budget)
    try:
        clamped.max_steps = max(1, min(int(clamped.max_steps), int(spec.max_steps)))
    except (TypeError, ValueError):
        pass
    try:
        clamped.token_total = max(1, min(int(clamped.token_total), int(spec.token_budget)))
    except (TypeError, ValueError):
        pass
    return clamped


def _manifest_of(entry: Any) -> ToolManifest | None:
    """registry.get() 的返回值可能是 (manifest, fn) / manifest / None。"""
    if entry is None:
        return None
    if isinstance(entry, ToolManifest):
        return entry
    if isinstance(entry, (tuple, list)) and entry:
        first = entry[0]
        if isinstance(first, ToolManifest):
            return first
    return None


def _enumerate_tool_names(registry: Any) -> list[str] | None:
    """尽力枚举 registry 的工具名；枚举不到返回 None（调用方 fail-closed）。"""
    manifests_attr = getattr(registry, "manifests", None)
    if callable(manifests_attr):
        try:
            manifests = manifests_attr()
        except Exception:                                   # noqa: BLE001
            manifests = None
        if isinstance(manifests, (list, tuple)):
            return [m.name for m in manifests if isinstance(m, ToolManifest)]
    elif isinstance(manifests_attr, (list, tuple)):
        return [m.name for m in manifests_attr if isinstance(m, ToolManifest)]

    names_attr = getattr(registry, "names", None)
    if callable(names_attr):
        try:
            names = names_attr()
        except Exception:                                   # noqa: BLE001
            names = None
        if isinstance(names, (list, tuple)):
            return [str(n) for n in names]
    return None


def _to_subagent_result(raw: Any, spec: SubagentSpec) -> SubagentResult:
    """把 runner 的返回值（Result / SubagentResult / dict）收口成 SubagentResult。"""
    if isinstance(raw, SubagentResult):
        return raw if raw.name else dataclasses.replace(raw, name=spec.name)

    if isinstance(raw, dict):
        text = str(raw.get("text") or raw.get("summary") or "")
        ok = bool(raw.get("ok", False))
        return SubagentResult(
            name=str(raw.get("name") or spec.name),
            ok=ok,
            summary=text[:_SUMMARY_LIMIT],
            artifacts=tuple(raw.get("artifacts") or ()),
            steps=int(raw.get("steps") or 0),
            term_reason=str(raw.get("term_reason") or ("DONE" if ok else "ABORTED")),
            error=str(raw.get("error") or ""),
        )

    if isinstance(raw, Result):
        phase = _enum_value(raw.phase)
        term = _enum_value(raw.term_reason)
        ok = phase == "DONE" and term == "DONE" and not raw.partial
        # **C3a**：子 Agent 停在审批点 → 把"要批什么"结构化带出去。
        # 不这么做的话父级只能看到 ok=False，和"跑挂了"无法区分，
        # 人也不知道该批哪一个。
        # 审批详情在子 Context 的 `pending_confirmation` 里（不在 Result 上），
        # 由调用方 `_attempt` 补齐 —— 这里只标出"是审批停的"。
        pending: dict = {"need": True} if term == "NEEDS_CONFIRMATION" else {}
        return SubagentResult(
            name=spec.name,
            ok=ok,
            summary=(raw.text or "")[:_SUMMARY_LIMIT],
            artifacts=tuple(raw.artifacts or ()),
            steps=int(raw.steps or 0),
            term_reason=term,
            error="" if ok else f"subagent 未完成: phase={phase} term={term}",
            needs_approval=pending,
        )

    return _failed(spec.name, f"runner 返回了无法识别的类型 {type(raw).__name__}",
                   term_reason="UNRECOVERABLE")
