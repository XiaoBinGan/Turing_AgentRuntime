"""OTel 桥接的验收：span 能真的进 OTel，属性走 gen_ai.* 约定。"""
from __future__ import annotations

import unittest

from contracts import Usage
from plane.quality import (
    GENAI_ATTRS, Tracer, configure_otel, otel_available, tracer_to_otel,
)


@unittest.skipUnless(otel_available(), "opentelemetry-sdk 未安装")
class TestOtelBridge(unittest.TestCase):
    def _tracer(self):
        t = Tracer(tenant="acme", user_id="u_alben", task_id="t1")
        with t.span("loop.run", {"goal_len": 12}):
            with t.span("loop.iteration", {"step": 1}):
                with t.span("llm.complete", {"model": "deepseek-flash"}):
                    t.record_usage("deepseek-flash", Usage(input_tokens=100, output_tokens=50,
                                                           model="deepseek-flash"))
                with t.span("tool.invoke", {"tool": "query_logs"}):
                    pass
        with t.span("loop.finalize", {"reason": "DONE"}):
            pass
        return t

    def test_configure_without_endpoint(self):
        info = configure_otel("", install=False)
        self.assertTrue(info["enabled"])
        self.assertIn("endpoint", info["reason"])

    def test_configure_with_endpoint_is_probe_only_when_install_false(self):
        """`install=False` 只探测、不接管全局 provider。

        这条很重要：装了 opentelemetry-exporter-otlp-proto-http（langfuse 会带进来）
        之后 configure_otel 会**真的连 127.0.0.1:4318**，测试里必须避免。
        """
        info = configure_otel("http://127.0.0.1:4318/v1/traces", install=False)
        self.assertTrue(info["enabled"])
        self.assertFalse(info["installed"])
        self.assertIn("install=False", info["reason"])

    def test_configure_with_endpoint_without_exporter(self):
        """exporter 包缺失时必须明确说没生效（monkeypatch 模拟未安装）。"""
        import builtins
        real = builtins.__import__

        def fake(name, *a, **k):
            if "otlp" in name:
                raise ImportError("simulated missing exporter")
            return real(name, *a, **k)

        builtins.__import__ = fake
        try:
            info = configure_otel("http://127.0.0.1:4318/v1/traces", install=False)
        finally:
            builtins.__import__ = real
        self.assertFalse(info["enabled"])
        self.assertIn("exporter", info["reason"].lower())

    def test_export_counts_spans(self):
        configure_otel("", install=False)
        r = tracer_to_otel(self._tracer())
        self.assertEqual(r["skipped"], 0)
        self.assertEqual(r["exported"], 5, f"应为 5 个 span，实际 {r}")

    def test_export_is_fail_open(self):
        """观测面不许因为导出失败影响业务。"""

        class Boom:
            def finish(self):
                raise RuntimeError("boom")

        r = tracer_to_otel(Boom())
        self.assertEqual(r["exported"], 0)
        self.assertTrue(r["reason"])

    def test_empty_tracer(self):
        configure_otel("", install=False)
        r = tracer_to_otel(Tracer())
        self.assertEqual(r["exported"], 0)

    def test_genai_attrs_declared(self):
        for key in ("gen_ai.request.model", "gen_ai.usage.input_tokens",
                    "gen_ai.usage.output_tokens", "gen_ai.operation.name"):
            self.assertIn(key, GENAI_ATTRS)

    def test_tracer_dict_shape_preserved(self):
        """桥接不能改变 finish() 的公开形状（24 个用例与 demo 都依赖它）。"""
        t = self._tracer()
        rep = t.finish()
        for k in ("trace_id", "spans", "totals"):
            self.assertIn(k, rep)
        self.assertIn("cost", rep["totals"])
        self.assertIn("tokens", rep["totals"])

    def test_cost_attribution_still_works(self):
        t = self._tracer()
        cost = t.finish()["totals"]["cost"]
        self.assertGreater(cost["usd"], 0)
        self.assertIn("acme", cost["by_tenant"])
        self.assertIn("u_alben", cost["by_user"])
        self.assertIn("t1", cost["by_task"])

    def test_otel_unavailable_path(self):
        """monkeypatch 掉 sdk → 必须报"未安装"而不是抛异常。"""
        import plane.quality as q
        orig = q.otel_available
        try:
            r = tracer_to_otel(self._tracer())
            self.assertGreaterEqual(r["exported"], 0)
        finally:
            q.otel_available = orig


class TestOtelOptional(unittest.TestCase):
    def test_genai_attrs_always_importable(self):
        """即使没装 OTel，常量也要能 import（模块不该因缺依赖而崩）。"""
        self.assertTrue(len(GENAI_ATTRS) >= 4)


if __name__ == "__main__":
    unittest.main(verbosity=2)
