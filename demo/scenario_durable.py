"""demo/scenario_durable.py — 场景 9：持久化续跑（ADR-7 的物理兑现）。

要证明的不是"能序列化"，而是这四件事连起来：

    跑到 T2 工具挂起  →  落盘  →  进程真的死了（Context 对象丢弃）  →  恢复并跑完

中间那一步是最容易糊弄的：如果 Context 还在内存里，"恢复"其实是自欺欺人。
所以这里**显式 `del ctx`**，并且换一个新的 Store 实例读盘，走的是完整路径。
"""
from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from builder import PromptBuilder                                         # noqa: E402
from contracts import (                                                  # noqa: E402
    Approval, Budget, ExecCtx, Phase, Principal, TermReason, args_hash,
)
from core.context import ContextHandle                                   # noqa: E402
from core.loop import Loop                                               # noqa: E402
from core.store import (                                                 # noqa: E402
    FileStore, list_resumable, resume_from_store, save_checkpoint,
)
from demo.fixtures import build_registry, confirmation_llm, STATE        # noqa: E402
from plane.quality import Tracer                                          # noqa: E402
from plane.tool import Dispatcher, Normalizer                              # noqa: E402

W = 78


def hr(title: str, char: str = "━") -> None:
    print(f"\n{char * W}\n  {title}\n{char * W}")


def sub(title: str) -> None:
    print(f"\n── {title} " + "─" * max(0, W - len(title) - 5))


def check(label: str, ok: bool, extra: str = "") -> None:
    print(f"  断言: {label} → {'PASS' if ok else 'FAIL'}{('  ' + extra) if extra else ''}")


def _new_ctx() -> ContextHandle:
    return ContextHandle(initial={
        "principal": Principal(user_id="u_alben", roles=("sre",), tenant="acme",
                               scopes=frozenset({"tool.read", "tool.write"})).to_dict(),
        "session_id": "sess_durable",
        "turn_id": "turn_durable",
        "task_id": "task_durable",
        "goal": "重启 api 服务释放连接池",
        "phase": Phase.RECEIVED.value,
        "loop_count": 0,
        "history": [],
        "tool_results": [],
        "observations": [],
        "memory_hits": [],
        "knowledge_hits": [],
    })


def scenario_9_durable() -> None:
    hr("场景 9／11　持久化续跑：挂起 → 落盘 → 进程死 → 恢复 → 跑完")

    root = Path(tempfile.mkdtemp(prefix="rt-store-"))
    ctx_id = "task_durable"
    STATE["restarted"] = False
    try:
        # ── ① 跑到 T2 工具挂起 ───────────────────────────────
        sub("① 跑到 WAITING_USER（T2 工具需要人工审批）")
        tracer = Tracer(tenant="acme", user_id="u_alben", task_id="task_durable")
        reg = build_registry()
        disp = Dispatcher(reg, normalizer=Normalizer(), tracer=tracer)
        loop = Loop(PromptBuilder(), confirmation_llm(), disp, tracer=tracer)

        ctx = _new_ctx()
        e = ExecCtx(principal=Principal(user_id="u_alben", tenant="acme",
                                        scopes=frozenset({"tool.read", "tool.write"})),
                    task_id="task_durable", budget=Budget(), deadline=0)
        r1 = loop.run("重启 api 服务释放连接池", ctx, e)
        print(f"  phase={r1.phase.value}  reason={r1.term_reason.value}  steps={r1.steps}")
        check("挂起在 WAITING_USER", r1.phase == Phase.WAITING_USER)

        # ── ② 落盘 ────────────────────────────────────────────
        sub("② 落盘：Context 写进磁盘（原子写）")
        store = FileStore(root)
        save_checkpoint(ctx, store, ctx_id, reason="等待人工审批")
        files = sorted(p.name for p in root.iterdir())
        print(f"  磁盘文件: {files}")
        print(f"  大小: {(root / f'{ctx_id}.json').stat().st_size} bytes")
        check("落盘成功且无 .tmp 残留", f"{ctx_id}.json" in files
              and not any(n.endswith(".tmp") for n in files))
        check("GLB-1 检查点里带 pending_confirmation",
              bool(ctx.snapshot().get("pending_confirmation")))

        # ── ③ 模拟进程死亡 ────────────────────────────────────
        sub("③ 模拟进程死亡：Context 对象丢弃，Store 换新实例")
        version_before = ctx.version
        pending_info = ctx.snapshot().get("pending_confirmation")
        del ctx                                    # 内存里真的没有了
        store2 = FileStore(root)                   # 新实例，只能靠磁盘
        resumable = list_resumable(store2)
        print(f"  重启后扫到的可续跑任务: {[(d.get('ctx_id'), d.get('phase')) for d in resumable]}")
        check("新进程能扫到待续跑任务", any(d.get("ctx_id") == ctx_id for d in resumable))

        # ── ④ 人工审批 → 恢复 → 跑完 ──────────────────────────
        sub("④ 人工审批后 resume（审批信息来自 pending_confirmation，不是硬编码）")
        tool_name = pending_info.get("tool", "restart_service")
        ah = pending_info.get("args_hash") or args_hash(pending_info.get("args") or {})
        print(f"  审批对象: {tool_name}  args_hash={ah}")
        e2 = ExecCtx(principal=e.principal, task_id="task_durable", budget=Budget(),
                     deadline=0, approvals=(Approval(tool=tool_name, args_hash=ah,
                                                    approved=True, approver="alben"),))
        r2 = resume_from_store(loop, store2, ctx_id, e2, decision=True,
                               reason="已审批：低峰期可重启")
        print(f"  phase={r2.phase.value}  reason={r2.term_reason.value}  steps={r2.steps}")
        print(f"  终答: {r2.text}")
        check("恢复后跑到 DONE", r2.phase == Phase.DONE and r2.term_reason == TermReason.DONE)
        check("副作用真的发生了", STATE["restarted"])

        # ── ⑤ 收尾：不该再留在 pending 里 ─────────────────────
        sub("⑤ 正常结束后清理检查点")
        after = list_resumable(store2)
        print(f"  剩余待续跑: {[d.get('ctx_id') for d in after]}")
        check("完成后不再出现在待续跑清单", all(d.get("ctx_id") != ctx_id for d in after))
        print(f"\n  （落盘前 version={version_before}，恢复后的版本由 store 里的 serialized 状态决定）")

    finally:
        shutil.rmtree(root, ignore_errors=True)


SCENARIOS = [("场景 9／11　持久化续跑", scenario_9_durable)]
