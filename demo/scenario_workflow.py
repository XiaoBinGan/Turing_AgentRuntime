"""demo/scenario_workflow.py — 场景 8：Workflow Plane 多 Agent 协同。

演示四件事：
  ① 共享黑板：分区隔离 + CAS 冲突 + 语义合并重试
  ② 强类型 Envelope：REQUEST / RESPONSE 配对，全程结构化
  ③ Saga：失败时的逆序补偿
  ④ 层次主从：Leader 拆解 → fan-out → fan-in 聚合（子 Agent fork + 降权）
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from adapters.llm_mock import ScriptedLLM, usage                       # noqa: E402
from builder import PromptBuilder                                      # noqa: E402
from contracts import (                                                # noqa: E402
    AgentEnvelope, Budget, ExecCtx, LLMResponse, MessageType,
    NormalizedToolCall, Partition, Principal, RiskTier, SagaStep,
    SideEffect, SubagentSpec, ToolManifest,
)
from core.context import ContextHandle                                 # noqa: E402
from plane.blackboard import Blackboard                                # noqa: E402
from plane.quality import Tracer                                       # noqa: E402
from plane.tool import Dispatcher, Normalizer, ToolRegistry            # noqa: E402
from plane.workflow import (                                           # noqa: E402
    EnvelopeBus, HierarchicalOrchestrator, Saga, SubagentRunner,
)

W = 78


def hr(title: str, char: str = "━") -> None:
    print(f"\n{char * W}\n  {title}\n{char * W}")


def sub(title: str) -> None:
    print(f"\n── {title} " + "─" * max(0, W - len(title) - 5))


def check(label: str, ok: bool, extra: str = "") -> None:
    print(f"  断言: {label} → {'PASS' if ok else 'FAIL'}{('  ' + extra) if extra else ''}")


# ══════════════════════════════════════════════════════════════
def scenario_8_workflow() -> None:
    hr("场景 8／11　Workflow Plane：黑板 · Envelope · Saga · 层次主从")

    # ── ① 共享黑板 ────────────────────────────────────────────
    sub("① 共享黑板：分区隔离 + CAS 乐观锁")
    bb = Blackboard()
    bb.publish_global("coding_style", {"indent": 4, "quote": "double"}, "leader")
    bb.write_private("scratch", {"note": "只有我能看"}, "coder_a")

    print(f"  GLOBAL  coding_style = {bb.read('coding_style', 'coder_b') .value if bb.read('coding_style', 'coder_b') else None}")
    print(f"  PRIVATE scratch(coder_b 视角) = {bb.read('scratch', 'coder_b')}")
    print(f"  PRIVATE scratch(coder_a 视角) = "
          f"{(bb.read('scratch', 'coder_a').value if bb.read('scratch', 'coder_a') else None)}")
    check("PRIVATE 区对他人不可见", bb.read("scratch", "coder_b") is None)

    # 两个 Agent 同时基于 v1 修改 → 只有一个成功
    bb.create("plan", {"steps": ["A"]}, "leader")
    first = bb.compare_and_set("plan", 1, {"steps": ["A", "B"]}, "coder_a")
    second = bb.compare_and_set("plan", 1, {"steps": ["A", "C"]}, "coder_b")
    print(f"  coder_a CAS(v1) → ok={first.ok} version={first.version}")
    print(f"  coder_b CAS(v1) → ok={second.ok} {second.reason}")
    check("并发写只有一个成功（另一个必须重读）", first.ok and not second.ok)

    # 标准 OCC 重试：重读 → 语义合并 → 再提交
    cur = bb.read("plan", "coder_b")
    merged = bb.compare_and_set("plan", cur.version,
                                {"steps": cur.value["steps"] + ["C"]}, "coder_b")
    print(f"  重读 v{cur.version} 语义合并后重提 → ok={merged.ok} version={merged.version}")
    check("重读后 CAS 成功（乐观并发控制闭环）", merged.ok)
    check("两位写者都进了审计链", {"coder_a", "coder_b"} <= set(bb.read("plan").writers))

    # ── ② 强类型 Envelope ─────────────────────────────────────
    sub("② 强类型 Envelope：禁止自然语言群聊")
    bus = EnvelopeBus()
    bus.send(AgentEnvelope(trace_id="t-1", message_type=MessageType.REQUEST,
                           sender="leader", recipient="coder_a",
                           payload={"goal": "改模块 A"}, task_id="task-1",
                           correlation_id="c-1"))
    bus.send(AgentEnvelope(trace_id="t-1", message_type=MessageType.RESPONSE,
                           sender="coder_a", recipient="leader",
                           payload={"diff": "+1 -0"}, correlation_id="c-1"))
    inbox = bus.inbox("leader")
    print(f"  leader 收件箱: {[(e.message_type.value, e.seq) for e in inbox]}")
    check("REQUEST/RESPONSE 按 correlation_id 配对",
          inbox[-1].correlation_id == "c-1" and inbox[-1].message_type == MessageType.RESPONSE)
    try:
        bus.send(AgentEnvelope(trace_id="", message_type=MessageType.EVENT,
                               sender="x", recipient="y", payload={}))
        ok = False
    except ValueError as exc:
        ok = True
        print(f"  空 trace_id 被拒: {exc}")
    check("契约校验拒绝畸形信元", ok)

    # ── ③ Saga 补偿 ───────────────────────────────────────────
    sub("③ Saga：失败 → 逆序补偿，不留脏环境")
    journal: list[str] = []

    def mk(name: str, fail: bool = False):
        def act():
            journal.append(f"do:{name}")
            if fail:
                raise RuntimeError(f"{name} 部署失败")
            return name
        def undo():
            journal.append(f"undo:{name}")
        return act, undo

    saga = Saga("deploy")
    for n in ("branch", "patch", "test"):
        a, u = mk(n)
        saga.add(SagaStep(name=n, action=a, compensate=u))
    a, u = mk("push", fail=True)
    saga.add(SagaStep(name="push", action=a, compensate=u))
    r = saga.run()
    print(f"  执行日志: {journal}")
    print(f"  ok={r['ok']} compensated={r['compensated']}")
    check("失败后逆序补偿", r["compensated"] == ["test", "patch", "branch"])
    check("补偿后无残留状态", journal[-3:] == ["undo:test", "undo:patch", "undo:branch"])

    # ── ④ 层次主从派发 ────────────────────────────────────────
    sub("④ 层次主从：Leader 拆解 → fan-out → fan-in（子 Agent fork + 降权）")
    tracer = Tracer(tenant="acme", user_id="u_leader", task_id="task_multi")
    reg = ToolRegistry()
    reg.register(
        ToolManifest(name="read_file", side_effect=SideEffect.READ_ONLY,
                     risk_tier=RiskTier.T0_SAFE, description="读文件"),
        lambda a, e: {"path": a.get("path"), "content": "def f(): pass"},
    )
    reg.register(
        ToolManifest(name="write_file", side_effect=SideEffect.MUTATING,
                     risk_tier=RiskTier.T2_HIGH, required_scope="tool.write",
                     idempotent=False, description="写文件（有副作用）"),
        lambda a, e: {"written": a.get("path")},
    )
    disp = Dispatcher(reg, normalizer=Normalizer(), tracer=tracer)

    parent_ctx = ContextHandle(initial={
        "principal": Principal(user_id="u_leader", roles=("lead",), tenant="acme",
                               scopes=frozenset({"tool.read", "tool.write"})).to_dict(),
        "phase": "EXECUTING", "loop_count": 0, "history": [],
        "tool_results": [], "observations": [],
    })
    parent_e = ExecCtx(principal=Principal(user_id="u_leader", tenant="acme",
                                          scopes=frozenset({"tool.read", "tool.write"})),
                       task_id="task_multi", budget=Budget(max_steps=12))

    def llm_factory(spec):
        return ScriptedLLM([
            LLMResponse(
                text=f"{spec.name} 开始工作",
                tool_calls=(NormalizedToolCall(
                    id="c1", name="read_file", args={"path": f"{spec.name}.py"}),),
                usage=usage(500, 80)),
            LLMResponse(
                text=f"{spec.name} 完成：已处理 {spec.name}.py",
                tool_calls=(), usage=usage(600, 120)),
        ])

    runner = SubagentRunner(disp, builder=PromptBuilder(), llm_factory=llm_factory,
                            tracer=tracer)
    orch = HierarchicalOrchestrator(runner, trace_id="t-multi")
    specs = [
        SubagentSpec(name="coder_a", goal="改模块 A", allowed_tools=("read_file",)),
        SubagentSpec(name="coder_b", goal="改模块 B", allowed_tools=("read_file",)),
        SubagentSpec(name="coder_c", goal="改模块 C", allowed_tools=("read_file",)),
    ]
    agg = orch.run(specs, parent_ctx, parent_e)
    print(f"  fan-in: ok={agg['ok']} failed={agg['failed']}")
    for res in agg["results"]:
        print(f"    - {res['name']:<8} ok={res['ok']} steps={res['steps']} "
              f"term={res['term_reason']}")
    check("3 个子 Agent 全部完成", agg["ok"] == 3)
    obs = parent_ctx.snapshot().get("observations") or []
    print(f"  父 Context 收到 {len(obs)} 条子 Agent 观测："
          f"{json.dumps(obs[:2], ensure_ascii=False)[:120]}")
    check("子 Agent 观测经白名单 merge 回父（不是空回写）", len(obs) > 0)
    check("父 Context 未被污染（history 等字段不跨分支）",
          "coder_a" not in json.dumps(parent_ctx.snapshot().get("history") or []))

    sub("⑤ 越权拦截：子 Agent 不得调用有副作用的工具")
    spec_bad = SubagentSpec(name="coder_evil", goal="直写生产", allowed_tools=("write_file",))
    bad = runner.dispatch(spec_bad, parent_ctx, parent_e)
    print(f"  coder_evil → ok={bad.ok} error={bad.error[:70]}")
    check("mutating 工具被拒（INV-18）", not bad.ok)

    sub("⑥ 看门狗：Agent 数量超限时停止 fan-out")
    many = [SubagentSpec(name=f"w{i}", goal="x") for i in range(9)]
    agg2 = orch.run(many, parent_ctx, parent_e)
    wd = agg2.get("watchdog", {})
    print(f"  watchdog = {json.dumps(wd, ensure_ascii=False)[:120]}")
    check("触发看门狗并返回部分结果", bool(wd.get("aborted_by_watchdog")))


SCENARIOS = [("场景 8／11　Workflow Plane 多 Agent", scenario_8_workflow)]
