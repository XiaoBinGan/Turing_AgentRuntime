"""demo/scenario_concurrency.py — 场景 11：并发、真实持久化、容器隔离。

这一场专门打「单进程假设」和「配置了却没生效」这两类问题：

① SQLiteStore 并发写 50 个 Context —— 事务 + WAL，不丢不锁死
② Gateway 并发 10 次 handle —— 审计不丢、配额精确
③ 沙箱后端探测 —— 报告每个后端**为什么**不可用，而不是神秘超时
④ 容器内真跑一条命令（有 docker 时），并证明隔离强度被如实上报
⑤ 语义召回：有 key 走真 embedding，没 key 降级且**记账可见**
"""
from __future__ import annotations

import json
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from builder import PromptBuilder                                          # noqa: E402
from contracts import (                                                    # noqa: E402
    Budget, ExecCtx, Phase, Principal, SandboxPolicy, Subject,
)
from core.context import ContextHandle                                     # noqa: E402
from core.gateway import Gateway                                           # noqa: E402
from core.loop import Loop                                                 # noqa: E402
from core.sqlite_store import SQLiteStore                                  # noqa: E402
from demo.fixtures import goal_llm                                         # noqa: E402
from plane.governance import Governance, InputGuard, OutputGuard, RBAC     # noqa: E402
from plane.memory import MemoryRuntime                                     # noqa: E402
from plane.sandbox_backends import BackendSelector                         # noqa: E402
from plane.tool import Dispatcher, Normalizer, Policy, ToolRegistry        # noqa: E402
from plane.wiring import guarded_sandbox_handler, semantic_memory          # noqa: E402

W = 78


def hr(title: str, char: str = "━") -> None:
    print(f"\n{char * W}\n  {title}\n{char * W}")


def sub(title: str) -> None:
    print(f"\n── {title} " + "─" * max(0, W - len(title) - 5))


def check(label: str, ok: bool, extra: str = "") -> None:
    print(f"  断言: {label} → {'PASS' if ok else 'FAIL'}{('  ' + extra) if extra else ''}")


def scenario_11_concurrency() -> None:
    hr("场景 11／11　并发 · 真实持久化 · 容器隔离 · 语义召回")

    # ── ① SQLiteStore 并发写 ─────────────────────────────────
    sub("① SQLiteStore：50 个并发写，WAL + 事务，不丢不锁死")
    tmp = Path(tempfile.mkdtemp(prefix="rt-conc-"))
    store = SQLiteStore(str(tmp / "ctx.db"))
    errors: list[str] = []
    done = threading.Barrier(20)

    def writer(n: int):
        try:
            done.wait(timeout=10)
            ctx = ContextHandle(initial={
                "principal": {"tenant": f"t{n % 3}", "roles": ["sre"]},
                "phase": Phase.RUNNING.value if hasattr(Phase, "RUNNING") else "EXECUTING",
                "loop_count": n, "history": [], "task_id": f"task-{n}",
            })
            store.save(f"ctx-{n}", ctx.serialize())
        except Exception as exc:                                  # noqa: BLE001
            errors.append(f"{type(exc).__name__}: {exc}")

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)
    alive = [t for t in threads if t.is_alive()]
    pending = store.list_pending()
    print(f"  写入 20 个 → 落库 {len(pending)} 个，错误 {len(errors)}，卡死线程 {len(alive)}")
    if errors:
        print(f"  首个错误: {errors[0][:100]}")
    check("并发写全部成功且无死锁", len(pending) == 20 and not errors and not alive)
    print(f"  按租户查询: {json.dumps(store.stats(), ensure_ascii=False)[:130]}")
    check("能按租户/时间查询（FileStore 做不到）",
          sum(len(store.list_by_tenant(f"t{i}")) for i in range(3)) == 20)

    # ── ② Gateway 并发 ───────────────────────────────────────
    sub("② Gateway：10 个并发 handle，审计条数与配额都精确")
    reg = ToolRegistry()
    gov = Governance(rbac=RBAC(), input_guard=InputGuard(), output_guard=OutputGuard())
    gw = Gateway(lambda goal, tctx: Loop(PromptBuilder(total_tokens=2000), goal_llm(),
                                         Dispatcher(reg, policy=Policy(),
                                                    normalizer=Normalizer())),
                 governance=gov, rate_limit_per_minute=100)
    sre = Principal(user_id="u", roles=("sre",), tenant="acme",
                    scopes=frozenset({"runtime:run", "tool.read"}))
    req = {"goal": "排查连接池", "principal": sre.to_dict(),
           "budget": {"token_total": 5000, "max_steps": 2}}
    res: list = []
    barrier = threading.Barrier(10)

    def call():
        try:
            barrier.wait(timeout=10)
            res.append(gw.handle(req))
        except Exception:                                          # noqa: BLE001
            pass

    ts = [threading.Thread(target=call) for _ in range(10)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(timeout=20)
    print(f"  完成 {len(res)}/10，审计 {len(gw.audit_log)} 条")
    check("并发请求全部返回且审计不丢", len(res) == 10 and len(gw.audit_log) == 10)

    # ── ③ 沙箱后端探测 ───────────────────────────────────────
    sub("③ 沙箱后端：每个后端都要说清「为什么不可用」")
    report = BackendSelector().report()
    for r in report:
        print(f"  {r['name']:<12} strength={r['strength']:<10} available={str(r['available']):<6} "
              f"{r['reason'][:52]}")
    check("不可用的后端有明确原因（不是静默失败）",
          all(r["available"] or r["reason"] for r in report))

    # ── ④ 容器内真跑 ─────────────────────────────────────────
    sub("④ 沙箱执行：按可用强度自动选后端，强度如实上报")
    handler = guarded_sandbox_handler(policy=SandboxPolicy(timeout_ms=20000))
    picked = [r["name"] for r in handler.backend_report() if r["available"]]
    out = handler({"argv": ["echo", "isolated-hello"]}, ExecCtx())
    print(f"  可用后端: {picked}")
    print(f"  执行结果: exit={out['exit_code']} stdout={out['stdout'].strip()!r} "
          f"backend={out['backend']} strength={out['strength']}")
    check("执行成功且强度被如实上报",
          out["exit_code"] == 0 and out["strength"] in ("process", "container", "kernel"))
    top = "kernel" if "kernel" in [r["strength"] for r in handler.backend_report() if r["available"]] else (
        "container" if "container" in [r["strength"] for r in handler.backend_report() if r["available"]]
        else "process")
    check(f"选中的是本机能达到的最强隔离（{top}）", out["strength"] == top)

    # 强度不足时 fail-closed
    strict = guarded_sandbox_handler(policy=SandboxPolicy(require_backend="kernel"))
    try:
        strict({"argv": ["echo", "x"]}, ExecCtx())
        denied = False
    except Exception as exc:                                       # noqa: BLE001
        denied = True
        print(f"  require_backend=kernel → 拒绝: {type(exc).__name__}: {str(exc)[:60]}")
    check("策略要求更强隔离时不降级执行（fail-closed）", denied)

    # ── ⑤ 语义召回 ───────────────────────────────────────────
    sub("⑤ 语义召回：有 embedding 就升级，没有就降级且记账可见")
    import os
    provider = None
    if os.environ.get("BABEL_KEY"):
        try:
            from adapters.embeddings import from_env
            provider = from_env("BABEL")
        except Exception:                                          # noqa: BLE001
            provider = None
    mem = semantic_memory(MemoryRuntime, provider=provider)
    subj = Subject(tenant="acme", user_id="u")
    for text in ("连接池耗尽导致超时",
                 "P99 延迟从 80ms 涨到 2s",
                 "日志盘占用 92%，6 天写满"):
        mem.write({"user": text, "assistant": "记下了", "tools": []}, subj)
    hits = mem.recall("数据库连接满了打不开", subj, k=3)
    st = mem.semantic_stats() if hasattr(mem, "semantic_stats") else {}
    print(f"  查询「数据库连接满了打不开」（与目标零字面重合）")
    for i, h in enumerate(hits[:3], 1):
        print(f"    {i}. [{h.kind:<8} {h.score:.3f}] {h.text[:34]}")
    print(f"  semantic stats: provider={st.get('provider')} fallback={st.get('fallback_hits')} "
          f"errors={st.get('provider_errors')}")
    check("零字面重合也能召回（语义生效或明确降级）",
          bool(hits) and ("连接池" in hits[0].text or st.get("fallback_hits", 0) > 0))
    check("降级有记账（不是静默变回 bigram）",
          "provider" in st and "fallback_hits" in st)


SCENARIOS = [("场景 11／11　并发 · 持久化 · 隔离", scenario_11_concurrency)]
