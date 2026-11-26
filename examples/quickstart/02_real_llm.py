"""02_real_llm.py —— 同一个例子，把假 LLM 换成 OpenAI 兼容端点。

    export BASE_URL='https://api.example.com/v1'
    export API_KEY='sk-...'          # 本地 vLLM / Ollama 可以为空
    export MODEL='your-model-name'
    .venv/bin/python examples/quickstart/02_real_llm.py

它接的是 ``POST {BASE_URL}/chat/completions``，所以 OpenAI / DeepSeek / Qwen
（DashScope 兼容模式）/ vLLM / Ollama 都能直接用，**一份代码换三个环境变量**。

和 01 的差别只有一行：``FakeLLM()`` → ``OpenAICompatAdapter(...)``。
Loop / Context / Dispatcher / 工具定义一个字都没改 —— 这是这个框架想证明的事：
「模型」是可替换的适配器，「编排」才是 Runtime 自己的。

密钥只从环境变量读，**不落盘、不进代码、不进日志**（adapter 会自己脱敏）。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from adapters.openai_compat import LLMError, OpenAICompatAdapter  # noqa: E402
from builder import PromptBuilder                                 # noqa: E402
from contracts import (                                           # noqa: E402
    Budget, ExecCtx, Principal, SideEffect, ToolManifest,
)
from core.context import ContextHandle                            # noqa: E402
from core.loop import Loop                                        # noqa: E402
from plane.tool import Dispatcher, ToolRegistry                    # noqa: E402


# ── 工具：与 01 完全相同的定义（只有工具名/参数不同，形状一模一样）──
def get_weather(args: dict, e: ExecCtx) -> dict:
    """假数据工具：真模型会自己决定调它、以及传什么参数。"""
    return {"city": args.get("city"), "temp_c": 23, "sky": "多云"}


def build_registry() -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(
        ToolManifest(
            name="get_weather",
            side_effect=SideEffect.READ_ONLY,
            risk_tier=0,
            description="查询某个城市当前的天气",
            input_schema={
                "type": "object",
                "properties": {"city": {"type": "string", "description": "城市名"}},
                "required": ["city"],
                "additionalProperties": False,
            },
        ),
        get_weather,
    )
    return reg


def main() -> int:
    # ── 1. 读配置：缺什么就说缺什么，而不是抛 traceback ──────────
    base_url = (os.environ.get("BASE_URL") or "").strip()
    api_key = (os.environ.get("API_KEY") or "").strip()
    model = (os.environ.get("MODEL") or "").strip()

    missing = [name for name, val in (("BASE_URL", base_url), ("MODEL", model)) if not val]
    if missing:
        print("这个例子需要先设置环境变量：" + "、".join(missing))
        print()
        print("    export BASE_URL='https://api.example.com/v1'   # 必填，要 http(s):// 开头")
        print("    export API_KEY='sk-...'                        # 本地 vLLM/Ollama 可留空")
        print("    export MODEL='your-model-name'                 # 必填")
        print()
        print("（01_minimal.py 不联网，可以先跑它确认骨架是好的）")
        return 2                          # 明确的退出码：配置缺失，不是代码坏了

    if not api_key:
        print("提示：API_KEY 为空。本地 vLLM / Ollama 通常不校验，继续。\n")

    # ── 2. 装配：唯一改动就是这一行 ────────────────────────────
    llm = OpenAICompatAdapter(
        base_url, model, api_key,
        timeout_ms=60_000,     # 单请求超时
        max_retries=2,         # 只对 429/5xx/网络抖动退避；401/400 不重试
        max_tokens=800,        # reasoning 模型不设会被 reasoning 吃满预算
    )
    registry = build_registry()
    dispatcher = Dispatcher(registry)
    ctx = ContextHandle(initial={"principal": Principal(user_id="u_demo").to_dict()},
                        budget=Budget())
    e = ExecCtx(principal=Principal(user_id="u_demo"), budget=ctx.budget)

    loop = Loop(PromptBuilder(), llm, dispatcher)

    # ── 3. 跑。真实模型会自己决定调不调工具、调几次 ─────────────
    print(f"── 请求 {model} @ {base_url} ─────────────────────")
    try:
        result = loop.run("上海现在天气怎么样？用一句话回答。", ctx, e)
    except LLMError as exc:
        # 理论上走不到：Loop **不让异常逃逸**，它会把 adapter 抛的异常收成
        # FAILED/UNRECOVERABLE 的 Result（见下面的分支）。留着只是为了自证这件事。
        print(f"模型调用失败：kind={exc.kind} status={exc.status}")
        return 3

    # ── 4. 失败也是 Result，不是异常 ────────────────────────────
    # 这是最容易踩的一条：模型挂了 → 你拿到的是 phase=FAILED 的**返回值**，
    # 而不是 traceback。所以**永远要检查 term_reason**，别只看 text。
    if result.term_reason.value == "UNRECOVERABLE":
        last = llm.history[-1] if llm.history else {}
        kind = last.get("kind", "unknown")
        # adapter 已经把消息里的密钥换成 ***，可以直接打
        print(f"模型调用失败（kind={kind}, status={last.get('status')}）：{last.get('error')}")
        if kind == "auth":
            print("  → 检查 API_KEY 是否正确/过期（401/403 不重试）")
        elif kind == "bad_request":
            print(f"  → 检查 MODEL={model!r} 是否是服务端支持的模型名")
        elif kind in ("network", "transient", "server", "transport"):
            print(f"  → 检查 BASE_URL={base_url!r} 是否可达（本地服务是否已启动）")
        return 3

    print(f"phase       = {result.phase.value}")
    print(f"term_reason = {result.term_reason.value}")
    print(f"steps       = {result.steps}")
    print(f"text        = {result.text}")
    obs = ctx.snapshot().get("observations", [])
    print(f"工具调用     = {[(o['tool'], o['ok']) for o in obs]}")
    print(f"usage       = in={llm.last_usage.input_tokens} out={llm.last_usage.output_tokens}"
          f"  估算={llm.last_estimated}")
    print(f"本次成本     = ${llm.last_cost_usd:.6f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
