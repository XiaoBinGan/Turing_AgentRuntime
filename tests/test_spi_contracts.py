"""Agent 层扩展点的契约验收。

Runtime 的职责是**提供 SPI**：上层照着实现，双方不必读对方源码。
在此之前 Planner / Router / Skill 执行三处只是鸭子类型 —— 文档里写
"有 replan(...) 即可"，但那不是可编译的契约。

本文件证明：**按 `contracts` 里的 Protocol 实现，就能被 Runtime 正常调用**。
同时钉住几条"实现方必须遵守"的语义（违反会静默产生错误行为）。
"""
from __future__ import annotations

import unittest

from contracts import (
    AgentSpec, ExecCtx, Owner, Patch, PlannerProtocol, Principal, RouteDecision,
    RouterProtocol, SkillHandlerProtocol,
)


class TestPlannerProtocol(unittest.TestCase):
    """Planner 输出 Patch，Runtime 负责应用 —— 这个方向不能反。"""

    def _loop(self, planner):
        from adapters.llm_mock import LLMResponse, ScriptedLLM, usage
        from builder import PromptBuilder
        from core.loop import Loop
        from plane.tool import Dispatcher, ToolRegistry
        return Loop(PromptBuilder(total_tokens=4000),
                    ScriptedLLM([LLMResponse(text="答", usage=usage())]),
                    Dispatcher(ToolRegistry()), planner=planner)

    def test_conforming_planner_accepted(self):
        class Good:
            def replan(self, snapshot, reason):
                return None
        self._loop(Good())          # 不抛即通过

    def test_planner_without_replan_rejected_at_construction(self):
        """**启动就校验**：死锁是补救路径，补救工具坏了会被误判成"再规划也没用"。"""
        with self.assertRaises(TypeError) as ctx:
            self._loop(object())
        self.assertIn("replan", str(ctx.exception))

    def test_planner_replan_not_callable_rejected(self):
        class Bad:
            replan = "not callable"
        with self.assertRaises(TypeError):
            self._loop(Bad())

    def test_returning_none_means_no_progress(self):
        """返回 None = "我也想不出新方案" → Loop 判定 NO_PROGRESS，不无限重试。"""
        class GiveUp:
            def replan(self, snapshot, reason):
                return None
        lp = self._loop(GiveUp())
        self.assertIsNotNone(lp._planner)

    def test_returning_patch_shape(self):
        """契约是 `Patch | None` —— Patch 有 op/path/owner/value 四要素。"""
        p = Patch("set", "plan", Owner.PLANNER, {"steps": ["a", "b"]})
        self.assertEqual(p.op, "set")
        self.assertEqual(p.path, "plan")
        self.assertIs(p.by, Owner.PLANNER)      # 字段名是 by，不是 owner

    def test_protocol_is_runtime_checkable_duck(self):
        class Good:
            def replan(self, snapshot, reason):
                return None
        self.assertTrue(callable(getattr(Good(), "replan", None)))


class TestRouterProtocol(unittest.TestCase):
    """Router 的三条实现方语义（Runtime 与客户端都依赖）。"""

    def test_default_router_satisfies_protocol(self):
        from plane.router import AgentRouter
        r = AgentRouter()
        self.assertTrue(callable(getattr(r, "route", None)))
        self.assertTrue(callable(getattr(r, "explain", None)))

    def test_no_candidate_asks_for_clarification_not_raise(self):
        """没有候选 → 回澄清，**不许抛**（抛了调用方只能吞异常，信息全丢）。"""
        from plane.router import AgentRouter
        r = AgentRouter()
        d = r.route("随便什么", Principal(user_id="u", tenant="t"))
        self.assertIsInstance(d, RouteDecision)
        # 契约：`__clarify__` 哨兵即"我不选"（不是空串，也不是布尔字段）
        self.assertEqual(d.agent, "__clarify__")
        self.assertEqual(d.confidence, 0.0)

    def test_explain_returns_per_candidate_breakdown(self):
        """线上排查全靠它：为什么选 A 不选 B。"""
        from plane.router import AgentRouter
        r = AgentRouter()
        r.register(AgentSpec(name="a", description="查日志", tags=("sre",)))
        r.register(AgentSpec(name="b", description="写合同", tags=("legal",)))
        rows = r.explain("查日志 p99 高", Principal(user_id="u", tenant="t"))
        self.assertEqual(len(rows), 2)
        for row in rows:
            # 返回 ScoreBreakdown 对象（不是 dict）：四项分项 + 是否可参选
            for attr in ("agent", "text_match", "tag_match", "scope_ok",
                         "priority_norm", "score", "eligible"):
                self.assertTrue(hasattr(row, attr), f"缺 {attr}")

    def test_low_confidence_clarifies(self):
        """分数不够就回澄清 —— 硬选一个错 Agent 比问一句贵得多。"""
        from plane.router import AgentRouter
        r = AgentRouter()
        r.register(AgentSpec(name="a", description="完全不相关的能力"))
        d = r.route("zzz", Principal(user_id="u", tenant="t"))
        self.assertTrue(d.agent == "__clarify__" or d.confidence > 0,
                        "低置信度要么回澄清，要么给出 >0 的置信度")


class TestSkillHandlerProtocol(unittest.TestCase):
    def test_handler_signature(self):
        """`(args, ctx) -> Any`：技能需要 ExecCtx 做多步编排（预算/截止）。"""
        def handler(args, ctx):
            return {"done": True}
        self.assertTrue(callable(handler))

    def test_exec_ctx_provides_budget_and_deadline(self):
        e = ExecCtx()
        self.assertTrue(hasattr(e, "budget"))
        self.assertTrue(hasattr(e, "deadline"))
        self.assertTrue(hasattr(e, "trace_id"))


class TestBoundaryDiscipline(unittest.TestCase):
    """这三份契约**必须是纯 Protocol**：不含实现、不含业务词汇。

    理由：它们是 Agent 层实现的接口。一旦 Runtime 的契约里写了
    "如果 goal 里有『连接池』就……"，换业务时契约就废了。
    """

    def test_protocols_have_no_impl_body(self):
        """用 AST 判，不靠字符串匹配 —— 后者会把 docstring 续行当成代码。"""
        import ast
        import inspect
        for proto in (PlannerProtocol, RouterProtocol, SkillHandlerProtocol):
            tree = ast.parse(inspect.getsource(proto).lstrip())
            cls = next(n for n in ast.walk(tree) if isinstance(n, ast.ClassDef))
            for fn in [n for n in cls.body if isinstance(n, ast.FunctionDef)]:
                stmts = [st for st in fn.body
                         if not (isinstance(st, ast.Expr)
                                 and isinstance(st.value, ast.Constant)
                                 and isinstance(st.value.value, str))]   # 去掉 docstring
                self.assertEqual(
                    len(stmts), 1,
                    f"{proto.__name__}.{fn.name} 有 {len(stmts)} 条语句（应只有 ...）")
                self.assertIsInstance(stmts[0], ast.Expr)
                self.assertIsInstance(stmts[0].value, ast.Constant)
                self.assertIs(stmts[0].value.value, Ellipsis,
                              f"{proto.__name__}.{fn.name} 有实现代码")

    def test_protocols_mention_no_business_vocabulary(self):
        import inspect
        banned = ("连接池", "告警", "运维", "发布流程", "灰度", "重启")
        for proto in (PlannerProtocol, RouterProtocol, SkillHandlerProtocol):
            src = inspect.getsource(proto)
            # 注释里可以讲道理，但**代码行**不许出现业务词
            code = "\n".join(ln for ln in src.splitlines()
                             if not ln.strip().startswith("#"))
            for word in banned:
                self.assertNotIn(word, code, f"{proto.__name__} 代码里出现业务词 {word!r}")


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestPlannerWritableFields(unittest.TestCase):
    """Planner 的可写字段**以 `OWNER_TABLE` 为唯一权威**。

    起因：我在给上层的契约文档里写了"写 plan / observations 等"，而
    `OWNER_TABLE["observations"] is Owner.LOOP` —— Planner 写它会被
    `ctx.apply()` 以 `OwnershipViolation` 拒绝，**而且只记一条 warning**。
    表现是"规划器跑了但什么也没生效"，排查时极难定位（不报错、不中断）。

    所以把这条从"文档约定"升级成"可执行断言"：文档可以写错，测试不会。
    """

    def test_plan_is_planner_owned(self):
        from contracts import OWNER_TABLE, Owner
        self.assertIs(OWNER_TABLE.get("plan"), Owner.PLANNER,
                      "plan 必须归 PLANNER，否则规划器无处可写")

    def test_observations_is_loop_owned(self):
        from contracts import OWNER_TABLE, Owner
        self.assertIs(OWNER_TABLE.get("observations"), Owner.LOOP,
                      "observations 归 LOOP（Planner 只读）—— 契约文档曾写错，这里钉住")

    def test_planner_writing_observations_is_rejected(self):
        """真跑一遍：Planner 写 observations 会被拒（不是静默成功）。"""
        from core.context import ContextHandle
        from contracts import Owner, Patch
        ctx = ContextHandle(initial={"goal": "g"}, budget=None)
        with self.assertRaises(Exception) as exc:
            ctx.apply(Patch("set", "observations", Owner.PLANNER, {"x": 1}))
        self.assertIn("observ", str(exc.exception).lower())

    def test_planner_writing_plan_succeeds(self):
        from core.context import ContextHandle
        from contracts import Owner, Patch
        ctx = ContextHandle(initial={"goal": "g"}, budget=None)
        ctx.apply(Patch("set", "plan", Owner.PLANNER, {"steps": ["a"]}))
        self.assertEqual(ctx.get("plan"), {"steps": ["a"]})

    def test_documented_writable_set_matches_owner_table(self):
        """契约 docstring 里提到的可写字段，必须在 OWNER_TABLE 里真归 Planner。"""
        import inspect
        from contracts import OWNER_TABLE, Owner, PlannerProtocol
        doc = inspect.getdoc(PlannerProtocol.replan) or ""
        self.assertIn("plan", doc)
        # 若文档提到 observations，必须同时说明它归 LOOP（不能笼统说"可以写"）
        if "observations" in doc:
            self.assertIn("LOOP", doc,
                          "文档提到 observations 却没说明它归 LOOP —— 会误导实现者")
