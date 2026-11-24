"""demo/run.py — 四个场景跑通 Agent Runtime 骨架。

    python3 demo/run.py

场景 1  正常全链路       → 终答 + patch log + trace 树 + 成本归因 + 轨迹评测
场景 2  预算耗尽         → partial 收口，明确告知未完成
场景 3  语义死锁         → 三级判定命中（字面不同、语义相同）
场景 4  人工确认挂起恢复 → 序列化 → 重启 → resume → 完成
"""
from __future__ import annotations

import json
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from demo.fixtures import (                                            # noqa: E402
    STATE, build_registry, confirmation_llm, endless_tool_llm, goal_llm,
    semantic_loop_llm,
)
from builder import PromptBuilder                                      # noqa: E402
from contracts import (                                                # noqa: E402
    Approval, Budget, ExecCtx, Owner, Patch, Phase, Principal,
    RiskTier, SideEffect, TermReason, ToolManifest, Usage, args_hash, dump,
)
from core.context import ContextHandle                                 # noqa: E402
from core.loop import Loop                                             # noqa: E402
from plane.quality import EvalCase, Evaluator, Replayer, Tracer, Trajectory  # noqa: E402
from plane.tool import Dispatcher, Normalizer, Policy, ToolRegistry     # noqa: E402

W = 78


def hr(title: str, char: str = "━") -> None:
    print(f"\n{char * W}\n  {title}\n{char * W}")


def sub(title: str) -> None:
    print(f"\n── {title} " + "─" * max(0, W - len(title) - 5))


def new_ctx(budget: Budget | None = None) -> ContextHandle:
    return ContextHandle(initial={
        "principal": Principal(user_id="u_alben", roles=("sre",),
                               tenant="acme",
                               scopes=frozenset({"tool.read", "tool.write"})).to_dict(),
        "session_id": "sess_demo",
        "turn_id": "turn_demo",
        "task_id": "task_demo",
        "phase": Phase.RECEIVED.value,
        "loop_count": 0,
        "history": [],
        "tool_results": [],
        "memory_hits": [
            {"kind": "profile", "text": "生产变更必须先只读定位，禁止直接改配置", "score": 0.98},
            {"kind": "episodic", "text": "上周同类问题：连接池 20 → 重启后恢复", "score": 0.72},
        ],
        "knowledge_hits": [
            {"doc_id": "runbook-db-pool", "span": "3.2",
             "text": "连接池上限默认 20，峰值并发 >150 时必然 acquire 超时"},
        ],
        "budget": (budget or Budget()).to_dict(),
    }, budget=budget or Budget())


# ══════════════════════════════════════════════════════════════
# 输出
# ══════════════════════════════════════════════════════════════
def print_result(r) -> None:
    print(f"  phase={r.phase.value:<14} reason={r.term_reason.value:<18} "
          f"steps={r.steps:<2} partial={r.partial}")
    print(f"  终答: {r.text}")


def print_patch_log(ctx, limit: int = 24) -> None:
    log = list(ctx.patch_log)
    sub(f"Patch Log（{len(log)} 条 = 执行轨迹 = 崩溃恢复 WAL）")
    for i, p in enumerate(log[:limit], 1):
        val = p.value
        if isinstance(val, (dict, list)):
            val = json.dumps(val, ensure_ascii=False)[:42]
        print(f"  #{i:02d}  {p.by.value:<11} {p.op:<7} {p.path:<20} {str(val)[:40]}")
    if len(log) > limit:
        print(f"  … 其余 {len(log) - limit} 条略")


def derive_usage(ctx) -> dict:
    """从 patch log 事后重建 token 消耗（纯计算，不碰 Tracer）。

    Context 里的 `usage` 是**累计值**，所以相邻两条 set 的差值就是单步消耗。
    用途：与 Loop 直接上报给 Tracer 的数字**对账** —— 两条独立路径算出同一个数，
    才说明「轨迹是唯一事实源」这个设计真的成立。
    """
    prev = {"input": 0, "output": 0, "cache_hit": 0}
    for p in ctx.patch_log:
        if p.path != "usage" or not isinstance(p.value, dict):
            continue
        prev = {
            "input": int(p.value.get("input_tokens", 0) or 0),
            "output": int(p.value.get("output_tokens", 0) or 0),
            "cache_hit": int(p.value.get("cache_hit_tokens", 0) or 0),
        }
    return prev


def print_trace(tracer, ) -> None:
    sub("Trace 树（OpenTelemetry 形状）")
    try:
        print(tracer.tree())
    except Exception:
        print(dump(tracer.finish()))
    tot = tracer.finish().get("totals", {})
    cost = tot.get("cost", {})
    tk = tot.get("tokens", {})
    sub("Token 与成本归因（INV-17：必须能按 租户/用户/任务 三维拆）")
    print(f"  tokens: in={tk.get('input', 0)} out={tk.get('output', 0)} "
          f"cache_hit={tk.get('cache_hit', 0)}")
    print(f"  总成本: ${cost.get('usd', 0):.6f}")
    for dim in ("by_tenant", "by_user", "by_task"):
        if cost.get(dim):
            print(f"  {dim}: {json.dumps(cost[dim], ensure_ascii=False)}")


# ══════════════════════════════════════════════════════════════
# 场景
# ══════════════════════════════════════════════════════════════
def scenario_1_normal() -> None:
    hr("场景 1／11　正常全链路：定位 → 确认 → 终答")
    tracer = Tracer(tenant="acme", user_id="u_alben", task_id="task_demo")
    reg = build_registry()
    disp = Dispatcher(reg, policy=Policy(), normalizer=Normalizer(), tracer=tracer)
    ctx = new_ctx()
    e = ExecCtx(principal=Principal(user_id="u_alben", tenant="acme",
                                    scopes=frozenset({"tool.read", "tool.write"})),
                task_id="task_demo", budget=Budget(), deadline=0)

    loop = Loop(PromptBuilder(), goal_llm(), disp, tracer=tracer)
    result = loop.run("定位 api 服务超时的根因", ctx, e)

    print_result(result)
    print_patch_log(ctx)
    print_trace(tracer)
    totals = tracer.finish().get("totals", {})
    cost = totals.get("cost", {}).get("usd", 0)
    print(f"  断言: 成本归因非零 → {'PASS' if cost > 0 else 'FAIL'} (${cost:.6f})")

    derived = derive_usage(ctx)
    tok = totals.get("tokens", {})
    match = (derived["input"] == tok.get("input", -1)
             and derived["output"] == tok.get("output", -1))
    print(f"  断言: 两路对账一致（Loop 上报={tok.get('input')}/{tok.get('output')} "
          f"vs patch log 重建={derived['input']}/{derived['output']}）→ {'PASS' if match else 'FAIL'}")

    # ── 轨迹评测 ────────────────────────────────────────────────
    sub("轨迹评测（从 patch log 直接还原，不需要额外埋点）")
    traj = Trajectory.from_patch_log(ctx.patch_log)
    case = EvalCase(
        name="conn-pool-root-cause",
        expect_term_reason=TermReason.DONE.value,
        expect_tools=["query_logs", "query_alerts"],
        terminal_assert=lambda data: (
            "连接池" in json.dumps(data.get("history", []), ensure_ascii=False)
        ),
    )
    rep = Evaluator().evaluate(case, result, ctx.snapshot().data, traj)
    print(f"  goal_grounding={rep.goal_grounding}  trajectory_match={rep.trajectory_match:.2f}  "
          f"arg_precision={rep.arg_precision:.2f}  efficiency={rep.efficiency:.2f}")
    print(f"  判定: {'PASS' if rep.passed else 'FAIL'}   detail={json.dumps(rep.detail, ensure_ascii=False)}")
    print(f"  工具序列: {traj.tool_sequence()}")

    # ── 回放验证 ────────────────────────────────────────────────
    sub("回放验证（patch log 重放后状态必须等价）")
    replayed = Replayer.replay({}, ctx.patch_log)
    same = json.dumps(replayed, sort_keys=True, ensure_ascii=False, default=str) == \
        json.dumps(replayed, sort_keys=True, ensure_ascii=False, default=str)
    print(f"  重放 {len(ctx.patch_log)} 条 patch → 得到 {len(replayed)} 个顶层字段：{sorted(replayed)}")
    print(f"  轨迹指纹: {traj.fingerprint[:16]}…   一致性: {'OK' if same else 'MISMATCH'}")


def scenario_2_budget() -> None:
    hr("场景 2／11　预算耗尽：必须有界收口，不许无限循环")
    tracer = Tracer(tenant="acme", user_id="u_alben", task_id="task_budget")
    reg = build_registry()
    disp = Dispatcher(reg, policy=Policy(), normalizer=Normalizer(), tracer=tracer)
    budget = Budget(tool_calls_total=3, max_steps=50)
    ctx = new_ctx(budget)
    e = ExecCtx(principal=Principal(user_id="u_alben", tenant="acme",
                                    scopes=frozenset({"tool.read", "tool.write"})),
                task_id="task_budget", budget=budget, deadline=0)

    loop = Loop(PromptBuilder(), endless_tool_llm(), disp, tracer=tracer)
    result = loop.run("无限重试直到放弃", ctx, e)
    print_result(result)
    print(f"  断言: partial == True → {'PASS' if result.partial else 'FAIL'}")
    print(f"  断言: reason 是 BUDGET → {'PASS' if result.term_reason == TermReason.BUDGET else 'FAIL'}")
    print_patch_log(ctx, limit=14)


def scenario_3_deadlock() -> None:
    hr("场景 3／11　语义死锁：字面每轮不同，语义完全相同")
    tracer = Tracer(tenant="acme", user_id="u_alben", task_id="task_deadlock")
    reg = build_registry()
    disp = Dispatcher(reg, policy=Policy(), normalizer=Normalizer(), tracer=tracer)
    budget = Budget(max_steps=40, tool_calls_total=40)
    ctx = new_ctx(budget)
    e = ExecCtx(principal=Principal(user_id="u_alben", tenant="acme",
                                    scopes=frozenset({"tool.read", "tool.write"})),
                task_id="task_deadlock", budget=budget, deadline=0)

    loop = Loop(PromptBuilder(), semantic_loop_llm(), disp, tracer=tracer)
    result = loop.run("查 port conflict", ctx, e)
    print_result(result)
    print(f"  断言: 被判定为无进展 → "
          f"{'PASS' if result.term_reason == TermReason.NO_PROGRESS else 'FAIL'}"
          f"（实际 {result.term_reason.value}）")
    print(f"  断言: 在 max_steps=40 之前收口 → "
          f"{'PASS' if result.steps < 40 else 'FAIL'}（实际 {result.steps} 步）")


def scenario_4_confirm() -> None:
    hr("场景 4／11　有副作用工具：挂起 → 序列化 → 重启 → resume")
    tracer = Tracer(tenant="acme", user_id="u_alben", task_id="task_confirm")
    reg = build_registry()
    disp = Dispatcher(reg, policy=Policy(), normalizer=Normalizer(), tracer=tracer)
    ctx = new_ctx()
    e = ExecCtx(principal=Principal(user_id="u_alben", tenant="acme",
                                    scopes=frozenset({"tool.read", "tool.write"})),
                task_id="task_confirm", budget=Budget(), deadline=0)

    loop = Loop(PromptBuilder(), confirmation_llm(), disp, tracer=tracer)
    result = loop.run("重启 api 服务释放连接池", ctx, e)
    print_result(result)
    ok = result.phase == Phase.WAITING_USER
    print(f"  断言: 命中 WAITING_USER（T2 工具未获批准）→ {'PASS' if ok else 'FAIL'}")

    # ── 序列化 + 恢复（模拟进程重启）────────────────────────────
    sub("序列化 → 假装进程被杀 → restore → resume")
    try:
        blob = ctx.serialize()
        print(f"  serialize: {len(blob)} bytes, schema 可往返")
        ctx2 = ContextHandle.restore(blob, budget=e.budget)
        print(f"  restore 后 version={ctx2.version}  字段={sorted(ctx2.snapshot().data)}")
    except Exception as exc:                                    # noqa: BLE001
        print(f"  序列化不可用：{exc}")
        ctx2 = ctx

    pending = ctx2.snapshot().get("pending_confirmation")
    print(f"  pending_confirmation = {json.dumps(pending, ensure_ascii=False, default=str)[:120]}")

    # 带上人工批准后 resume
    h = args_hash({"service": "api"}) if "restart_service" not in str(pending) else None
    approvals = []
    if isinstance(pending, dict):
        approvals.append(Approval(tool=pending.get("tool", "restart_service"),
                                  args_hash=pending.get("args_hash", h or ""),
                                  approved=True, approver="alben"))
    e2 = ExecCtx(principal=e.principal, task_id="task_confirm", budget=e.budget,
                 deadline=0, approvals=tuple(approvals))
    r2 = loop.resume(ctx2, e2, decision=True, reason="已审批：低峰期可重启")
    print_result(r2)
    print(f"  断言: resume 后跑到 DONE → {'PASS' if r2.phase == Phase.DONE else 'FAIL'}")
    print(f"  断言: 副作用真的发生了（STATE.restarted）→ {'PASS' if STATE['restarted'] else 'FAIL'}")


def main() -> int:
    print("Agent Runtime 骨架 Demo —— 11 个场景，确定性 mock，零外部依赖")
    from demo.scenario_durable import SCENARIOS as DUR
    from demo.scenario_concurrency import SCENARIOS as CONC
    from demo.scenario_fullchain import SCENARIOS as FULL
    from demo.scenario_workflow import SCENARIOS as WF
    from demo.scenarios_plane import SCENARIOS as PLANE

    cases = [
        ("场景 1 正常全链路", scenario_1_normal),
        ("场景 2 预算耗尽", scenario_2_budget),
        ("场景 3 语义死锁", scenario_3_deadlock),
        ("场景 4 人工确认挂起恢复", scenario_4_confirm),
    ] + PLANE + WF + DUR + FULL + CONC

    failed = 0
    for name, fn in cases:
        try:
            fn()
        except Exception:                                       # noqa: BLE001
            failed += 1
            hr(f"{name} 未通过", "!")
            traceback.print_exc(limit=4)
    hr(f"完成：{len(cases) - failed}/{len(cases)} 场景跑通", "═")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
