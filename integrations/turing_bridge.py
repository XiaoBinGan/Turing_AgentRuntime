"""turing_bridge.py — 把 Agent Runtime 以 NDJSON JSON-RPC 暴露给 Turing（TypeScript 侧）。

**这不是 Runtime 的一部分**，是集成适配层：它依赖 demo/fixtures（业务脚本），
所以必须住在 integrations/，不能和 contracts.py 并排放在仓库根。

约定（与 DSH 的 ACP profile 同一条纪律）：
  * **stdout 只归协议**，所有日志走 stderr；
  * 一行一条 JSON-RPC 2.0 消息；
  * 事件用 DSH v4 的同形状（`{type, seq, time, data}`）推送，于是 Turing 侧只需要一份映射
    （`turing/src/harness/dsh/events.ts`）。

方法：
  runtime.health                 → 运行时自述
  runtime.run_turn {goal}        → 真跑一轮 Loop，期间推送事件通知
  runtime.invariants             → 不变量清单（供 UI/验收展示）

不修改 contracts.py（该文件是团队并行施工的冻结契约）。
"""
from __future__ import annotations

import json
import sys
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from demo.fixtures import goal_llm  # noqa: E402
from builder import PromptBuilder  # noqa: E402
from contracts import (  # noqa: E402
    Budget, ExecCtx, Principal, RiskTier, SideEffect, ToolManifest,
)
from core.context import ContextHandle  # noqa: E402
from core.loop import Loop  # noqa: E402
from plane.quality import Tracer  # noqa: E402
from plane.tool import Dispatcher, Normalizer, Policy, ToolRegistry  # noqa: E402

VERSION = "1.0.0"
INVARIANTS = [
    ("INV-2", "Builder 纯函数，同快照两次 build 字节一致"),
    ("INV-3", "非 owner 写入抛 OwnershipViolation"),
    ("INV-4", "Context 可 JSON round-trip"),
    ("INV-5/15", "Tool 必须声明 side_effect 与 risk_tier"),
    ("INV-6", "Tier2/3 未经 approval 一律拒绝或挂起"),
    ("INV-9", "Tracer fail-open"),
    ("INV-14", "不可信外部数据带 untrusted 标记"),
    ("INV-16", "patch log 可重放出等价状态"),
    ("INV-17", "Token 成本按 tenant/user/task 三维归因"),
    ("INV-18", "子 Agent 权限 ⊆ 父 + 只读工具"),
]

_seq = 0


def emit(event_type: str, data: dict, writer) -> None:
    """推送一条 DSH 形状的事件（通知，不带 id）。"""
    global _seq
    _seq += 1
    writer({
        "jsonrpc": "2.0",
        "method": "runtime/event",
        "params": {"type": event_type, "seq": _seq, "time": int(time.time() * 1000), "data": data},
    })


def _scripted_llm() -> ScriptedLLM:
    """桥自带脚本：查日志 → 查告警 → 终答。

    不依赖 `demo/fixtures.py` 或 `adapters/llm_mock.py` 里的便捷工厂 —— 那些是 demo 的资产，
    会随 demo 重构而变动（实测：llm_mock 在 23:20 被改过，goal_llm 已不在其中）。
    """
    return ScriptedLLM([
        LLMResponse(
            tool_calls=(NormalizedToolCall(id="call_1", name="query_logs", args={"service": "api", "lines": 200}),),
            usage=Usage(input_tokens=1200, output_tokens=60),
        ),
        LLMResponse(
            tool_calls=(NormalizedToolCall(id="call_2", name="query_alerts", args={"service": "api"}),),
            usage=Usage(input_tokens=1100, output_tokens=60),
        ),
        LLMResponse(
            text="根因：连接池上限 20，峰值并发 180，导致 acquire 超时。建议上调至 200 并加熔断。",
            usage=Usage(input_tokens=920, output_tokens=140),
        ),
    ])


def _registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(
        ToolManifest(name="query_logs", side_effect=SideEffect.READ_ONLY,
                     risk_tier=RiskTier.T0_SAFE, description="按服务查询最近日志"),
        lambda args, _e: {"service": args.get("service", "api"), "p99_ms": 3200, "errors": 41},
    )
    registry.register(
        ToolManifest(name="query_alerts", side_effect=SideEffect.READ_ONLY,
                     risk_tier=RiskTier.T0_SAFE, description="查询当前告警"),
        lambda args, _e: {"alert": "conn_pool_exhausted", "severity": "P1"},
    )
    return registry


def _ctx() -> ContextHandle:
    return ContextHandle(initial={
        "principal": Principal(user_id="u_alben", roles=("sre",), tenant="acme",
                               scopes=frozenset({"tool.read", "tool.write"})).to_dict(),
        "session_id": "turing_bridge",
        "turn_id": "turn_bridge",
        "task_id": "task_bridge",
        "phase": "received",
        "loop_count": 0,
        "history": [],
        "tool_results": [],
        "memory_hits": [],
        "knowledge_hits": [],
        "budget": Budget().to_dict(),
    })


class EmittingDispatcher:
    """包一层 Dispatcher：把每次工具调用/结果翻成 Turing 事件（不改 contracts）。"""

    def __init__(self, inner: Dispatcher, writer) -> None:  # noqa: ANN001
        self._inner = inner
        self._writer = writer

    def allowed_names(self):
        return self._inner.allowed_names()

    def invoke(self, name, args, e):  # noqa: ANN001
        call_id = f"bridge-{int(time.time() * 1000)}-{name}"
        emit("tool/call", {"callId": call_id, "name": name,
                           "arguments": json.dumps(args, ensure_ascii=False)}, self._writer)
        outcome = self._inner.invoke(name, args, e)
        ok = getattr(outcome, "ok", True)
        payload = getattr(outcome, "result", None) or getattr(outcome, "error", None) or {}
        # ToolResult 之类的包装取内层 payload，避免把 Python repr 直接写给用户
        payload = getattr(payload, "payload", payload)
        try:
            text = json.dumps(payload, ensure_ascii=False, default=str)
        except TypeError:
            text = str(payload)
        emit("tool/result", {"toolCallId": call_id, "isError": not ok,
                             "message": {"role": "tool", "content": [{"type": "text", "text": text}]}},
             self._writer)
        return outcome


def _canon_budget(data: dict) -> dict:
    """把重放得到的嵌套 budget 归一到 context 实际落盘的扁平形状。

    背景（实测缺陷）：core/context.py 的 _commit_budget 写的是 budget.token_used /
    budget.cost_total_usd 这类扁平字段，而 patch log 里记的是整块 budget 对象
    （{"token": {"total":…, "used":…}}）。两边形状不一致，导致 INV-16「patch log 可重放出
    等价状态」在 budget 上不成立。

    这里做的是**校验侧的归一**，不改任何一方的语义：
      · 重放侧按 patch 语义得到嵌套结构 → 摊平成 context 的形状再比对；
      · 真正的修法在 runtime 侧二选一：要么 _commit_budget 改成写嵌套路径，
        要么 Replayer 增加红色的 budget 兼容分支。由 runtime owner 决定。
    """
    budget = data.get("budget")
    if not isinstance(budget, dict):
        return data
    flat = {}
    token = budget.get("token")
    if isinstance(token, dict):
        flat["token_total"] = token.get("total")
        flat["token_used"] = token.get("used")
    cost = budget.get("cost_usd")
    if isinstance(cost, dict):
        flat["cost_total_usd"] = cost.get("total")
        flat["cost_used_usd"] = cost.get("used")
    calls = budget.get("tool_calls")
    if isinstance(calls, dict):
        flat["tool_calls_total"] = calls.get("total")
        flat["tool_calls_used"] = calls.get("used")
    if not flat:
        return data
    out = dict(data)
    out["budget"] = {**{k: v for k, v in budget.items() if not isinstance(v, dict)}, **flat}
    return out


def _digest(data) -> str:  # noqa: ANN001
    import hashlib
    payload = json.dumps(data, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def run_turn(goal: str, writer, budget_limit: int | None = None) -> dict:
    tracer = Tracer(tenant="acme", user_id="u_alben", task_id="task_bridge")
    registry = _registry()
    dispatcher = EmittingDispatcher(
        Dispatcher(registry, policy=Policy(), normalizer=Normalizer(), tracer=tracer), writer)
    loop = Loop(PromptBuilder(), goal_llm(), dispatcher, tracer=tracer)
    initial = {
        "principal": Principal(user_id="u_alben", roles=("sre",), tenant="acme",
                               scopes=frozenset({"tool.read", "tool.write"})).to_dict(),
        "session_id": "turing_bridge",
        "turn_id": "turn_bridge",
        "task_id": "task_bridge",
        "phase": "received",
        "loop_count": 0,
        "history": [],
        "tool_results": [],
        "memory_hits": [],
        "knowledge_hits": [],
        "budget": Budget().to_dict(),
    }
    if budget_limit:
        initial["budget"] = Budget(token=budget_limit).to_dict()
    ctx = ContextHandle(initial=initial)
    e = ExecCtx(principal=Principal(user_id="u_alben", tenant="acme",
                                    scopes=frozenset({"tool.read", "tool.write"})),
                task_id="task_bridge", budget=Budget(), deadline=0)

    emit("turn/start", {"turn": 1}, writer)
    emit("user/message", {"message": {"role": "user", "content": [{"type": "text", "text": goal}]}}, writer)
    result = loop.run(goal, ctx, e)
    text = getattr(result, "text", "") or ""
    emit("assistant/message",
         {"turn": 1, "step": 1, "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}},
         writer)
    totals = tracer.finish().get("totals", {})
    emit("turn/end", {"turn": 1, "reason": {"kind": "completed"}}, writer)
    patches = [
        {"op": p.op, "path": p.path, "by": str(getattr(p.by, "value", p.by)), "value": p.value}
        for p in ctx.patch_log
    ]
    final_data = ctx.snapshot().to_dict() if hasattr(ctx.snapshot(), "to_dict") else ctx.snapshot().data
    # 摘要必须与"重放侧能看到的表示"完全一致：先归一到 JSON 形状，再走同一个 budget 归一，
    # 否则两边哈希的是两种形状，INV-16 校验会假红。
    final_data = _canon_budget(json.loads(json.dumps(final_data, ensure_ascii=False, default=str)))
    return {
        "text": text,
        "phase": str(getattr(getattr(result, "phase", None), "value", getattr(result, "phase", ""))),
        "tokens": totals.get("tokens", {}),
        "costUsd": totals.get("cost", {}).get("usd", 0),
        "patchCount": len(patches),
        # INV-16 的对外形态：初始态 + patch log + 终态摘要，重放方据此自证等价。
        "replay": {
            "initial": initial,
            "patches": patches,
            # 终态一起带上：重放方才能给出"哪几个字段不等价"，而不是只回一个 false。
            "state": final_data,
            "stateDigest": _digest(final_data),
        },
    }


def replay(payload: dict) -> dict:
    """按 initial + patch log 重建终态，并与记录时的摘要比对（重放必须自证等价）。"""
    from contracts import Patch, Owner
    from plane.quality import Replayer

    initial = payload.get("initial") or {}
    raw_patches = payload.get("patches") or []
    patches = [
        Patch(op=item["op"], path=item["path"], by=Owner(item["by"]), value=item.get("value"))
        for item in raw_patches
    ]
    rebuilt = _canon_budget(Replayer.replay(initial, patches))
    digest = _digest(rebuilt)
    expected = payload.get("stateDigest")
    recorded = payload.get("state")
    diffs = _diff(recorded, rebuilt) if isinstance(recorded, dict) else []
    digest_matches = (digest == expected) if expected else None
    # 等价判定以**字段级比对**为准：摘要只是快路径。两者不一致时必须如实暴露（见 digestMatches）。
    matches = (len(diffs) == 0) if isinstance(recorded, dict) else digest_matches
    return {
        "ok": True,
        "patches": len(patches),
        "stateDigest": digest,
        "digestMatches": digest_matches,
        "matches": matches,
        "diffs": diffs,
    }


def _diff(recorded, rebuilt, path="", out=None, limit=20):  # noqa: ANN001
    """逐字段比对：只报差异（最多 limit 条），用于把"重放不等价"变成可修的清单。"""
    out = [] if out is None else out
    if len(out) >= limit:
        return out
    if type(recorded) is not type(rebuilt):
        out.append({"path": path or "$", "kind": "type",
                    "recorded": type(recorded).__name__, "rebuilt": type(rebuilt).__name__})
        return out
    if isinstance(recorded, dict):
        for key in sorted(set(recorded) | set(rebuilt)):
            child = f"{path}.{key}" if path else key
            if key not in recorded:
                out.append({"path": child, "kind": "missing-in-recorded", "rebuilt": rebuilt[key]})
            elif key not in rebuilt:
                out.append({"path": child, "kind": "missing-in-rebuilt", "recorded": recorded[key]})
            else:
                _diff(recorded[key], rebuilt[key], child, out, limit)
            if len(out) >= limit:
                return out
        return out
    if isinstance(recorded, list):
        if len(recorded) != len(rebuilt):
            out.append({"path": path, "kind": "length", "recorded": len(recorded), "rebuilt": len(rebuilt)})
            return out
        for index, (left, right) in enumerate(zip(recorded, rebuilt)):
            _diff(left, right, f"{path}[{index}]", out, limit)
        return out
    if recorded != rebuilt:
        out.append({"path": path, "kind": "value", "recorded": recorded, "rebuilt": rebuilt})
    return out


def handle(method: str, params: dict, writer) -> dict:
    if method == "runtime.health":
        return {"ok": True, "runtime": "agent-runtime", "version": VERSION,
                "invariants": len(INVARIANTS), "python": sys.version.split()[0]}
    if method == "runtime.invariants":
        return {"items": [{"id": i, "text": t} for i, t in INVARIANTS]}
    if method == "runtime.run_turn":
        return run_turn(str(params.get("goal", "定位 api 服务超时的根因")), writer,
                        params.get("budgetTokens"))
    if method == "runtime.replay":
        return replay(params.get("replay") or {})
    raise ValueError(f"unknown method: {method}")


def main() -> None:
    writer_lock = __import__("threading").Lock()

    def write(obj: dict) -> None:
        with writer_lock:
            sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
            sys.stdout.flush()

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
            result = handle(request.get("method", ""), request.get("params") or {}, write)
            write({"jsonrpc": "2.0", "id": request.get("id"), "result": result})
        except Exception as exc:  # noqa: BLE001
            print(f"[bridge] {exc}\n{traceback.format_exc()}", file=sys.stderr)
            write({"jsonrpc": "2.0", "id": (json.loads(line).get("id") if line.startswith("{") else None),
                   "error": {"message": str(exc)}})


if __name__ == "__main__":
    main()
