"""默认装配下的 prompt 必须真的装进模型上下文。

这条路径此前没有任何用例：`tests/test_serve.py` 全部注入假 gateway，
`tests/test_loop.py` 自己 new `PromptBuilder`，`demo/` 自带 builder ——
**`build_gateway_from_env()` 的默认装配反而没人读一眼它产出的 prompt。**

实际状态是坏的：不配 `AGENT_RUNTIME_PROMPT_TEMPLATE` 时，`template` 是 `None`，
而 `PromptBuilder(template=None)` 不会退回内置模板（默认值只在参数缺省时生效）。
于是每次渲染都在 `template.system` 上抛 `AttributeError`，`core/loop.py` 把它
吞成一句 88 字节的兜底串：

    [builder 失败：AttributeError: 'NoneType' object has no attribute 'system'] 请基于 history 继续。

后果：HTTP 面 200/DONE、审计日志齐全，**模型收到的是一个不含 goal、不含历史、
不含工具表、不含记忆的 prompt**。所有 2300+ 用例都绿，因为没有一个用例
读过默认装配产出的 prompt。

本文件把这条锁住。
"""
from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest

from fastapi.testclient import TestClient

import core.loop as loop_mod
import serve
from builder import PromptTemplate
from contracts import LLMResponse, Usage

PRINCIPAL = {"user_id": "u_test", "tenant": "acme", "roles": ["sre"],
             "scopes": ["runtime:run"]}

SILENT = lambda *_a, **_k: None  # noqa: E731


class _Log:
    def info(self, *a, **k) -> None: pass
    def warn(self, *a, **k) -> None: pass
    def error(self, *a, **k) -> None: pass
    def debug(self, *a, **k) -> None: pass


class _Capture:
    """记下模型真正收到的 prompt。"""

    def __init__(self) -> None:
        self.prompts: list[str] = []

    def complete(self, prompt, tools, e):        # noqa: ANN001, ARG002
        self.prompts.append(str(prompt))
        return LLMResponse(text="好", usage=Usage(1, 1, 0, "mock"),
                           finish_reason="stop")


class TestDefaultPromptAssembly(unittest.TestCase):
    def setUp(self) -> None:
        self._saved = loop_mod.Loop
        self.llm = _Capture()
        _llm = self.llm

        class _L(self._saved):                 # type: ignore[misc,valid-type]
            def __init__(self, builder, _ignored, dispatcher, **kw):  # noqa: ANN001
                super().__init__(builder, _llm, dispatcher, **kw)

        loop_mod.Loop = _L
        self.tmp = tempfile.mkdtemp()
        if self.tmp not in sys.path:
            sys.path.insert(0, self.tmp)

    def tearDown(self) -> None:
        loop_mod.Loop = self._saved
        if self.tmp in sys.path:
            sys.path.remove(self.tmp)

    def _turn(self, env: dict) -> str:
        gw = serve.build_gateway_from_env(logger=_Log(), env=env)
        app = serve.create_app(gateway=gw, logger=_Log(), auth_required=False)
        r = TestClient(app).post("/v1/turns",
                                 json={"goal": "查一下连接池为什么满",
                                       "principal": PRINCIPAL})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(self.llm.prompts, "模型一次都没被调用")
        return self.llm.prompts[0]

    def test_default_env_renders_goal_and_no_fallback(self) -> None:
        """不配模板 → 用内置模板，prompt 里必须有 goal、且不能是兜底串。"""
        prompt = self._turn({})
        self.assertNotIn("builder 失败", prompt,
                         "默认装配渲染失败 → 模型只收到一句'请基于 history 继续'")
        self.assertIn("连接池", prompt, "goal 没进 prompt")
        self.assertGreater(len(prompt), 60, f"prompt 过短：{prompt!r}")

    def test_custom_template_still_wins(self) -> None:
        """配了 `AGENT_RUNTIME_PROMPT_TEMPLATE` → 用它的措辞，不被默认覆盖。"""
        pathlib.Path(self.tmp, "_tmpl.py").write_text(
            "from builder import PromptTemplate\n"
            "def make():\n"
            "    return PromptTemplate(system='SYS_CANARY_42', tools_header='')\n")
        prompt = self._turn({"AGENT_RUNTIME_PROMPT_TEMPLATE": "_tmpl:make"})
        self.assertIn("SYS_CANARY_42", prompt)


if __name__ == "__main__":
    unittest.main(verbosity=2)
