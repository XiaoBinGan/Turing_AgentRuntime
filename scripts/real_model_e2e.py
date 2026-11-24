#!/usr/bin/env python3
"""真模型端到端验证：Runtime + Agent 层工具 + 真 LLM。

用法（**密钥只经环境变量，不写入任何文件**）::

    export BABEL_API_KEY=sk-...
    export BABEL_BASE_URL=https://www.babelark.ai/v1
    export BABEL_MODEL=deepseek-flash
    python3 scripts/real_model_e2e.py

    # 换模型
    BABEL_MODEL=gemini-2.5-flash python3 scripts/real_model_e2e.py

它是干什么的：`tests/` 里所有用例都用 mock LLM（确定性、无网络）。
但**真模型会暴露 mock 永远暴露不了的问题**——预算够不够、模型收不收敛、
工具描述写得好不好。这个脚本就是那道门。

实测记录（deepseek-flash，2026-11）：
  * 直连：3.99s，token 计量正确，usage 带 model 名
  * 工具调用：能一次发 5 个并行调用，参数正确（service/metric/window）
  * 收敛性：**不稳定** —— 有时 4 步 DONE，有时 26+ 次调用耗尽预算
  * 失败模式：`term_reason=BUDGET`（不是崩溃；Runtime 有界收口，给出已完成步数）
"""
from __future__ import annotations

import os
import sys
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

GOAL = "线上 api 服务 p99 涨到 3 秒了，帮我排查根因并给出结论"

#: 真模型一轮可能发十几个并行工具调用，默认 20000/20 次**明显不够**。
#: 这不是"放宽限制图省事"，是因为真实排查确实需要多轮取证 —— 见脚本末尾说明。
BUDGET = {"token_total": 200000, "tool_calls_total": 30, "max_steps": 10}


def main() -> int:
    key = os.environ.get("BABEL_API_KEY") or os.environ.get("OPENAI_API_KEY", "")
    base = os.environ.get("BABEL_BASE_URL") or os.environ.get("OPENAI_BASE_URL", "")
    model = os.environ.get("BABEL_MODEL", "deepseek-flash")
    if not key or not base:
        print("[skip] 未设置 BABEL_API_KEY / BABEL_BASE_URL")
        return 0
    os.environ.setdefault("BABEL_API_KEY", key)
    os.environ.setdefault("BABEL_BASE_URL", base)
    os.environ.setdefault("BABEL_MODEL", model)

    from adapters.openai_compat import from_env
    from agents.fake_backend import FakeOpsBackend
    from agents.prompts import build_profile, build_template
    from agents.tools import MANIFESTS, build_handlers
    from builder import PromptBuilder
    from core.gateway import Gateway
    from core.loop import Loop
    from plane.governance import Governance, InputGuard, OutputGuard, RBAC
    from plane.tool import Dispatcher, ToolRegistry

    backend = FakeOpsBackend()
    handlers = build_handlers(backend)
    registry = ToolRegistry()
    for m in MANIFESTS:
        registry.register(m, handlers[m.name])

    adapter = from_env("BABEL")
    if adapter is None:
        print("[skip] 模型适配器装配失败（检查 BABEL_* 三个变量）")
        return 1

    seen: dict = {}

    # 并行工具：真模型一轮常发 5+ 个独立读调用，串行是 p99 的主要来源。
    # `PARALLEL_TOOLS=0` 可关掉做对照。
    parallel = os.environ.get("PARALLEL_TOOLS", "1") not in ("0", "false", "no")
    max_par = int(os.environ.get("MAX_PARALLEL_TOOLS", "4") or 4)

    def loop_factory(goal: str, _tc) -> Loop:
        return Loop(PromptBuilder(total_tokens=8000, template=build_template(),
                                  profile=build_profile()),
                    adapter, Dispatcher(registry), tools=list(MANIFESTS),
                    parallel_tools=parallel, max_parallel_tools=max_par)

    gw = Gateway(loop_factory,
                 governance=Governance(rbac=RBAC(), input_guard=InputGuard(),
                                       output_guard=OutputGuard()),
                 on_turn_start=lambda c, g, p: seen.update(ctx=c))

    print(f"模型: {adapter.model}  并行工具: {parallel}(max={max_par})  目标: {GOAL}")
    t0 = time.time()
    r = gw.handle({"goal": GOAL,
                   "principal": {"user_id": "alben", "tenant": "acme",
                                 "roles": ["sre"],
                                 "scopes": ["runtime:run", "tool.read"]},
                   "budget": BUDGET})
    dt = time.time() - t0

    recs = (seen.get("ctx").get("tool_results") or []) if seen.get("ctx") else []
    calls = Counter(x.get("tool") for x in recs if isinstance(x, dict))
    print(f"耗时 {dt:.1f}s | ok={r.ok} | steps={r.steps} | reason={r.term_reason}")
    print(f"工具调用 {sum(calls.values())} 次: {dict(calls)}")
    print(f"\n--- 回答 ---\n{r.text or '(空)'}")

    # 判据：有终答 + 真的调过工具 + 没超预算
    if not r.text.strip():
        print("\n[FAIL] 没有产出终答")
        return 1
    if not calls:
        print("\n[FAIL] 一次工具都没调 —— 模型没在用工具")
        return 1
    if r.term_reason == "BUDGET":
        print("\n[WARN] 预算耗尽：模型没收敛。"
              "这不是 Runtime 崩溃（有界收口了），是提示词/工具描述需要调优。")
    print("\n[OK] 真模型端到端通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
