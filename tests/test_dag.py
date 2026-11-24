"""tests/test_dag.py —— W23 静态 DAG 编排的自带测试（stdlib unittest，零第三方依赖）。

跑法：
    cd /Users/supre/Documents/tmp/agent-runtime && python3 -m unittest tests.test_dag -v

覆盖五个立面：
- ① 拓扑校验：重名 / 依赖不存在 / 自依赖 / 成环（**报出环成员**）/ 一次报全部
- ② 拓扑视图：`topo_order` 确定性、`layers` 分层、`flatten(layers) == topo_order`
- ③ 就绪计算：`ready_nodes` 初始 / 后继解锁
- ④ 执行语义：全成功、失败短路、同层继续跑、异常隔离、空 DAG、goals 覆盖
- ⑤ 确定性与资源：`max_parallel>1` 结果仍按名字序、线程真并行且不被中途 kill、
  tracer fail-open、深链不爆递归栈

约定：只依赖 `contracts.py`（DAGSpec/DAGNode/SubagentSpec/SubagentResult），
runner 用 fake，**不 import plane/workflow.py**——那边是 W9 的实现，鸭子类型即可。
"""
from __future__ import annotations

import contextlib
import os
import sys
import threading
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from contracts import (  # noqa: E402
    DAGNode,
    DAGSpec,
    SubagentResult,
    SubagentSpec,
)
from plane.dag import (  # noqa: E402
    NODE_SPAN,
    SKIP_TERM,
    DAG,
    DAGExecutionError,
    DAGRunner,
    DAGValidationError,
)


# ══════════════════════════════════════════════════════════════
# 构造 helper
# ══════════════════════════════════════════════════════════════
def node(name: str, *deps: str, goal: str = "", agent: str = "") -> DAGNode:
    return DAGNode(name=name, agent=agent, depends_on=tuple(deps), goal=goal)


def spec(*nodes: DAGNode, name: str = "sop") -> DAGSpec:
    return DAGSpec(name=name, nodes=tuple(nodes))


def chain(*names: str) -> DAGSpec:
    """线性链 names[0] → names[1] → ...（后者依赖前者）。"""
    node_list = [node(names[0])]
    for prev, cur in zip(names, names[1:]):
        node_list.append(node(cur, prev))
    return spec(*node_list)


def diamond() -> DAGSpec:
    """A → (B, C) → D：典型「A 做完才能做 B/C，B 与 C 可并行」。"""
    return spec(node("A"), node("B", "A"), node("C", "A"), node("D", "B", "C"))


class FakeRunner:
    """鸭子类型的 runner：记录调用、可控失败/爆炸/耗时、统计并发峰值。"""

    def __init__(
        self,
        fail: tuple[str, ...] = (),
        boom: tuple[str, ...] = (),
        delay: dict[str, float] | None = None,
    ) -> None:
        self.fail = set(fail)
        self.boom = set(boom)
        self.delay = dict(delay or {})
        self.calls: list[str] = []
        self.specs: list[SubagentSpec] = []
        self.concurrent_peak = 0
        self._active = 0
        self._lock = threading.Lock()

    def dispatch(self, spec: SubagentSpec, parent_ctx, parent_e) -> SubagentResult:
        with self._lock:
            self.calls.append(spec.name)
            self.specs.append(spec)
            self._active += 1
            self.concurrent_peak = max(self.concurrent_peak, self._active)
        try:
            wait = self.delay.get(spec.name, 0.0)
            if wait:
                time.sleep(wait)
            if spec.name in self.boom:
                raise RuntimeError(f"节点 {spec.name} 内部炸了")
            ok = spec.name not in self.fail
            return SubagentResult(
                name=spec.name,
                ok=ok,
                summary="" if not ok else f"{spec.name} 完成",
                steps=1,
                term_reason="DONE" if ok else "ABORTED",
                error="" if ok else f"{spec.name} 业务失败",
            )
        finally:
            with self._lock:
                self._active -= 1


class FakeTracer:
    """最小 tracer：记录 span 名与属性；可配置成「坏了」（span 抛异常）。"""

    def __init__(self, broken: bool = False) -> None:
        self.broken = broken
        self.names: list[str] = []
        self.attrs: list[dict] = []

    def span(self, name: str, attrs: dict | None = None):
        if self.broken:
            raise RuntimeError("tracer 挂了")
        self.names.append(name)
        self.attrs.append(dict(attrs or {}))
        return contextlib.nullcontext()


CTX = object()      # parent_ctx 占位：fake runner 不碰它
EXEC = object()     # parent_e 占位：同上


def run(spec_obj: DAGSpec, runner: FakeRunner, goals=None, **kw) -> dict:
    """`kw` 进 DAGRunner（tracer/max_parallel），`goals` 进 run()。"""
    return DAGRunner(runner, **kw).run(spec_obj, CTX, EXEC, goals=goals)


# ══════════════════════════════════════════════════════════════
# ① 拓扑校验
# ══════════════════════════════════════════════════════════════
class TestValidation(unittest.TestCase):
    def test_00_valid_dags_report_no_error(self):
        """正面对照：线性链/菱形/孤立节点都必须 0 错误，否则后面全是假阳性。"""
        self.assertEqual(DAG(chain("A", "B", "C")).validate(), [])
        self.assertEqual(DAG(diamond()).validate(), [])
        self.assertEqual(DAG(spec(node("solo"))).validate(), [])

    def test_01_cycle_reports_node_names(self):
        """A→B→A：错误消息里必须点名 A 和 B（不能只说「有环」）。"""
        dag = DAG(spec(node("A", "B"), node("B", "A")))
        errors = dag.validate()
        self.assertEqual(len(errors), 1, f"应只报一个环，实际: {errors}")
        self.assertIn("A", errors[0])
        self.assertIn("B", errors[0])
        self.assertIn("成环", errors[0])

    def test_02_self_dependency_reported(self):
        dag = DAG(spec(node("A", "A"), node("B")))
        errors = dag.validate()
        self.assertTrue(any("自依赖" in e and "A" in e for e in errors), errors)
        # 自依赖不该被误报成「依赖不存在的节点」
        self.assertFalse(any("不存在的节点" in e for e in errors), errors)

    def test_03_unknown_dependency_reported(self):
        dag = DAG(spec(node("A"), node("B", "A", "ghost")))
        errors = dag.validate()
        self.assertEqual(len(errors), 1, errors)
        self.assertIn("ghost", errors[0])
        self.assertIn("B", errors[0])

    def test_04_duplicate_name_reported(self):
        dag = DAG(spec(node("A"), node("A"), node("B", "A")))
        errors = dag.validate()
        self.assertEqual(len(errors), 1, errors)
        self.assertIn("重名", errors[0])
        self.assertIn("A", errors[0])

    def test_05_all_errors_reported_at_once(self):
        """四类错误混在一起：必须一次全报（不是遇到第一个就停）。"""
        dag = DAG(spec(
            node("dup"),
            node("dup"),            # 重名
            node("self", "self"),   # 自依赖
            node("orphan", "ghost"),  # 依赖不存在
            node("x", "y"), node("y", "x"),  # 成环
        ))
        errors = dag.validate()
        kinds = {
            "重名": any("重名" in e for e in errors),
            "自依赖": any("自依赖" in e for e in errors),
            "不存在": any("不存在的节点" in e for e in errors),
            "成环": any("成环" in e for e in errors),
        }
        self.assertTrue(all(kinds.values()), f"缺少某类错误: {kinds} / {errors}")
        self.assertGreaterEqual(len(errors), 4, errors)

    def test_06_cycle_message_excludes_downstream_victims(self):
        """环 A→B→C→A，外加 D 依赖 A：D 是受害者，不该被算进环成员。"""
        dag = DAG(spec(
            node("A", "C"), node("B", "A"), node("C", "B"), node("D", "A"),
        ))
        errors = dag.validate()
        cycle_lines = [e for e in errors if "成环" in e]
        self.assertEqual(len(cycle_lines), 1, errors)
        joined = " ".join(cycle_lines[0].split("->"))
        for member in ("A", "B", "C"):
            self.assertIn(member, joined)
        self.assertNotIn("D", joined, f"环消息混进了下游节点: {cycle_lines[0]}")

    def test_07_layers_and_topo_raise_on_invalid(self):
        dag = DAG(spec(node("A", "B"), node("B", "A")))
        for call in (dag.layers, dag.topo_order):
            with self.assertRaises(DAGValidationError) as ctx:
                call()
            self.assertIn("A", str(ctx.exception))
            self.assertTrue(ctx.exception.errors)


# ══════════════════════════════════════════════════════════════
# ② 拓扑视图
# ══════════════════════════════════════════════════════════════
class TestTopology(unittest.TestCase):
    def test_10_linear_chain_topo_order(self):
        dag = DAG(chain("A", "B", "C"))
        self.assertEqual(dag.topo_order(), ["A", "B", "C"])
        self.assertEqual(dag.layers(), [["A"], ["B"], ["C"]])

    def test_11_diamond_layers(self):
        dag = DAG(diamond())
        self.assertEqual(dag.layers(), [["A"], ["B", "C"], ["D"]])
        self.assertEqual(dag.topo_order(), ["A", "B", "C", "D"])

    def test_12_isolated_nodes(self):
        dag = DAG(spec(node("solo"), node("lonely")))
        self.assertEqual(dag.layers(), [["lonely", "solo"]])   # 同层按名字排序
        self.assertEqual(dag.ready_nodes(set()), ["lonely", "solo"])

    def test_13_topo_order_is_deterministic_and_insertion_independent(self):
        """插入顺序与名字顺序相反：两次调用相等，且不随构造顺序漂移。"""
        forward = spec(node("a"), node("b"), node("c"))
        backward = spec(node("c"), node("b"), node("a"))
        d1, d2 = DAG(forward), DAG(backward)
        self.assertEqual(d1.layers(), [["a", "b", "c"]])
        self.assertEqual(d2.layers(), [["a", "b", "c"]])
        self.assertEqual(d1.topo_order(), d2.topo_order())
        self.assertEqual(d1.topo_order(), d1.topo_order())

    def test_14_topo_order_ignores_dict_insertion_inside_layers(self):
        """依赖相同的一组节点，插进 spec 的顺序不该改变任何输出。"""
        a = spec(node("z"), node("y", "z"), node("x", "z"), node("w", "z"))
        b = spec(node("w", "z"), node("x", "z"), node("y", "z"), node("z"))
        self.assertEqual(DAG(a).layers(), [["z"], ["w", "x", "y"]])
        self.assertEqual(DAG(a).topo_order(), DAG(b).topo_order())

    def test_15_layers_flatten_equals_topo_order(self):
        specs = [
            diamond(),
            chain("A", "B", "C", "D"),
            spec(node("A"), node("B", "A"), node("C"), node("D", "B", "C")),
            spec(node("A"), node("B")),
        ]
        for s in specs:
            dag = DAG(s)
            flat = [n for layer in dag.layers() for n in layer]
            self.assertEqual(flat, dag.topo_order(), f"自洽性被破坏: {s.name}")
            self.assertEqual(sorted(flat), sorted(dag.names))

    def test_16_deep_chain_does_not_blow_recursion(self):
        """2000 节点线性链：校验/分层必须走迭代而不是递归。"""
        names = [f"n{i:04d}" for i in range(2000)]
        s = chain(*names)
        dag = DAG(s)
        self.assertEqual(dag.validate(), [])
        self.assertEqual(len(dag.layers()), 2000)
        self.assertEqual(dag.topo_order(), names)


# ══════════════════════════════════════════════════════════════
# ③ 就绪计算
# ══════════════════════════════════════════════════════════════
class TestReadyNodes(unittest.TestCase):
    def test_20_only_dependency_free_nodes_initially(self):
        dag = DAG(diamond())
        self.assertEqual(dag.ready_nodes(set()), ["A"])
        self.assertEqual(dag.ready_nodes(frozenset()), ["A"])
        self.assertEqual(dag.ready_nodes(None), ["A"])

    def test_21_successors_unlock_after_done(self):
        dag = DAG(diamond())
        self.assertEqual(dag.ready_nodes({"A"}), ["B", "C"])
        self.assertEqual(dag.ready_nodes({"A", "B"}), ["C"])       # D 还差 C
        self.assertEqual(dag.ready_nodes({"A", "B", "C"}), ["D"])
        self.assertEqual(dag.ready_nodes({"A", "B", "C", "D"}), [])

    def test_22_ready_nodes_are_name_sorted(self):
        dag = DAG(spec(node("zeta"), node("alpha"), node("mike")))
        self.assertEqual(dag.ready_nodes(set()), ["alpha", "mike", "zeta"])


# ══════════════════════════════════════════════════════════════
# ④ 执行语义
# ══════════════════════════════════════════════════════════════
class TestRunnerSuccessAndFailure(unittest.TestCase):
    def test_30_all_success(self):
        runner = FakeRunner()
        report = run(diamond(), runner)
        self.assertEqual(report["ok"], 4)
        self.assertEqual(report["failed"], 0)
        self.assertEqual(report["skipped"], 0)
        self.assertFalse(report["short_circuited"])
        self.assertEqual(report["order"], ["A", "B", "C", "D"])
        self.assertEqual(sorted(runner.calls), ["A", "B", "C", "D"])

    def test_31_results_keyed_by_node_name(self):
        report = run(diamond(), FakeRunner())
        self.assertEqual(set(report["results"]), {"A", "B", "C", "D"})
        self.assertEqual(report["results"]["B"]["name"], "B")
        self.assertTrue(report["results"]["B"]["ok"])

    def test_32_failure_skips_successors_but_same_layer_runs(self):
        """B 失败 → D 被 skipped；同层的 C 照常执行完（不能中途杀）。"""
        runner = FakeRunner(fail=("B",))
        report = run(diamond(), runner)
        self.assertEqual(report["ok"], 2)          # A, C
        self.assertEqual(report["failed"], 1)      # B
        self.assertEqual(report["skipped"], 1)     # D
        self.assertIn("C", runner.calls, "同层节点被短路误伤")
        self.assertIn("D", report["results"])
        self.assertFalse(report["results"]["D"]["ok"])
        self.assertEqual(report["results"]["D"]["term_reason"], SKIP_TERM)
        self.assertIn("B", report["results"]["D"]["error"])

    def test_33_short_circuited_flag_only_when_skipped(self):
        self.assertTrue(run(diamond(), FakeRunner(fail=("A",)))["short_circuited"])
        self.assertFalse(run(diamond(), FakeRunner())["short_circuited"])

    def test_34_failure_of_root_skips_whole_downstream(self):
        report = run(diamond(), FakeRunner(fail=("A",)))
        self.assertEqual(report["ok"], 0)
        self.assertEqual(report["failed"], 1)
        self.assertEqual(report["skipped"], 3)
        self.assertEqual(report["order"], ["A"])   # 一个后继都没派发
        self.assertEqual({n: r["term_reason"] for n, r in report["results"].items()}["B"], SKIP_TERM)

    def test_35_dispatch_exception_isolated(self):
        """dispatch 抛异常 → 该节点 failed 且错误文本带上异常类型，DAG 不崩。"""
        report = run(diamond(), FakeRunner(boom=("B",)))
        self.assertEqual(report["failed"], 1)
        self.assertFalse(report["results"]["B"]["ok"])
        self.assertIn("RuntimeError", report["results"]["B"]["error"])
        self.assertIn("节点 B 内部炸了", report["results"]["B"]["error"])
        self.assertEqual(report["results"]["B"]["term_reason"], "UNRECOVERABLE")
        self.assertEqual(report["skipped"], 1)     # D
        self.assertTrue(report["results"]["C"]["ok"])

    def test_36_invalid_dag_executes_nothing(self):
        runner = FakeRunner()
        with self.assertRaises(DAGValidationError) as ctx:
            run(spec(node("A", "B"), node("B", "A")), runner)
        self.assertIn("A", str(ctx.exception))
        self.assertEqual(runner.calls, [], "非法 DAG 竟然派发了节点")

    def test_37_empty_dag(self):
        runner = FakeRunner()
        report = run(spec(name="empty"), runner)
        self.assertEqual(report["ok"], 0)
        self.assertEqual(report["failed"], 0)
        self.assertEqual(report["skipped"], 0)
        self.assertEqual(report["results"], {})
        self.assertEqual(report["order"], [])
        self.assertEqual(report["layers"], [])
        self.assertFalse(report["short_circuited"])
        self.assertEqual(runner.calls, [])

    def test_38_goals_override_then_node_then_default(self):
        s = spec(
            node("A", goal="节点自带目标"),
            node("B", goal="被 goals 覆盖"),
            node("C"),
            name="pipeline",
        )
        runner = FakeRunner()
        run(s, runner, goals={"B": "外部注入目标", "A": "A 也被覆盖"})
        goals = {sp.name: sp.goal for sp in runner.specs}
        self.assertEqual(goals["A"], "A 也被覆盖")      # goals 优先于 node.goal
        self.assertEqual(goals["B"], "外部注入目标")
        self.assertEqual(goals["C"], "pipeline:C")      # 都没给 → name:node

    def test_39_node_goal_used_when_no_override(self):
        runner = FakeRunner()
        run(spec(node("A", goal="节点自带目标")), runner)
        self.assertEqual(runner.specs[0].goal, "节点自带目标")

    def test_40_subagent_spec_is_readonly_and_named_by_node(self):
        """INV-18：allowed_tools 必须为空（= 只读），name 必须等于节点名。"""
        runner = FakeRunner()
        run(spec(node("worker", agent="researcher")), runner)
        sub = runner.specs[0]
        self.assertIsInstance(sub, SubagentSpec)
        self.assertEqual(sub.name, "worker")
        self.assertEqual(tuple(sub.allowed_tools), ())

    def test_41_runner_without_dispatch_rejected(self):
        with self.assertRaises(DAGExecutionError):
            DAGRunner(object())

    def test_42_last_run_and_last_status_exposed(self):
        runner_obj = DAGRunner(FakeRunner(fail=("B",)))
        report = runner_obj.run(diamond(), CTX, EXEC)
        self.assertIs(runner_obj.last_run, report)
        self.assertEqual(runner_obj.last_status["D"], "skipped")


# ══════════════════════════════════════════════════════════════
# ⑤ 并行 + 确定性 + 观测
# ══════════════════════════════════════════════════════════════
class TestParallelDeterminism(unittest.TestCase):
    def test_50_parallel_results_keep_name_order(self):
        """max_parallel=2：完成顺序被 delay 反向打乱，账本仍按名字序。

        c 最先完成、a 最后完成——如果实现按「完成先后」记账，这个用例会红。
        """
        s = spec(node("c"), node("a"), node("b"))
        runner = FakeRunner(delay={"a": 0.06, "b": 0.03, "c": 0.0})
        report = run(s, runner, max_parallel=2)
        self.assertEqual(list(report["results"]), ["a", "b", "c"])
        self.assertEqual(report["order"], ["a", "b", "c"])
        self.assertEqual(report["layers"], [["a", "b", "c"]])
        self.assertEqual(report["ok"], 3)

    def test_51_parallel_and_serial_reports_agree(self):
        serial = run(diamond(), FakeRunner(), max_parallel=1)
        parallel = run(diamond(), FakeRunner(), max_parallel=4)
        self.assertEqual(list(serial["results"]), list(parallel["results"]))
        self.assertEqual(serial["order"], parallel["order"])
        self.assertEqual(serial["layers"], parallel["layers"])
        self.assertEqual(
            {k: v["ok"] for k, v in serial["results"].items()},
            {k: v["ok"] for k, v in parallel["results"].items()},
        )

    def test_52_parallel_really_overlaps(self):
        """真并行：同层两个慢节点必须重叠执行（否则 max_parallel 是假的）。"""
        s = spec(node("a"), node("b"))
        runner = FakeRunner(delay={"a": 0.08, "b": 0.08})
        started = time.monotonic()
        report = run(s, runner, max_parallel=2)
        elapsed = time.monotonic() - started
        self.assertEqual(report["ok"], 2)
        self.assertGreaterEqual(runner.concurrent_peak, 2, "两个节点没有重叠执行")
        self.assertLess(elapsed, 0.15, f"串行化了，耗时 {elapsed:.3f}s")

    def test_53_serial_mode_is_serial(self):
        runner = FakeRunner(delay={"a": 0.05, "b": 0.05})
        run(spec(node("a"), node("b")), runner, max_parallel=1)
        self.assertEqual(runner.concurrent_peak, 1)

    def test_54_failure_does_not_kill_running_sibling(self):
        """同层 a 失败、b 很慢：b 必须跑完并留下结果（不许提前 shutdown/取消）。"""
        s = spec(node("a"), node("b"), node("c", "a", "b"))
        runner = FakeRunner(fail=("a",), delay={"b": 0.09})
        report = run(s, runner, max_parallel=2)
        self.assertIn("b", runner.calls)
        self.assertTrue(report["results"]["b"]["ok"], "同层已启动的节点被中途杀了")
        self.assertEqual(report["failed"], 1)
        self.assertEqual(report["skipped"], 1)      # c
        self.assertEqual(report["short_circuited"], True)

    def test_55_tracer_gets_one_span_per_node(self):
        tracer = FakeTracer()
        run(diamond(), FakeRunner(), tracer=tracer)
        self.assertEqual(tracer.names, [NODE_SPAN] * 4)
        self.assertEqual(sorted(a["node"] for a in tracer.attrs), ["A", "B", "C", "D"])
        self.assertEqual({a["dag"] for a in tracer.attrs}, {"sop"})
        self.assertEqual([a["layer"] for a in tracer.attrs], [0, 1, 1, 2])

    def test_56_broken_tracer_is_fail_open(self):
        tracer = FakeTracer(broken=True)
        report = run(diamond(), FakeRunner(), tracer=tracer)
        self.assertEqual(report["ok"], 4, "tracer 坏掉不该影响流水线")
        self.assertEqual(tracer.names, [])

    def test_57_no_tracer_no_span_no_crash(self):
        report = run(diamond(), FakeRunner(), tracer=None)
        self.assertEqual(report["ok"], 4)

    def test_58_max_parallel_normalized(self):
        self.assertEqual(DAGRunner(FakeRunner(), max_parallel=0).max_parallel, 1)
        self.assertEqual(DAGRunner(FakeRunner(), max_parallel=-3).max_parallel, 1)
        self.assertEqual(DAGRunner(FakeRunner(), max_parallel="2").max_parallel, 2)
        self.assertEqual(DAGRunner(FakeRunner(), max_parallel=None).max_parallel, 1)


if __name__ == "__main__":       # pragma: no cover
    unittest.main(verbosity=2)


class TestUpstreamResultsFlow(unittest.TestCase):
    """下游节点必须能看到上游结论 —— 这是「先检查、再决定」能成立的前提。

    没有它，`{"name":"decide","goal":"根据检查结果决定是否扩容"}` 里
    根本没有"检查结果"，下游只能瞎猜。静态 DAG 最常见的形态就会失效。
    """

    class _Runner:
        def __init__(self, *, summary="连接池 20/20 已满", ok=True):
            self.goals = {}
            self._summary = summary
            self._ok = ok

        def dispatch(self, spec, ctx, e):
            self.goals[spec.name] = spec.goal
            return {"ok": self._ok, "steps": 2, "summary": self._summary,
                    "error": "" if self._ok else "boom"}

    def _spec(self, *nodes):
        return DAGSpec(name="d", nodes=tuple(nodes))

    def test_downstream_sees_upstream_summary(self):
        r = self._Runner()
        DAGRunner(r).run(self._spec(
            DAGNode(name="check", goal="检查数据库"),
            DAGNode(name="decide", goal="决定是否扩容", depends_on=("check",))), None, None)
        self.assertIn("上游节点结论", r.goals["decide"])
        self.assertIn("连接池", r.goals["decide"], "下游拿不到上游的实际结论")

    def test_root_node_has_no_upstream_block(self):
        r = self._Runner()
        DAGRunner(r).run(self._spec(DAGNode(name="a", goal="x")), None, None)
        self.assertNotIn("上游节点结论", r.goals["a"])

    def test_only_declared_deps_injected(self):
        """只有**自己声明的依赖**才注入，不相关节点的结论不该混进来。"""
        r = self._Runner()
        r2 = self._Runner(summary="另一条线的结论")
        DAGRunner(r).run(self._spec(
            DAGNode(name="a", goal="x"),
            DAGNode(name="b", goal="y"),
            DAGNode(name="c", goal="z", depends_on=("a",))), None, None)
        self.assertIn("a", r.goals["c"])
        self.assertNotIn("b:", r.goals["c"], "无关节点被塞进来了")

    def test_failed_upstream_reports_error(self):
        r = self._Runner(ok=False, summary="")
        DAGRunner(r).run(self._spec(
            DAGNode(name="a", goal="x"),
            DAGNode(name="b", goal="y", depends_on=("a",))), None, None)
        # a 失败 → b 被短路跳过，不会执行（短路语义优先）
        self.assertNotIn("b", r.goals)

    def test_summary_truncated(self):
        """摘要要截断 —— 全量 payload 塞进 goal 会撑爆预算。"""
        r = self._Runner(summary="x" * 5000)
        DAGRunner(r).run(self._spec(
            DAGNode(name="a", goal="x"),
            DAGNode(name="b", goal="y", depends_on=("a",))), None, None)
        self.assertLess(len(r.goals["b"]), 1000, "上游摘要没有截断")

    def test_upstream_does_not_override_goal(self):
        """注入是**追加**，不能把节点自己的 goal 冲掉。"""
        r = self._Runner()
        DAGRunner(r).run(self._spec(
            DAGNode(name="a", goal="x"),
            DAGNode(name="b", goal="原始目标文本", depends_on=("a",))), None, None)
        self.assertTrue(r.goals["b"].startswith("原始目标文本"))
