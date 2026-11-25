"""Dispatcher 保障自述 + 幂等三态语义。

**为什么需要这个文件**：`Dispatcher` 是可替换的（SPI），但换掉它会**静默丢掉
一整排保障**。实测过一个"只调 handler"的天真实现：T2 审批、T3 硬拒、幂等缓存、
预算记账、工具超时全部消失，而回合照常返回 `ok` —— 运行期没有任何信号。

`capabilities()` 把这件事挪到**启动期**：申报缺了就拒绝启动。

同时钉住 `ToolManifest.idempotent` 的三态语义（None / True / False），
因为默认值方向选错会让写工具"重试即谎报成功"。
"""
from __future__ import annotations

import unittest

from contracts import (
    DISPATCH_CAPABILITIES, ExecCtx, Principal, RiskTier, SideEffect, ToolManifest,
    ToolOutcome, ToolResult,
)
from core.gateway import DispatcherCapabilityError, Gateway
from plane.tool import Dispatcher, ToolRegistry


def manifest(name="t", effect=SideEffect.READ_ONLY, idem="omit", limit=10):
    kw = {} if idem == "omit" else {"idempotent": idem}
    return ToolManifest(name=name, side_effect=effect, risk_tier=RiskTier.T0_SAFE,
                        description=name, per_turn_limit=limit,
                        input_schema={"type": "object", "properties": {}}, **kw)


def ctx(**kw):
    return ExecCtx(principal=Principal(user_id="u", tenant="t",
                                       scopes=("tool.read", "tool.write")), **kw)


class NaiveDispatcher:
    """只调 handler 的实现 —— 换掉 Dispatcher 时最可能写出的样子。"""

    def __init__(self, fns=None):
        self._fns = fns or {}

    def invoke(self, name, args, e):
        return ToolOutcome.ok(ToolResult(tool=name, payload={}))

    def manifests(self):
        return []


# ══════════════════════════════════════════════════════════════
# ① 能力自述
# ══════════════════════════════════════════════════════════════
class TestCapabilitiesDeclaration(unittest.TestCase):
    def test_real_dispatcher_declares_all(self):
        d = Dispatcher(ToolRegistry())
        self.assertEqual(d.capabilities(), DISPATCH_CAPABILITIES,
                         "真 Dispatcher 必须申报全部保障")

    def test_capability_names_are_stable(self):
        """清单是公开契约，删项等于静默取消一项保障。"""
        for name in ("approval", "timeout", "budget", "idempotency",
                     "rate_limit", "scope", "schema", "secrets"):
            self.assertIn(name, DISPATCH_CAPABILITIES)

    def test_protocol_surface_present(self):
        """协议要的三件事：invoke / manifests / capabilities。"""
        d = Dispatcher(ToolRegistry())
        for attr in ("invoke", "manifests", "capabilities"):
            self.assertTrue(callable(getattr(d, attr, None)),
                            f"Dispatcher 缺少 {attr}()")

    def test_manifests_returns_list(self):
        reg = ToolRegistry()
        reg.register(manifest("a"), lambda a, e: {})
        self.assertEqual([m.name for m in Dispatcher(reg).manifests()], ["a"])


class TestStartupRefusal(unittest.TestCase):
    def _gw(self, required):
        return Gateway(lambda g, tc: None, required_dispatch=required)

    def test_complete_dispatcher_accepted(self):
        provided = self._gw(DISPATCH_CAPABILITIES).check_dispatch(
            Dispatcher(ToolRegistry()))
        self.assertEqual(provided, DISPATCH_CAPABILITIES)

    def test_naive_dispatcher_refused(self):
        """核心用例：申报缺失 → 启动期抛，不是运行期静默降级。"""
        with self.assertRaises(DispatcherCapabilityError) as caught:
            self._gw(DISPATCH_CAPABILITIES).check_dispatch(NaiveDispatcher())
        missing = caught.exception.vertex["missing"]
        self.assertIn("approval", missing, "审批缺失必须被点名")
        self.assertIn("timeout", missing)
        self.assertEqual(len(missing), len(DISPATCH_CAPABILITIES))

    def test_partial_requirement_checked(self):
        with self.assertRaises(DispatcherCapabilityError) as caught:
            self._gw(frozenset({"timeout"})).check_dispatch(NaiveDispatcher())
        self.assertEqual(caught.exception.vertex["missing"], ["timeout"])

    def test_no_requirement_keeps_history(self):
        """默认不校验：老代码不该因为加了这道闸就起不来。"""
        provided = self._gw(None).check_dispatch(NaiveDispatcher())
        self.assertEqual(provided, frozenset())

    def test_error_message_is_actionable(self):
        with self.assertRaises(DispatcherCapabilityError) as caught:
            self._gw(DISPATCH_CAPABILITIES).check_dispatch(NaiveDispatcher())
        msg = str(caught.exception)
        self.assertIn("静默失效", msg, "错误信息要说清后果")

    def test_dispatcher_without_capabilities_treated_as_empty(self):
        class Legacy:
            def invoke(self, n, a, e):
                return ToolOutcome.failed("x")
        with self.assertRaises(DispatcherCapabilityError):
            self._gw(frozenset({"approval"})).check_dispatch(Legacy())


# ══════════════════════════════════════════════════════════════
# ② idempotent 三态
# ══════════════════════════════════════════════════════════════
class TestIdempotentThreeState(unittest.TestCase):
    def test_undeclared_read_only_caches(self):
        self.assertTrue(manifest(effect=SideEffect.READ_ONLY).cacheable(
            has_business_key=False))

    def test_undeclared_mutating_does_not_cache_without_key(self):
        """**核心**：写工具默认不缓存，否则重试会谎报成功。"""
        self.assertFalse(manifest(effect=SideEffect.MUTATING).cacheable(
            has_business_key=False))

    def test_undeclared_mutating_caches_with_business_key(self):
        """业务显式给幂等键 → 允许缓存（防重复部署的唯一正确入口）。"""
        self.assertTrue(manifest(effect=SideEffect.MUTATING).cacheable(
            has_business_key=True))

    def test_explicit_false_overrides_business_key(self):
        """显式 `False` 压过一切 —— 调用方说了不要缓存就不要。"""
        self.assertFalse(manifest(effect=SideEffect.MUTATING, idem=False).cacheable(
            has_business_key=True))
        self.assertFalse(manifest(effect=SideEffect.READ_ONLY, idem=False).cacheable(
            has_business_key=False))

    def test_explicit_true_caches(self):
        self.assertTrue(manifest(effect=SideEffect.READ_ONLY, idem=True).cacheable(
            has_business_key=False))


class TestNoLieOnRetry(unittest.TestCase):
    """写工具重试必须真执行 —— 这是"谎报成功"的防线。"""

    def test_mutating_runs_every_time(self):
        runs = []
        reg = ToolRegistry()
        reg.register(manifest("deploy", SideEffect.MUTATING),
                     lambda a, e: (runs.append(1), {"n": len(runs)})[1])
        d = Dispatcher(reg)
        e = ctx()
        kinds = [d.invoke("deploy", {"env": "prod"}, e).kind for _ in range(3)]
        self.assertEqual(kinds, ["ok"] * 3)
        self.assertEqual(len(runs), 3, f"写工具被缓存了：只执行 {len(runs)} 次")

    def test_mutating_with_business_key_deduped(self):
        runs = []
        reg = ToolRegistry()
        reg.register(manifest("deploy", SideEffect.MUTATING),
                     lambda a, e: (runs.append(1), {"n": len(runs)})[1])
        d = Dispatcher(reg)
        e = ctx(idempotency_key="release:42:deploy")
        d.invoke("deploy", {"env": "prod"}, e)
        second = d.invoke("deploy", {"env": "prod"}, e)
        self.assertTrue(second.result.deduped)
        self.assertEqual(len(runs), 1, "业务幂等键没生效")


# ══════════════════════════════════════════════════════════════
# ③ per_turn_limit 与缓存的关系
# ══════════════════════════════════════════════════════════════
class TestRateLimitVsCache(unittest.TestCase):
    """`per_turn_limit` 限**调用次数**，预算限**真执行**。

    两道闸各管一件事。早先缓存检查在限流之前，命中就直接返回，
    计数永远不涨 —— 实测 limit=2 的工具能连调 4 次。
    """

    def test_limit_counts_cache_hits(self):
        reg = ToolRegistry()
        reg.register(manifest("t", SideEffect.READ_ONLY, limit=2), lambda a, e: {"n": 1})
        d = Dispatcher(reg)
        e = ctx()
        kinds = [d.invoke("t", {}, e).kind for _ in range(4)]
        self.assertEqual(kinds, ["ok", "ok", "denied", "denied"],
                         "同参数重复调用也必须计入 per_turn_limit")

    def test_cache_hit_does_not_consume_budget(self):
        reg = ToolRegistry()
        reg.register(manifest("t", SideEffect.READ_ONLY, limit=10), lambda a, e: {"n": 1})
        d = Dispatcher(reg)
        e = ctx()
        for _ in range(4):
            d.invoke("t", {}, e)
        self.assertEqual(e.budget.tool_calls_used, 1,
                         "缓存命中没有真执行，不该占预算")

    def test_denied_message_mentions_cache(self):
        reg = ToolRegistry()
        reg.register(manifest("t", SideEffect.READ_ONLY, limit=1), lambda a, e: {"n": 1})
        d = Dispatcher(reg)
        e = ctx()
        d.invoke("t", {}, e)
        second = d.invoke("t", {}, e)
        self.assertEqual(second.kind, "denied")
        self.assertIn("缓存", second.reason, "拒绝原因要说清含缓存命中")


if __name__ == "__main__":
    unittest.main(verbosity=2)
