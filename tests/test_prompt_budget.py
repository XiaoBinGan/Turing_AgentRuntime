"""tools schema 开销核算的验收测试（W18）。

要修的账实不符：Builder 把工具渲染成**人读文本**

    - query_logs [read_only/T0_SAFE]: query logs

而 adapter 实际发出去的是 **JSON Schema**（且带 ``input_schema`` 之后更长）。
两者的 token 量差 2~3 倍，于是预算说「还有余量」而真实请求已经超窗；
或者反过来，为了腾地方裁掉了 history，而真正占地方的是 schema。

本文件盯 4 件事：

1. ``estimate_tool_schema_tokens`` 按 **adapter 的形状**算，且带固定结构开销常数；
2. ``PromptBuilder`` 真的把这笔钱从可用预算里扣掉，并遵守 40% 地板价；
3. ``reserve_tool_schema=False`` 与引入本参数之前**逐字节一致**（老调用零影响）；
4. 全部新逻辑是**纯函数**（同参数同结果、同快照同字节）。

实测参考值（本次的标定来源）：186 字符的 function schema → 真实请求 +258 tok。
本文件 ``test_magnitude_matches_measurement`` 用一个 185 字符的同形状工具
（估算 252 tok）与它对表。
"""
from __future__ import annotations

import unittest

import builder as builder_mod
from adapters.openai_compat import OpenAICompatAdapter
from builder import (
    CALIBRATED,
    DEFAULT_PROFILE,
    MIN_AVAILABLE_BUDGET_RATIO,
    TOOL_SCHEMA_FIXED_OVERHEAD_TOKENS,
    PromptBuilder,
    estimate_tokens,
    estimate_tool_schema_tokens,
    tool_schema_request_payload,
)
from contracts import ContextSnapshot, RiskTier, SideEffect, ToolManifest

# ── 工具工厂 ─────────────────────────────────────────────────────
TYPICAL_SCHEMA = {
    "type": "object",
    "properties": {
        "service": {"type": "string", "description": "service name"},
        "lines": {"type": "integer", "minimum": 1, "maximum": 500},
        "level": {"type": "string", "enum": ["debug", "info", "warn", "error"]},
    },
    "required": ["service"],
    "additionalProperties": False,
}


def tool(name: str = "query_logs", description: str = "query logs",
         schema: dict | None = None,
         side_effect: SideEffect = SideEffect.READ_ONLY,
         risk_tier: RiskTier = RiskTier.T0_SAFE) -> ToolManifest:
    return ToolManifest(name=name, side_effect=side_effect, risk_tier=risk_tier,
                        description=description, input_schema=schema)


#: 与实测标定那条 186 字符 schema 同量级的「典型工具」
PLAIN = tool()
#: 同名同描述、只多了 input_schema 的对照工具（验证「形状变长 ≠ 文本变长」）
WITH_SCHEMA = tool(schema=TYPICAL_SCHEMA)
#: 3 个工具：验证单调增长
TRIO = [tool("a_tool", "does a"), tool("b_tool", "does b"), tool("c_tool", "does c")]


def crowded(n: int = 20, schema: dict | None = TYPICAL_SCHEMA) -> list[ToolManifest]:
    """n 个工具：用来把预算吃穿、触发 clamp。"""
    return [tool(f"tool_{i:02d}", f"does thing number {i}", schema) for i in range(n)]


def snap(**over) -> ContextSnapshot:
    data = {
        "principal": {"tenant": "acme", "roles": ["sre"], "user_id": "u1"},
        "phase": "EXECUTING",
        "loop_count": 2,
        "memory_hits": [{"kind": "episodic", "text": "pool issue fixed by restart",
                         "score": 0.71}],
        "knowledge_hits": [{"doc_id": "runbook", "span": "3.2",
                            "text": "pool cap is 20"}],
        "history": [{"role": "user", "text": "api timeout"}],
        "tool_results": [{"tool": "query_logs", "payload": {"p99_ms": 3200},
                          "trusted": False}],
    }
    data.update(over)
    return ContextSnapshot(version=3, data=data)


def knowledge_heavy_snap() -> ContextSnapshot:
    """知识段刚好卡在「有无工具开销」的分界线上：预算被扣就整段丢弃。"""
    return snap(knowledge_hits=[
        {"doc_id": f"doc{i}", "span": "1.0", "text": "x" * 120} for i in range(5)
    ])


def heavy_snap() -> ContextSnapshot:
    """内容塞满各段上限的 snapshot：用来比较「新旧两种账法」的产出大小。"""
    return ContextSnapshot(version=3, data={
        "principal": {"tenant": "acme", "roles": ["sre"], "user_id": "u1"},
        "phase": "EXECUTING",
        "loop_count": 2,
        "memory_hits": ([{"kind": "profile", "text": "p" * 700, "score": 0.9}]
                        + [{"kind": "episodic", "text": "e" * 200, "score": 0.7}
                           for _ in range(6)]),
        "knowledge_hits": [{"doc_id": f"d{i}", "span": "1", "text": "k" * 300}
                           for i in range(6)],
        "history": [{"role": "user", "text": "h" * 300} for _ in range(8)],
        "tool_results": [{"tool": "query_logs", "payload": {"x": "y" * 500}}
                         for _ in range(6)],
    })


#: 引入 ``reserve_tool_schema`` 之前（builder.py mtime 2026-11-23 01:15）的
#: ``PromptBuilder(total_tokens=1000).build(snapshot, TOOLS)`` 输出，逐字节抄录。
#: 抄录方式：把改动前的 builder.py 拷到临时模块里执行同一个 build 再对比（已核对相等）。
GOLDEN_OUTPUT = "\n\n".join([
    "<<<TOOLS>>>\n- query_logs [read_only/T0_SAFE]: query logs\n"
    "- restart_service [irreversible/T3_CRITICAL]: restart a service",
    "<<<TOOL_RESULTS>>>\n- query_logs: p99_ms=3200 <external_data_untrusted>\n"
    "</external_data_untrusted>",
    "<<<HISTORY>>>\n- user: api timeout",
    "<<<MEMORY>>>\n- [episodic 0.71] pool issue fixed by restart",
    "<<<KNOWLEDGE>>>\n- (runbook#3.2) pool cap is 20",
    "<<<STATE>>>\nphase=EXECUTING step=2",
])

GOLDEN_TOOLS = [
    tool("query_logs", "query logs"),
    tool("restart_service", "restart a service",
         side_effect=SideEffect.IRREVERSIBLE, risk_tier=RiskTier.T3_CRITICAL),
]


class TestEstimateToolSchemaTokens(unittest.TestCase):
    """① 估算函数本身：语义、形状、常数、量级。"""

    def test_01_empty_tools_cost_zero_not_constant(self):
        """无工具 → 0（不是 200）：adapter 在 tools 为空时根本不发 tools 键。"""
        self.assertEqual(estimate_tool_schema_tokens([]), 0)
        self.assertEqual(estimate_tool_schema_tokens(()), 0)
        self.assertEqual(estimate_tool_schema_tokens(None), 0)

    def test_02_single_tool_costs_more_than_zero(self):
        self.assertGreater(estimate_tool_schema_tokens([PLAIN]), 0)

    def test_03_overhead_grows_with_tool_count(self):
        """1 → 3 → 10 个工具，开销单调递增（且每加一个都真的加钱）。"""
        one = estimate_tool_schema_tokens(crowded(1))
        three = estimate_tool_schema_tokens(crowded(3))
        ten = estimate_tool_schema_tokens(crowded(10))
        self.assertLess(one, three)
        self.assertLess(three, ten)
        # 单调到「每多一个非空工具就严格变大」
        steps = [estimate_tool_schema_tokens(crowded(n)) for n in (1, 2, 3)]
        self.assertEqual(steps, sorted(steps))
        self.assertEqual(len(set(steps)), len(steps))

    def test_04_input_schema_costs_more_than_plain_twin(self):
        """带 input_schema 的工具 ≫ 同名无 schema 的工具（形状变长就是真金白银）。"""
        plain = estimate_tool_schema_tokens([PLAIN])
        enriched = estimate_tool_schema_tokens([WITH_SCHEMA])
        self.assertGreater(enriched, plain)
        self.assertGreaterEqual(enriched - plain, 40,
                                f"schema 只带来 {enriched - plain} tok 差异，太小了")

    def test_05_fixed_constant_is_included(self):
        """常数确实被算进去：1 个工具的开销 ≥ 固定常数，且严格大于纯字符估算。"""
        self.assertEqual(TOOL_SCHEMA_FIXED_OVERHEAD_TOKENS, 200)
        one = estimate_tool_schema_tokens([PLAIN])
        self.assertGreaterEqual(one, TOOL_SCHEMA_FIXED_OVERHEAD_TOKENS)
        # 去掉常数后剩下的「按字符算」部分必须 > 0，说明常数是**额外**的
        self.assertGreater(one - TOOL_SCHEMA_FIXED_OVERHEAD_TOKENS, 0)
        # 形态上：常数 + 逐项字符估算
        body = sum(
            estimate_tokens(
                builder_mod._schema_text(tool_schema_request_payload(m)), DEFAULT_PROFILE)
            for m in TRIO
        )
        self.assertEqual(estimate_tool_schema_tokens(TRIO),
                         TOOL_SCHEMA_FIXED_OVERHEAD_TOKENS + body)

    def test_06_magnitude_matches_measurement(self):
        """与真实值同量级：典型工具落在 200~800，且贴着实测的 258。"""
        one = estimate_tool_schema_tokens([PLAIN])
        self.assertGreaterEqual(one, 200)
        self.assertLessEqual(one, 800)
        # 实测参考：186 字符 schema → +258 tok；本工具的 schema 是 185 字符
        self.assertLessEqual(abs(one - 258), 60,
                             f"估算 {one} tok 离实测 258 太远，常数值需要重标定")

    def test_07_sensitive_to_schema_size(self):
        """input_schema 越大开销越大（严格单调）。"""
        small = {"type": "object", "properties": {"a": {"type": "string"}}}
        medium = {"type": "object",
                  "properties": {f"p{i}": {"type": "string"} for i in range(6)}}
        large = {"type": "object",
                 "properties": {f"p{i}": {"type": "string", "description": "x" * 20}
                                for i in range(20)}}
        costs = [estimate_tool_schema_tokens([tool(schema=s)])
                 for s in (small, medium, large)]
        self.assertLess(costs[0], costs[1])
        self.assertLess(costs[1], costs[2])
        self.assertGreater(costs[2] - costs[0], 50)

    def test_08_none_and_empty_schema_are_safe(self):
        """``input_schema=None`` 与 ``{}`` 都不炸。

        语义差别照实记：``None`` / 非 dict / **空 dict** 一律 → 保守容器。
        空 dict 曾被原样发出（``"parameters": {}``），而它不合法（严格服务 400），已修正。
        """
        none_tool = tool(schema=None)
        empty_tool = tool(schema={})
        plain_params = {"type": "object",
                        "properties": {"args": {"type": "object"}},
                        "required": ["args"]}

        self.assertEqual(tool_schema_request_payload(none_tool)["function"]["parameters"],
                         plain_params)
        # 空 dict 当「没声明」处理：parameters 是必填的完整 schema，
        # 发 {} 出去严格服务会 400。（最初是原样发 {}，已修正 —— 这条断言随之更新）
        self.assertEqual(tool_schema_request_payload(empty_tool)["function"]["parameters"],
                         plain_params)
        # 两者都安全、都计入固定开销，且现在开销相同（形状一致）
        none_cost = estimate_tool_schema_tokens([none_tool])
        empty_cost = estimate_tool_schema_tokens([empty_tool])
        self.assertGreaterEqual(none_cost, TOOL_SCHEMA_FIXED_OVERHEAD_TOKENS)
        self.assertGreaterEqual(empty_cost, TOOL_SCHEMA_FIXED_OVERHEAD_TOKENS)
        self.assertEqual(empty_cost, none_cost)
        # 非 dict 的脏值也不能抛，按「没声明」处理
        self.assertEqual(estimate_tool_schema_tokens([tool(schema="not-a-dict")]), none_cost)
        self.assertEqual(estimate_tool_schema_tokens([tool(schema=None)]), none_cost)

    def test_09_shape_matches_adapter_conservative_path(self):
        """形状与 adapter 一致（不 import，但必须对得上）。

        这条是**防漂移哨兵**：改 ``adapters/openai_compat.py`` 的 tools 形状时，
        ``builder._request_parameters`` 必须同步，否则预算又会和真实请求脱节。
        覆盖三条路径：无 description、保守 parameters、input_schema 原样发送。
        """
        for m in (PLAIN,
                  tool("restart", "", side_effect=SideEffect.IRREVERSIBLE,
                       risk_tier=RiskTier.T3_CRITICAL),
                  WITH_SCHEMA,
                  tool("empty_schema", "no declared args", {})):
            self.assertEqual(tool_schema_request_payload(m),
                             OpenAICompatAdapter.tool_schema(m),
                             f"{m.name} 的请求侧形状与 adapter 不一致")
        # 序列化口径也要一致（adapter 用 json.dumps(..., ensure_ascii=False)）
        text = builder_mod._schema_text(tool_schema_request_payload(PLAIN))
        self.assertIn('"type": "function"', text)

    def test_10_estimate_differs_from_builder_rendered_text(self):
        """关键：不能拿 Builder 渲染的那段人读文本当估算（两者差 2~3 倍以上）。"""
        b = PromptBuilder()
        rendered = estimate_tokens(b._render_tools(TRIO), DEFAULT_PROFILE)
        schema = estimate_tool_schema_tokens(TRIO, DEFAULT_PROFILE)
        self.assertGreater(schema, rendered * 2,
                           f"schema 估算 {schema} vs 渲染文本 {rendered}：没体现出形状差异")

    def test_11_profile_affects_estimate(self):
        """档位影响的是「字符折算」那部分；固定开销与档位无关、可独立标定。"""
        from dataclasses import replace

        # 只比正文部分：把固定项拉到 0，隔离出字符折算的差异
        flat_default = replace(DEFAULT_PROFILE, tool_overhead_fixed=0.0)
        flat_cal = replace(CALIBRATED, tool_overhead_fixed=0.0)
        self.assertLess(estimate_tool_schema_tokens([WITH_SCHEMA], flat_cal),
                        estimate_tool_schema_tokens([WITH_SCHEMA], flat_default))

        # 固定项仍被计入，且按档位取值（CALIBRATED 取实测上界，比默认大）
        self.assertGreaterEqual(estimate_tool_schema_tokens([WITH_SCHEMA], CALIBRATED),
                                int(CALIBRATED.tool_overhead_fixed))
        self.assertGreaterEqual(estimate_tool_schema_tokens([WITH_SCHEMA], DEFAULT_PROFILE),
                                int(DEFAULT_PROFILE.tool_overhead_fixed))

        # 标定档对「一个典型工具」的总开销必须贴近实测值 274（±30）
        self.assertLess(abs(estimate_tool_schema_tokens([WITH_SCHEMA], CALIBRATED) - 274), 30)

    def test_12_order_independent(self):
        """工具顺序不影响总额（加法可交换）——否则排序策略会悄悄改预算。"""
        self.assertEqual(estimate_tool_schema_tokens(TRIO),
                         estimate_tool_schema_tokens(list(reversed(TRIO))))


class TestBudgetBreakdown(unittest.TestCase):
    """② budget_breakdown：账要能对上，clamp 要有地板价。"""

    def test_13_not_clamped_sums_to_total(self):
        b = PromptBuilder(total_tokens=4000)
        bd = b.budget_breakdown(TRIO)
        self.assertEqual(set(bd), {"total", "tool_overhead", "available", "clamped"})
        self.assertFalse(bd["clamped"])
        self.assertEqual(bd["available"] + bd["tool_overhead"], bd["total"])
        self.assertEqual(bd["tool_overhead"], estimate_tool_schema_tokens(TRIO))
        self.assertEqual(bd["total"], 4000)
        self.assertLess(bd["available"], 4000)

    def test_14_clamp_respects_floor(self):
        """工具极多 → 可用预算被压到 40% 地板价，但绝不为 0 或负数。"""
        total = 1000
        many = crowded(20)
        bd = PromptBuilder(total_tokens=total).budget_breakdown(many)
        floor = int(total * MIN_AVAILABLE_BUDGET_RATIO)
        self.assertEqual(floor, 400)
        self.assertGreaterEqual(bd["available"], floor)
        self.assertGreaterEqual(bd["available"], total * MIN_AVAILABLE_BUDGET_RATIO)
        self.assertGreater(bd["available"], 0)
        self.assertGreater(bd["tool_overhead"], bd["total"])   # 触发 clamp 的前提
        self.assertEqual(bd["available"], floor)               # 正好压在地板上

    def test_15_clamped_flag(self):
        self.assertTrue(PromptBuilder(total_tokens=1000).budget_breakdown(crowded(20))["clamped"])
        self.assertFalse(PromptBuilder(total_tokens=1000).budget_breakdown([PLAIN])["clamped"])
        self.assertFalse(PromptBuilder(total_tokens=1000).budget_breakdown([])["clamped"])
        # 预算充裕的大窗口里 4 个工具不该触发 clamp
        self.assertFalse(PromptBuilder(total_tokens=8000).budget_breakdown(crowded(4))["clamped"])

    def test_16_legacy_mode_reports_zero_overhead(self):
        """reserve=False → 账上就是「没预留」，如实反映 build 的行为。"""
        b = PromptBuilder(total_tokens=1000, reserve_tool_schema=False)
        bd = b.budget_breakdown(crowded(20))
        self.assertEqual(bd, {"total": 1000, "tool_overhead": 0,
                              "available": 1000, "clamped": False})

    def test_17_pure(self):
        """纯函数：同参数两次 breakdown 相等；不因调用次数改变期望。"""
        b = PromptBuilder(total_tokens=1234)
        first = b.budget_breakdown(TRIO)
        second = b.budget_breakdown(list(TRIO))
        self.assertEqual(first, second)
        # 调用 breakdown 不改变 builder 自身状态（build 之后也一样）
        _ = b.build(snap(), TRIO)
        self.assertEqual(b.budget_breakdown(TRIO), first)
        # 两个独立实例给同一答案
        self.assertEqual(PromptBuilder(total_tokens=1234).budget_breakdown(TRIO), first)

    def test_18_zero_total_is_safe(self):
        bd = PromptBuilder(total_tokens=0).budget_breakdown([PLAIN])
        self.assertEqual(bd["available"], 0)
        self.assertTrue(bd["clamped"])
        self.assertEqual(PromptBuilder(total_tokens=0).build(snap(), [PLAIN]),
                         PromptBuilder(total_tokens=0).build(snap(), [PLAIN]))


class TestReserveInBuild(unittest.TestCase):
    """③ build() 真的按预留后的预算工作，且老行为一字不改。"""

    def test_19_legacy_mode_is_byte_identical(self):
        """``reserve_tool_schema=False`` → 与引入本参数之前逐字节一致。"""
        legacy = PromptBuilder(total_tokens=1000, reserve_tool_schema=False)
        self.assertEqual(legacy.build(snap(), GOLDEN_TOOLS), GOLDEN_OUTPUT)
        # 老调用（不传关键字）在同快照上配 reserve=False 才是旧行为
        self.assertEqual(PromptBuilder(total_tokens=1000,
                                       reserve_tool_schema=False).build(snap(), GOLDEN_TOOLS),
                         GOLDEN_OUTPUT)

    def test_20_default_reserves_and_shrinks_budget(self):
        """默认 True：可用预算变小，且差额正好等于 tools 开销。"""
        legacy = PromptBuilder(total_tokens=4000, reserve_tool_schema=False)
        default = PromptBuilder(total_tokens=4000)
        self.assertEqual(default.reserve_tool_schema, True)
        legacy_bd, default_bd = legacy.budget_breakdown(TRIO), default.budget_breakdown(TRIO)
        self.assertEqual(legacy_bd["available"], 4000)
        self.assertEqual(default_bd["available"],
                         4000 - estimate_tool_schema_tokens(TRIO, default.profile))
        self.assertLess(default_bd["available"], legacy_bd["available"])

    def test_21_tool_overhead_actually_eats_knowledge(self):
        """核心用例：同一 snapshot，工具多 → knowledge 先被丢掉；工具为 0 → 它还在。

        这就是「预算真的被 tools schema 占走了」的可观测证据。
        """
        s = knowledge_heavy_snap()
        b = PromptBuilder(total_tokens=1000)

        without_tools = b.build(s, [])
        self.assertIn("<<<KNOWLEDGE>>>", without_tools)
        bd_none = b.budget_breakdown([])
        self.assertEqual(bd_none["tool_overhead"], 0)

        with_tools = b.build(s, crowded(20))
        self.assertNotIn("<<<KNOWLEDGE>>>", with_tools)
        bd_many = b.budget_breakdown(crowded(20))
        self.assertGreater(bd_many["tool_overhead"], 0)
        self.assertLess(bd_many["available"], bd_none["available"])

        # 隔离变量：同一批工具 + 同一 snapshot，只关掉预留 → knowledge 又回来了
        legacy = PromptBuilder(total_tokens=1000, reserve_tool_schema=False)
        self.assertIn("<<<KNOWLEDGE>>>", legacy.build(s, crowded(20)))

    def test_22_reserve_keeps_prompt_inside_window(self):
        """账实相符的意义：同一批工具 + 同一 snapshot，新账法产出的 prompt 塞得进窗，
        旧账法（只数 prompt、不数 schema）产出的 prompt 塞不进。"""
        total = 2000
        many = crowded(6)
        s = heavy_snap()
        reserved_b = PromptBuilder(total_tokens=total)
        legacy_b = PromptBuilder(total_tokens=total, reserve_tool_schema=False)
        bd = reserved_b.budget_breakdown(many)
        self.assertFalse(bd["clamped"])                       # 这一例不靠地板价兜底
        self.assertEqual(bd["available"] + bd["tool_overhead"], total)

        reserved = estimate_tokens(reserved_b.build(s, many), DEFAULT_PROFILE)
        legacy = estimate_tokens(legacy_b.build(s, many), DEFAULT_PROFILE)

        # 新账法：prompt + schema 开销 仍在窗口内
        self.assertLessEqual(reserved + bd["tool_overhead"], total)
        # 旧账法：账面只算 prompt，真实字节多出整笔 schema 开销 → 超窗
        self.assertGreater(legacy + bd["tool_overhead"], total)
        # 更直接的说法：旧账法产出的 prompt 根本塞不进「真实可用空间」
        self.assertGreater(legacy, bd["available"])

    def test_23_build_is_deterministic_with_tools(self):
        """纯函数性：同快照两次 build 字节一致；工具顺序不影响产出。"""
        b = PromptBuilder(total_tokens=2000)
        s = snap()
        first, second = b.build(s, crowded(6)), b.build(s, crowded(6))
        self.assertEqual(first, second)
        self.assertEqual(b.build(s, list(reversed(crowded(6)))),
                         b.build(s, crowded(6)))
        # 不同 profile 的两个实例互不影响
        self.assertEqual(PromptBuilder(total_tokens=2000, profile=CALIBRATED).build(s, TRIO),
                         PromptBuilder(total_tokens=2000, profile=CALIBRATED).build(s, TRIO))

    def test_24_legacy_fit_signature_still_works(self):
        """``_fit(sections)`` 单参数老调用不炸（budget=None → total_tokens）。"""
        b = PromptBuilder(total_tokens=900)
        sections = {"system": "s", "tools": "", "tool_results": "", "history": "h" * 400,
                    "memory": "", "knowledge": "k" * 2000}
        out = b._fit(sections)
        self.assertEqual(out["system"], "s")
        self.assertEqual(out["knowledge"], "")
        self.assertLessEqual(estimate_tokens(out["history"], DEFAULT_PROFILE), 180)

    def test_25_knowledge_priority_unchanged_when_tools_absent(self):
        """没工具时，新预算路径与旧行为完全等价（空工具 = 零开销 = 不 clamp）。"""
        s = knowledge_heavy_snap()
        self.assertEqual(PromptBuilder(total_tokens=1000).build(s, []),
                         PromptBuilder(total_tokens=1000,
                                       reserve_tool_schema=False).build(s, []))


if __name__ == "__main__":
    unittest.main(verbosity=2)
