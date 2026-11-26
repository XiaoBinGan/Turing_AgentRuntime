"""01_minimal.py —— 最小可用回合：一个自定义工具 + 一个假 LLM + 跑一次 Loop。

    .venv/bin/python examples/quickstart/01_minimal.py

**这个例子不联网、不装第三方库、不需要 API Key。**
它证明的是：Runtime 的骨架（Loop / Context / Dispatcher / Builder）可以脱离
任何模型服务独立跑通 —— 你先有一个确定性的骨架，再去接真模型，
出问题时你才知道坏的是骨架还是模型。

装配只有四步（对应下面四个 ═ 段落）：
    ① 定义工具（manifest 声明契约 + 一个普通函数）
    ② 装 Dispatcher（唯一的副作用出口）
    ③ 给一个 LLM（这里用 30 行假实现，接口就是 complete(...)）
    ④ 跑 Loop，看 Result
"""
from __future__ import annotations

import sys
from pathlib import Path

# 仓库是"扁平布局"（contracts.py / core/ / plane/ 都在根目录），
# 所以要先把自己加进 sys.path 才能 import。用 .venv/bin/python 跑脚本时
# sys.path[0] 是 examples/quickstart/，不是仓库根 —— 这一行是必需的。
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from builder import PromptBuilder                                   # noqa: E402
from contracts import (                                             # noqa: E402
    Budget, ExecCtx, LLMResponse, NormalizedToolCall, Principal,
    RiskTier, SideEffect, ToolManifest,
)
from core.context import ContextHandle                              # noqa: E402
from core.loop import Loop                                          # noqa: E402
from plane.tool import Dispatcher, ToolRegistry                     # noqa: E402

# ══════════════════════════════════════════════════════════════
# ① 工具：manifest 是"契约"，函数是"实现"
# ══════════════════════════════════════════════════════════════
def count_words(args: dict, e: ExecCtx) -> dict:
    """工具实现。签名固定为 (args, ExecCtx) -> 任意可 JSON 化的值。

    第二个参数是 ExecCtx（预算 / 主体 / 截止 / 追踪一次带到任何地方），
    这里用不到，但**不要从闭包里偷全局状态** —— 那正是 Runtime 想帮你避免的。
    """
    text = str(args.get("text", ""))
    return {"chars": len(text), "words": len(text.split())}


def build_registry() -> ToolRegistry:
    """注册工具。**没有 input_schema 模型就只能猜参数名**，所以一定要写。

    坑：``risk_tier`` 要传 ``RiskTier`` 枚举，不要传裸 ``0``。
    ``PromptBuilder._render_tools`` 读的是 ``m.risk_tier.name``，传 int 会在
    **拼 prompt 时**抛 AttributeError，而 Loop 会把它吞成一行
    ``[builder 失败：...] 请基于 history 继续。`` 继续跑 —— 结果就是"模型看不到
    任何工具、却一声不吭"，只有 ``loop.warnings`` 里留了痕。下面 main() 会把它打出来。
    """
    reg = ToolRegistry()
    reg.register(
        ToolManifest(
            name="count_words",
            side_effect=SideEffect.READ_ONLY,          # 只读 → 可缓存、无需审批
            risk_tier=RiskTier.T0_SAFE,                # 不要写 0，见上面的坑
            description="统计一段文本的字符数与词数",
            input_schema={
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
                # 故意 False：未知字段必须报错，不能静默丢弃
                "additionalProperties": False,
            },
        ),
        count_words,
    )
    return reg


# ══════════════════════════════════════════════════════════════
# ② 假 LLM：实现 contracts.LLMProtocol 的 complete 即可
# ══════════════════════════════════════════════════════════════
class FakeLLM:
    """两轮：先调工具，再基于工具结果给终答。真实模型换成 02 里的 adapter。

    注意它**不修改 budget** —— 记账是 Loop 的职责（避免双记账）。
    """

    def __init__(self) -> None:
        self.i = 0
        self.seen_prompt = ""

    def complete(self, prompt: str, tools, e: ExecCtx) -> LLMResponse:
        self.seen_prompt = prompt                 # 想看 Builder 拼出了什么就打印它
        self.i += 1
        if self.i == 1:
            return LLMResponse(
                text="先数一下字数。",
                tool_calls=(NormalizedToolCall(id="call_0", name="count_words",
                                               args={"text": "hello runtime world"}),),
            )
        return LLMResponse(text="这句话有 3 个词。")   # 无 tool_calls → 回合结束


# ══════════════════════════════════════════════════════════════
# ③ 装配 + ④ 跑一轮
# ══════════════════════════════════════════════════════════════
def main() -> int:
    registry = build_registry()
    dispatcher = Dispatcher(registry)             # 唯一的副作用出口（校验/限流/审批都在这）

    # ContextHandle 是**唯一可变状态**，所有写入都必须走 Patch（Loop 内部负责）。
    # 这里给最小初始状态：主体 + 预算。预算默认 max_steps=8、tool_calls_total=20。
    ctx = ContextHandle(
        initial={"principal": Principal(user_id="u_demo").to_dict()},
        budget=Budget(),
    )
    e = ExecCtx(principal=Principal(user_id="u_demo"), budget=ctx.budget)

    loop = Loop(PromptBuilder(), FakeLLM(), dispatcher)
    result = loop.run("数一下 hello runtime world 有几个词", ctx, e)

    print("── 结果 ─────────────────────────────────────────────")
    print(f"phase       = {result.phase.value}")
    print(f"term_reason = {result.term_reason.value}")
    print(f"steps       = {result.steps}")
    print(f"text        = {result.text}")
    print(f"工具调用     = {[o['tool'] for o in ctx.snapshot().get('observations', [])]}")
    print(f"patch 条数   = {len(ctx.patch_log)}（执行轨迹 = 崩溃恢复的 WAL）")
    # Loop 不会把内部异常打到 stdout；它只记在 warnings 里。**每次跑完都该看它一眼。**
    print(f"loop 警告    = {loop.warnings or '（无）'}")
    return 0 if result.phase.value == "DONE" else 1


if __name__ == "__main__":
    raise SystemExit(main())
