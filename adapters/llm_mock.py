"""adapters/llm_mock.py — 确定性 LLM 替身（**中性 Runtime 能力**）。

真实项目里这里换成 OpenAI / DeepSeek / Qwen 的 adapter；本 Demo 只需要可控。
所有 mock 都实现 ``contracts.LLMProtocol``：``complete(prompt, tools, e) -> LLMResponse``。
**不修改 budget**：预算记账由 Loop 负责（避免双记账）。

目录边界（见 ``tests/test_architecture.py``）：
本文件属于 Runtime 的 adapter 层，只放「屏蔽不同模型接口差异」的通用实现。
具体业务的演练脚本（演示 fixture 数据）住在 ``demo/fixtures.py`` ——
那里属于 demo 层，不是 Runtime 能力，不要把它们搬回这里。
"""
from __future__ import annotations

from typing import Callable, Sequence

from contracts import (
    ExecCtx, LLMResponse, NormalizedToolCall, ToolManifest, Usage,
)


def _call(name: str, args: dict, idx: int) -> NormalizedToolCall:
    """内部辅助：构造一个确定的工具调用（id 由序号决定，保证可重放）。"""
    return NormalizedToolCall(id=f"call_{idx}", name=name, args=args)


class ScriptedLLM:
    """按脚本顺序返回。脚本用完后重复最后一条。"""

    model = "mock-1"

    def __init__(self, script: Sequence[LLMResponse]):
        if not script:
            raise ValueError("script 不能为空")
        self.script = list(script)
        self.i = 0

    def complete(self, prompt: str, tools: Sequence[ToolManifest], e: ExecCtx) -> LLMResponse:
        resp = self.script[min(self.i, len(self.script) - 1)]
        self.i += 1
        return resp


class CallableLLM:
    """由回调决定下一步，便于构造死锁/预算耗尽场景。"""

    model = "mock-1"

    def __init__(self, fn: Callable[[int, str], LLMResponse]):
        self.fn = fn
        self.i = 0

    def complete(self, prompt: str, tools: Sequence[ToolManifest], e: ExecCtx) -> LLMResponse:
        resp = self.fn(self.i, prompt)
        self.i += 1
        return resp


def usage(inp: int = 900, out: int = 180, model: str = "mock-1") -> Usage:
    """构造一个确定的 Usage（token 数由调用方给，保证成本归因可对账）。"""
    return Usage(input_tokens=inp, output_tokens=out, model=model)
