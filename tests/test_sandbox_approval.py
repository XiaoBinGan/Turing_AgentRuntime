"""T2 高风险工具的审批闭环，走**真实 HTTP 面**。

这条链路此前一条用例都没走通过：`tests/test_optional_planes.py` 只断言
`sandbox_run` 注册成 T2（`test_default_is_t2`），没人验过
「挂起 → 批准 → 真的执行」和「拒绝 → 真的不执行」。

为什么必须端到端：《plane/sandbox_tool.py` 的单测证明的是 handler 会拦白名单，
`core/loop.py` 的单测证明的是 pending_confirmation 的形状；
**两者之间的接缝**（`/v1/turns` 返回挂起、`/v1/turns/{ctx_id}/resume` 带着
decision 重新进入同一条 Context）没有任何用例。接缝上的错法都很安静：
要么挂起了但其实已经执行过（副作用重复），要么批准了却没执行（人以为跑了）。

判据用**文件系统副作用**，不用响应文本：响应文本只能证明循环走完了，
证明不了命令真的被执行过。
"""
from __future__ import annotations

import os
import pathlib
import shutil
import tempfile
import unittest
from unittest import mock

from fastapi.testclient import TestClient

import core.loop as loop_mod
import serve
from contracts import LLMResponse, NormalizedToolCall, Usage

PRINCIPAL = {"user_id": "u_test", "tenant": "acme", "roles": ["sre"],
             # `sandbox_run.required_scope="tool.write"`；`runtime:run` 是回合门槛
             "scopes": ["runtime:run", "tool.read", "tool.write", "sandbox.run"]}

SILENT = lambda *_a, **_k: None  # noqa: E731


def _resp(text: str = "", calls=(), finish: str = "stop") -> LLMResponse:
    return LLMResponse(
        text=text,
        tool_calls=tuple(
            NormalizedToolCall(id=c[0], name=c[1], args=c[2]) for c in calls),
        usage=Usage(input_tokens=10, output_tokens=5, cache_hit_tokens=0,
                    model="mock"),
        finish_reason=finish,
    )


class _Scripted:
    """第一轮发一条 `sandbox_run`，之后给终局文本（脚本耗尽后固定返回最后一条）。"""

    def __init__(self, argv: list[str]) -> None:
        self.prompts: list[str] = []
        self.script = [
            _resp("", [("call_1", "sandbox_run", {"argv": argv})],
                  finish="tool_calls"),
            _resp("已处理。"),
        ]
        self.i = 0

    def complete(self, prompt, tools, e):        # noqa: ANN001, ARG002
        self.prompts.append(str(prompt))
        item = self.script[min(self.i, len(self.script) - 1)]
        self.i += 1
        return item


class TestSandboxApprovalOverHTTP(unittest.TestCase):
    """`AGENT_RUNTIME_SANDBOX_TOOL=1`（默认 T2）下的完整审批闭环。"""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()
        self.proof = os.path.join(self.tmp, "proof.txt")
        if os.path.exists(self.proof):
            os.remove(self.proof)
        # 白名单只放 python3：命令本身用 `-c` 写一个文件，作为"真的跑过"的证据
        self.argv = ["python3", "-c",
                     f"open({self.proof!r},'w').write('ran')"]

        # `build_gateway_from_env` 内部 `from core.loop import Loop`，所以补丁
        # 要打在 `core.loop` 上，而不是 `serve` 上（serve 没有 Loop 属性）。
        self._saved_loop = loop_mod.Loop
        # **同一个 LLM 实例跨回合复用**：resume 会重建 Loop，若每次都新建脚本，
        # 「拒绝理由是否进 prompt」这条就没法观察 —— 新建的那个没有历史。
        self.llm = _Scripted(self.argv)
        _llm = self.llm      # 闭包取名字，别用 `self.` —— 类体里的 self 是 Loop

        class _ToolCallLoop(self._saved_loop):     # type: ignore[misc,valid-type]
            def __init__(self, builder, _ignored, dispatcher, **kw):  # noqa: ANN001
                super().__init__(builder, _llm, dispatcher, **kw)

        loop_mod.Loop = _ToolCallLoop

        self._env = mock.patch.dict(os.environ, {
            "AGENT_RUNTIME_SANDBOX_TOOL": "1",
            "AGENT_RUNTIME_SANDBOX_ALLOW": "python3",
        })
        self._env.start()
        self.gw = serve.build_gateway_from_env(logger=None)
        app = serve.create_app(gateway=self.gw, logger=None, auth_required=False)
        self.client = TestClient(app)

    def tearDown(self) -> None:
        self._env.stop()
        loop_mod.Loop = self._saved_loop
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ── 工具面 ────────────────────────────────────────────────
    def _start_turn(self) -> tuple[dict, str]:
        r = self.client.post("/v1/turns", json={"goal": "跑一条命令",
                                                "principal": PRINCIPAL})
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        ctx_id = (body.get("vertex") or {}).get("ctx_id")
        self.assertTrue(ctx_id, "挂起的回合必须回传 ctx_id，否则客户端无从续跑")
        return body, ctx_id

    def test_sandbox_run_declares_approval(self) -> None:
        """T2 + MUTATING → `/v1/tools` 必须说它要审批（前端靠这个准备 UI）。"""
        tools = {t["name"]: t for t in
                 self.client.get("/v1/tools").json()["tools"]}
        self.assertIn("sandbox_run", tools)
        self.assertTrue(tools["sandbox_run"]["needs_approval"])
        self.assertEqual(tools["sandbox_run"]["risk_level"], 2)

    # ── ① 挂起 ────────────────────────────────────────────────
    def test_suspend_does_not_execute(self) -> None:
        """挂起期间命令**一次都不能跑** —— 挂了却执行过，等于审批是装饰。"""
        body, _ctx = self._start_turn()
        self.assertEqual(body["phase"], "WAITING_USER")
        self.assertEqual(body["term_reason"], "NEEDS_CONFIRMATION")
        self.assertFalse(os.path.exists(self.proof),
                         "挂起时就已经产生副作用 = 审批形同虚设")

    # ── ② 批准 ────────────────────────────────────────────────
    def test_approve_executes_exactly_once(self) -> None:
        _body, ctx_id = self._start_turn()
        self.assertFalse(os.path.exists(self.proof))

        r = self.client.post(f"/v1/turns/{ctx_id}/resume",
                             json={"decision": True,
                                   "reason": "确认只写一个本地文件",
                                   "principal": PRINCIPAL})
        self.assertEqual(r.status_code, 200, r.text)
        out = r.json()
        self.assertEqual(out["phase"], "DONE")
        self.assertTrue(os.path.exists(self.proof),
                        "批准了却没执行 —— 人以为跑过了，比报错更贵")
        self.assertEqual(pathlib.Path(self.proof).read_text(), "ran")

    # ── ③ 拒绝 ────────────────────────────────────────────────
    def test_reject_does_not_execute(self) -> None:
        _body, ctx_id = self._start_turn()
        r = self.client.post(f"/v1/turns/{ctx_id}/resume",
                             json={"decision": False,
                                   "reason": "测试拒绝路径",
                                   "principal": PRINCIPAL})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertFalse(os.path.exists(self.proof),
                         "拒绝了还是执行了 = 最严重的一类缺陷")

    # ── ④ 拒绝理由必须进 history ──────────────────────────────
    def test_reject_reason_reaches_the_model(self) -> None:
        """拒绝要带理由回到模型，否则它只会原样再提一次同样的动作。"""
        _body, ctx_id = self._start_turn()
        before = len(self.llm.prompts)
        r = self.client.post(f"/v1/turns/{ctx_id}/resume",
                             json={"decision": False, "reason": "别碰这台机器",
                                   "principal": PRINCIPAL})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertGreater(len(self.llm.prompts), before,
                           "拒绝后模型没被再问一次，它就不会换方案")
        self.assertTrue(any("别碰这台机器" in p for p in self.llm.prompts[before:]),
                        "拒绝理由没进 prompt，模型无法据此换方案")


if __name__ == "__main__":
    unittest.main(verbosity=2)
