"""运维端点：`/v1/tools`、`/v1/capabilities`、`/v1/admin/tools/reload`、`/v1/whoami`。

这四项的共同点：**此前没有任何 HTTP 途径能回答它们**。

  * 装了哪些工具、各自什么风险档、需不需要审批 → 只能翻 YAML
  * 派发器申报了哪些保障 → 只能在 Python 里读 `Dispatcher.capabilities()`
  * 改一项工具参数 → 必须重启（中断正在跑的回合）
  * `/v1/whoami` 未启用鉴权时返回裸 404 → 客户端不知道该开什么开关

`secrets` 字段是硬红线：`ToolManifest.secrets` 是要注入的环境变量名，
属于部署内部信息。序列化用**白名单**，不是"除了 secrets 都放"。
"""
from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest

from fastapi.testclient import TestClient

import serve
from tests.test_serve import FakeGateway, make_app

TOOLS_PY = '''
def echo(args, ctx): return {"echo": args.get("text", "")}
def send(args, ctx): return {"sent": True}
def extra(args, ctx): return {"extra": True}
'''

YAML_BASE = '''version: 1
tools:
  - name: echo
    description: 回显文本（只读）
    import: "mytools:echo"
    side_effect: read_only
    risk_tier: 0
  - name: send
    description: 发送消息（有副作用，需审批）
    import: "mytools:send"
    side_effect: mutating
    risk_tier: 2
    required_scope: tool.write
'''


class _WithTools(unittest.TestCase):
    """起一个真的装配了工具表的实例。"""

    admin_token = "s3cret-admin"

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()
        pathlib.Path(self.tmp, "mytools.py").write_text(TOOLS_PY)
        self.yaml = pathlib.Path(self.tmp, "tools.yaml")
        self.yaml.write_text(YAML_BASE)
        if self.tmp not in sys.path:
            sys.path.insert(0, self.tmp)
        self.gateway = serve.build_gateway_from_env(
            env={"AGENT_RUNTIME_TOOLS": str(self.yaml)})
        self.client = TestClient(
            serve.create_app(gateway=self.gateway, admin_token=self.admin_token))

    @property
    def auth(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.admin_token}"}


class TestToolsEndpoint(_WithTools):
    def test_lists_registered_tools(self) -> None:
        response = self.client.get("/v1/tools")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["count"], 2)
        names = [t["name"] for t in body["tools"]]
        self.assertEqual(names, ["echo", "send"], "应按风险档排序")

    def test_tier_and_approval_flag(self) -> None:
        tools = {t["name"]: t for t in self.client.get("/v1/tools").json()["tools"]}
        self.assertEqual(tools["echo"]["risk_tier"], "T0_SAFE")
        self.assertFalse(tools["echo"]["needs_approval"])
        self.assertEqual(tools["send"]["risk_tier"], "T2_HIGH")
        self.assertTrue(tools["send"]["needs_approval"],
                        "T2+ 必须标 needs_approval —— 前端据此准备审批 UI")

    def test_needs_approval_count(self) -> None:
        body = self.client.get("/v1/tools").json()
        self.assertEqual(body["needs_approval"], 1)

    def test_secrets_never_exposed(self) -> None:
        """**硬红线**：`ToolManifest.secrets` 是环境变量名，出网即泄露面。"""
        raw = self.client.get("/v1/tools").text
        self.assertNotIn("secrets", raw)
        for tool in self.client.get("/v1/tools").json()["tools"]:
            self.assertNotIn("secrets", tool)

    def test_empty_registry_is_200_not_404(self) -> None:
        """没装配工具表是**合法部署**（只做对话的实例），不是错误。"""
        client = TestClient(make_app(FakeGateway()))
        response = client.get("/v1/tools")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["count"], 0)


class TestCapabilitiesEndpoint(_WithTools):
    def test_dispatch_capabilities_reported(self) -> None:
        body = self.client.get("/v1/capabilities").json()
        self.assertIn("idempotency", body["dispatch"])
        self.assertIn("approval", body["dispatch"])

    def test_tool_count(self) -> None:
        body = self.client.get("/v1/capabilities").json()
        self.assertEqual(body["counts"]["tools"], 2)


class TestReloadEndpoint(_WithTools):
    def test_requires_admin_token(self) -> None:
        self.assertEqual(self.client.post("/v1/admin/tools/reload").status_code, 403)

    def test_wrong_token_is_403(self) -> None:
        response = self.client.post("/v1/admin/tools/reload",
                                    headers={"Authorization": "Bearer wrong"})
        self.assertEqual(response.status_code, 403)

    def test_reload_picks_up_new_tool(self) -> None:
        self.yaml.write_text(YAML_BASE + '''  - name: extra
    description: 新增
    import: "mytools:extra"
    side_effect: read_only
''')
        response = self.client.post("/v1/admin/tools/reload", headers=self.auth)
        self.assertEqual(response.status_code, 200)
        diff = response.json()["diff"]
        self.assertEqual(diff["added"], ["extra"])
        names = [t["name"] for t in self.client.get("/v1/tools").json()["tools"]]
        self.assertIn("extra", names)

    def test_reload_removes_tool(self) -> None:
        one = YAML_BASE.split("  - name: send")[0]
        self.yaml.write_text(one)
        response = self.client.post("/v1/admin/tools/reload", headers=self.auth)
        self.assertEqual(response.json()["diff"]["removed"], ["send"])
        names = [t["name"] for t in self.client.get("/v1/tools").json()["tools"]]
        self.assertNotIn("send", names)

    def test_broken_config_keeps_old_table(self) -> None:
        """**fail-closed 的关键动作**：坏配置不能把服务变成"没有工具"。

        换上一张坏表的后果是"改配置"变成"停服" —— 保留旧表，
        服务继续用上一份能工作的工具集。
        """
        self.yaml.write_text("tools: [broken")
        response = self.client.post("/v1/admin/tools/reload", headers=self.auth)
        self.assertEqual(response.status_code, 422)
        self.assertIn("旧工具表保持不动", response.json()["note"])
        # 旧表还在
        self.assertEqual(self.client.get("/v1/tools").json()["count"], 2)

    def test_endpoint_absent_without_admin_token(self) -> None:
        """没配 admin token → 端点**根本不注册**（404），不暴露存在性。"""
        gw = serve.build_gateway_from_env(
            env={"AGENT_RUNTIME_TOOLS": str(self.yaml)})
        client = TestClient(serve.create_app(gateway=gw))
        self.assertEqual(client.post("/v1/admin/tools/reload").status_code, 404)


class TestWhoamiDisabled(unittest.TestCase):
    def test_404_with_explanation(self) -> None:
        """状态码仍是 404（能力不存在），但 body 说清为什么、怎么开。

        裸 `{"detail":"Not Found"}` 会让客户端以为路径写错，去改 URL ——
        而它需要的是改一个环境变量。
        """
        client = TestClient(make_app(FakeGateway()))
        response = client.get("/v1/whoami")
        self.assertEqual(response.status_code, 404)
        body = response.json()
        self.assertEqual(body["error"], "whoami_requires_auth")
        self.assertIn("AGENT_RUNTIME_AUTH", body["enable"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
