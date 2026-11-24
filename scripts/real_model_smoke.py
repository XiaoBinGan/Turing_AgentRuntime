#!/usr/bin/env python3
"""scripts/real_model_smoke.py — 拿真实模型跑一次完整链路。

**为什么需要这个脚本**：mock LLM 不读 prompt，所以「prompt 里到底有没有工具」
「tool_calls 解包对不对」「token 预算准不准」这三件事在 mock 下**永远不会暴露**。
本项目就因此漏过一个致命缺陷（见 README 第 ⑤ 类）：
Loop 解析工具清单失败 → 模型一本正经地说"没有可用工具"，而 493 个测试全绿。

用法：

    export BABEL_KEY=sk-...
    export BABEL_BASE=https://www.babelark.ai/v1
    export BABEL_MODEL=deepseek-flash
    python3 scripts/real_model_smoke.py

不设环境变量时直接跳过（退出码 0），所以可以放进 CI 而不需要密钥。
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from adapters.openai_compat import OpenAICompatAdapter                    # noqa: E402
from builder import CALIBRATED, PromptBuilder, estimate_tokens            # noqa: E402
from contracts import (                                                   # noqa: E402
    Budget, ExecCtx, Principal, RiskTier, SideEffect, ToolManifest,
)
from core.context import ContextHandle                                    # noqa: E402
from core.loop import Loop                                                # noqa: E402
from plane.quality import Tracer                                          # noqa: E402
from plane.tool import Dispatcher, Normalizer, Policy, ToolRegistry       # noqa: E402


def _skip(msg: str) -> int:
    print(f"[skip] {msg}")
    return 0


def main() -> int:
    key = os.environ.get("BABEL_KEY") or os.environ.get("OPENAI_API_KEY", "")
    base = os.environ.get("BABEL_BASE") or os.environ.get("OPENAI_BASE_URL", "")
    model = os.environ.get("BABEL_MODEL", "deepseek-flash")
    if not key or not base:
        return _skip("未设置 BABEL_KEY / BABEL_BASE，跳过真实模型冒烟")

    seen: dict[str, list] = {}
    prompts: list[str] = []

    def mk(name: str, payload: dict):
        def fn(args: dict, e: ExecCtx):
            seen.setdefault(name, []).append(dict(args))
            return payload
        return fn

    reg = ToolRegistry()
    reg.register(
        ToolManifest(
            name="query_logs", side_effect=SideEffect.READ_ONLY,
            risk_tier=RiskTier.T0_SAFE, description="按服务查询最近日志行",
            input_schema={
                "type": "object",
                "properties": {"service": {"type": "string"},
                               "lines": {"type": "integer", "minimum": 1, "maximum": 2000}},
                "required": ["service"],
                "additionalProperties": False,
            }),
        mk("query_logs", {"p99_ms": 3210, "errors": 41,
                          "top": ["acquire timeout after 3000ms", "pool exhausted (20/20)"]}),
    )
    reg.register(
        ToolManifest(
            name="query_alerts", side_effect=SideEffect.READ_ONLY,
            risk_tier=RiskTier.T0_SAFE, description="查询当前告警",
            input_schema={
                "type": "object",
                "properties": {"service": {"type": "string"}},
                "required": ["service"],
                "additionalProperties": False,
            }),
        mk("query_alerts", {"alert": "ConnPoolExhausted", "severity": "P1"}),
    )

    class Capturing(OpenAICompatAdapter):
        def _request_body(self, prompt: str, tools_schema: list) -> dict:
            prompts.append(prompt)
            return super()._request_body(prompt, tools_schema)

    tracer = Tracer(tenant="acme", user_id="u_smoke", task_id="smoke")
    adapter = Capturing(base_url=base, model=model, api_key=key,
                        timeout_ms=90000, max_retries=2, max_tokens=2048)
    loop = Loop(PromptBuilder(total_tokens=3000), adapter,
                Dispatcher(reg, policy=Policy(), normalizer=Normalizer(), tracer=tracer),
                tracer=tracer)

    ctx = ContextHandle(initial={
        "principal": Principal(user_id="u_smoke", roles=("sre",), tenant="acme",
                               scopes=frozenset({"tool.read", "tool.write"})).to_dict(),
        "session_id": "s_smoke", "turn_id": "t_smoke", "task_id": "smoke",
        "phase": "RECEIVED", "loop_count": 0,
        "goal": "定位 api 服务 p99 升高的根因",
        "history": [], "tool_results": [], "observations": [],
        "memory_hits": [{"kind": "profile", "text": "生产变更前必须先只读定位", "score": 0.9}],
        "knowledge_hits": [{"doc_id": "runbook-pool", "span": "3.2", "version": "2",
                            "text": "连接池上限 200，超过 150 并发需检查泄漏"}],
    })
    e = ExecCtx(principal=Principal(user_id="u_smoke", tenant="acme",
                                    scopes=frozenset({"tool.read", "tool.write"})),
                task_id="smoke",
                budget=Budget(token_total=60000, cost_total_usd=0.5,
                              tool_calls_total=6, max_steps=5))

    t0 = time.time()
    r = loop.run("定位 api 服务 p99 升高的根因，需要时调用工具查证", ctx, e)
    wall = time.time() - t0

    usage = ctx.snapshot().get("usage") or {}
    st = adapter.stats()
    report = {
        "model": model,
        "phase": r.phase.value,
        "term_reason": r.term_reason.value,
        "steps": r.steps,
        "wall_s": round(wall, 2),
        "tools_called": sorted(seen),
        "args_received": seen,
        "tokens": {"in": usage.get("input_tokens"), "out": usage.get("output_tokens")},
        "cost_usd": round(float(st.get("cost_usd", 0)), 6),
        "loop_warnings": loop.warnings[:3],
        "answer_head": (r.text or "")[:200],
    }

    # ── 三条关键断言（mock 下测不出来的那三条） ─────────────────
    fails: list[str] = []
    if not seen:
        fails.append("模型没调用任何工具 —— 检查『工具清单是否真的进了 Prompt』")
    if loop.warnings:
        fails.append(f"Loop 有告警：{loop.warnings[0]}")
    from contracts import validate_against_schema
    schema_errors: dict[str, list[str]] = {}
    for name, calls in seen.items():
        entry = reg.get(name)
        manifest = entry[0] if entry else None
        for args in calls:
            if "args" in args and isinstance(args["args"], dict) and len(args) == 1:
                fails.append(f"{name} 的参数没解包（仍是 args 包装层）：{args}")
            if manifest is not None:
                errs = validate_against_schema(manifest.input_schema, args)
                if errs:
                    schema_errors.setdefault(name, []).extend(errs)
    report["schema_errors"] = schema_errors
    if schema_errors:
        fails.append(f"模型给的参数不符合 schema：{schema_errors}")
    # 预算分解：tools schema 占多少、留给上下文的还剩多少
    try:
        builder = PromptBuilder(total_tokens=3000)
        report["budget"] = builder.budget_breakdown(reg.manifests() if hasattr(reg, "manifests")
                                                    else [])
        if prompts:
            report["prompt_estimate"] = estimate_tokens(prompts[0], CALIBRATED)
    except Exception as exc:                                        # noqa: BLE001
        report["budget"] = {"error": f"{type(exc).__name__}: {exc}"}
    report["fails"] = fails

    print(json.dumps(report, ensure_ascii=False, indent=2))
    if fails:
        print("\n[FAIL] " + " | ".join(fails))
        return 1
    print("\n[OK] 真实链路通过：工具进 Prompt、参数解包正确、无告警")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
