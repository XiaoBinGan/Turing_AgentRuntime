"""`POST /v1/turns/{ctx_id}/resume` —— 审批的 HTTP 入口。

**为什么需要这个端点**：在这之前，审批状态机（`Phase.WAITING_USER` +
`loop.resume()` + `Approval`）只在 Python API 和 demo 里可用。HTTP 面：

  * `/v1/turns` 里 `decision` 是**硬编码 `True`**
  * 没有回合级 resume 端点
  * 请求体没有 `approvals` / `decision` 字段（`grep approvals serve.py` = 0 次）

后果不是"少一个功能"，是把一个**需要人类判断**的动作退化成
"任何持有 ctx_id 的人发一次请求就自动批准"。拒绝路径根本不存在。

这个文件覆盖三件事：
  1. 批准能跑通
  2. **拒绝也能跑通**（这是审批存在的理由）
  3. ctx_id 不存在 / 不在等待状态 / 非法字符 各自的响应
"""
from __future__ import annotations

import unittest

from fastapi.testclient import TestClient

import serve
from contracts import Phase, TermReason
from tests.test_serve import FakeGateway, make_app, make_result, PRINCIPAL


def _resume(client: TestClient, ctx_id: str = "ctx_abc", **body):
    payload = {"principal": PRINCIPAL, **body}
    return client.post(f"/v1/turns/{ctx_id}/resume", json=payload)


class TestResumeEndpoint(unittest.TestCase):
    def setUp(self) -> None:
        self.gateway = FakeGateway()
        self.client = TestClient(make_app(self.gateway))

    # ── 批准 ──────────────────────────────────────────────
    def test_approve_returns_200(self) -> None:
        response = _resume(self.client, decision=True)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])

    def test_approve_passes_decision_true_to_gateway(self) -> None:
        _resume(self.client, decision=True)
        sent = self.gateway.requests[0]
        self.assertIs(sent["decision"], True)
        self.assertEqual(sent["ctx_id"], "ctx_abc")

    # ── 拒绝（核心）──────────────────────────────────────
    def test_reject_returns_200(self) -> None:
        response = _resume(self.client, decision=False)
        self.assertEqual(response.status_code, 200)

    def test_reject_passes_decision_false_to_gateway(self) -> None:
        """**这一条是关键**：拒绝不能被执行层当成批准。"""
        _resume(self.client, decision=False)
        self.assertIs(self.gateway.requests[0]["decision"], False)

    def test_reason_is_forwarded(self) -> None:
        _resume(self.client, decision=False, reason="收件人写错了")
        self.assertEqual(self.gateway.requests[0]["reason"], "收件人写错了")

    # ── 参数校验 ──────────────────────────────────────────
    def test_decision_is_required(self) -> None:
        """不给 decision 必须 422 —— 这个端点唯一的语义就是表态。"""
        response = self.client.post("/v1/turns/ctx_abc/resume",
                                    json={"principal": PRINCIPAL})
        self.assertEqual(response.status_code, 422)

    def test_unknown_field_rejected(self) -> None:
        """拼错字段必须 422，不能被静默忽略。"""
        response = _resume(self.client, decision=True, aproved=True)
        self.assertEqual(response.status_code, 422)

    def test_illegal_ctx_id_rejected(self) -> None:
        """ctx_id 进 store 键空间，非法字符在端点就先拦。"""
        for bad in ("../etc/passwd", "ctx abc", "ctx/abc"):
            with self.subTest(ctx_id=bad):
                response = self.client.post(
                    f"/v1/turns/{bad}/resume",
                    json={"principal": PRINCIPAL, "decision": True})
                # 路径里的斜杠会先被路由吃掉（404），其余走校验（422）
                self.assertIn(response.status_code, (404, 422))

    # ── 错误映射 ──────────────────────────────────────────
    def test_missing_ctx_is_404(self) -> None:
        gw = FakeGateway(result=make_result(ok=False,
                                            error="resume_failed: 载入失败"))
        client = TestClient(make_app(gw))
        response = _resume(client, decision=True)
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["error"], "ctx_not_found")

    def test_not_waiting_is_409(self) -> None:
        gw = FakeGateway(result=make_result(
            ok=False, error="resume_needs_decision: 不在等待确认"))
        client = TestClient(make_app(gw))
        response = _resume(client, decision=True)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"], "not_awaiting_confirmation")

    def test_internal_error_is_500_json(self) -> None:
        """Gateway 内部异常 → 500，且 body 是 JSON 不是 HTML。

        error 字段会被 Gateway 包成 `internal_error: <类型>: <消息>`，
        所以这里用前缀匹配，不要求精确相等。
        """
        gw = FakeGateway(exc=RuntimeError("boom"))
        client = TestClient(make_app(gw))
        response = _resume(client, decision=True)
        self.assertEqual(response.status_code, 500)
        self.assertTrue(response.json()["error"].startswith("internal_error"))


class TestResumeErrorsOnPlainTurnsEndpoint(unittest.TestCase):
    """`/v1/turns` 带 ctx_id 续跑时的错误码。

    `resume_needs_decision` 必须是 **409** 而不是 500：
    客户端补上 `decision` 就能成功，所以这是"状态冲突"，不是服务端故障。
    """

    def test_missing_decision_is_409(self) -> None:
        gw = FakeGateway(result=make_result(
            ok=False, error="resume_needs_decision: 需要显式表态"))
        client = TestClient(make_app(gw))
        response = client.post("/v1/turns", json={
            "goal": "x", "principal": PRINCIPAL, "ctx_id": "ctx_abc"})
        self.assertEqual(response.status_code, 409)


class TestDecisionOnPlainTurn(unittest.TestCase):
    """`/v1/turns` 也接受 decision，供那些不想换端点的客户端。"""

    def setUp(self) -> None:
        self.gateway = FakeGateway()
        self.client = TestClient(make_app(self.gateway))

    def test_decision_forwarded_on_turns(self) -> None:
        self.client.post("/v1/turns", json={
            "goal": "排查 api 超时", "principal": PRINCIPAL,
            "ctx_id": "ctx_abc", "decision": False})
        self.assertIs(self.gateway.requests[0]["decision"], False)

    def test_no_decision_means_field_absent(self) -> None:
        """不给就别带这个键 —— Gateway 靠"缺字段"区分"没表态"。"""
        self.client.post("/v1/turns",
                         json={"goal": "排查 api 超时", "principal": PRINCIPAL})
        self.assertNotIn("decision", self.gateway.requests[0])


if __name__ == "__main__":
    unittest.main(verbosity=2)
