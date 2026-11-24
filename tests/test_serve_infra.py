"""serve.py 的两处装配缺口（施工后期补的）+ 可选身份字段。

对应三个真缺陷：
  ① `AGENT_RUNTIME_LLM_PREFIX` 硬编码 "OPENAI" → 用 BABEL_* 凭据恒失败
  ② `serve.py` 没有 OTel 装配 → `OTEL_EXPORTER_OTLP_ENDPOINT` 写了也不发
  ③ 启用鉴权后请求体仍强制要 `principal.user_id/tenant` → 多抄一遍身份
"""
from __future__ import annotations

import unittest
from unittest import mock

from fastapi.testclient import TestClient

import serve


PRINCIPAL = {"user_id": "u1", "tenant": "acme", "roles": ["sre"],
             "scopes": ["runtime:run", "tool.read"]}


class TestOtelHeaders(unittest.TestCase):
    """OTLP 鉴权头：Langfuse 靠 Basic base64(pk:sk)。"""

    def test_standard_header_wins(self):
        h = serve._otel_headers({"OTEL_EXPORTER_OTLP_HEADERS": "k=v,k2=v2"})
        self.assertEqual(h, {"k": "v", "k2": "v2"})

    def test_langfuse_basic_auth(self):
        h = serve._otel_headers({"LANGFUSE_PUBLIC_KEY": "pk-x",
                                 "LANGFUSE_SECRET_KEY": "sk-x"})
        self.assertIn("Authorization", h)
        self.assertTrue(h["Authorization"].startswith("Basic "))

    def test_no_credentials_empty(self):
        self.assertEqual(serve._otel_headers({}), {})

    def test_only_public_key_no_auth(self):
        self.assertEqual(serve._otel_headers({"LANGFUSE_PUBLIC_KEY": "pk-x"}), {})

    def test_secret_not_leaked_in_key_name(self):
        h = serve._otel_headers({"LANGFUSE_PUBLIC_KEY": "pk-supersecret",
                                 "LANGFUSE_SECRET_KEY": "sk-supersecret"})
        # 头里是 base64，不是明文
        self.assertNotIn("supersecret", h["Authorization"])


class TestLlmPrefix(unittest.TestCase):
    """模型前缀可配：默认 OPENAI，可用 AGENT_RUNTIME_LLM_PREFIX 换成 BABEL 等。"""

    def test_default_prefix_is_openai(self):
        # 未设前缀 + 无 OpenAI 凭据 → 明确报错（而不是静默 None）
        with mock.patch.dict("os.environ", {"AGENT_RUNTIME_LLM": "openai"}, clear=False):
            import os
            for k in ("OPENAI_API_KEY", "OPENAI_BASE_URL"):
                os.environ.pop(k, None)
            with self.assertRaises(RuntimeError) as ctx:
                gw = serve.build_gateway_from_env(env={"AGENT_RUNTIME_LLM": "openai"})
                gw.loop_factory("g", None)
            self.assertIn("OPENAI", str(ctx.exception))

    def test_prefix_from_env_used_in_error(self):
        with self.assertRaises(RuntimeError) as ctx:
            gw = serve.build_gateway_from_env(
                env={"AGENT_RUNTIME_LLM": "openai",
                     "AGENT_RUNTIME_LLM_PREFIX": "BABEL"})
            gw.loop_factory("g", None)
        self.assertIn("BABEL", str(ctx.exception))


class TestOptionalPrincipal(unittest.TestCase):
    """启用鉴权时，身份来自凭据，请求体不必重复。"""

    def _app(self, **kw):
        return serve.create_app(authenticator=self._auth(), **kw)

    @staticmethod
    def _auth():
        from auth import Authenticator
        from contracts import Principal
        return Authenticator(api_keys={"sk-t": Principal(
            user_id="u1", tenant="acme", roles=("sre",),
            scopes=("runtime:run", "tool.read"))})

    def test_principal_omitted_with_auth(self):
        c = TestClient(self._app())
        r = c.post("/v1/turns", headers={"X-API-Key": "sk-t"},
                   json={"goal": "x"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()["ok"])

    def test_principal_empty_with_auth(self):
        c = TestClient(self._app())
        r = c.post("/v1/turns", headers={"X-API-Key": "sk-t"},
                   json={"goal": "x", "principal": {}})
        self.assertEqual(r.status_code, 200, r.text)

    def test_identity_mismatch_still_403(self):
        c = TestClient(self._app())
        r = c.post("/v1/turns", headers={"X-API-Key": "sk-t"},
                   json={"goal": "x",
                         "principal": {"user_id": "u1", "tenant": "evilcorp"}})
        self.assertEqual(r.status_code, 403)

    def test_principal_required_without_auth(self):
        """未启用鉴权：请求体是唯一身份来源，必须给。"""
        c = TestClient(serve.create_app())
        r = c.post("/v1/turns", json={"goal": "x"})
        self.assertEqual(r.status_code, 422)
        self.assertIn("principal", r.text)

    def test_missing_tenant_reported(self):
        c = TestClient(serve.create_app())
        r = c.post("/v1/turns", json={"goal": "x",
                                      "principal": {"user_id": "u1"}})
        self.assertEqual(r.status_code, 422)
        self.assertIn("tenant", r.text)

    def test_stream_also_optional_principal(self):
        c = TestClient(self._app())
        with c.stream("POST", "/v1/turns/stream", headers={"X-API-Key": "sk-t"},
                      json={"goal": "x"}) as r:
            self.assertEqual(r.status_code, 200)
            body = "".join(r.iter_text())
        self.assertIn("completed", body)


class TestOtelInstall(unittest.TestCase):
    """不配 endpoint 不接管全局 provider（否则退出刷噪声且撤不掉）。"""

    def test_no_endpoint_not_installed(self):
        from plane.quality import configure_otel
        info = configure_otel("", install=True)
        self.assertFalse(info["installed"])

    def test_with_endpoint_probe_only(self):
        from plane.quality import configure_otel
        info = configure_otel("http://127.0.0.1:1/v1/traces", install=False)
        self.assertTrue(info["enabled"])
        self.assertFalse(info["installed"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
