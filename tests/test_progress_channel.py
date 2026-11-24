"""C2 验收：子 Agent 进度冒泡到流式端点。

为什么需要这条通道：子 Agent 有自己的 Context，父层的 `patch_log` 看不见它。
没有它，子 Agent 跑 10 步客户端一个字节都收不到，只在最后收到聚合结果。

三条硬约束：
  1. 进度**不进 Context**（那会污染 OWNER_TABLE 与回放）
  2. 队列有界，满了**丢弃并计数**（播报不能把主链路拖死）
  3. 播报实现异常**不能影响执行**（fail-open）
"""
from __future__ import annotations

import asyncio
import time
import unittest

import serve
from contracts import (
    ExecCtx, LLMResponse, NormalizedToolCall, ProgressSink, RiskTier, SideEffect,
    SubagentSpec, ToolManifest, Usage,
)
from builder import PromptBuilder
from core.gateway import Gateway
from core.loop import Loop
from core.observability import QueueProgressSink, ShutdownCoordinator
from core.streaming import StreamingGateway
from plane.governance import Governance, InputGuard, OutputGuard, RBAC
from plane.tool import Dispatcher, ToolRegistry
from plane.workflow import SubagentRunner

P = {"user_id": "u", "tenant": "acme", "roles": ["r"], "scopes": ["runtime:run"]}


def probe_manifest() -> ToolManifest:
    return ToolManifest(name="probe", side_effect=SideEffect.READ_ONLY,
                        risk_tier=RiskTier.T0_SAFE, description="查",
                        input_schema={"type": "object", "properties": {}})


class TestSink(unittest.TestCase):
    """队列实现本身。"""

    def test_emit_and_drain(self):
        s = QueueProgressSink()
        s.emit("subagent.start", {"agent": "a"})
        got = s.drain()
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["kind"], "subagent.start")
        self.assertEqual(got[0]["data"]["agent"], "a")

    def test_bounded_and_counts_drops(self):
        s = QueueProgressSink(maxsize=3)
        for i in range(10):
            s.emit("step", {"i": i})
        self.assertEqual(len(s.drain()), 3)
        self.assertEqual(s.dropped, 7, "丢弃必须计数（否则'播报不全'不可见）")

    def test_drain_empty_never_blocks(self):
        s = QueueProgressSink()
        t0 = time.time()
        self.assertEqual(s.drain(), [])
        self.assertLess(time.time() - t0, 0.1)

    def test_close_stops_accepting(self):
        s = QueueProgressSink()
        s.close()
        s.emit("x", {})
        self.assertEqual(s.drain(), [])

    def test_drain_limit(self):
        s = QueueProgressSink()
        for i in range(100):
            s.emit("step", {"i": i})
        self.assertEqual(len(s.drain(limit=10)), 10)
        self.assertEqual(len(s.drain(limit=10)), 10)

    def test_satisfies_protocol(self):
        self.assertIsInstance(QueueProgressSink(), ProgressSink)


class TestSubagentEmits(unittest.TestCase):
    """SubagentRunner 在关键点播报。"""

    def _runner(self, *, boom=False):
        reg = ToolRegistry()
        reg.register(probe_manifest(), lambda a, e: {"v": 1})

        def llm_factory(spec):
            class Sub:
                model = "sub"

                def __init__(self):
                    self.i = 0

                def complete(self, p, t, e):
                    if boom:
                        raise RuntimeError("子 Agent 炸了")
                    self.i += 1
                    return LLMResponse(text=f"{spec.name} ok", usage=Usage())
            return Sub()
        return SubagentRunner(Dispatcher(reg), builder=PromptBuilder(total_tokens=4000),
                              llm_factory=llm_factory)

    def test_start_and_finish(self):
        sink = QueueProgressSink()
        runner = self._runner()
        ctx = __import__("core.context", fromlist=["ContextHandle"]).ContextHandle(
            initial={"goal": "parent", "principal": P}, budget=None)
        e = ExecCtx(progress=sink)
        runner.dispatch(SubagentSpec(name="child-a", goal="查 api"), ctx, e)
        kinds = [x["kind"] for x in sink.drain()]
        self.assertIn("subagent.start", kinds)
        self.assertIn("subagent.finish", kinds)

    def test_finish_reports_failure(self):
        sink = QueueProgressSink()
        runner = self._runner(boom=True)
        ctx = __import__("core.context", fromlist=["ContextHandle"]).ContextHandle(
            initial={"goal": "parent", "principal": P}, budget=None)
        runner.dispatch(SubagentSpec(name="child-b", goal="x"), ctx,
                        ExecCtx(progress=sink))
        fins = [x for x in sink.drain() if x["kind"] == "subagent.finish"]
        self.assertTrue(fins, "失败也要播报 finish（否则'子 Agent 消失了'没解释）")
        self.assertFalse(fins[0]["data"]["ok"])

    def test_bad_sink_does_not_break_execution(self):
        """fail-open：播报坏了绝不能影响执行。"""
        class Broken:
            def emit(self, *a, **k):
                raise RuntimeError("sink down")

        runner = self._runner()
        ctx = __import__("core.context", fromlist=["ContextHandle"]).ContextHandle(
            initial={"goal": "parent", "principal": P}, budget=None)
        r = runner.dispatch(SubagentSpec(name="c", goal="x"), ctx,
                            ExecCtx(progress=Broken()))
        self.assertIsNotNone(r)

    def test_no_sink_is_fine(self):
        runner = self._runner()
        ctx = __import__("core.context", fromlist=["ContextHandle"]).ContextHandle(
            initial={"goal": "parent", "principal": P}, budget=None)
        self.assertIsNotNone(
            runner.dispatch(SubagentSpec(name="c", goal="x"), ctx, ExecCtx()))


class TestStreamingDrains(unittest.TestCase):
    """StreamingGateway 把进度转成 step 事件。"""

    def _frames(self, sink, *, lead_sleep=0.4, emit_after=0.1):
        class SlowLead:
            model = "slow"

            def complete(self, p, t, e):
                time.sleep(lead_sleep)
                return LLMResponse(text="聚合完成", usage=Usage())

        reg = ToolRegistry()
        reg.register(probe_manifest(), lambda a, e: {"v": 1})

        def factory(goal, tc):
            return Loop(PromptBuilder(total_tokens=4000), SlowLead(), Dispatcher(reg))

        gw = StreamingGateway(Gateway(factory, governance=Governance(
            rbac=RBAC(), input_guard=InputGuard(), output_guard=OutputGuard())))

        import threading

        def later():
            time.sleep(emit_after)
            sink.emit("subagent.start", {"agent": "check-api"})
            sink.emit("subagent.finish", {"agent": "check-api", "ok": True, "steps": 2})
        threading.Thread(target=later, daemon=True).start()

        async def run():
            return [f async for f in serve._event_stream(
                gw, {"goal": "x", "principal": P, "_progress": sink},
                ShutdownCoordinator(), heartbeat_s=30)]
        return asyncio.run(run())

    def test_progress_becomes_step_events(self):
        frames = self._frames(QueueProgressSink())
        sub = [f for f in frames if '"kind":"subagent"' in f.replace(" ", "")]
        self.assertEqual(len(sub), 2, f"进度事件没流出来（共 {len(frames)} 帧）")
        self.assertIn("subagent.start", sub[0])
        self.assertIn("check-api", sub[0])

    def test_progress_has_seq(self):
        frames = self._frames(QueueProgressSink())
        sub = [f for f in frames if '"kind":"subagent"' in f.replace(" ", "")]
        for f in sub:
            self.assertIn('"seq"', f.replace(" ", ""))

    def test_dropped_count_visible(self):
        sink = QueueProgressSink(maxsize=1)     # 立刻满
        frames = self._frames(sink)
        sub = [f for f in frames if '"kind":"subagent"' in f.replace(" ", "")]
        if sub:
            self.assertIn('"dropped"', sub[0].replace(" ", ""))

    def test_progress_not_in_context(self):
        """进度**不进 Context**（否则污染 OWNER_TABLE 与回放）。"""
        sink = QueueProgressSink()
        holder = {}

        class SlowLead:
            model = "slow"

            def complete(self, p, t, e):
                time.sleep(0.2)
                return LLMResponse(text="ok", usage=Usage())

        def factory(goal, tc):
            return Loop(PromptBuilder(total_tokens=4000), SlowLead(),
                        Dispatcher(ToolRegistry()))

        gw = StreamingGateway(Gateway(
            factory, governance=Governance(rbac=RBAC(), input_guard=InputGuard(),
                                           output_guard=OutputGuard()),
            on_turn_start=lambda c, g, p: holder.update(ctx=c)))

        sink.emit("subagent.start", {"agent": "x"})

        async def run():
            return [f async for f in serve._event_stream(
                gw, {"goal": "x", "principal": P, "_progress": sink},
                ShutdownCoordinator(), heartbeat_s=30)]
        asyncio.run(run())
        snap = holder["ctx"].snapshot()
        self.assertNotIn("progress", snap.data)
        self.assertNotIn("subagent", str(snap.data.get("observations") or []))


class TestNoSinkNoEvents(unittest.TestCase):
    def test_stream_without_sink_still_works(self):
        def factory(goal, tc):
            return Loop(PromptBuilder(total_tokens=4000),
                        __import__("adapters.llm_mock",
                                   fromlist=["ScriptedLLM"]).ScriptedLLM(
                            [LLMResponse(text="ok", usage=Usage())]),
                        Dispatcher(ToolRegistry()))

        gw = StreamingGateway(Gateway(factory, governance=Governance(
            rbac=RBAC(), input_guard=InputGuard(), output_guard=OutputGuard())))

        async def run():
            return [f async for f in serve._event_stream(
                gw, {"goal": "x", "principal": P}, ShutdownCoordinator(),
                heartbeat_s=30)]
        frames = asyncio.run(run())
        self.assertTrue(any("completed" in f for f in frames))


if __name__ == "__main__":
    unittest.main(verbosity=2)
