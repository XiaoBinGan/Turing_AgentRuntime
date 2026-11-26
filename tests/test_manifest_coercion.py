"""`ToolManifest` 的构造期收口。

**为什么需要它**：`risk_tier` 与 `side_effect` 有枚举，但 Python 不拦裸 int。
`ToolManifest(risk_tier=0)` 构造成功，然后 `builder._render_tools` 读
`m.risk_tier.name` 抛 `AttributeError` —— 而 `Loop._build_prompt` 把 builder 的
异常**吞成一条 warning** 继续跑。表现是：

    phase=DONE、steps=2、text 正常，但**模型一个工具都看不到**

这是最糟的一类失败：不报错、结果看似正常、能力静默消失。
收口放在构造期，让错误在**定义工具的那一行**爆出来。

这条不是假想：写 quickstart 示例时第一次就写成 `risk_tier=0`，
现象是"工具明明注册了，模型却从来不调用"。
"""
from __future__ import annotations

import unittest

from builder import PromptBuilder
from contracts import RiskTier, SideEffect, ToolManifest
from core.context import ContextHandle


class TestRiskTierCoercion(unittest.TestCase):
    def test_bare_int_coerced(self):
        """裸 int 要变成枚举，而不是留一个没有 `.name` 的 int。"""
        m = ToolManifest(name="t", side_effect=SideEffect.READ_ONLY,
                         risk_tier=0, description="x")
        self.assertIsInstance(m.risk_tier, RiskTier)
        self.assertIs(m.risk_tier, RiskTier.T0_SAFE)
        self.assertEqual(m.risk_tier.name, "T0_SAFE")

    def test_all_valid_ints(self):
        for i, tier in enumerate((RiskTier.T0_SAFE, RiskTier.T1_LOW,
                                  RiskTier.T2_HIGH, RiskTier.T3_CRITICAL)):
            m = ToolManifest(name="t", side_effect=SideEffect.READ_ONLY,
                             risk_tier=i, description="x")
            self.assertIs(m.risk_tier, tier)

    def test_enum_passthrough(self):
        m = ToolManifest(name="t", side_effect=SideEffect.READ_ONLY,
                         risk_tier=RiskTier.T2_HIGH, description="x")
        self.assertIs(m.risk_tier, RiskTier.T2_HIGH)

    def test_invalid_int_rejected(self):
        for bad in (9, -1, 99):
            with self.subTest(v=bad):
                with self.assertRaises(ValueError) as caught:
                    ToolManifest(name="t", side_effect=SideEffect.READ_ONLY,
                                 risk_tier=bad, description="x")
                self.assertIn("不是合法档位", str(caught.exception))

    def test_non_numeric_rejected(self):
        for bad in ("high", None, object()):
            with self.subTest(v=bad):
                with self.assertRaises((ValueError, TypeError)):
                    ToolManifest(name="t", side_effect=SideEffect.READ_ONLY,
                                 risk_tier=bad, description="x")


class TestSideEffectCoercion(unittest.TestCase):
    def test_string_value_coerced(self):
        m = ToolManifest(name="t", side_effect="read_only", risk_tier=0,
                         description="x")
        self.assertIs(m.side_effect, SideEffect.READ_ONLY)

    def test_invalid_rejected(self):
        with self.assertRaises(ValueError):
            ToolManifest(name="t", side_effect="sometimes", risk_tier=0,
                         description="x")


class TestBuilderNoLongerSilentlySwallows(unittest.TestCase):
    """收口之后，builder 不该再遇到没有 `.name` 的枚举。"""

    def test_build_renders_tool_from_bare_int_manifest(self):
        m = ToolManifest(name="my_tool", side_effect=SideEffect.READ_ONLY,
                         risk_tier=0, description="写个天气", input_schema=None)
        ctx = ContextHandle(initial={"goal": "x"}, budget=None)
        out = PromptBuilder().build(ctx.snapshot(), tools=[m])
        self.assertIn("my_tool", out, "工具没进 prompt —— 又静默消失了")


if __name__ == "__main__":
    unittest.main(verbosity=2)
