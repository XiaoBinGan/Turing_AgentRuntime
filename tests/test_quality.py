"""tests/test_quality.py — Quality Plane 自带用例（W4）。

覆盖：树状 span / duration / tokens 累加 / 三维成本 / fail-open /
      patch log → steps / tool_sequence / LCS 轨迹匹配 / efficiency /
      lucky_pass（碰对了 ≠ 做对了）/ Replayer / 轨迹指纹。

只依赖 plane/quality.py + contracts.py，不 import core/*。
"""
from __future__ import annotations

import json

import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from contracts import (  # noqa: E402
    Owner, Patch, Phase, Result, TermReason, Usage,
)
from plane.quality import (  # noqa: E402
    PRICE_TABLE, EvalCase, Evaluator, Replayer, Tracer, Trajectory,
)


# ── 构造 helper ────────────────────────────────────────────────
def tool_patch(tool: str, args: dict | None = None, by: Owner = Owner.LOOP) -> Patch:
    """loop 层每次工具调用会 append 一条 tool_results。"""
    return Patch("append", "tool_results", by,
                 {"tool": tool, "args": args or {}, "payload": {"ok": True}})


def patched(*patches: Patch) -> list[Patch]:
    return list(patches)


def ok_result(steps: int = 1, term: TermReason = TermReason.DONE) -> Result:
    return Result(phase=Phase.DONE, term_reason=term, text="ok", steps=steps)


class TestTracer(unittest.TestCase):
    """Trace：树状 span + 三维成本归因（INV-17）。"""

    def test_nested_span_tree_three_levels(self):
        """1) 3 层嵌套：children 关系正确，根只有一棵。"""
        t = Tracer(tenant="acme", user_id="u1", task_id="task_a")
        with t.span("turn", {"phase": "EXECUTING"}):
            time.sleep(0.001)
            with t.span("llm"):
                time.sleep(0.001)
                with t.span("tool", {"name": "search"}):
                    time.sleep(0.001)
        out = t.finish()

        self.assertEqual(len(out["spans"]), 1)
        root = out["spans"][0]
        self.assertEqual(root["name"], "turn")
        self.assertEqual(root["attrs"], {"phase": "EXECUTING"})
        self.assertEqual(len(root["children"]), 1)

        llm = root["children"][0]
        self.assertEqual(llm["name"], "llm")
        self.assertEqual(len(llm["children"]), 1)

        tool = llm["children"][0]
        self.assertEqual(tool["name"], "tool")
        self.assertEqual(tool["attrs"]["name"], "search")
        self.assertEqual(tool["children"], [])
        # 根 span 覆盖子 span
        self.assertGreaterEqual(root["duration_ms"], llm["duration_ms"])

    def test_span_duration_positive(self):
        """2) 每个 span 都自动记录 duration_ms > 0（含嵌套层）。"""
        t = Tracer()
        with t.span("outer"):
            with t.span("inner"):
                time.sleep(0.002)
        root = t.finish()["spans"][0]
        self.assertGreater(root["duration_ms"], 0.0)
        self.assertGreater(root["children"][0]["duration_ms"], 0.0)

    def test_tree_ascii_render(self):
        """3) tree() 给出人读的 ASCII 树。"""
        t = Tracer()
        with t.span("turn"):
            with t.span("plan"):
                pass
            with t.span("tool"):
                pass
        text = t.tree()
        self.assertIn("turn", text)
        self.assertIn("├── plan", text)
        self.assertIn("└── tool", text)
        # 每个节点都带耗时（自适应 µs / ms）
        self.assertRegex(text, r"\[(\d+\.\d+)(ms|µs)\]")
        self.assertEqual(Tracer().tree(), "(empty trace)")

    def test_record_usage_accumulates_tokens(self):
        """4) record_usage 后 totals.tokens 累加正确。"""
        t = Tracer(tenant="acme", user_id="u1", task_id="task_a")
        t.record_usage("mock-1", Usage(input_tokens=100, output_tokens=50, cache_hit_tokens=10))
        t.record_usage("mock-cheap", Usage(input_tokens=200, output_tokens=100, cache_hit_tokens=5))
        totals = t.finish()["totals"]
        self.assertEqual(totals["tokens"], {"input": 300, "output": 150, "cache_hit": 15})
        self.assertEqual(totals["llm_calls"], 2)

    def test_cost_aggregates_by_tenant_user_task(self):
        """5) 成本按 tenant / user / task 三维分别聚合（验收点）。"""
        t = Tracer(tenant="acme", user_id="u1", task_id="task_a")
        t.record_usage("mock-1", Usage(1000, 1000))            # acme / u1 / task_a
        t.record_usage("mock-cheap", Usage(1000, 1000),
                       user_id="u2", task_id="task_b", tenant="acme")
        t.record_usage("mock-1", Usage(500, 500), user_id="u3",
                       task_id="task_c", tenant="beta")

        cost = t.finish()["totals"]["cost"]
        # 手算：mock-1 1000/1000 → 0.0005+0.0015=0.002；mock-cheap → 0.0001+0.0003=0.0004
        self.assertAlmostEqual(cost["usd"], 0.002 + 0.0004 + 0.001, places=9)
        # by_tenant
        self.assertAlmostEqual(cost["by_tenant"]["acme"], 0.0024, places=9)
        self.assertAlmostEqual(cost["by_tenant"]["beta"], 0.001, places=9)
        # by_user（两个不同 user 必须分开）
        self.assertAlmostEqual(cost["by_user"]["u1"], 0.002, places=9)
        self.assertAlmostEqual(cost["by_user"]["u2"], 0.0004, places=9)
        self.assertAlmostEqual(cost["by_user"]["u3"], 0.001, places=9)
        # by_task
        self.assertAlmostEqual(cost["by_task"]["task_a"], 0.002, places=9)
        self.assertAlmostEqual(cost["by_task"]["task_c"], 0.001, places=9)
        # 三个维度各自求和都等于总额
        for dim in ("by_tenant", "by_user", "by_task"):
            self.assertAlmostEqual(sum(cost[dim].values()), cost["usd"], places=9)
        self.assertAlmostEqual(PRICE_TABLE["mock-cheap"]["input"], 0.1 / 1e6, places=15)

    def test_scope_switches_attribution(self):
        """6) scope() 临时切换归因维度（子 Agent 扇出场景）。"""
        t = Tracer(tenant="acme", user_id="parent", task_id="task_a")
        t.record_usage("mock-1", Usage(1000, 0))
        with t.scope(user_id="child", task_id="task_a.sub"):
            t.record_usage("mock-1", Usage(1000, 0))
        cost = t.finish()["totals"]["cost"]
        self.assertAlmostEqual(cost["by_user"]["parent"], 0.0005, places=9)
        self.assertAlmostEqual(cost["by_user"]["child"], 0.0005, places=9)
        self.assertEqual(t.user_id, "parent")  # 出栈后恢复

    def test_fail_open_span_exception_propagates(self):
        """7) fail-open：span 内异常原样传播，Tracer 不二次抛错。"""
        t = Tracer()
        with self.assertRaises(RuntimeError):
            with t.span("boom"):
                raise RuntimeError("业务炸了")

        # 异常后结构仍完整：span 已闭环，栈已清空
        out = t.finish()  # 不得抛
        self.assertEqual(len(out["spans"]), 1)
        self.assertEqual(out["spans"][0]["name"], "boom")
        self.assertGreater(out["spans"][0]["duration_ms"], 0.0)

        # 栈没被污染：后续 span 挂在根上，不在 boom 下面
        with t.span("after"):
            pass
        spans = t.finish()["spans"]
        self.assertEqual([s["name"] for s in spans], ["boom", "after"])
        self.assertEqual(spans[0]["children"], [])

    def test_fail_open_malformed_usage(self):
        """8) fail-open：喂畸形 usage 也不炸，返回零值占位。"""
        t = Tracer()
        rec = t.record_usage("mock-1", None)  # type: ignore[arg-type]
        self.assertEqual(rec["usd"], 0.0)
        self.assertEqual(t.finish()["totals"]["tokens"]["input"], 0)

        class Weird:
            input_tokens = "not-a-number"
            output_tokens = None
            cache_hit_tokens = 0

        t.record_usage("unknown-model", Weird())  # type: ignore[arg-type]
        totals = t.finish()["totals"]
        self.assertGreaterEqual(totals["cost"]["usd"], 0.0)  # 未崩即可


class TestTrajectory(unittest.TestCase):
    """patch log → 轨迹。"""

    def test_from_patch_log_steps(self):
        """9) steps 数量与字段正确（patch 数 == step 数）。"""
        patches = patched(
            Patch("set", "phase", Owner.LOOP, "EXECUTING"),
            tool_patch("search", {"q": "x"}),
            Patch("incr", "loop_count", Owner.LOOP, 1),
            tool_patch("calc", {"expr": "1+1"}),
            Patch("set", "phase", Owner.LOOP, "DONE"),
        )
        traj = Trajectory.from_patch_log(patches)
        self.assertEqual(len(traj.steps), 5)
        first = traj.steps[0]
        self.assertEqual(first["index"], 0)
        self.assertEqual(first["op"], "set")
        self.assertEqual(first["path"], "phase")
        self.assertEqual(first["by"], "LOOP")
        self.assertEqual(first["value"], "EXECUTING")
        self.assertEqual([s["index"] for s in traj.steps], [0, 1, 2, 3, 4])

    def test_tool_sequence_extraction(self):
        """10) tool_sequence 只捞 path=="tool_results"，顺序保持。"""
        patches = patched(
            Patch("set", "phase", Owner.LOOP, "EXECUTING"),
            tool_patch("search"),
            Patch("incr", "loop_count", Owner.LOOP, 1),
            tool_patch("calc"),
            tool_patch("summarize"),
        )
        traj = Trajectory.from_patch_log(patches)
        self.assertEqual(traj.tool_sequence(), ["search", "calc", "summarize"])
        self.assertEqual(traj.loop_count(), 1)

    def test_tool_sequence_tolerates_shapes(self):
        """11) tool_sequence 兼容多种 tool_results 写法。"""
        patches = patched(
            Patch("append", "tool_results", Owner.LOOP, {"name": "raw_call",
                                                         "arguments": {"a": 1}}),
            Patch("append", "tool_results", Owner.LOOP, "bare_string"),
            Patch("set", "tool_results", Owner.LOOP,
                  [{"tool": "batched_a"}, {"tool": "batched_b"}]),
        )
        traj = Trajectory.from_patch_log(patches)
        self.assertEqual(traj.tool_sequence(),
                         ["raw_call", "bare_string", "batched_a", "batched_b"])
        self.assertEqual(traj.tool_calls()[0]["args"], {"a": 1})

    def test_fingerprint_stable_and_discriminative(self):
        """12) 相同 patch 序列指纹相同；不同序列指纹不同。"""
        a = patched(tool_patch("search"), Patch("set", "phase", Owner.LOOP, "DONE"))
        b = patched(tool_patch("search"), Patch("set", "phase", Owner.LOOP, "DONE"))
        c = patched(tool_patch("calc"), Patch("set", "phase", Owner.LOOP, "DONE"))
        d = patched(Patch("set", "phase", Owner.LOOP, "DONE"), tool_patch("search"))

        fa = Trajectory.from_patch_log(a).fingerprint
        fb = Trajectory.from_patch_log(b).fingerprint
        fc = Trajectory.from_patch_log(c).fingerprint
        fd = Trajectory.from_patch_log(d).fingerprint

        self.assertEqual(fa, fb)
        self.assertNotEqual(fa, fc)   # 工具不同
        self.assertNotEqual(fa, fd)   # 顺序不同（顺序即语义）
        self.assertEqual(len(fa), 16)


class TestEvaluator(unittest.TestCase):
    """状态轨迹匹配 vs 文本匹配。"""

    def _traj(self, *tools: str) -> Trajectory:
        return Trajectory.from_patch_log([tool_patch(t) for t in tools])

    def test_trajectory_match_is_subsequence(self):
        """13) 期望 [a,b,c]，实际 [a,x,b,y,c] → LCS 3/3 = 1.0。"""
        case = EvalCase(
            name="subseq",
            expect_term_reason="DONE",
            expect_tools=["a", "b", "c"],
            terminal_assert=lambda d: d.get("answer") == 42,
        )
        report = Evaluator().evaluate(
            case, ok_result(steps=5), {"answer": 42},
            self._traj("a", "x", "b", "y", "c"),
        )
        self.assertEqual(report.trajectory_match, 1.0)
        self.assertTrue(report.goal_grounding)
        # 5 步做 3 步的事 → 有冗余，但不能因此误判成 trajectory 不匹配
        self.assertEqual(report.detail["actual_tools"], ["a", "x", "b", "y", "c"])
        self.assertEqual(report.detail["missing_from_expected_path"], [])

    def test_trajectory_match_partial(self):
        """14) 缺一个工具 → 分数 2/3，并报出缺失项。"""
        case = EvalCase("partial", "DONE", ["a", "b", "c"], lambda d: True)
        report = Evaluator().evaluate(case, ok_result(3), {}, self._traj("a", "b"))
        self.assertAlmostEqual(report.trajectory_match, 2 / 3, places=6)
        self.assertEqual(report.detail["missing_from_expected_path"], ["c"])

    def test_efficiency_low_when_wasteful(self):
        """15) 期望 3 步实际 9 步 → efficiency < 0.5。"""
        case = EvalCase("wasteful", "DONE", ["a", "b", "c"], lambda d: True)
        report = Evaluator().evaluate(
            case, ok_result(9), {},
            self._traj("a", "x", "b", "y", "c", "z", "w", "v", "u"),
        )
        self.assertLess(report.efficiency, 0.5)
        self.assertEqual(report.detail["actual_steps"], 9)
        self.assertEqual(report.detail["expected_steps"], 3)
        self.assertEqual(report.detail["redundant_steps"], 6)

    def test_lucky_pass_is_not_a_pass(self):
        """16) 验收点：goal_grounding=True 但效率低 → passed=False + lucky_pass=True。"""
        case = EvalCase("lucky", "DONE", ["a", "b", "c"],
                        lambda d: d.get("answer") == 42)
        traj = self._traj("a", "x", "b", "y", "c", "z", "w", "v", "u")
        report = Evaluator().evaluate(case, ok_result(9), {"answer": 42}, traj)

        self.assertTrue(report.goal_grounding)      # 终态确实对了
        self.assertLess(report.efficiency, 0.6)     # 但过程很脏
        self.assertTrue(report.detail["lucky_pass"])
        self.assertFalse(report.passed)             # 所以不算通过
        self.assertTrue(any("冗余步" in r for r in report.detail["sloppy_reasons"]))

    def test_clean_pass(self):
        """17) 对照组：终态对 + 路径干净 + 参数合法 → passed=True。"""
        checker = lambda tool, args: tool in {"a", "b", "c"} and "q" in args  # noqa: E731
        case = EvalCase("clean", "DONE", ["a", "b", "c"],
                        lambda d: d.get("answer") == 42)
        patches = [tool_patch(t, {"q": t}) for t in ("a", "b", "c")]
        report = Evaluator(checker).evaluate(
            case, ok_result(3), {"answer": 42},
            Trajectory.from_patch_log(patches),
        )
        self.assertTrue(report.passed)
        self.assertFalse(report.detail["lucky_pass"])
        self.assertEqual(report.trajectory_match, 1.0)
        self.assertEqual(report.efficiency, 1.0)
        self.assertEqual(report.arg_precision, 1.0)

    def test_arg_precision_and_term_reason_gate(self):
        """18) 参数被 manifest 拒 + 终止条件不符 → 不通过。"""
        checker = lambda tool, args: tool == "allowed"  # noqa: E731
        case = EvalCase("bad_args", "DONE", ["allowed"], lambda d: True)
        report = Evaluator(checker).evaluate(
            case, ok_result(1, TermReason.BUDGET), {},
            Trajectory.from_patch_log([tool_patch("forbidden")]),
        )
        self.assertFalse(report.passed)
        self.assertEqual(report.arg_precision, 0.0)
        self.assertEqual(report.detail["rejected_calls"][0]["tool"], "forbidden")
        self.assertFalse(report.detail["term_reason"]["ok"])

    def test_terminal_assert_exception_is_contained(self):
        """19) 终态断言自己抛错 → goal_grounding=False，原因留在 detail。"""
        def boom(_d):
            raise KeyError("answer")

        case = EvalCase("boom", "DONE", ["a"], boom)
        report = Evaluator().evaluate(case, ok_result(1), {}, self._traj("a"))
        self.assertFalse(report.goal_grounding)
        self.assertFalse(report.passed)
        self.assertIn("terminal_assert raised", report.detail["terminal_error"])
        self.assertIn("KeyError", report.detail["terminal_error"])

    def test_max_steps_guard(self):
        """20) 超过 max_steps 直接不通过。"""
        case = EvalCase("capped", "DONE", ["a"], lambda d: True, max_steps=1)
        report = Evaluator().evaluate(case, ok_result(3), {}, self._traj("a", "b", "c"))
        self.assertTrue(report.detail["max_steps_exceeded"])
        self.assertFalse(report.passed)


class TestReplayer(unittest.TestCase):
    """patch log 重放。"""

    def test_replay_five_patches(self):
        """21) initial + 5 个 patch → 与手工推演一致，且 initial 不被污染。"""
        initial = {"a": 1, "b": [1], "c": {"x": 1}, "n": 0}
        patches = patched(
            Patch("set", "a", Owner.LOOP, 2),
            Patch("append", "b", Owner.LOOP, 2),
            Patch("incr", "n", Owner.LOOP, 3),
            Patch("merge", "c", Owner.LOOP, {"y": 2}),
            Patch("append", "b", Owner.LOOP, 3),
        )
        final = Replayer.replay(initial, patches)
        self.assertEqual(final, {"a": 2, "b": [1, 2, 3], "c": {"x": 1, "y": 2}, "n": 3})
        self.assertEqual(initial, {"a": 1, "b": [1], "c": {"x": 1}, "n": 0})
        self.assertTrue(Replayer.equivalent(initial, patches, final))

    def test_replay_budget_is_equivalent_to_context(self):
        """INV-16b：整块 `budget` 的补丁，重放终态必须与 `ContextHandle` 逐字段相等。

        两类分歧都会让"重放等价"变成假话：
          * **键名**：patch log 里可能是嵌套写法（`{"token": {"total": …}}`），
            而 Context 落盘是扁平的（`token_total` / `token_used`）；
          * **默认值**：Context 会把整块 budget 还原成 `Budget` 对象再落盘，
            缺省字段被补成默认值（`max_steps=8`、`token_total=20000`…）。
        `plane/` 不许 import `core/`（架构约束），所以 `Replayer` 只能照抄这套
        语义；少抄一半就会产出一个"看起来等价"的假终态。
        """
        from contracts import Budget
        from core.context import ContextHandle, _budget_flat

        initial = {"budget": _budget_flat(Budget())}
        cases = {
            "set 嵌套": [Patch("set", "budget", Owner.LOOP,
                        {"token": {"total": 20000, "used": 15},
                         "cost_usd": {"total": 0.5, "used": 0.0},
                         "tool_calls": {"total": 20, "used": 3}})],
            "set 扁平": [Patch("set", "budget", Owner.LOOP,
                        {"token_used": 7, "max_steps": 12})],
            "merge 嵌套": [Patch("merge", "budget", Owner.LOOP,
                          {"token": {"used": 9}, "cost_usd": {"used": 0.25}})],
            "单字段 + 整块": [Patch("set", "budget.token_used", Owner.LOOP, 5),
                          Patch("set", "budget", Owner.LOOP,
                                {"token": {"total": 500}})],
        }
        for name, patches in cases.items():
            with self.subTest(name):
                snap = ContextHandle.replay(
                    patches, initial=json.loads(json.dumps(initial))).snapshot()
                ctx_budget = getattr(snap, "data", snap)["budget"]
                rep_budget = Replayer.replay(
                    json.loads(json.dumps(initial)), patches)["budget"]
                self.assertEqual(rep_budget, ctx_budget)

    def test_replay_builds_missing_paths(self):
        """22) set 自动建中间 dict；append 到缺失路径建 list；merge 到缺失建 dict。"""
        final = Replayer.replay({}, [
            Patch("set", "plan.steps", Owner.PLANNER, ["s1"]),
            Patch("append", "history", Owner.LOOP, {"t": 1}),
            Patch("merge", "usage", Owner.LOOP, {"input": 10}),
            Patch("incr", "retry_count", Owner.DISPATCHER, 1),
        ])
        self.assertEqual(final["plan"]["steps"], ["s1"])
        self.assertEqual(final["history"], [{"t": 1}])
        self.assertEqual(final["usage"], {"input": 10})
        self.assertEqual(final["retry_count"], 1)

    def test_replay_is_strict(self):
        """23) 重放必须严格：未知 op / 类型不符 raise（静默重放会造假等价）。"""
        with self.assertRaises(ValueError):
            Replayer.replay({}, [Patch("replace", "a", Owner.LOOP, 1)])
        with self.assertRaises(ValueError):
            Replayer.replay({"a": 1}, [Patch("append", "a", Owner.LOOP, 2)])

    def test_replay_steps_and_roundtrip(self):
        """24) replay_steps 逐步快照；重放结果等于真 Context 终态（此处用替身）。"""
        initial = {"tool_results": []}
        patches = [tool_patch("search"), tool_patch("calc")]
        snaps = Replayer.replay_steps(initial, patches)
        self.assertEqual(len(snaps), 3)  # initial + 2
        self.assertEqual(snaps[0], initial)
        final = Replayer.replay(initial, patches)
        self.assertEqual([r["tool"] for r in final["tool_results"]], ["search", "calc"])
        # 重放出的终态能被 Trajectory 直接消费（三件套同一口径）
        traj = Trajectory.from_patch_log(patches)
        self.assertEqual(traj.tool_sequence(), [r["tool"] for r in final["tool_results"]])


if __name__ == "__main__":
    unittest.main(verbosity=2)
