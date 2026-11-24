"""引擎互斥 + workflow_state 可复盘。

**问题**：工作流（B，DAGRunner）与回合内编排（C，SubagentRunner）都会写父
Context 的 `observations`（owner=LOOP）。两条路径并发写同一字段时，顺序与归属
都不确定 —— 而 `OWNER_TABLE` 存在的全部意义就是防这个。

**另一个真空**：`DAGRunner` 原先一个字都不写 Context（`ctx.apply` 计数 0）。
后果是"跑到第几层、哪个节点被跳过、为什么跳过"没有任何记录 —— 出事后无法复盘。
"""
from __future__ import annotations

import unittest

from contracts import DAGNode, DAGSpec, OWNER_TABLE, Owner
from core.context import ContextError, ContextHandle
from plane.dag import DAGExecutionError, DAGRunner


def ctx():
    return ContextHandle(initial={"goal": "x"}, budget=None)


class FakeRunner:
    """最小 runner：`dispatch(spec, ctx, e)` → duck-typed 结果。"""

    def __init__(self, *, fail_on=()):
        self.calls = []
        self.fail_on = set(fail_on)

    def dispatch(self, spec, parent_ctx, parent_e):
        self.calls.append(spec.name)
        ok = spec.name not in self.fail_on
        return {"ok": ok, "steps": 1, "text": f"{spec.name} done",
                "error": "" if ok else "boom", "term_reason": "DONE" if ok else "UNRECOVERABLE"}


def spec(*nodes) -> DAGSpec:
    return DAGSpec(name="release", nodes=tuple(nodes))


class TestOwnerTable(unittest.TestCase):
    def test_workflow_owner_exists(self):
        self.assertIs(OWNER_TABLE.get("workflow_state"), Owner.WORKFLOW)

    def test_workflow_state_separate_from_observations(self):
        """两个字段 owner 必须不同 —— 否则 C 与 B 会互相覆盖。"""
        self.assertIsNot(OWNER_TABLE["workflow_state"], OWNER_TABLE["observations"])


class TestBindEngine(unittest.TestCase):
    def test_first_bind_sets(self):
        c = ctx()
        c.bind_engine("loop")
        self.assertEqual(c.engine, "loop")

    def test_rebind_same_is_idempotent(self):
        c = ctx()
        c.bind_engine("loop")
        c.bind_engine("loop")           # Loop 每轮可能都调
        self.assertEqual(c.engine, "loop")

    def test_conflicting_bind_rejected(self):
        c = ctx()
        c.bind_engine("loop")
        with self.assertRaises(ContextError) as exc:
            c.bind_engine("dag")
        self.assertIn("observations", str(exc.exception),
                      "错误信息要说清为什么互斥（不是一句'冲突'）")

    def test_empty_bind_is_noop(self):
        c = ctx()
        c.bind_engine("")
        self.assertEqual(c.engine, "")

    def test_fork_does_not_inherit_engine(self):
        """子 Context 是**另一个**执行单元，不该继承父的引擎归属。"""
        c = ctx()
        c.bind_engine("loop")
        child = c.fork()
        child.bind_engine("dag")        # 不该抛
        self.assertEqual(child.engine, "dag")


class TestDagBindsEngine(unittest.TestCase):
    def test_dag_binds_dag_engine(self):
        c = ctx()
        DAGRunner(FakeRunner()).run(spec(DAGNode(name="a", goal="x")), c, None)
        self.assertEqual(c.engine, "dag")

    def test_dag_on_loop_bound_context_refused(self):
        """C 正在跑的 Context 上不能再跑 B —— fail-closed。"""
        c = ctx()
        c.bind_engine("loop")
        with self.assertRaises(DAGExecutionError) as exc:
            DAGRunner(FakeRunner()).run(spec(DAGNode(name="a", goal="x")), c, None)
        self.assertIn("编排引擎", str(exc.exception))


class TestWorkflowStateRecorded(unittest.TestCase):
    """DAG 必须留下自己的执行事实（否则无法复盘）。"""

    def test_state_written(self):
        c = ctx()
        DAGRunner(FakeRunner()).run(
            spec(DAGNode(name="a", goal="x"), DAGNode(name="b", goal="y")),
            c, None)
        st = c.get("workflow_state")
        self.assertIsInstance(st, dict)
        self.assertTrue(st.get("nodes"), "没有任何节点记录 = 无法复盘")

    def test_state_has_layers_and_order(self):
        c = ctx()
        DAGRunner(FakeRunner()).run(
            spec(DAGNode(name="a", goal="x"), DAGNode(name="b", goal="y")),
            c, None)
        st = c.get("workflow_state")
        self.assertIn("layers", st)
        self.assertIn("order", st)

    def test_skipped_node_is_marked(self):
        """被跳过的节点必须能看出来（`term_reason == "SKIPPED"`）。

        DAG 的节点 payload 没有 `status`/`blocked_by` 字段，
        跳过靠 `term_reason` 标识 —— 早先按"我以为是"的字段读，
        结果全是空串：**观测面读错字段比不读更坏**，它看起来有内容、实际全错。
        """
        c = ctx()
        DAGRunner(FakeRunner(fail_on={"a"})).run(
            spec(DAGNode(name="a", goal="x"),
                 DAGNode(name="b", goal="y", depends_on=("a",))), c, None)
        nodes = {n["name"]: n for n in c.get("workflow_state")["nodes"]}
        self.assertTrue(nodes["b"]["skipped"], "依赖失败的节点应被跳过")
        self.assertEqual(nodes["b"]["term_reason"], "SKIPPED")
        self.assertIn("a", nodes["b"]["error"], "跳过原因必须能看出是被谁拖累的")

    def test_failed_node_has_ok_false(self):
        c = ctx()
        DAGRunner(FakeRunner(fail_on={"a"})).run(
            spec(DAGNode(name="a", goal="x")), c, None)
        nodes = {n["name"]: n for n in c.get("workflow_state")["nodes"]}
        self.assertFalse(nodes["a"]["ok"])
        self.assertEqual(nodes["a"]["error"], "boom")

    def test_state_does_not_pollute_observations(self):
        """`workflow_state` 归 WORKFLOW，绝不能写进 observations（那是 LOOP 的）。"""
        c = ctx()
        DAGRunner(FakeRunner()).run(spec(DAGNode(name="a", goal="x")), c, None)
        self.assertEqual(c.get("observations") or [], [],
                         "DAG 不该往 observations 里写")

    def test_no_ctx_does_not_break(self):
        """传 None / 假 ctx 时不许崩（观测面 fail-open）。"""
        DAGRunner(FakeRunner()).run(spec(DAGNode(name="a", goal="x")), None, None)

    def test_state_write_failure_fail_open(self):
        class NoApply:
            def bind_engine(self, name):
                pass
        DAGRunner(FakeRunner()).run(spec(DAGNode(name="a", goal="x")), NoApply(), None)


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestChildInheritsChannels(unittest.TestCase):
    """`child()` 必须继承**取消信号**与**进度通道**。

    踩过的坑：这两个字段没继承，后果分别是
      * 超时只停住父层，子 Agent 继续烧 token（真中止失效）
      * 层叠子 Agent（子再派子）的进度永远传不到客户端（C2 失效）
    两者都是**静默失效** —— 功能看起来在，实际不生效。
    """

    def test_child_inherits_progress(self):
        import threading
        from contracts import ExecCtx
        from core.observability import QueueProgressSink
        sink = QueueProgressSink()
        child = ExecCtx(progress=sink).child()
        self.assertIs(child.progress, sink)

    def test_child_inherits_cancel_event(self):
        import threading
        from contracts import ExecCtx
        ev = threading.Event()
        child = ExecCtx(cancel_event=ev).child()
        self.assertIs(child.cancel_event, ev)

    def test_child_aborted_when_parent_cancelled(self):
        """父层被取消 → 子层立刻看到 aborted（不用等下一轮）。"""
        import threading
        from contracts import ExecCtx
        ev = threading.Event()
        child = ExecCtx(cancel_event=ev).child()
        self.assertFalse(child.aborted)
        ev.set()                        # 外部线程置位
        self.assertTrue(child.aborted, "子 Agent 没继承取消信号 → 超时后仍在跑")

    def test_child_still_downgrades_scopes(self):
        """继承通道不能顺手把降权也放松了（INV-18）。"""
        from contracts import ExecCtx, Principal
        e = ExecCtx(principal=Principal(user_id="u", tenant="t",
                                       roles=("sre",),
                                       scopes=("runtime:run", "tool.read",
                                               "tool.write")))
        child = e.child()
        self.assertIn("tool.read", child.principal.scopes)
        self.assertNotIn("tool.write", child.principal.scopes)


class TestWorkflowContext(unittest.TestCase):
    """每条 workflow 必须有**自己的** Context —— 否则 DAG 无法复盘。

    为什么需要：`DAGRunner` 的复盘能力依赖 `parent_ctx`。没有 Context 时
    `_stamp_workflow_state` 只能 fail-open 跳过，"跑到第几层、哪个节点被跳过"
    不留任何痕迹。

    为什么是独立 Context 而不是复用回合的：workflow 是独立执行单元，
    与回合内编排（C）共享同一个 Context 会撞 `bind_engine` 互斥。
    """

    def test_make_workflow_ctx_binds_dag(self):
        import serve
        ctx = serve.WorkflowRegistry._make_workflow_ctx(
            type("S", (), {"name": "rel"})(), {"user_id": "u", "tenant": "t"}, None)
        self.assertIsNotNone(ctx)
        self.assertEqual(ctx.engine, "dag", "workflow Context 必须绑 dag")

    def test_independent_ctx_does_not_conflict(self):
        """两个 workflow 各有各的 Context，互不干扰。"""
        import serve
        mk = serve.WorkflowRegistry._make_workflow_ctx
        a = mk(type("S", (), {"name": "a"})(), {"user_id": "u", "tenant": "t"}, None)
        b = mk(type("S", (), {"name": "b"})(), {"user_id": "u", "tenant": "t"}, None)
        self.assertIsNot(a, b)
        self.assertEqual(a.engine, "dag")
        self.assertEqual(b.engine, "dag")

    def test_ctx_initial_has_principal(self):
        import serve
        ctx = serve.WorkflowRegistry._make_workflow_ctx(
            type("S", (), {"name": "r"})(), {"user_id": "alben", "tenant": "acme"}, None)
        self.assertEqual(ctx.get("principal", {}).get("user_id"), "alben")

    def test_end_to_end_state_lands(self):
        """真跑一条 DAG，`workflow_state` 必须落进 Context。"""
        from fastapi.testclient import TestClient
        import serve
        from core.observability import JsonLogger, Metrics, ShutdownCoordinator

        captured = {}

        class Rec:
            def dispatch(self, spec, parent_ctx, parent_e):
                captured["ctx"] = parent_ctx
                return {"ok": True, "steps": 1, "text": "x"}

        class G:
            def handle(self, r):
                class R:
                    ok = True
                    text = "ok"
                    steps = 1
                    phase = "DONE"
                    term_reason = "DONE"
                    error = ""
                    audit_id = "a"
                    task_id = "t"
                    latency_ms = 1.0
                    vertex: dict = {}
                    partial = False
                return R()

        app = serve.create_app(
            gateway=G(), metrics=Metrics(), logger=JsonLogger(sink=lambda *a, **k: None),
            shutdown=ShutdownCoordinator(), deps={"store": None}, authenticator=None,
            workflow_runner_factory=lambda p: Rec())
        c = TestClient(app)
        res = c.post("/v1/workflows", json={
            "name": "w",
            "nodes": [{"name": "a", "goal": "x"},
                      {"name": "b", "goal": "y", "depends_on": ["a"]}],
            "principal": {"user_id": "u", "tenant": "acme", "roles": ["r"],
                          "scopes": ["runtime:run"]}})
        self.assertEqual(res.status_code, 202)
        import time
        for _ in range(40):
            if captured.get("ctx") is not None and captured["ctx"].get("workflow_state"):
                break
            time.sleep(0.05)
        ctx = captured.get("ctx")
        self.assertIsNotNone(ctx)
        state = ctx.get("workflow_state")
        self.assertTrue(state, "workflow_state 没落进 Context —— DAG 无法复盘")
        self.assertEqual(state["layers"], 2)
        self.assertEqual([n["name"] for n in state["nodes"]], ["a", "b"])
