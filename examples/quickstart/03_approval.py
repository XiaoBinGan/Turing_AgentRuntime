"""03_approval.py —— 高风险工具触发人工确认，以及两种「带着批准续跑」的方式。

    .venv/bin/python examples/quickstart/03_approval.py

这是这个框架和「直接调 LLM」最大的区别：
**模型想做的事，不等于系统允许做的事。** 一个声明了 ``risk_tier=T2_HIGH`` 的工具
在拿到人工批准之前，Dispatcher 会把它拦成 ``needs_confirmation``，回合停在
``WAITING_USER``；批准之后，被拦下的那一次调用会**原样重放**，然后继续循环。

三个演示：
  ① 不批准           → 停在 WAITING_USER，副作用**没有发生**
  ② resume(approved) → 框架替你补 Approval 记录并重放，副作用真的发生
  ③ 直接带 Approval  → 进程重启 / HTTP 层做审批时用的方式（新 ExecCtx + approvals）
另外附一个 ④ 拒绝路径：模型会被告知"换方案"，而不是硬做。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from builder import PromptBuilder                                   # noqa: E402
from contracts import (                                             # noqa: E402
    Approval, Budget, ExecCtx, LLMResponse, NormalizedToolCall,
    Principal, RiskTier, SideEffect, ToolManifest, args_hash,
)
from core.context import ContextHandle                              # noqa: E402
from core.loop import Loop                                          # noqa: E402
from plane.tool import Dispatcher, ToolRegistry                     # noqa: E402

SENT: list[str] = []          # 假的"外部世界"：只有真的执行了才往里写


# ══════════════════════════════════════════════════════════════
# 工具：T2 + MUTATING —— 三道闸都要过（scope → 风险分级 → 审批）
# ══════════════════════════════════════════════════════════════
def send_email(args: dict, e: ExecCtx) -> dict:
    SENT.append(str(args.get("to")))
    return {"sent_to": args.get("to"), "subject": args.get("subject")}


def build_registry() -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(
        ToolManifest(
            name="send_email",
            side_effect=SideEffect.MUTATING,          # 有副作用
            risk_tier=RiskTier.T2_HIGH,                # ≥T2 且未获批 → needs_confirmation
            idempotent=False,                          # 发信不可重放，拒绝缓存
            required_scope="tool.write",               # 主体必须有这个 scope
            description="给指定收件人发邮件（有副作用，需人工确认）",
            input_schema={
                "type": "object",
                "properties": {"to": {"type": "string"}, "subject": {"type": "string"}},
                "required": ["to"],
                "additionalProperties": False,
            },
        ),
        send_email,
    )
    return reg


class ScriptedLLM:
    """第 1 次：要求发信。之后每次：都收敛到终答（够演示审批流了）。"""

    def __init__(self) -> None:
        self.i = 0

    def complete(self, prompt: str, tools, e: ExecCtx) -> LLMResponse:
        self.i += 1
        if self.i == 1:
            return LLMResponse(
                text="需要给老板发一封周报。",
                tool_calls=(NormalizedToolCall(id="call_0", name="send_email",
                                               args={"to": "boss@example.com",
                                                     "subject": "本周进展"}),),
            )
        return LLMResponse(text="已处理完毕。")


def fresh() -> tuple[ContextHandle, ExecCtx]:
    """每次演示都用干净的 Context + ExecCtx（避免上一轮的状态污染结论）。"""
    principal = Principal(user_id="u_demo",
                          scopes=frozenset({"tool.read", "tool.write"}))
    ctx = ContextHandle(initial={"principal": principal.to_dict()}, budget=Budget())
    return ctx, ExecCtx(principal=principal, budget=ctx.budget)


def main() -> int:
    # 注意：`ScriptedLLM` **是有状态的**（每条脚本只出一次，用完重复最后一条）。
    # 四段演示共用同一个实例的话，第二段开始模型就直接给终答了 —— 看起来
    # "审批没触发"，其实是脚本已经播完。所以每段都新建 Loop。
    dispatcher = Dispatcher(build_registry())      # 同一个注册表，四段共用
    new_loop = lambda: Loop(PromptBuilder(), ScriptedLLM(), dispatcher)   # noqa: E731

    # ── ① 未批准：停在 WAITING_USER，副作用没发生 ──────────────
    print("① 不批准")
    loop1 = new_loop()
    ctx, e = fresh()
    r = loop1.run("给老板发一封周报", ctx, e)
    print(f"   phase={r.phase.value}  term_reason={r.term_reason.value}  steps={r.steps}")
    print(f"   text = {r.text}")
    pending = ctx.snapshot().get("pending_confirmation")
    print(f"   待确认: tool={pending['tool']}  args={pending['args']}")
    print(f"   args_hash={pending['args_hash']}   ← 批准时要原样带回来")
    print(f"   外部世界 SENT={SENT}  ← 空，说明工具根本没执行")

    # ── ② resume(decision=True)：框架补 Approval 并重放被拦的调用 ──
    print("\n② resume(decision=True)")
    r2 = loop1.resume(ctx, e, decision=True, reason="低风险周报，同意发送")  # 同一个 loop 续跑
    print(f"   phase={r2.phase.value}  steps={r2.steps}")
    print(f"   text = {r2.text}")
    print(f"   外部世界 SENT={SENT}  ← 这次真的发了")

    # ── ③ 直接带 Approval：进程重启 / HTTP 层审批后重建 ExecCtx 的写法 ──
    # `ExecCtx.approved(tool, args_hash)` 逐字比对，所以 args_hash 必须一致 ——
    # 参数被改过就不算"同一件事"，闸会重新拦（这是故意的，防止批 A 做 B）。
    print("\n③ 新 ExecCtx 里直接带 Approval（模拟重启后续跑）")
    ctx3, e3 = fresh()
    e3.approvals = (Approval(tool="send_email",
                             args_hash=args_hash({"to": "boss@example.com",
                                                  "subject": "本周进展"}),
                             approved=True, approver="alben"),)
    r3 = new_loop().run("给老板发一封周报", ctx3, e3)
    print(f"   phase={r3.phase.value}  steps={r3.steps}  ← 不再挂起，直接跑完")

    # ── ④ 拒绝：模型被要求换方案，而不是硬做 ────────────────────
    print("\n④ resume(decision=False)：拒绝")
    ctx4, e4 = fresh()
    loop4 = new_loop()
    before = len(SENT)
    r4 = loop4.run("给老板发一封周报", ctx4, e4)
    r5 = loop4.resume(ctx4, e4, decision=False, reason="收件人写错了，先别发")
    print(f"   第一次 phase={r4.phase.value} → 拒绝后 phase={r5.phase.value}")
    last = ctx4.snapshot().get("history")[-2]   # 注意：快照要 .get()，不能用 [] 下标
    print(f"   模型收到的通知（写进 history）: {last['text']}")
    print(f"   发信次数：{before} → {len(SENT)}（拒绝没有产生副作用）")
    print(f"   loop 警告={loop4.warnings or '（无）'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
