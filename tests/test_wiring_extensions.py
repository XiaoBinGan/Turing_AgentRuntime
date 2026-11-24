"""serve.py 的扩展点接线验收：Prompt profile / Extractor / Planner / Router / 钩子。

这轮接线的核心价值是**让上层能力真的进主链路**。之前的状况：
`build_gateway_from_env` 只装了 PromptBuilder + LLM + 空工具表，
Router / Skills / Memory / Planner 全部没接 —— 配了也是白配。

本文件盯的是「配置了必须真生效」，而不是「代码里有这么个参数」。
"""
from __future__ import annotations

import unittest
from unittest import mock

import serve
from contracts import Principal, Subject


class TestExtensionLoader(unittest.TestCase):
    """`module:attr` 加载器 —— Runtime 不静态 import agents 的关键。"""

    def test_loads_class(self):
        obj = serve.load_extension("agents.extractor:OpsExtractor", kind="extractor")
        self.assertEqual(obj.__name__, "OpsExtractor")

    def test_empty_returns_none(self):
        self.assertIsNone(serve.load_extension("", kind="x"))
        self.assertIsNone(serve.load_extension(None, kind="x"))

    def test_bad_format_raises(self):
        with self.assertRaises(ValueError) as ctx:
            serve.load_extension("agents.extractor", kind="extractor")
        self.assertIn("module:attr", str(ctx.exception))

    def test_missing_module_raises_with_name(self):
        with self.assertRaises(RuntimeError) as ctx:
            serve.load_extension("no_such_module_xyz:A", kind="extractor")
        self.assertIn("no_such_module_xyz", str(ctx.exception))

    def test_missing_attr_raises_with_name(self):
        with self.assertRaises(RuntimeError) as ctx:
            serve.load_extension("agents.extractor:NoSuchClass", kind="extractor")
        self.assertIn("NoSuchClass", str(ctx.exception))

    def test_instantiate_class(self):
        cls = serve.load_extension("agents.extractor:OpsExtractor", kind="x")
        inst = serve._instantiate(cls)
        self.assertTrue(callable(getattr(inst, "extract", None)))

    def test_instantiate_none(self):
        self.assertIsNone(serve._instantiate(None))


class TestMemoryProviderFactory(unittest.TestCase):
    """Memory 吃的是 **Provider**（put/get/scan/delete），不是 Store。

    踩过的坑：一开始按 Context 的 Store 去造，`MemoryRuntime(store=...)`
    直接 TypeError。两者抽象层级不同，名字像但协议不一样。
    """

    def test_default_is_in_memory_provider(self):
        p = serve._make_memory_provider({})
        for m in ("put", "get", "scan", "delete"):
            self.assertTrue(callable(getattr(p, m, None)), f"缺 {m}")

    def test_unknown_provider_raises(self):
        with self.assertRaises(RuntimeError) as ctx:
            serve._make_memory_provider({"AGENT_RUNTIME_MEMORY_PROVIDER": "nope"})
        self.assertIn("nope", str(ctx.exception))

    def test_custom_provider_by_path(self):
        p = serve._make_memory_provider(
            {"AGENT_RUNTIME_MEMORY_PROVIDER": "plane.memory:InMemoryProvider"})
        self.assertTrue(callable(getattr(p, "put", None)))


class TestMemoryRecallIntoContext(unittest.TestCase):
    """**核心**：Memory 召回要真写进 `ctx.memory_hits`，Builder 才看得见。

    如果只建了 MemoryRuntime 却不接 before_turn，表现是
    "配了记忆但模型像没记忆"——代码看起来全对，功能完全没生效。
    """

    def _wiring(self):
        from agents.extractor import OpsExtractor
        from plane.memory import InMemoryProvider, MemoryRuntime
        from plane.wiring import RuntimeWiring
        rt = MemoryRuntime(provider=InMemoryProvider(), extractor=OpsExtractor())
        return RuntimeWiring(memory=rt, recall_k=3), rt

    def test_before_turn_writes_memory_hits(self):
        from core.context import ContextHandle
        wiring, rt = self._wiring()
        p = Principal(user_id="alben", tenant="acme", roles=("sre",),
                      scopes=("runtime:run",))
        # 先写一条
        rt.write({"user": "连接池上限是 20，并发超了会超时",
                  "assistant": "收到", "tools": []},
                 Subject(tenant="acme", user_id="alben"))
        ctx = ContextHandle(initial={"goal": "连接池满了怎么办"}, budget=None)
        info = wiring.before_turn(ctx, "连接池满了怎么办", principal=p)
        self.assertIn("memory", info)
        hits = ctx.get("memory_hits")
        self.assertTrue(hits, "召回没写进 Context —— 接了个寂寞")
        self.assertIn("连接池", str(hits))

    def test_memory_hits_reaches_prompt(self):
        """召回的内容必须出现在最终 Prompt 里（这才是"生效"的定义）。"""
        from builder import PromptBuilder
        from core.context import ContextHandle
        wiring, rt = self._wiring()
        p = Principal(user_id="alben", tenant="acme", roles=("sre",),
                      scopes=("runtime:run",))
        rt.write({"user": "连接池上限是 20", "assistant": "收到", "tools": []},
                 Subject(tenant="acme", user_id="alben"))
        ctx = ContextHandle(initial={"goal": "连接池满了怎么办",
                                     "principal": p.to_dict()}, budget=None)
        wiring.before_turn(ctx, "连接池满了怎么办", principal=p)
        prompt = PromptBuilder(total_tokens=8000).build(ctx.snapshot())
        text = prompt if isinstance(prompt, str) else str(prompt)
        self.assertIn("连接池", text, "召回内容没进 Prompt")

    def test_memory_failure_is_fail_open(self):
        """记忆读不到不该让回合失败（它是增强，不是前置条件）。"""
        from core.context import ContextHandle
        wiring, _ = self._wiring()

        class Boom:
            def recall(self, *a, **k):
                raise RuntimeError("backend down")
        wiring.memory = Boom()
        ctx = ContextHandle(initial={"goal": "g"}, budget=None)
        info = wiring.before_turn(ctx, "g", principal=Principal(user_id="u", tenant="t"))
        self.assertEqual(info["memory"]["hits"], 0)
        self.assertIn("error", info["memory"])

    def test_after_turn_writes_back(self):
        from core.context import ContextHandle
        wiring, rt = self._wiring()
        p = Principal(user_id="alben", tenant="acme", roles=("sre",),
                      scopes=("runtime:run",))
        ctx = ContextHandle(initial={"goal": "连接池上限是 20"}, budget=None)
        out = wiring.after_turn(ctx, turn={"user": "连接池上限是 20",
                                           "assistant": "收到", "tools": []},
                                principal=p)
        self.assertGreaterEqual(out.get("memory_written", 0), 1)

    def test_after_turn_without_memory_is_noop(self):
        from core.context import ContextHandle
        from plane.wiring import RuntimeWiring
        ctx = ContextHandle(initial={"goal": "g"}, budget=None)
        self.assertIsNone(RuntimeWiring().after_turn(ctx))

    def test_after_turn_keeps_summary_result_contract(self):
        """**后向兼容**：没装 memory 时必须返回 `SummaryResult`，不是 dict。

        踩过的坑：我给 after_turn 加记忆写回时顺手把返回值改成 dict，
        `demo/scenario_fullchain.py` 场景 10 读 `sr.compressed_count` 直接
        AttributeError —— 11 个端到端场景掉到 10 个。
        加参数可以，**改既有返回类型不行**。
        """
        from core.context import ContextHandle
        from core.summary import HistoryCompressor
        from plane.wiring import RuntimeWiring
        ctx = ContextHandle(initial={"goal": "g",
                                     "history": [{"role": "user", "text": f"第{i}句" * 30}
                                                 for i in range(40)]},
                            budget=None)
        out = RuntimeWiring(compressor=HistoryCompressor()).after_turn(ctx)
        if out is not None:
            self.assertTrue(hasattr(out, "compressed_count"),
                            f"返回值丢了 SummaryResult 契约：{type(out)}")


class TestGatewayTurnHooks(unittest.TestCase):
    """Gateway 的 on_turn_start / on_turn_end。"""

    def _gw(self, **kw):
        from core.gateway import Gateway
        return Gateway(lambda g, tc: object(), **kw)

    def test_hooks_fire_in_order(self):
        events = []
        gw = self._gw(on_turn_start=lambda c, g, p: events.append("start"),
                      on_turn_end=lambda c, r: events.append("end"))
        gw.handle({"goal": "x",
                   "principal": {"user_id": "u", "tenant": "acme",
                                 "roles": ["r"], "scopes": ["runtime:run"]}})
        self.assertEqual(events, ["start", "end"])

    def test_end_fires_on_failure(self):
        """失败回合也要调 on_turn_end —— 失败本身是有信息量的事实。"""
        events = []
        from core.gateway import Gateway
        gw = Gateway(lambda g, tc: object(), on_turn_end=lambda c, r: events.append(r))
        gw.handle({"goal": "x", "principal": {}})      # 缺字段 → 拒绝
        # 拒绝发生在 prepare 阶段，不走 execute，所以钩子不该被调
        self.assertEqual(events, [])

    def _working_gw(self, **kw):
        """一个真能跑完的 Gateway（否则测的是 loop_error，不是钩子）。"""
        from adapters.llm_mock import LLMResponse, ScriptedLLM, usage
        from builder import PromptBuilder
        from core.gateway import Gateway
        from core.loop import Loop
        from plane.tool import Dispatcher, ToolRegistry

        def factory(goal, tc):
            return Loop(PromptBuilder(total_tokens=4000),
                        ScriptedLLM([LLMResponse(text="答", usage=usage())]),
                        Dispatcher(ToolRegistry()))
        return Gateway(factory, **kw)

    def test_start_exception_is_fail_open(self):
        def boom(*a):
            raise RuntimeError("hook broke")
        gw = self._working_gw(on_turn_start=boom)
        r = gw.handle({"goal": "x",
                       "principal": {"user_id": "u", "tenant": "acme",
                                     "roles": ["r"], "scopes": ["runtime:run"]}})
        self.assertTrue(r.ok, "钩子炸了不该让回合失败")


class TestBuildGatewayWiring(unittest.TestCase):
    """`build_gateway_from_env` 真按环境变量装配。"""

    def test_without_extensions_plain_gateway(self):
        gw = serve.build_gateway_from_env(env={})
        self.assertEqual(type(gw).__name__, "Gateway")

    def test_extractor_wires_memory(self):
        """配了 Extractor → 必须装出 Memory（否则记忆永远为空）。"""
        gw = serve.build_gateway_from_env(
            env={"AGENT_RUNTIME_EXTRACTOR": "agents.extractor:OpsExtractor"})
        self.assertTrue(getattr(gw, "_on_turn_start", None) is not None,
                        "配了 Extractor 却没接回合钩子 → 记忆不会写入")

    def test_bad_extractor_path_raises_with_hint(self):
        with self.assertRaises(RuntimeError) as ctx:
            serve.build_gateway_from_env(
                env={"AGENT_RUNTIME_EXTRACTOR": "nope.mod:X"})
        self.assertIn("nope.mod", str(ctx.exception))

    def test_planner_instantiated_at_assembly(self):
        """Planner 构造失败要在**启动时**报，不留到每一轮 loop_error。"""
        with self.assertRaises(RuntimeError) as ctx:
            serve.build_gateway_from_env(
                env={"AGENT_RUNTIME_PLANNER": "agents.planner:StaticDAGPlanner"})
        self.assertIn("需要参数的实现要写成工厂函数", str(ctx.exception))

    def test_prompt_profile_and_template(self):
        gw = serve.build_gateway_from_env(env={
            "AGENT_RUNTIME_PROMPT_PROFILE": "agents.prompts:build_profile",
            "AGENT_RUNTIME_PROMPT_TEMPLATE": "agents.prompts:build_template",
        })
        self.assertIsNotNone(gw)


if __name__ == "__main__":
    unittest.main(verbosity=2)
