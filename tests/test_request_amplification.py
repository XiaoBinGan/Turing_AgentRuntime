"""「单请求放大」防护：一个请求不能把自己变成无限资源消耗。

这类漏洞的特点：**不是持续攻击，一次请求就够**。而且没有上限时完全静默 ——
服务照常返回 202/200，只是后台开始烧预算/线程/内存。

本文件盯三个入口：预算、workflow 节点数、分页参数。
"""
from __future__ import annotations

import unittest

from fastapi.testclient import TestClient

import serve
from core.observability import JsonLogger, Metrics, ShutdownCoordinator

P = {"user_id": "u", "tenant": "acme", "roles": ["r"],
     "scopes": ["runtime:run", "tool.read"]}
SILENT = lambda *_a, **_k: None                             # noqa: E731


class _Result:
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


class _Gateway:
    def __init__(self):
        self.seen = []

    def handle(self, request):
        self.seen.append(request)
        return _Result()

    def handle_stream(self, request):
        yield {"type": "accepted", "seq": 0, "data": {}}
        yield {"type": "completed", "seq": 1, "data": {"ok": True, "text": "x"}}


def _client(**kw):
    gw = _Gateway()
    app = serve.create_app(gateway=gw, metrics=Metrics(),
                           logger=JsonLogger(sink=SILENT),
                           shutdown=ShutdownCoordinator(),
                           deps={"store": None}, authenticator=None, **kw)
    return TestClient(app), gw


class TestBudgetAmplification(unittest.TestCase):
    """预算请求体必须有上限 —— 否则一次请求就能架空所有闸门。"""

    def test_huge_token_total_rejected(self):
        c, _ = _client()
        r = c.post("/v1/turns", json={"goal": "x", "principal": P,
                                      "budget": {"token_total": 10 ** 12}})
        self.assertEqual(r.status_code, 422)

    def test_huge_max_steps_rejected(self):
        c, _ = _client()
        r = c.post("/v1/turns", json={"goal": "x", "principal": P,
                                      "budget": {"max_steps": 10 ** 6}})
        self.assertEqual(r.status_code, 422)

    def test_huge_cost_rejected(self):
        c, _ = _client()
        r = c.post("/v1/turns", json={"goal": "x", "principal": P,
                                      "budget": {"cost_total_usd": 10 ** 6}})
        self.assertEqual(r.status_code, 422)

    def test_negative_rejected(self):
        c, _ = _client()
        r = c.post("/v1/turns", json={"goal": "x", "principal": P,
                                      "budget": {"token_total": -1}})
        self.assertEqual(r.status_code, 422)

    def test_unknown_budget_key_rejected(self):
        """拼错键必须报错 —— 否则"以为设了预算、其实没设"。"""
        c, _ = _client()
        r = c.post("/v1/turns", json={"goal": "x", "principal": P,
                                      "budget": {"token_ttl": 100}})
        self.assertEqual(r.status_code, 422)

    def test_reasonable_budget_accepted(self):
        c, gw = _client()
        r = c.post("/v1/turns", json={"goal": "x", "principal": P,
                                      "budget": {"token_total": 50_000,
                                                 "max_steps": 8}})
        self.assertEqual(r.status_code, 200)

    def test_budget_arrives_as_plain_dict(self):
        """Gateway 吃 dict，不是 pydantic 模型 —— 传模型会"静默不生效"。"""
        c, gw = _client()
        c.post("/v1/turns", json={"goal": "x", "principal": P,
                                  "budget": {"token_total": 50_000, "max_steps": 8}})
        budget = gw.seen[-1].get("budget")
        self.assertIsInstance(budget, dict)
        self.assertEqual(budget["token_total"], 50_000)
        self.assertEqual(budget["max_steps"], 8)

    def test_partial_budget_only_sends_given_keys(self):
        """只传了一项时，不该把其余键塞成 None（那会让 Gateway 用 None 覆盖默认）。"""
        c, gw = _client()
        c.post("/v1/turns", json={"goal": "x", "principal": P,
                                  "budget": {"max_steps": 5}})
        self.assertEqual(set(gw.seen[-1]["budget"]), {"max_steps"})


class TestWorkflowNodeAmplification(unittest.TestCase):
    """workflow 节点数必须有上限 —— 每节点跑一个回合。"""

    def _submit(self, nodes, **kw):
        c, _ = _client(**kw)
        return c.post("/v1/workflows",
                      json={"name": "w", "nodes": nodes, "principal": P})

    def test_limit_is_64(self):
        self.assertEqual(serve.DEFAULT_MAX_WORKFLOW_NODES, 64)

    def test_at_limit_accepted(self):
        nodes = [{"name": f"n{i}", "goal": "x"}
                 for i in range(serve.DEFAULT_MAX_WORKFLOW_NODES)]
        self.assertEqual(self._submit(nodes).status_code, 202)

    def test_over_limit_rejected(self):
        nodes = [{"name": f"n{i}", "goal": "x"}
                 for i in range(serve.DEFAULT_MAX_WORKFLOW_NODES + 1)]
        self.assertEqual(self._submit(nodes).status_code, 422)

    def test_1000_nodes_rejected(self):
        """实测过：没有上限时 1000 节点返回 202，然后排队烧光资源。"""
        nodes = [{"name": f"n{i}", "goal": "x"} for i in range(1000)]
        self.assertEqual(self._submit(nodes).status_code, 422)


class TestPaginationBounds(unittest.TestCase):
    """分页参数要有界，否则 `limit=99999` 一次拉全表。"""

    def test_bad_pagination_rejected(self):
        c, _ = _client()
        for q in ("limit=0", "limit=-1", "limit=99999", "offset=-5",
                  "limit=abc", "offset=abc"):
            with self.subTest(q=q):
                self.assertEqual(c.get(f"/v1/workflows?{q}").status_code, 422)

    def test_normal_pagination_ok(self):
        c, _ = _client()
        self.assertEqual(c.get("/v1/workflows?limit=10&offset=0").status_code, 200)


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestWorkflowNodeBudget(unittest.TestCase):
    """workflow 的**每个节点**必须有预算上限。

    不传 budget 时每个节点都用 Gateway 的默认额度（2 万 token / 20 次工具）。
    64 个节点 = 128 万 token 的放大 —— 而它来自**一次请求**。
    这是"单请求放大"的另一种形态：放大器是节点数，不是参数值。
    """

    def _run(self, n_nodes, **kw):
        gw = _Gateway()
        app = serve.create_app(
            gateway=gw, metrics=Metrics(), logger=JsonLogger(sink=SILENT),
            shutdown=ShutdownCoordinator(), deps={"store": None},
            authenticator=None, workflow_workers=1, **kw)
        c = TestClient(app)
        nodes = [{"name": f"n{i}", "goal": "x"} for i in range(n_nodes)]
        wid = c.post("/v1/workflows",
                     json={"name": "w", "nodes": nodes, "principal": P}).json()["workflow_id"]
        import time as _t
        for _ in range(200):
            if c.get(f"/v1/workflows/{wid}").json()["status"] in ("done", "failed"):
                break
            _t.sleep(0.02)
        return gw

    def test_each_node_gets_bounded_budget(self):
        gw = self._run(2)
        self.assertTrue(gw.seen, "节点没执行")
        for req in gw.seen:
            b = req.get("budget") or {}
            self.assertLessEqual(b.get("token_total", 0),
                                 serve.GatewayNodeRunner.DEFAULT_NODE_TOKEN_TOTAL)
            self.assertLessEqual(b.get("tool_calls_total", 0),
                                 serve.GatewayNodeRunner.DEFAULT_NODE_TOOL_CALLS)
            self.assertLessEqual(b.get("max_steps", 0),
                                 serve.GatewayNodeRunner.DEFAULT_NODE_MAX_STEPS)

    def test_node_budget_injected(self):
        """可显式注入节点预算（例如"这次发布每个节点只给 5 千 token"）。"""
        gw = _Gateway()
        app = serve.create_app(
            gateway=gw, metrics=Metrics(), logger=JsonLogger(sink=SILENT),
            shutdown=ShutdownCoordinator(), deps={"store": None},
            authenticator=None, workflow_workers=1,
            workflow_runner_factory=lambda p: serve.GatewayNodeRunner(
                gw, p, node_budget={"token_total": 5000, "max_steps": 3}))
        c = TestClient(app)
        wid = c.post("/v1/workflows",
                     json={"name": "w", "nodes": [{"name": "a", "goal": "x"}],
                           "principal": P}).json()["workflow_id"]
        import time as _t
        for _ in range(100):
            if c.get(f"/v1/workflows/{wid}").json()["status"] in ("done", "failed"):
                break
            _t.sleep(0.02)
        self.assertTrue(gw.seen)
        self.assertEqual(gw.seen[0]["budget"]["token_total"], 5000)

    def test_total_amplification_is_bounded(self):
        """64 个节点的总预算上限 = 64 × 单节点上限，而不是 64 × 默认。"""
        per = serve.GatewayNodeRunner.DEFAULT_NODE_TOKEN_TOTAL
        total = per * serve.DEFAULT_MAX_WORKFLOW_NODES
        # 这个数字必须远小于"64 × Gateway 默认"（如果没上限就是后者）
        self.assertLess(total, 64 * 100_000, "总放大仍然过大")
