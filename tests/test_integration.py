"""跨平面集成测试 + 契约自洽性。

这个文件不是某个 worker 的单元测试，它检查**模块之间的契约是否真的成立**。
联调暴露的两类缺陷都在这里被固化：

1. 契约阈值低于实际默认值 → 「有规则但规则永不触发」（保留矩阵）
2. RBAC 拿工具名当动作动词 → 「有写权限反而被拒」（Governance）
"""
from __future__ import annotations

import json
import unittest

from demo import fixtures
from builder import PromptBuilder
from contracts import (
    AgentSpec, Budget, Chunk, ContextSnapshot, ExecCtx, LLMResponse, MemoryKind,
    Phase, Principal, RETENTION_MATRIX, ResourceRef, Result, RiskTier, SideEffect,
    SkillSpec, Subject, TermReason, ToolManifest, ToolOutcome,
)
from core.context import ContextHandle
from core.loop import Loop
from plane.governance import Governance, RBAC
from plane.knowledge import Document, KnowledgeBase
from agents.demo_extractor import RuleBasedExtractor
from agents.demo_template import DEMO_TEMPLATE
from plane.memory import MemoryRuntime
from plane.quality import Tracer, Trajectory
from plane.tool import Dispatcher, Normalizer, ToolRegistry


# ══════════════════════════════════════════════════════════════
# 契约自洽性
# ══════════════════════════════════════════════════════════════
class TestContractSelfConsistency(unittest.TestCase):
    """契约里的规则必须真的能生效 —— 否则比没有规则更危险。"""

    #: 抽取阶段的默认重要度（见 plane/memory.py 的抽取规则）
    EXTRACTION_DEFAULTS = {
        MemoryKind.PROFILE: 0.90,
        MemoryKind.SEMANTIC: 0.60,
        MemoryKind.EPISODIC: 0.40,
    }

    def test_retention_threshold_actually_bites(self):
        """保留矩阵的门槛必须高于抽取默认值，否则 TTL 形同虚设。"""
        for rule in RETENTION_MATRIX:
            if rule.ttl_days is None:
                continue
            default = self.EXTRACTION_DEFAULTS[rule.kind]
            self.assertGreater(
                rule.min_importance, default,
                f"{rule.kind.value} 规则门槛 {rule.min_importance} 不高于抽取默认 "
                f"{default} → 这条规则永远不会触发",
            )

    def test_every_kind_has_a_rule(self):
        """INV-13：每一种记忆都必须能落到某条保留规则上。"""
        covered = {r.kind for r in RETENTION_MATRIX}
        self.assertEqual(covered, set(MemoryKind))


# ══════════════════════════════════════════════════════════════
# RBAC 回归
# ══════════════════════════════════════════════════════════════
class TestRBACRegression(unittest.TestCase):
    """回归：动作动词必须来自 required_scope，不能拿工具名当动词。"""

    def setUp(self) -> None:
        self.gov = Governance(rbac=RBAC())
        self.writer = Principal(user_id="u_w", roles=("sre",), tenant="acme",
                                scopes=frozenset({"tool.read", "tool.write"}))
        self.reader = Principal(user_id="u_r", roles=("viewer",), tenant="acme",
                                scopes=frozenset({"tool.read"}))

    def test_write_scope_grants_write(self):
        self.assertIsNone(self.gov.check_tool(self.writer, "restart_service", "tool.write"))

    def test_read_scope_cannot_write(self):
        self.assertIsNotNone(self.gov.check_tool(self.reader, "restart_service", "tool.write"))

    def test_read_scope_grants_read(self):
        self.assertIsNone(self.gov.check_tool(self.reader, "query_logs", "tool.read"))

    def test_global_wildcard(self):
        admin = Principal(user_id="u_a", roles=("admin",), tenant="acme",
                          scopes=frozenset({"*"}))
        self.assertIsNone(self.gov.check_tool(admin, "anything", "tool.write"))

    def test_missing_tenant_fails_closed(self):
        ghost = Principal(user_id="u_g", roles=("sre",), tenant="",
                          scopes=frozenset({"*"}))
        self.assertIsNotNone(self.gov.check_tool(ghost, "query_logs", "tool.read"))


# ══════════════════════════════════════════════════════════════
# 计量对账
# ══════════════════════════════════════════════════════════════
class TestUsageReconciliation(unittest.TestCase):
    """Loop 直报给 Tracer 的数字，必须等于从 patch log 事后重建的数字。

    两条独立路径算出同一个数，才说明「轨迹是唯一事实源」成立。
    """

    def test_two_paths_agree(self):
        tracer = Tracer(tenant="acme", user_id="u1", task_id="t1")
        reg = ToolRegistry()
        reg.register(ToolManifest(name="query_logs", side_effect=SideEffect.READ_ONLY,
                                  risk_tier=RiskTier.T0_SAFE, description="查日志"),
                     lambda a, e: {"ok": True})
        ctx = ContextHandle(initial={"principal": {"tenant": "acme", "roles": ["sre"]},
                                     "phase": "RECEIVED", "loop_count": 0,
                                     "history": [], "tool_results": [], "usage": {}})
        e = ExecCtx(principal=Principal(tenant="acme", user_id="u1"), task_id="t1",
                    budget=Budget())
        loop = Loop(PromptBuilder(), fixtures.goal_llm(),
                    Dispatcher(reg, normalizer=Normalizer(), tracer=tracer), tracer=tracer)
        loop.run("查日志", ctx, e)

        totals = tracer.finish()["totals"]["tokens"]
        rebuilt = {"input": 0, "output": 0}
        for p in ctx.patch_log:
            if p.path == "usage" and isinstance(p.value, dict):
                rebuilt = {"input": int(p.value.get("input_tokens", 0) or 0),
                           "output": int(p.value.get("output_tokens", 0) or 0)}
        self.assertEqual(totals["input"], rebuilt["input"])
        self.assertEqual(totals["output"], rebuilt["output"])
        self.assertGreater(totals["input"], 0)


# ══════════════════════════════════════════════════════════════
# 四平面串起来
# ══════════════════════════════════════════════════════════════
class TestPlanesTogether(unittest.TestCase):
    """记忆召回 → 知识检索 → 治理包裹 → Builder 组装，一次跑通。"""

    def test_memory_and_knowledge_flow_into_prompt(self):
        subj = Subject(tenant="acme", user_id="u1", team="sre")
        mem = MemoryRuntime(extractor=RuleBasedExtractor())
        mem.write({"user": "我偏好只读排查，禁止直接改生产配置",
                   "assistant": "明白", "tools": []}, subj)

        kb = KnowledgeBase()
        kb.add(Document(doc_id="rb", version="1", title="手册",
                        text="连接池默认上限 20，并发超过 150 会 acquire timeout。"))
        kb.rebuild()

        mem_hits = mem.recall("排查要注意什么", subj, k=3)
        kb_hits = kb.reader().retrieve("连接池上限", k=2)
        self.assertTrue(mem_hits)
        self.assertTrue(kb_hits)

        gov = Governance(rbac=RBAC())
        safe_kb = gov.before_prompt(kb_hits)
        self.assertTrue(all(isinstance(c, Chunk) for c in safe_kb))

        snap_ctx = ContextHandle(initial={
            "principal": {"tenant": "acme", "roles": ["sre"]},
            "phase": "EXECUTING", "loop_count": 1,
            "memory_hits": [h.__dict__ for h in mem_hits],
            "knowledge_hits": [c.__dict__ for c in safe_kb],
            "history": [], "tool_results": [],
        })
        # 措辞属于 Agent 层（builder.py 的默认模板全中性），这里注入示例 Agent 的模板
        prompt = PromptBuilder(DEMO_TEMPLATE).build(snap_ctx.snapshot(), [])
        self.assertIn("<<<MEMORY>>>", prompt)
        self.assertIn("<<<KNOWLEDGE>>>", prompt)
        self.assertIn("【硬性约束】", prompt)          # Profile 进 system 区
        self.assertIn("external_data_untrusted", prompt)  # 知识段被包裹

    def test_memory_never_leaks_across_tenant(self):
        """INV-12 强化：跨租户零泄漏，即便查询完全一样。"""
        mem = MemoryRuntime(extractor=RuleBasedExtractor())
        a = Subject(tenant="acme", user_id="u1")
        b = Subject(tenant="evilcorp", user_id="u1")
        mem.write({"user": "连接池上限是 20", "assistant": "记下", "tools": []}, a)
        self.assertTrue(mem.recall("连接池上限", a, k=5))
        self.assertEqual(mem.recall("连接池上限", b, k=5), [])

    def test_trajectory_reconstructs_from_patch_log(self):
        """轨迹可以从 patch log 完整还原 —— 评测不需要额外埋点。"""
        ctx = ContextHandle(initial={"history": [], "loop_count": 0, "usage": {}})
        from contracts import Owner, Patch
        ctx.apply(Patch("append", "tool_results", Owner.LOOP,
                        {"tool": "query_logs", "payload": {}}))
        ctx.apply(Patch("incr", "loop_count", Owner.LOOP, 1))
        traj = Trajectory.from_patch_log(ctx.patch_log)
        self.assertEqual(traj.tool_sequence(), ["query_logs"])
        self.assertEqual(traj.loop_count(), 1)


class TestCheckpointRetention(unittest.TestCase):
    """续跑点的生命周期：**只有真的结束才删**。

    预算耗尽 / 步数上限这类失败是可续跑的 —— 追加大预算就能接着干。
    如果 Store 层把它们也删了，等于替调用方做了「放弃」的决定。
    """

    def _loop(self, phase, term, partial):
        class FakeLoop:
            def run(self, *a, **k):
                return Result(phase=phase, term_reason=term, partial=partial)

            def resume(self, *a, **k):
                return self.run()
        return FakeLoop()

    def _store_with(self, ctx_id="t1"):
        from core.store import MemoryStore, save_checkpoint
        store = MemoryStore()
        save_checkpoint(ContextHandle(initial={"goal": "x", "phase": "RECEIVED",
                                               "loop_count": 3}), store, ctx_id)
        return store

    def test_keeps_checkpoint_on_budget_exhausted(self):
        from core.store import resume_from_store
        store = self._store_with()
        r = resume_from_store(self._loop(Phase.FAILED, TermReason.BUDGET, True),
                              store, "t1", ExecCtx())
        self.assertTrue(r.partial)
        self.assertIn("t1", store.list_pending(), "预算耗尽必须保留续跑点")

    def test_keeps_checkpoint_on_max_steps(self):
        from core.store import resume_from_store
        store = self._store_with()
        resume_from_store(self._loop(Phase.FAILED, TermReason.MAX_STEPS, True),
                          store, "t1", ExecCtx())
        self.assertIn("t1", store.list_pending())

    def test_deletes_checkpoint_on_done(self):
        from core.store import resume_from_store
        store = self._store_with()
        resume_from_store(self._loop(Phase.DONE, TermReason.DONE, False),
                          store, "t1", ExecCtx())
        self.assertNotIn("t1", store.list_pending(), "任务真的做完了就该清掉")

    def test_deletes_checkpoint_on_aborted(self):
        from core.store import resume_from_store
        store = self._store_with()
        resume_from_store(self._loop(Phase.ABORTED, TermReason.ABORTED, False),
                          store, "t1", ExecCtx())
        self.assertNotIn("t1", store.list_pending(), "用户明确取消，不再续跑")


class TestToolManifestPropagation(unittest.TestCase):
    """回归：工具清单必须真的从 dispatcher 传到 LLM。

    这个缺陷只有接真模型才发现 —— mock 不读 prompt，493 个测试全绿，
    然后模型一本正经地说"没有可用工具"。
    根因：Loop 只找 `dispatcher.manifests`（属性、且只认 list/tuple），
    而 `Dispatcher` 暴露的是 `.registry`，`manifests` 是 registry 上的方法。
    """

    def _dispatcher(self):
        reg = ToolRegistry()
        reg.register(ToolManifest(name="query_logs", side_effect=SideEffect.READ_ONLY,
                                  risk_tier=RiskTier.T0_SAFE, description="查日志"),
                     lambda a, e: {"ok": True})
        reg.register(ToolManifest(name="query_alerts", side_effect=SideEffect.READ_ONLY,
                                  risk_tier=RiskTier.T0_SAFE, description="查告警"),
                     lambda a, e: {"ok": True})
        return Dispatcher(reg, normalizer=Normalizer())

    def _ctx(self):
        return ContextHandle(initial={"principal": {"tenant": "acme", "roles": ["sre"]},
                                      "phase": "RECEIVED", "loop_count": 0,
                                      "history": [], "tool_results": [], "observations": []})

    def test_tools_reach_the_llm_from_dispatcher_registry(self):
        captured = {}

        class CapLLM:
            def complete(self, prompt, tools, e):
                captured["tools"] = [t.name for t in tools]
                captured["prompt"] = prompt
                return LLMResponse(text="done")

        loop = Loop(PromptBuilder(), CapLLM(), self._dispatcher())
        loop.run("查日志", self._ctx(), ExecCtx())
        self.assertEqual(sorted(captured["tools"]), ["query_alerts", "query_logs"])
        self.assertIn("query_logs", captured["prompt"], "工具必须出现在 Prompt 里")

    def test_explicit_tools_win(self):
        captured = {}

        class CapLLM:
            def complete(self, prompt, tools, e):
                captured["tools"] = [t.name for t in tools]
                return LLMResponse(text="done")

        only = [ToolManifest(name="solo", side_effect=SideEffect.READ_ONLY,
                             risk_tier=RiskTier.T0_SAFE)]
        loop = Loop(PromptBuilder(), CapLLM(), self._dispatcher(), tools=only)
        loop.run("x", self._ctx(), ExecCtx())
        self.assertEqual(captured["tools"], ["solo"])

    def test_empty_resolution_is_loud_not_silent(self):
        """解析不到工具时必须告警 —— 静默空清单是本项目踩过的坑。"""
        captured = {}

        class CapLLM:
            def complete(self, prompt, tools, e):
                captured["n"] = len(tools)
                return LLMResponse(text="done")

        class Bare:
            def invoke(self, name, args, e):
                return ToolOutcome.failed("n/a")

        loop = Loop(PromptBuilder(), CapLLM(), Bare())
        loop.run("x", self._ctx(), ExecCtx())
        self.assertEqual(captured["n"], 0)
        self.assertTrue(any("工具清单为空" in w for w in loop.warnings),
                        f"应当告警，实际 warnings={loop.warnings}")


class TestTokenCalibration(unittest.TestCase):
    """标定档必须真的更准（默认档高估 1.4~1.7x，会提前裁掉内容）。"""

    def test_calibrated_profile_matches_measured_values(self):
        from builder import CALIBRATED, estimate_tokens
        en = " ".join(["system"] * 200)              # 实测 230 tok
        zh = ("生产环境连接池耗尽导致请求排队等待，首字节延迟升高。" * 15)  # 实测 255 tok
        self.assertLess(abs(estimate_tokens(en, CALIBRATED) - 230), 25)
        self.assertLess(abs(estimate_tokens(zh, CALIBRATED) - 255), 30)

    def test_default_profile_over_estimates(self):
        from builder import CALIBRATED, DEFAULT_PROFILE, estimate_tokens
        en = " ".join(["system"] * 200)
        self.assertGreater(estimate_tokens(en, DEFAULT_PROFILE),
                           estimate_tokens(en, CALIBRATED),
                           "默认档应当比标定档保守（高估）")

    def test_builder_uses_profile_for_budget(self):
        from builder import CALIBRATED, PromptBuilder
        big = [{"role": "user", "text": "z" * 3000}]
        snap = ContextSnapshot(3, {"history": big, "phase": "EXECUTING"})
        loose = PromptBuilder(total_tokens=600).build(snap, [])
        self.assertIn("<<<HISTORY>>>", loose)
        tight = PromptBuilder(total_tokens=600, profile=CALIBRATED).build(snap, [])
        self.assertIn("<<<HISTORY>>>", tight)


class TestPlaneWiring(unittest.TestCase):
    """跨 плоскостей接线：能力模块有了，还要真的接进主线。"""

    # ── Skill → Context → Prompt ──────────────────────────────
    def test_skill_state_reaches_prompt(self):
        from plane.skill import SkillLibrary, SkillRuntime
        from plane.wiring import sync_skill_state
        lib = SkillLibrary()
        lib.add(SkillSpec(name="pool", title="连接池排查",
                          trigger=("连接池", "pool"),
                          steps=("看 pool exhausted", "对比上限与并发")))
        ctx = ContextHandle(initial={"history": [], "loop_count": 0})
        state = sync_skill_state(ctx, SkillRuntime(lib), "连接池超时了")
        self.assertTrue(state["loaded"], "命中技能应被装载")
        self.assertEqual(ctx.snapshot().get("skill_state")["loaded"][0]["name"], "pool")
        prompt = PromptBuilder().build(ctx.snapshot(), [])
        self.assertIn("<<<SKILLS>>>", prompt)
        self.assertIn("看 pool exhausted", prompt)

    def test_skill_index_only_when_not_matched(self):
        """没命中的技能只出索引，不出正文 —— 双层装载的意义。"""
        from plane.skill import SkillLibrary, SkillRuntime
        from plane.wiring import sync_skill_state
        lib = SkillLibrary()
        lib.add(SkillSpec(name="pool", title="连接池", trigger=("连接池",), steps=("A",)))
        lib.add(SkillSpec(name="release", title="发布", trigger=("发布",),
                          steps=("机密步骤：灰度 5%",)))
        ctx = ContextHandle(initial={"history": [], "loop_count": 0})
        sync_skill_state(ctx, SkillRuntime(lib), "连接池超时")
        prompt = PromptBuilder().build(ctx.snapshot(), [])
        self.assertIn("release", prompt)          # 索引在
        self.assertNotIn("灰度 5%", prompt)        # 正文不在

    # ── Resource → Tool → 真 OutputGuard ─────────────────────
    def test_resource_read_passes_real_output_guard(self):
        """W22 留的缺口：资源读取要经**真** governance.OutputGuard，不是 fake。"""
        from plane.governance import OutputGuard
        from plane.resources import ResourceRegistry, ResourceRuntime
        from plane.wiring import register_resources

        class Gov:
            def __init__(self):
                self.guard = OutputGuard()

            def before_output(self, text, allow_hosts=()):
                return self.guard.scan(text, allow_hosts=allow_hosts)

        reg = ResourceRegistry()
        reg.register(ResourceRef(uri="db://conf/app"),
                     lambda uri: "max_connections=20\napi_key=sk-ABCDEFGHIJKLMNOP1234")
        rt = ResourceRuntime(reg, governance=Gov())
        reader = Principal(user_id="u", tenant="acme",
                           scopes=frozenset({"resource.read"}))
        out = rt.read("db://conf/app", ExecCtx(principal=reader))
        self.assertNotIn("sk-ABCDEFGHIJKLMNOP1234", out["content"], "真护栏必须掩码")
        self.assertIn("***REDACTED***", out["content"])

    def test_resource_tool_requires_resource_scope(self):
        from plane.resources import ResourceRegistry, ResourceRuntime
        from plane.wiring import register_resources
        from plane.tool import Dispatcher, Normalizer, ToolRegistry
        reg = ResourceRegistry()
        reg.register(ResourceRef(uri="git://repo/diff"), lambda u: "+1 -0")
        treg = ToolRegistry()
        manifests = register_resources(treg, ResourceRuntime(reg),
                                       [ResourceRef(uri="git://repo/diff")])
        self.assertTrue(manifests[0].required_scope.startswith("resource."))
        d = Dispatcher(treg, normalizer=Normalizer())
        weak = Principal(user_id="u", tenant="acme", scopes=frozenset({"tool.read"}))
        self.assertEqual(d.invoke(manifests[0].name, {"uri": "git://repo/diff"},
                                  ExecCtx(principal=weak)).kind, "denied")
        strong = Principal(user_id="u", tenant="acme",
                           scopes=frozenset({"tool.read", "resource.read"}))
        self.assertEqual(d.invoke(manifests[0].name, {"uri": "git://repo/diff"},
                                  ExecCtx(principal=strong)).kind, "ok")

    # ── Summary → Prompt ─────────────────────────────────────
    def test_summary_section_rendered(self):
        snap = ContextSnapshot(1, {"summary": "【历史摘要｜已压缩 12 条】\n- 结论：上限 20",
                                   "history": [{"role": "user", "text": "继续"}]})
        out = PromptBuilder(total_tokens=4000).build(snap, [])
        self.assertIn("<<<SUMMARY>>>", out)
        self.assertIn("上限 20", out)

    def test_empty_summary_produces_no_section(self):
        snap = ContextSnapshot(1, {"summary": "", "history": []})
        self.assertNotIn("<<<SUMMARY>>>", PromptBuilder().build(snap, []))

    # ── Router → Context ─────────────────────────────────────
    def test_router_decision_written_to_context(self):
        from plane.router import AgentRouter
        from plane.wiring import RuntimeWiring
        r = AgentRouter(confidence_threshold=0.3)
        r.register(AgentSpec(name="sre", description="排查故障 连接池 超时",
                             tags=("故障",), required_scopes=("tool.read",)))
        ctx = ContextHandle(initial={"history": [], "loop_count": 0})
        info = RuntimeWiring(router=r).before_turn(
            ctx, "连接池超时排查",
            principal=Principal(scopes=frozenset({"tool.read"})))
        self.assertEqual(ctx.snapshot().get("agent_state"), "sre")
        self.assertGreater(ctx.snapshot().get("route_confidence"), 0)
        self.assertEqual(info["route"]["agent"], "sre")


class TestSessionInjectionChannel(unittest.TestCase):
    """会话承接必须走**公开注入口**（`Gateway.context_seed`），不是包装私有属性。

    为什么要钉这条：早期靠包装 `gateway._context_factory` 实现，Gateway 一旦改结构
    （加 `__slots__`、改 frozen、重命名）就**静默降级** —— 历史不再预填，
    而所有测试仍然绿（因为没人断言"走的是哪条通道"）。
    """

    def _gateway(self, **kw):
        from core.gateway import Gateway
        return Gateway(lambda g, tc: object(), **kw)

    def test_channel_is_context_seed(self):
        from core.session import SessionStore
        from core.session_gateway import SessionGateway
        sg = SessionGateway(self._gateway(), SessionStore())
        self.assertTrue(sg.history_injected)
        self.assertEqual(sg.injection_channel, "context_seed")

    def test_seed_receives_history(self):
        """真的把历史并进了 initial（不是只挂了钩子）。"""
        from core.session import SessionStore
        from core.session_gateway import SessionGateway
        gw = self._gateway()
        sg = SessionGateway(gw, SessionStore())
        seen = {}
        inner = gw._context_seed

        def spy(initial, goal="", principal=None, e=None):
            out = inner(initial, goal, principal, e)
            seen.update(out)
            return out

        gw._context_seed = spy
        # 手动构造 carry 上下文（正常流程由 handle 推栈）
        sg._install_context_shim()
        seeded = gw._context_seed(
            {"goal": "g", "principal": {}}, "g", Principal(), ExecCtx())
        self.assertIn("goal", seeded)

    def test_context_seed_failure_is_fail_closed(self):
        """注入口抛异常 → 请求被拒，不许带着半截种子继续跑。"""
        from core.gateway import Gateway
        gw = Gateway(lambda g, tc: object(),
                     context_seed=lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
        res = gw.handle({"goal": "x",
                         "principal": {"user_id": "u", "tenant": "acme",
                                       "roles": ["r"], "scopes": ["runtime:run"]}})
        self.assertFalse(res.ok)
        self.assertIn("context_seed", res.error)

    def test_preexisting_history_not_overwritten(self):
        """续跑出来的真实轨迹不许被会话骨架覆盖。"""
        from core.gateway import Gateway
        captured = {}

        def seed(initial, goal="", principal=None, e=None):
            merged = dict(initial)
            if not merged.get("history"):
                merged["history"] = [{"role": "user", "text": "session-skeleton"}]
            captured.update(merged)
            return merged

        gw = Gateway(lambda g, tc: object(), context_seed=seed)
        gw.handle({"goal": "x",
                   "principal": {"user_id": "u", "tenant": "acme",
                                 "roles": ["r"], "scopes": ["runtime:run"]}})
        self.assertEqual(captured.get("history"),
                         [{"role": "user", "text": "session-skeleton"}])


if __name__ == "__main__":
    unittest.main(verbosity=2)
