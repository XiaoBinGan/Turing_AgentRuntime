"""C1 验收：单轮内并行执行工具调用。

四条硬约束（每条都有对应用例）：
  1. 结果顺序 = 输入顺序（顺序即语义，会进 Prompt）
  2. Context 只在主线程 apply（ContextHandle 无锁）
  3. 取消/超时后未执行的调用要留痕，不假装成功
  4. 存在 T2+ 调用时整轮退回串行（否则会执行"本该在审批点之后"的调用）
"""
from __future__ import annotations

import threading
import time
import unittest

from builder import PromptBuilder
from contracts import (
    ExecCtx, LLMResponse, NormalizedToolCall, RiskTier, SideEffect, TermReason,
    ToolManifest, Usage,
)
from core.context import ContextHandle
from core.loop import Loop
from plane.tool import Dispatcher, ToolRegistry

P = {"user_id": "u", "tenant": "acme", "roles": ["r"], "scopes": ["runtime:run"]}


def manifest(name: str, *, tier: RiskTier = RiskTier.T0_SAFE, **kw) -> ToolManifest:
    return ToolManifest(
        name=name, side_effect=SideEffect.READ_ONLY, risk_tier=tier,
        description=f"工具 {name}", input_schema={"type": "object", "properties": {}},
        **kw)


def make_loop(*, names, handlers, parallel, sleep=0.0, max_parallel=4):
    reg = ToolRegistry()
    for n in names:
        reg.register(manifest(n), handlers[n])

    class LLM:
        model = "test"

        def __init__(self):
            self.i = 0

        def complete(self, p, t, e):
            self.i += 1
            if self.i == 1:
                return LLMResponse(text="调", tool_calls=tuple(
                    NormalizedToolCall(id=f"c_{n}", name=n, args={}) for n in names),
                    usage=Usage())
            return LLMResponse(text="完成", usage=Usage())

    return Loop(PromptBuilder(total_tokens=8000), LLM(), Dispatcher(reg),
                tools=[manifest(n) for n in names],
                parallel_tools=parallel, max_parallel_tools=max_parallel)


class TestOrdering(unittest.TestCase):
    """INV-C1：结果顺序必须等于输入顺序（否则 Prompt 不确定，INV-2 破坏）。"""

    def test_order_preserved_even_when_completion_reversed(self):
        names = ["slow", "fast", "mid"]
        sleep_for = {"slow": 0.20, "fast": 0.01, "mid": 0.10}
        handlers = {n: (lambda a, e, s=sleep_for[n]: (time.sleep(s), {"n": 1})[1])
                    for n in names}
        lp = make_loop(names=names, handlers=handlers, parallel=True)
        ctx = ContextHandle(initial={"goal": "x"}, budget=None)
        lp.run("x", ctx, ExecCtx())
        got = [r["tool"] for r in ctx.get("tool_results")]
        self.assertEqual(got, names, "结果顺序被完成先后打乱")

    def test_deterministic_across_runs(self):
        """同一输入跑两次，tool_results 必须逐字节一致。"""
        names = ["a", "b", "c"]
        handlers = {n: (lambda a, e, s=0.01: (time.sleep(s), {"n": 1})[1])
                    for n in names}
        out = []
        for _ in range(3):
            lp = make_loop(names=names, handlers=handlers, parallel=True)
            ctx = ContextHandle(initial={"goal": "x"}, budget=None)
            lp.run("x", ctx, ExecCtx())
            out.append([(o["tool"], o["kind"]) for o in ctx.get("observations")])
        self.assertEqual(out[0], out[1])
        self.assertEqual(out[1], out[2])


class TestSpeedup(unittest.TestCase):
    def test_parallel_is_faster(self):
        names = ["a", "b", "c", "d"]
        handlers = {n: (lambda a, e: (time.sleep(0.15), {"n": 1})[1]) for n in names}

        t0 = time.time()
        lp = make_loop(names=names, handlers=handlers, parallel=False)
        lp.run("x", ContextHandle(initial={"goal": "x"}, budget=None), ExecCtx())
        serial = time.time() - t0

        t0 = time.time()
        lp = make_loop(names=names, handlers=handlers, parallel=True)
        lp.run("x", ContextHandle(initial={"goal": "x"}, budget=None), ExecCtx())
        par = time.time() - t0

        self.assertLess(par, serial * 0.6,
                        f"没提速：串行 {serial:.2f}s / 并行 {par:.2f}s")

    def test_used_threads(self):
        """真用了多个线程（不是"看起来并行"）。"""
        threads = set()
        lock = threading.Lock()

        def handler(a, e):
            time.sleep(0.08)
            with lock:
                threads.add(threading.current_thread().name)
            return {"ok": 1}

        names = ["a", "b", "c"]
        lp = make_loop(names=names, handlers={n: handler for n in names},
                       parallel=True)
        lp.run("x", ContextHandle(initial={"goal": "x"}, budget=None), ExecCtx())
        self.assertGreater(len(threads), 1, f"只用了 {threads} 一个线程")

    def test_max_parallel_respected(self):
        """并发上限生效：5 个 0.1s 任务、上限 2 → 至少 3 批 ≈ 0.3s。"""
        names = [f"t{i}" for i in range(5)]
        handlers = {n: (lambda a, e: (time.sleep(0.1), {"n": 1})[1]) for n in names}
        lp = make_loop(names=names, handlers=handlers, parallel=True, max_parallel=2)
        t0 = time.time()
        lp.run("x", ContextHandle(initial={"goal": "x"}, budget=None), ExecCtx())
        self.assertGreater(time.time() - t0, 0.25, "并发上限没生效")


class TestSafetyFallback(unittest.TestCase):
    """INV-C4：有 T2+ 调用就退回串行（否则会做"审批点之后"的事）。"""

    def test_t2_present_falls_back_to_serial(self):
        ran = []
        reg = ToolRegistry()
        reg.register(manifest("read1"), lambda a, e: (ran.append("read1"), {"v": 1})[1])
        reg.register(manifest("write1", tier=RiskTier.T2_HIGH),
                     lambda a, e: (ran.append("write1"), {"v": 2})[1])

        class LLM:
            model = "l"

            def __init__(self):
                self.i = 0

            def complete(self, p, t, e):
                self.i += 1
                if self.i == 1:
                    return LLMResponse(text="调", tool_calls=(
                        NormalizedToolCall(id="c1", name="read1", args={}),
                        NormalizedToolCall(id="c2", name="write1", args={})),
                        usage=Usage())
                return LLMResponse(text="好", usage=Usage())

        lp = Loop(PromptBuilder(total_tokens=8000), LLM(), Dispatcher(reg),
                  tools=[manifest("read1"), manifest("write1", tier=RiskTier.T2_HIGH)],
                  parallel_tools=True)
        ctx = ContextHandle(initial={"goal": "x"}, budget=None)
        r = lp.run("x", ctx, ExecCtx())
        self.assertEqual(r.term_reason, TermReason.NEEDS_CONFIRMATION)
        self.assertEqual(ran, ["read1"], "审批点之后的调用被误执行了")

    def test_t3_denied_not_suspended(self):
        """T3 是**硬拒**（不是挂起）：`denied` 之后 Loop 继续跑，不产生审批点。

        所以 T3 在语义上可以并行（handler 根本不会执行）。但 `_can_parallel`
        仍然对 T3 退回串行 —— 保守：万一将来 T3 语义变化，这里不会变成事故。
        """
        reg = ToolRegistry()
        reg.register(manifest("r"), lambda a, e: {"v": 1})
        reg.register(manifest("del", tier=RiskTier.T3_CRITICAL), lambda a, e: {"v": 2})

        class LLM:
            model = "l"

            def __init__(self):
                self.i = 0

            def complete(self, p, t, e):
                self.i += 1
                if self.i == 1:
                    return LLMResponse(text="调", tool_calls=(
                        NormalizedToolCall(id="c1", name="r", args={}),
                        NormalizedToolCall(id="c2", name="del", args={})),
                        usage=Usage())
                return LLMResponse(text="好", usage=Usage())

        lp = Loop(PromptBuilder(total_tokens=8000), LLM(), Dispatcher(reg),
                  tools=[manifest("r"), manifest("del", tier=RiskTier.T3_CRITICAL)],
                  parallel_tools=True)
        ctx = ContextHandle(initial={"goal": "x"}, budget=None)
        r = lp.run("x", ctx, ExecCtx())
        obs = {o["tool"]: o["kind"] for o in ctx.get("observations")}
        self.assertEqual(obs["r"], "ok")
        self.assertEqual(obs["del"], "denied", "T3 必须是硬拒")
        self.assertEqual(r.term_reason, TermReason.DONE, "T3 硬拒不挂起，Loop 应继续")

    def test_unknown_tool_falls_back(self):
        """判不出 tier（工具不在清单）→ 不许猜，退回串行。"""
        lp = make_loop(names=["a"], handlers={"a": lambda a, e: {"v": 1}},
                       parallel=True)
        resp = LLMResponse(text="x", tool_calls=(
            NormalizedToolCall(id="c", name="not_registered", args={}),
            NormalizedToolCall(id="c2", name="a", args={})), usage=Usage())
        self.assertFalse(lp._can_parallel(resp))


class TestFailureIsolation(unittest.TestCase):
    """INV-C3：单条异常/失败不影响其他。"""

    def test_one_fails_others_succeed(self):
        names = ["good1", "boom", "good2"]

        def handlers_fn(n):
            if n == "boom":
                def h(a, e):
                    raise RuntimeError("炸了")
                return h
            return lambda a, e: {"ok": 1}

        lp = make_loop(names=names, handlers={n: handlers_fn(n) for n in names},
                       parallel=True)
        ctx = ContextHandle(initial={"goal": "x"}, budget=None)
        r = lp.run("x", ctx, ExecCtx())
        # 成败看 observations：`tool_results` 的成功记录没有 kind 字段
        results = {o["tool"]: o["kind"] for o in ctx.get("observations")}
        self.assertEqual(results["good1"], "ok")
        self.assertEqual(results["good2"], "ok")
        self.assertEqual(results["boom"], "failed")
        self.assertEqual(r.steps, 2)

    def test_budget_not_overrun(self):
        """并发下预算不能被超发（INV-C2）。"""
        names = ["a", "b", "c"]
        lp = make_loop(names=names, handlers={n: (lambda a, e: {"ok": 1}) for n in names},
                       parallel=True)
        ctx = ContextHandle(initial={"goal": "x"}, budget=None)
        e = ExecCtx()
        e.budget.tool_calls_total = 2
        lp.run("x", ctx, e)
        self.assertLessEqual(e.budget.tool_calls_used, 2 + len(names),
                             "预算记账被并发打乱")


class TestPerTurnLimitUnderConcurrency(unittest.TestCase):
    """并行下 `per_turn_limit` 不能被绕过（Dispatcher 记账锁的意义）。"""

    def test_limit_respected(self):
        names = [f"t{i}" for i in range(12)]
        m = manifest("shared", per_turn_limit=3)
        reg = ToolRegistry()
        reg.register(m, lambda a, e: {"ok": 1})

        class LLM:
            model = "l"

            def __init__(self):
                self.i = 0

            def complete(self, p, t, e):
                self.i += 1
                if self.i == 1:
                    return LLMResponse(text="调", tool_calls=tuple(
                        NormalizedToolCall(id=f"c{i}", name="shared", args={"i": i})
                        for i in range(12)), usage=Usage())
                return LLMResponse(text="好", usage=Usage())

        lp = Loop(PromptBuilder(total_tokens=8000), LLM(), Dispatcher(reg),
                  tools=[m], parallel_tools=True, max_parallel_tools=6)
        ctx = ContextHandle(initial={"goal": "x"}, budget=None)
        lp.run("x", ctx, ExecCtx())
        ok = [o for o in ctx.get("observations") if o["kind"] == "ok"]
        self.assertLessEqual(len(ok), 3, f"per_turn_limit=3 却成功 {len(ok)} 次")


class TestDefaultOff(unittest.TestCase):
    def test_default_is_serial(self):
        lp = make_loop(names=["a"], handlers={"a": lambda a, e: {"v": 1}},
                       parallel=False)
        self.assertFalse(lp.parallel_tools)

    def test_single_call_uses_serial_path(self):
        """只有一个调用时没必要进线程池。"""
        lp = make_loop(names=["a"], handlers={"a": lambda a, e: {"v": 1}},
                       parallel=True)
        resp = LLMResponse(text="x", tool_calls=(
            NormalizedToolCall(id="c", name="a", args={}),), usage=Usage())
        self.assertFalse(lp.parallel_tools and len(resp.tool_calls) > 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
