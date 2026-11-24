"""C3a 验收：子 Agent 卡在权限/审批时，父级要拿到**结构化原因**。

为什么不能只是 `ok=False`：那和"子 Agent 跑挂了"无法区分。
父级只能整体重跑 —— 白重复只读取证、烧预算，人也不知道该批什么。

实测发现的关键事实：子 Agent 被**降权为只读**（INV-18），
所以 T2 写工具在**静态白名单**阶段就被过滤，根本到不了审批点。
因此 C3a 的正确形态是「告知父级：它想写但被降权拒了」，
而不是「等人工批准」。
"""
from __future__ import annotations

import unittest

from builder import PromptBuilder
from contracts import (
    ExecCtx, LLMResponse, NormalizedToolCall, RiskTier, SideEffect, SubagentSpec,
    ToolManifest, Usage,
)
from core.context import ContextHandle
from core.observability import QueueProgressSink
from plane.tool import Dispatcher, ToolRegistry
from plane.workflow import SubagentRunner

P = {"user_id": "u", "tenant": "acme", "roles": ["r"],
     "scopes": ["runtime:run", "tool.read", "tool.write"]}


def manifest(name: str, *, effect=SideEffect.READ_ONLY, tier=RiskTier.T0_SAFE):
    return ToolManifest(name=name, side_effect=effect, risk_tier=tier,
                        description=f"工具 {name}",
                        input_schema={"type": "object", "properties": {}})


def runner(*, registry=None, calls=None):
    reg = registry if registry is not None else ToolRegistry()
    reg.register(manifest("read1"), lambda a, e: {"v": 1})
    reg.register(manifest("restart", effect=SideEffect.MUTATING,
                          tier=RiskTier.T2_HIGH), lambda a, e: {"ok": 1})

    def llm_factory(spec):
        class Sub:
            model = "sub"

            def __init__(self):
                self.i = 0

            def complete(self, p, t, e):
                self.i += 1
                if calls is not None:
                    calls.append(1)
                return LLMResponse(text=f"{spec.name} done", usage=Usage())
        return Sub()
    return SubagentRunner(Dispatcher(reg), builder=PromptBuilder(total_tokens=4000),
                          llm_factory=llm_factory)


def ctx():
    return ContextHandle(initial={"goal": "parent", "principal": P}, budget=None)


class TestPrivilegeFiltering(unittest.TestCase):
    """子 Agent 拿到的是**只读清单**。"""

    def test_mutating_filtered_out(self):
        r = runner()
        allowed, why = r._resolve_allowlist(SubagentSpec(name="c", goal="x"))
        self.assertEqual(why, "")
        self.assertEqual(sorted(allowed), ["read1"], "写工具必须被过滤")
        self.assertIn("restart", r._last_filtered, "过滤名单要记下来（父级要知道）")

    def test_declared_mutating_rejected(self):
        r = runner()
        allowed, why = r._resolve_allowlist(
            SubagentSpec(name="c", goal="x", allowed_tools=("restart",)))
        self.assertIsNone(allowed)
        self.assertIn("越权", why)

    def test_unknown_tool_rejected(self):
        r = runner()
        allowed, why = r._resolve_allowlist(
            SubagentSpec(name="c", goal="x", allowed_tools=("nope",)))
        self.assertIsNone(allowed)
        self.assertIn("未注册", why)


class TestStructuredDenial(unittest.TestCase):
    """被拒时要把原因结构化带出去。"""

    def test_declared_privilege_denial_is_structured(self):
        r = runner()
        res = r.dispatch(SubagentSpec(name="c", goal="x",
                                      allowed_tools=("restart",)), ctx(), ExecCtx())
        self.assertFalse(res.ok)
        self.assertEqual(res.needs_approval.get("kind"), "privilege_denied")
        self.assertEqual(res.needs_approval.get("agent"), "c")
        self.assertIn("越权", res.needs_approval.get("reason", ""))

    def test_denial_reported_via_progress(self):
        sink = QueueProgressSink()
        r = runner()
        r.dispatch(SubagentSpec(name="c", goal="x", allowed_tools=("restart",)),
                   ctx(), ExecCtx(progress=sink))
        kinds = [x["kind"] for x in sink.drain()]
        self.assertIn("subagent.rejected", kinds,
                      "被拒不播报的话，客户端只看到子 Agent 凭空消失")

    def test_ok_result_has_no_needs_approval(self):
        r = runner()
        res = r.dispatch(SubagentSpec(name="c", goal="x"), ctx(), ExecCtx())
        self.assertTrue(res.ok)
        self.assertEqual(res.needs_approval, {})
        self.assertNotIn("needs_approval", res.to_dict())


class TestRuntimePrivilegeEscalation(unittest.TestCase):
    """运行期调越权工具（模型幻觉）也要拦，不能静默。"""

    def test_hallucinated_tool_blocked(self):
        reg = ToolRegistry()
        reg.register(manifest("read1"), lambda a, e: {"v": 1})
        reg.register(manifest("restart", effect=SideEffect.MUTATING,
                              tier=RiskTier.T2_HIGH), lambda a, e: {"ok": 1})
        r = SubagentRunner(Dispatcher(reg), builder=PromptBuilder(total_tokens=4000),
                           llm_factory=lambda spec: type("S", (), {
                               "model": "s",
                               "complete": lambda self, p, t, e: LLMResponse(
                                   text="调", tool_calls=(NormalizedToolCall(
                                       id="c", name="restart", args={}),),
                                   usage=Usage())})())
        res = r.dispatch(SubagentSpec(name="c", goal="x"), ctx(), ExecCtx())
        # 运行期越权 → ok=False 且不回写父上下文
        self.assertFalse(res.ok)
        self.assertTrue(r.denied, "越权要留痕（供安全度量）")


class TestParentCanAct(unittest.TestCase):
    """父级拿到结构化原因后能做出判断（这是 C3a 的全部意义）。"""

    def test_parent_sees_which_tool_was_denied(self):
        r = runner()
        res = r.dispatch(SubagentSpec(name="c", goal="x",
                                      allowed_tools=("restart",)), ctx(), ExecCtx())
        d = res.to_dict()
        self.assertIn("needs_approval", d)
        self.assertIn("restart", d["needs_approval"]["reason"])

    def test_parent_sees_filtered_tools_after_success(self):
        """成功也要能看出"它本来想用什么但被过滤了"。"""
        r = runner()
        r.dispatch(SubagentSpec(name="c", goal="x"), ctx(), ExecCtx())
        self.assertEqual(r._last_filtered, ("restart",))


if __name__ == "__main__":
    unittest.main(verbosity=2)
