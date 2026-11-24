"""tests/test_workflow.py — W9 Workflow Plane 的自带测试（stdlib unittest，零第三方依赖）。

跑法：
    cd /Users/supre/Documents/tmp/agent-runtime && python3 -m unittest tests.test_workflow -v

覆盖的四个立面：
- ① 强类型信元：校验/定序/排序/消费/审计
- ② 子 Agent 降权（INV-18）：principal、depth、上下文隔离、白名单回写、越权拦截、
    默认只读、fan-out 保序
- ③ Saga 补偿：成功/失败/逆序/补偿自身异常/无 compensate/reset
- ④ 层次主从 + 看门狗：正常聚合、max_agents、max_total_steps、REQUEST→RESPONSE 配对

约定：只依赖 contracts.py + core.context 的 fork/merge/snapshot，其余全部 fake；
**不 import core.loop**（runner 注入），所以 W2 的实现怎么改都不影响本文件。
"""
from __future__ import annotations

import os
import sys
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from contracts import (  # noqa: E402
    AgentEnvelope,
    ExecCtx,
    MessageType,
    Owner,
    Patch,
    Phase,
    Result,
    RiskTier,
    SagaStep,
    SideEffect,
    SubagentSpec,
    TermReason,
    ToolManifest,
    ToolResult,
    ToolOutcome,
    Budget,
)
from core.context import ContextHandle  # noqa: E402  只用 fork()/merge()/snapshot()
from plane.workflow import (  # noqa: E402
    EnvelopeBus,
    HierarchicalOrchestrator,
    Saga,
    SubagentRunner,
)


# ══════════════════════════════════════════════════════════════
# Fake 基础设施（不依赖任何其他 worker 的模块）
# ══════════════════════════════════════════════════════════════
def make_env(**overrides) -> AgentEnvelope:
    base = dict(
        trace_id="trace-1",
        message_type=MessageType.REQUEST,
        sender="leader",
        recipient="worker-1",
        payload={"goal": "x"},
    )
    base.update(overrides)
    return AgentEnvelope(**base)


class FakeRegistry:
    """最小 registry：`get(name) -> (manifest, fn)` + `manifests()`。"""

    def __init__(self) -> None:
        self._tools: dict[str, tuple[ToolManifest, object]] = {}

    def add(self, manifest: ToolManifest, fn=None) -> "FakeRegistry":
        self._tools[manifest.name] = (manifest, fn or (lambda args, e: {"echo": args}))
        return self

    def get(self, name: str):
        return self._tools.get(name)

    def manifests(self) -> list[ToolManifest]:
        return [m for m, _ in self._tools.values()]


class FakeDispatcher:
    """只记录调用并回包，不实现 tool.py 的任何策略。"""

    def __init__(self, registry: FakeRegistry | None = None) -> None:
        self.registry = registry if registry is not None else FakeRegistry()
        self.invoked: list[dict] = []

    def invoke(self, name: str, args: dict, e: ExecCtx) -> ToolOutcome:
        entry = self.registry.get(name)
        if entry is None:
            return ToolOutcome.failed(f"unknown tool: {name}")
        manifest, fn = entry
        self.invoked.append({"name": name, "args": args, "depth": e.depth})
        payload = fn(args, e)
        return ToolOutcome.ok(ToolResult(tool=name, payload=payload))


class FakeRunner:
    """假的子 Agent 执行体，直接替代 Loop。

    记录它看到的一切（这是验证降权是否真的生效的唯一证据来源）。
    """

    def __init__(
        self,
        *,
        writes: tuple[Patch, ...] = (),
        tool_calls: tuple[tuple[str, dict], ...] = (),
        steps: int = 1,
        ok: bool = True,
        delay: float = 0.0,
        text: str = "done",
    ) -> None:
        self.writes = writes
        self.tool_calls = tool_calls
        self.steps = steps
        self.ok = ok
        self.text = text
        self.delay_by_name: dict[str, float] = {}
        self.default_delay = delay
        self.seen: list[dict] = []
        self.outcomes: list[tuple[str, ToolOutcome]] = []

    def __call__(self, spec, ctx, e, dispatcher, tools):
        self.seen.append({
            "spec_name": spec.name,
            "spec_max_steps": spec.max_steps,
            "depth": e.depth,
            "principal": e.principal,
            "scopes": set(e.principal.scopes),
            "budget": e.budget,
            "tools": [t.name for t in tools],
            "ctx_id": id(ctx),
            "has_registry": hasattr(dispatcher, "registry"),
            "ctx_principal": ctx.get("principal"),
        })
        wait = self.delay_by_name.get(spec.name, self.default_delay)
        if wait:
            time.sleep(wait)
        for name, args in self.tool_calls:
            self.outcomes.append((name, dispatcher.invoke(name, args, e)))
        for patch in self.writes:
            ctx.apply(patch)
        if not self.ok:
            return Result(phase=Phase.FAILED, term_reason=TermReason.UNRECOVERABLE,
                          text="boom", steps=self.steps)
        return Result(phase=Phase.DONE, term_reason=TermReason.DONE,
                      text=self.text, steps=self.steps,
                      artifacts=(dict(spec_name=spec.name),))


def build_runner(fake_runner: FakeRunner, **kwargs) -> SubagentRunner:
    return SubagentRunner(FakeDispatcher(), builder=object(), runner=fake_runner, **kwargs)


def fresh_ctx(**initial) -> ContextHandle:
    return ContextHandle(initial=dict(initial) if initial else None)


def read_manifest(name: str = "read_note", **kw) -> ToolManifest:
    return ToolManifest(name=name, side_effect=SideEffect.READ_ONLY,
                        risk_tier=RiskTier.T0_SAFE, required_scope="tool.read", **kw)


def write_manifest(name: str = "write_note") -> ToolManifest:
    return ToolManifest(name=name, side_effect=SideEffect.MUTATING,
                        risk_tier=RiskTier.T2_HIGH, required_scope="tool.write")


# ══════════════════════════════════════════════════════════════
# ① 强类型信元
# ══════════════════════════════════════════════════════════════
class TestEnvelopeBus(unittest.TestCase):
    def test_rejects_empty_trace_id(self):
        bus = EnvelopeBus()
        with self.assertRaises(ValueError):
            bus.send(make_env(trace_id=""))
        with self.assertRaises(ValueError):
            bus.send(make_env(trace_id="   "))
        self.assertEqual(bus.log(), [])

    def test_rejects_non_dict_payload(self):
        bus = EnvelopeBus()
        for bad in ("hello 帮我干活", ["a"], None, 42):
            with self.assertRaises(ValueError):
                bus.send(make_env(payload=bad))

    def test_rejects_empty_sender_or_recipient(self):
        bus = EnvelopeBus()
        with self.assertRaises(ValueError):
            bus.send(make_env(sender=""))
        with self.assertRaises(ValueError):
            bus.send(make_env(recipient="  "))
        with self.assertRaises(ValueError):
            bus.send({"trace_id": "t"})          # 非 AgentEnvelope 一律不收

    def test_rejects_non_message_type(self):
        bus = EnvelopeBus()
        with self.assertRaises(ValueError):
            bus.send(make_env(message_type="REQUEST"))   # 裸字符串不是强类型

    def test_seq_auto_increments_and_monotonic_per_trace(self):
        bus = EnvelopeBus()
        bus.send(make_env(recipient="w1"))
        bus.send(make_env(recipient="w1"))
        bus.send(make_env(recipient="w2", trace_id="trace-1"))
        bus.send(make_env(recipient="w1", trace_id="trace-2"))   # 另一条 trace 重新计数

        seqs = [e.seq for e in bus.inbox("w1") if e.trace_id == "trace-1"]
        self.assertEqual(seqs, [1, 2])
        self.assertEqual([e.seq for e in bus.inbox("w2")], [3])   # 同一 trace 共用计数器
        # 调用方填了 seq 也会被总线覆盖：序号是总线的账本
        bus.send(make_env(recipient="w3", seq=999))
        self.assertEqual(bus.inbox("w3")[0].seq, 4)
        self.assertEqual(bus.seq_of("trace-2"), 1)

    def test_inbox_sorted_by_created_at_seq(self):
        bus = EnvelopeBus()
        for i in range(5):
            bus.send(make_env(recipient="w1", payload={"i": i}))
        box = bus.inbox("w1")
        self.assertEqual([e.payload["i"] for e in box], [0, 1, 2, 3, 4])
        self.assertEqual([e.seq for e in box], sorted(e.seq for e in box))
        keyed = [(e.created_at, e.seq) for e in box]
        self.assertEqual(keyed, sorted(keyed))
        # inbox 是只读的：读两次拿到的长度一样
        self.assertEqual(len(bus.inbox("w1")), 5)

    def test_drain_empties_inbox_but_keeps_log(self):
        bus = EnvelopeBus()
        bus.send(make_env(recipient="w1"))
        bus.send(make_env(recipient="w1"))
        drained = bus.drain("w1")
        self.assertEqual(len(drained), 2)
        self.assertEqual(bus.inbox("w1"), [])
        self.assertEqual(len(bus.log()), 2)      # 审计轨迹不因消费而消失
        self.assertEqual(bus.drain("w1"), [])

    def test_log_is_full_audit_trail(self):
        bus = EnvelopeBus()
        bus.send(make_env(correlation_id="c1"))
        entry = bus.log()[0]
        self.assertEqual(entry["correlation_id"], "c1")
        self.assertEqual(entry["message_type"], "REQUEST")
        self.assertIn("created_at", entry)
        # 改返回的 dict 不影响总线内部
        entry["payload"]["goal"] = "tampered"
        self.assertEqual(bus.log()[0]["payload"]["goal"], "x")


# ══════════════════════════════════════════════════════════════
# ② Saga 补偿
# ══════════════════════════════════════════════════════════════
class TestSaga(unittest.TestCase):
    def test_all_success(self):
        calls: list[str] = []
        saga = Saga("happy")
        for name in ("a", "b", "c"):
            saga.add(SagaStep(name=name,
                              action=lambda n=name: calls.append(n) or f"r-{n}",
                              compensate=lambda n=name: calls.append(f"undo-{n}")))
        out = saga.run()
        self.assertTrue(out["ok"])
        self.assertEqual(out["completed"], ["a", "b", "c"])
        self.assertEqual(out["compensated"], [])
        self.assertEqual(out["error"], "")
        self.assertEqual(calls, ["a", "b", "c"])     # 补偿没被误触发

    def test_second_step_failure_compensates_first_only(self):
        calls: list[str] = []
        saga = Saga("fail@2")
        saga.add(SagaStep(name="s1", action=lambda: calls.append("s1"),
                          compensate=lambda: calls.append("undo-s1")))
        saga.add(SagaStep(name="s2", action=lambda: (_ for _ in ()).throw(RuntimeError("boom")),
                          compensate=lambda: calls.append("undo-s2")))
        saga.add(SagaStep(name="s3", action=lambda: calls.append("s3"),
                          compensate=lambda: calls.append("undo-s3")))
        out = saga.run()
        self.assertFalse(out["ok"])
        self.assertEqual(out["completed"], ["s1"])
        self.assertEqual(out["compensated"], ["s1"])
        self.assertNotIn("s3", calls)                 # 第 3 步根本没执行
        self.assertNotIn("undo-s2", calls)            # 失败步骤自己不需要补偿
        self.assertIn("boom", out["error"])

    def test_compensation_is_reverse_order(self):
        order: list[str] = []
        saga = Saga("fail@4")
        for name in ("1", "2", "3"):
            saga.add(SagaStep(name=name, action=lambda: None,
                              compensate=lambda n=name: order.append(n)))
        saga.add(SagaStep(name="4", action=lambda: (_ for _ in ()).throw(ValueError("nope"))))
        out = saga.run()
        self.assertFalse(out["ok"])
        self.assertEqual(out["completed"], ["1", "2", "3"])
        self.assertEqual(order, ["3", "2", "1"])          # 逆序，铁的
        self.assertEqual(out["compensated"], ["3", "2", "1"])

    def test_compensate_exception_does_not_stop_others(self):
        order: list[str] = []

        def boom():
            order.append("c2")
            raise RuntimeError("compensate blew up")

        saga = Saga("comp-boom")
        saga.add(SagaStep(name="a", action=lambda: None,
                          compensate=lambda: order.append("c1")))
        saga.add(SagaStep(name="b", action=lambda: None, compensate=boom))
        saga.add(SagaStep(name="c", action=lambda: None,
                          compensate=lambda: order.append("c3")))
        saga.add(SagaStep(name="d", action=lambda: (_ for _ in ()).throw(KeyError("k"))))
        out = saga.run()
        self.assertFalse(out["ok"])
        self.assertEqual(order, ["c3", "c2", "c1"])       # c2 抛了，c1 照样补偿
        self.assertEqual(out["compensated"], ["c", "a"])
        self.assertIn("compensate b failed", out["error"])
        self.assertIn("compensate blew up", out["error"])

    def test_step_without_compensate_is_noted_not_blocking(self):
        order: list[str] = []
        saga = Saga("no-comp")
        saga.add(SagaStep(name="A", action=lambda: None))            # 无补偿
        saga.add(SagaStep(name="B", action=lambda: None,
                          compensate=lambda: order.append("undo-B")))
        saga.add(SagaStep(name="C", action=lambda: (_ for _ in ()).throw(RuntimeError("x"))))
        out = saga.run()
        self.assertFalse(out["ok"])
        self.assertEqual(out["compensated"], ["B"])
        self.assertEqual(order, ["undo-B"])
        self.assertIn("no_compensate:A", saga.notes)
        self.assertIn("no_compensate:A", out["error"])

    def test_rejects_bad_step_and_duplicate_name(self):
        saga = Saga()
        with self.assertRaises(ValueError):
            saga.add("not a step")
        with self.assertRaises(ValueError):
            saga.add(SagaStep(name="", action=lambda: None))
        saga.add(SagaStep(name="dup", action=lambda: None))
        with self.assertRaises(ValueError):
            saga.add(SagaStep(name="dup", action=lambda: None))

    def test_reset_allows_rerun(self):
        attempts: list[int] = []

        def flaky():
            attempts.append(1)
            if len(attempts) == 1:
                raise RuntimeError("first time fails")
            return "ok"

        saga = Saga("rerun")
        saga.add(SagaStep(name="x", action=flaky))
        self.assertFalse(saga.run()["ok"])
        saga.reset()
        second = saga.run()
        self.assertTrue(second["ok"])
        self.assertEqual(second["completed"], ["x"])
        self.assertEqual(saga.notes, [])


# ══════════════════════════════════════════════════════════════
# ③ 子 Agent 降权（INV-18）
# ══════════════════════════════════════════════════════════════
class TestSubagentDowngrade(unittest.TestCase):
    def setUp(self):
        self.registry = FakeRegistry().add(read_manifest()).add(write_manifest())
        self.dispatcher = FakeDispatcher(self.registry)

    def runner_for(self, fake: FakeRunner, **kw) -> SubagentRunner:
        return SubagentRunner(self.dispatcher, builder=object(), runner=fake, **kw)

    def test_principal_downgraded_to_read_scope_only(self):
        fake = FakeRunner()
        runner = self.runner_for(fake)
        ctx = fresh_ctx()
        parent_e = ExecCtx()          # 默认 principal 含 tool.read + tool.write
        res = runner.dispatch(SubagentSpec(name="w", goal="g"), ctx, parent_e)
        self.assertTrue(res.ok)
        seen = fake.seen[0]
        self.assertEqual(seen["scopes"], {"tool.read"})
        self.assertTrue(all(s.endswith(".read") for s in seen["scopes"]))
        # 子上下文里落章的是同一份降权主体（父上下文不留痕：principal 不在回写白名单）
        self.assertEqual(seen["ctx_principal"]["scopes"], ["tool.read"])
        self.assertIsNone(ctx.get("principal"))

    def test_depth_increments_and_max_depth_rejects(self):
        fake = FakeRunner()
        runner = self.runner_for(fake)
        ctx = fresh_ctx()
        res = runner.dispatch(SubagentSpec(name="a", goal="g"), ctx, ExecCtx(depth=0))
        self.assertTrue(res.ok)
        self.assertEqual(fake.seen[0]["depth"], 1)

        deep_parent = ExecCtx(depth=2)
        res2 = runner.dispatch(SubagentSpec(name="b", goal="g"), fresh_ctx(), deep_parent)
        self.assertFalse(res2.ok)
        self.assertIn("depth", res2.error)
        self.assertEqual(len(fake.seen), 1)           # 被拒的那次根本没跑

    def test_child_context_isolated_from_parent(self):
        fake = FakeRunner(writes=(
            Patch("append", "observations", Owner.LOOP, {"child": True}),
        ), ok=False)                                   # 不 ok → 不回写
        runner = self.runner_for(fake)
        ctx = fresh_ctx()
        ctx.apply(Patch("append", "observations", Owner.LOOP, {"parent": True}))
        before = ctx.serialize()
        res = runner.dispatch(SubagentSpec(name="iso", goal="g"), ctx, ExecCtx())
        self.assertFalse(res.ok)
        self.assertEqual(ctx.serialize(), before)      # 父完全没被污染
        self.assertEqual(runner.merge_log, [])         # 不 ok → 连 merge 都不发生

    def test_merge_back_only_whitelisted_fields(self):
        fake = FakeRunner(writes=(
            Patch("append", "observations", Owner.LOOP, {"from": "child"}),
            Patch("set", "artifacts", Owner.NORMALIZER, [{"a": 1}]),
            Patch("append", "history", Owner.LOOP, {"secret": "child-only"}),
        ))
        runner = self.runner_for(fake)
        ctx = fresh_ctx()
        ctx.apply(Patch("append", "observations", Owner.LOOP, {"from": "parent"}))
        res = runner.dispatch(SubagentSpec(name="merge", goal="g"), ctx, ExecCtx())
        self.assertTrue(res.ok)
        snap = ctx.snapshot()
        self.assertEqual([o["from"] for o in snap.get("observations")], ["parent", "child"])
        self.assertEqual(snap.get("artifacts"), [{"a": 1}])
        self.assertIsNone(snap.get("history"))         # 白名单外的写入没回来
        self.assertEqual(sorted(runner.merge_log[-1]["merged"]), ["artifacts", "observations"])

    def test_declared_mutating_tool_rejected_before_execution(self):
        fake = FakeRunner()
        runner = self.runner_for(fake)
        res = runner.dispatch(
            SubagentSpec(name="evil", goal="g", allowed_tools=("write_note",)),
            fresh_ctx(), ExecCtx())
        self.assertFalse(res.ok)
        self.assertIn("越权", res.error)
        self.assertIn("write_note", res.error)
        self.assertEqual(fake.seen, [])                # 执行前就拒了
        self.assertEqual(self.dispatcher.invoked, [])
        self.assertTrue(any("越权" in d for d in runner.denied))

    def test_default_read_only_blocks_mutation_allows_read(self):
        # 只读默认：mutating 工具被拦
        mutating = FakeRunner(tool_calls=(("write_note", {"v": 1}),))
        runner = self.runner_for(mutating)
        res = runner.dispatch(SubagentSpec(name="m", goal="g"), fresh_ctx(), ExecCtx())
        self.assertFalse(res.ok)
        self.assertIn("越权", res.error)
        self.assertEqual(self.dispatcher.invoked, [])
        self.assertEqual(mutating.outcomes[0][1].kind, "denied")
        self.assertEqual(mutating.seen[0]["tools"], ["read_note"])

        # read_only 通过，且真的打到了 Dispatcher
        reading = FakeRunner(tool_calls=(("read_note", {"id": 7}),))
        runner2 = self.runner_for(reading)
        res2 = runner2.dispatch(SubagentSpec(name="r", goal="g"), fresh_ctx(), ExecCtx())
        self.assertTrue(res2.ok)
        self.assertEqual([c["name"] for c in self.dispatcher.invoked], ["read_note"])
        self.assertEqual(reading.outcomes[0][1].kind, "ok")

    def test_guarded_dispatcher_hides_registry(self):
        fake = FakeRunner()
        runner = self.runner_for(fake)
        runner.dispatch(SubagentSpec(name="h", goal="g"), fresh_ctx(), ExecCtx())
        self.assertFalse(fake.seen[0]["has_registry"])   # 子 Agent 无权枚举全量工具
        self.assertEqual(fake.seen[0]["tools"], ["read_note"])   # 清单也已收窄

    def test_unknown_allowed_tool_fails_closed(self):
        fake = FakeRunner()
        runner = self.runner_for(fake)
        res = runner.dispatch(
            SubagentSpec(name="u", goal="g", allowed_tools=("hallucinated_tool",)),
            fresh_ctx(), ExecCtx())
        self.assertFalse(res.ok)
        self.assertIn("越权", res.error)
        self.assertEqual(fake.seen, [])

    def test_missing_registry_fails_closed(self):
        fake = FakeRunner()
        runner = SubagentRunner(object(), builder=object(), runner=fake)
        res = runner.dispatch(SubagentSpec(name="n", goal="g"), fresh_ctx(), ExecCtx())
        self.assertFalse(res.ok)
        self.assertIn("fail-closed", res.error)
        self.assertEqual(fake.seen, [])

    def test_registry_without_manifests_fails_closed_for_empty_allowlist(self):
        class NoEnumRegistry:
            def get(self, name):
                return None

        class Disp:
            registry = NoEnumRegistry()

            def invoke(self, name, args, e):
                return ToolOutcome.failed("never")

        fake = FakeRunner()
        runner = SubagentRunner(Disp(), builder=object(), runner=fake)
        res = runner.dispatch(SubagentSpec(name="ne", goal="g"), fresh_ctx(), ExecCtx())
        self.assertFalse(res.ok)
        self.assertIn("fail-closed", res.error)
        self.assertEqual(fake.seen, [])

    def test_budget_clamped_by_spec(self):
        fake = FakeRunner()
        runner = self.runner_for(fake)
        parent_e = ExecCtx(budget=Budget(token_total=20000, max_steps=8))
        runner.dispatch(SubagentSpec(name="b", goal="g", max_steps=2, token_budget=1000),
                        fresh_ctx(), parent_e)
        budget = fake.seen[0]["budget"]
        self.assertEqual(budget.max_steps, 2)
        self.assertEqual(budget.token_total, 1000)
        # 子预算只能更小：父预算没被改动
        self.assertEqual(parent_e.budget.max_steps, 8)

    def test_runner_exception_does_not_escape(self):
        def exploding(spec, ctx, e, dispatcher, tools):
            raise RuntimeError("child crashed")

        runner = SubagentRunner(self.dispatcher, builder=object(), runner=exploding)
        res = runner.dispatch(SubagentSpec(name="x", goal="g"), fresh_ctx(), ExecCtx())
        self.assertFalse(res.ok)
        self.assertIn("child crashed", res.error)
        self.assertEqual(res.term_reason, "UNRECOVERABLE")

    def test_dispatch_many_preserves_input_order(self):
        fake = FakeRunner(steps=2)
        fake.delay_by_name = {"a0": 0.06, "a1": 0.03, "a2": 0.0}   # 完成顺序被故意打乱
        runner = self.runner_for(fake)
        specs = [SubagentSpec(name=f"a{i}", goal="g") for i in range(3)]
        results = runner.dispatch_many(specs, fresh_ctx(), ExecCtx(), max_workers=3)
        self.assertEqual([r.name for r in results], ["a0", "a1", "a2"])
        self.assertTrue(all(r.ok for r in results))
        self.assertEqual(len(results), 3)


# ══════════════════════════════════════════════════════════════
# ④ 层次主从 + 死锁看门狗
# ══════════════════════════════════════════════════════════════
class TestHierarchicalOrchestrator(unittest.TestCase):
    def setUp(self):
        registry = FakeRegistry().add(read_manifest()).add(write_manifest())
        self.dispatcher = FakeDispatcher(registry)

    def make_orchestrator(self, fake: FakeRunner, **kw) -> HierarchicalOrchestrator:
        runner = SubagentRunner(self.dispatcher, builder=object(), runner=fake)
        return HierarchicalOrchestrator(runner, trace_id="trace-orch", **kw)

    def test_three_agents_all_ok(self):
        fake = FakeRunner(steps=2)
        orch = self.make_orchestrator(fake)
        plan = [SubagentSpec(name=f"w{i}", goal=f"g{i}") for i in range(3)]
        agg = orch.run(plan, fresh_ctx(), ExecCtx())
        self.assertEqual(agg["ok"], 3)
        self.assertEqual(agg["failed"], 0)
        self.assertEqual(len(agg["results"]), 3)
        self.assertFalse(agg["aborted_by_watchdog"])
        self.assertEqual(agg["watchdog"]["steps_used"], 6)
        self.assertEqual(agg["results"][0]["name"], "w0")

    def test_watchdog_aborts_when_plan_exceeds_max_agents(self):
        fake = FakeRunner(steps=1)
        orch = self.make_orchestrator(fake, max_agents=3)
        plan = [SubagentSpec(name=f"w{i}", goal="g") for i in range(7)]
        agg = orch.run(plan, fresh_ctx(), ExecCtx())
        self.assertTrue(agg["aborted_by_watchdog"])
        self.assertTrue(agg["watchdog"]["aborted_by_watchdog"])
        self.assertIn("max_agents", agg["watchdog"]["reason"])
        self.assertEqual(agg["watchdog"]["agents_dispatched"], 3)
        self.assertEqual(len(agg["results"]), 3)
        self.assertEqual(len(fake.seen), 3)            # 剩下的确实没派发

    def test_watchdog_aborts_on_total_steps(self):
        fake = FakeRunner(steps=3)
        orch = self.make_orchestrator(fake, max_total_steps=5)
        plan = [SubagentSpec(name=f"w{i}", goal="g") for i in range(4)]
        agg = orch.run(plan, fresh_ctx(), ExecCtx())
        self.assertTrue(agg["aborted_by_watchdog"])
        self.assertEqual(agg["watchdog"]["agents_dispatched"], 2)   # 3 → 6 ≥ 5 后停
        self.assertEqual([r["name"] for r in agg["results"]], ["w0", "w1"])
        self.assertEqual(agg["watchdog"]["steps_used"], 6)
        self.assertIn("max_total_steps", agg["watchdog"]["reason"])
        self.assertEqual(len(fake.seen), 2)

    def test_watchdog_clips_remaining_steps_before_dispatch(self):
        """事前裁剪：剩余额度写进派发规格，异常 runner 也拿不到超额步数。"""
        fake = FakeRunner(steps=3)
        orch = self.make_orchestrator(fake, max_total_steps=5)
        plan = [SubagentSpec(name=f"w{i}", goal="g", max_steps=4) for i in range(3)]
        orch.run(plan, fresh_ctx(), ExecCtx())
        requests = [e for e in orch.bus.log() if e["message_type"] == "REQUEST"]
        self.assertEqual([r["payload"]["max_steps"] for r in requests], [4, 2])
        self.assertEqual([s["spec_name"] for s in fake.seen], ["w0", "w1"])
        # 裁剪确实传到了 runner，而不只是写在一封信里
        self.assertEqual([s["spec_max_steps"] for s in fake.seen], [4, 2])

    def test_failed_subagent_counted_as_failed(self):
        fake = FakeRunner(ok=False, steps=1)
        orch = self.make_orchestrator(fake)
        agg = orch.run([SubagentSpec(name="w0", goal="g")], fresh_ctx(), ExecCtx())
        self.assertEqual(agg["ok"], 0)
        self.assertEqual(agg["failed"], 1)
        self.assertFalse(agg["results"][0]["ok"])

    def test_aborted_parent_stops_fanout(self):
        fake = FakeRunner()
        orch = self.make_orchestrator(fake)
        parent_e = ExecCtx()
        parent_e.abort()
        agg = orch.run([SubagentSpec(name="w0", goal="g")], fresh_ctx(), parent_e)
        self.assertTrue(agg["aborted_by_watchdog"])
        self.assertEqual(agg["results"], [])
        self.assertEqual(fake.seen, [])
        # 看门狗的裁决必须留痕（EVENT 信元）
        self.assertTrue(any(e["sender"] == "watchdog" for e in orch.bus.log()))

    def test_end_to_end_envelope_request_response_pairing(self):
        fake = FakeRunner(steps=1)
        orch = self.make_orchestrator(fake)
        plan = [SubagentSpec(name=f"w{i}", goal=f"goal-{i}") for i in range(3)]
        agg = orch.run(plan, fresh_ctx(), ExecCtx())

        log = orch.bus.log()
        self.assertEqual(len(log), 6)                  # 3 × (REQUEST + RESPONSE)
        for env in log:                                # 全程强类型，无自然语言 payload
            self.assertIsInstance(env["payload"], dict)
            self.assertTrue(env["trace_id"])

        requested = [e for e in log if e["message_type"] == "REQUEST"]
        responded = [e for e in log if e["message_type"] in ("RESPONSE", "ERROR")]
        self.assertEqual(len(requested), 3)
        self.assertEqual(len(responded), 3)

        for req, resp in zip(requested, responded):
            self.assertEqual(req["correlation_id"], resp["correlation_id"])
            self.assertEqual(req["sender"], "leader")
            self.assertEqual(req["recipient"], resp["sender"])
            self.assertEqual(resp["recipient"], "leader")
            self.assertEqual(req["payload"]["goal"], f"goal-{req['recipient'][1:]}")
            self.assertEqual(resp["payload"]["name"], req["recipient"])
            self.assertLess(req["seq"], resp["seq"])   # 同一 trace 内单调

        self.assertEqual(agg["trace_id"], "trace-orch")
        # 收件箱视图：leader 手上正好是 3 条回执
        self.assertEqual(orch.bus.count("leader", MessageType.RESPONSE), 3)

    def test_plan_not_a_subagent_spec_still_isolated(self):
        """非法 spec 不得炸掉整轮编排：坏的那个 ok=False，好的照跑。"""
        fake = FakeRunner(steps=1)
        orch = self.make_orchestrator(fake)
        agg = orch.run([SubagentSpec(name="good", goal="g"), "junk"],
                       fresh_ctx(), ExecCtx())
        self.assertEqual(agg["ok"], 1)
        self.assertEqual(agg["failed"], 1)
        self.assertEqual(len(agg["results"]), 2)


if __name__ == "__main__":  # pragma: no cover
    unittest.main(verbosity=2)
