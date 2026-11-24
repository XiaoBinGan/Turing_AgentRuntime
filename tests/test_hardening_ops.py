"""运维级加固：真中止、分布式限流、可信反代。

这些问题都属于「配了没生效」或「看起来保护了其实没有」，
是功能性测试（请求能通）永远发现不了的那一类。
"""
from __future__ import annotations

import threading
import time
import unittest

import serve
from core.kv import InMemoryKV
from core.observability import JsonLogger, Metrics, ShutdownCoordinator


SILENT = lambda *_a, **_k: None                            # noqa: E731
P = {"user_id": "u", "tenant": "acme", "roles": ["r"],
     "scopes": ["runtime:run", "tool.read"]}


class FakeGateway:
    def handle(self, request):
        class R:
            ok = True
            text = "ok"
            steps = 1
            phase = "DONE"
            term_reason = "DONE"
            error = ""
            audit_id = "a1"
            task_id = "t1"
            latency_ms = 1.0
            vertex: dict = {}
            partial = False
        return R()


def make_app(**kw):
    kw.setdefault("gateway", FakeGateway())
    kw.setdefault("metrics", Metrics())
    kw.setdefault("logger", JsonLogger(sink=SILENT))
    kw.setdefault("shutdown", ShutdownCoordinator())
    kw.setdefault("deps", {"store": None})
    return serve.create_app(**kw)


class TestTrustedProxies(unittest.TestCase):
    """XFF 只在可信反代后面采信 —— 否则任何人都能换桶绕过限流。"""

    def test_empty_trust_ignores_xff(self):
        scope = {"client": ("1.2.3.4", 1),
                 "headers": [(b"x-forwarded-for", b"9.9.9.9")]}
        self.assertEqual(serve._client_ip(scope), "1.2.3.4",
                         "默认必须忽略 XFF（防伪造）")

    def test_trusted_peer_honours_xff(self):
        scope = {"client": ("1.2.3.4", 1),
                 "headers": [(b"x-forwarded-for", b"9.9.9.9")]}
        self.assertEqual(serve._client_ip(scope, ("1.2.3.0/24",)), "9.9.9.9")

    def test_untrusted_peer_still_ignored(self):
        scope = {"client": ("1.2.3.4", 1),
                 "headers": [(b"x-forwarded-for", b"9.9.9.9")]}
        self.assertEqual(serve._client_ip(scope, ("10.0.0.0/8",)), "1.2.3.4")

    def test_first_hop_only(self):
        scope = {"client": ("1.2.3.4", 1),
                 "headers": [(b"x-forwarded-for", b"9.9.9.9, 10.0.0.1, 10.0.0.2")]}
        self.assertEqual(serve._client_ip(scope, ("1.2.3.4",)), "9.9.9.9")

    def test_non_ip_peer_literal_match(self):
        """TestClient / unix socket 的对端不是 IP，但运维显式写了就该信任。"""
        self.assertTrue(serve._ip_in_networks("testclient", ("testclient",)))

    def test_env_var_parsed(self):
        nets = serve._resolve_trusted_proxies(
            {"AGENT_RUNTIME_TRUSTED_PROXIES": "10.0.0.0/8, 192.168.0.0/16"})
        self.assertEqual(nets, ("10.0.0.0/8", "192.168.0.0/16"))

    def test_explicit_param_wins(self):
        nets = serve._resolve_trusted_proxies(
            {"AGENT_RUNTIME_TRUSTED_PROXIES": "10.0.0.0/8"}, ("1.1.1.1",))
        self.assertEqual(nets, ("1.1.1.1",))

    def test_reported_in_info(self):
        app = make_app(trusted_proxies=("10.0.0.0/8",))
        self.assertEqual(app.state.hardening["trusted_proxies"], ["10.0.0.0/8"])


class TestDistributedRateWindow(unittest.TestCase):
    """KV 窗口：跨副本共享配额。

    内存版是 N 个副本各算 N 次（实际阈值×N）；KV 版共享计数。
    """

    def test_shared_across_instances(self):
        """两个实例必须打到**同一个计数器**上（这是"分布式"的全部意义）。"""
        kv = InMemoryKV()
        clock = lambda: 1000.0                             # noqa: E731
        a = serve.KvRateWindow(kv, limit_per_minute=3, window_s=60, clock=clock)
        b = serve.KvRateWindow(kv, limit_per_minute=3, window_s=60, clock=clock)
        # 各打 3 次（不短路，两边都真调）
        for _ in range(3):
            a._hit("1.2.3.4")
            b._hit("1.2.3.4")
        used = int(kv.get("rl:1.2.3.4:16") or 0)
        self.assertEqual(used, 6, f"计数器应被累加到 6，实际 {used}")

    def test_limit_enforced_jointly(self):
        kv = InMemoryKV()
        clock = lambda: 1000.0                             # noqa: E731
        a = serve.KvRateWindow(kv, limit_per_minute=2, window_s=60, clock=clock)
        b = serve.KvRateWindow(kv, limit_per_minute=2, window_s=60, clock=clock)
        self.assertIsNone(a._hit("x"))
        self.assertIsNone(b._hit("x"))
        self.assertIsNotNone(a._hit("x"), "第 3 次必须被拒（两实例共享配额）")
        self.assertIsNotNone(b._hit("x"))

    def test_retry_after_within_window(self):
        kv = InMemoryKV()
        w = serve.KvRateWindow(kv, limit_per_minute=1, window_s=60,
                               clock=lambda: 1030.0)
        w._hit("x")
        retry = w._hit("x")
        self.assertIsNotNone(retry)
        self.assertGreater(retry, 0)
        self.assertLessEqual(retry, 61)

    def test_kv_failure_degrades_not_silently(self):
        """KV 挂了要**降级到内存窗口 + WARN**，不是变成不限流。"""

        class BrokenKv:
            def incr(self, *a, **k):
                raise RuntimeError("redis down")

        logs = []
        fallback = serve.RateLimitMiddleware(
            lambda *a: None, limit_per_minute=1)
        w = serve.KvRateWindow(BrokenKv(), limit_per_minute=1,
                              fallback=fallback,
                              logger=JsonLogger(sink=logs.append))
        self.assertIsNone(w._hit("x"))          # 第一次放行（内存窗口计数）
        self.assertIsNotNone(w._hit("x"))       # 第二次被内存窗口拒
        self.assertTrue(w.degraded)
        self.assertTrue(any("degraded" in str(e) for e in logs),
                        "降级必须有日志，不能静默")

    def test_distributed_requires_shared_kv(self):
        """配了"分布式"却拿到进程内 KV = 配了没生效，必须报错。"""
        with self.assertRaises(RuntimeError) as ctx:
            serve._build_rate_window({"AGENT_RUNTIME_KV": "memory"}, True,
                                     limit=10)
        self.assertIn("进程内", str(ctx.exception))

    def test_disabled_by_default(self):
        self.assertIsNone(serve._build_rate_window({}, None, limit=10))

    def test_reported_in_info(self):
        app = make_app(rate_limit_per_minute=10)
        self.assertFalse(app.state.hardening["rate_limit_distributed"],
                         "默认必须自述为未启用（否则运维以为跨副本共享了）")


class TestTrueCancellation(unittest.TestCase):
    """HTTP 超时必须让回合**提前退出**，不是"继续跑完"。"""

    def test_cancel_event_reaches_exec_ctx(self):
        from contracts import ExecCtx
        ev = threading.Event()
        e = ExecCtx(cancel_event=ev)
        self.assertFalse(e.aborted)
        ev.set()                        # 另一个线程置位
        self.assertTrue(e.aborted, "外部取消信号必须被 aborted 看到")

    def test_abort_sets_external_event(self):
        from contracts import ExecCtx
        ev = threading.Event()
        e = ExecCtx(cancel_event=ev)
        e.abort()
        self.assertTrue(ev.is_set(), "abort() 要同步置位外部信号（跨线程）")

    def test_implicit_deadline_from_http_timeout(self):
        """中间件把超时转成隐式 deadline：中止只有一个权威来源。"""
        app = make_app(request_timeout_s=0.5)
        self.assertEqual(app.state.hardening["request_timeout_s"], 0.5)

    def test_timeout_makes_turn_exit_early(self):
        """端到端：LLM 每步睡 0.3s、共 10 步，HTTP 超时 0.5s → 远少于 10 步。"""
        from fastapi.testclient import TestClient

        calls = []

        class Slow:
            model = "slow"

            def complete(self, p, t, e):
                if e.aborted:
                    raise RuntimeError("aborted")
                calls.append(1)
                time.sleep(0.2)
                from contracts import LLMResponse, Usage
                return LLMResponse(text="继续" if len(calls) < 10 else "完成",
                                   usage=Usage())

        from builder import PromptBuilder
        from core.gateway import Gateway
        from core.loop import Loop
        from plane.governance import Governance, InputGuard, OutputGuard, RBAC
        from plane.tool import Dispatcher, ToolRegistry

        def factory(goal, tc):
            return Loop(PromptBuilder(total_tokens=4000), Slow(),
                        Dispatcher(ToolRegistry()))

        gw = Gateway(factory, governance=Governance(
            rbac=RBAC(), input_guard=InputGuard(), output_guard=OutputGuard()))
        client = TestClient(make_app(gateway=gw, request_timeout_s=0.5))
        client.post("/v1/turns", json={"goal": "x", "principal": P})
        time.sleep(0.6)
        self.assertLess(len(calls), 10,
                        f"超时后仍在跑完整回合（调了 {len(calls)} 次）")


if __name__ == "__main__":
    unittest.main(verbosity=2)
