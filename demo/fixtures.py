"""demo/fixtures.py — 演示用的业务脚本与工具 fixture（**不是 Runtime 的一部分**）。

这个文件里写死的每一条都是**具体业务知识**，不是 Runtime 能力：

* ``goal_llm()``            —— "连接池上限 20，峰值并发 180" 的排障脚本
* ``endless_tool_llm()``    —— 无限重试，用来撞预算/步数上限
* ``semantic_loop_llm()``   —— "port conflict on api-gateway" ×6，字面不同语义相同
* ``confirmation_llm()``    —— "重启 api 服务释放连接池"，用来触发人工确认挂起
* ``build_registry()``      —— SRE 演示用的 query_logs / query_alerts / dump_logs / restart_service

它们的职责是**给 Loop / Tool / Quality 造边界场景**，所以它们属于 demo 层，
不属于 ``adapters/``。Runtime 侧只保留中性适配能力：
``ScriptedLLM`` / ``CallableLLM`` / ``usage()``。

依赖方向：``demo`` → ``adapters`` / ``contracts`` / ``plane``（单向）。
反方向被 ``tests/test_architecture.py`` 的目录边界测试钉死：
``adapters/*.py`` 不许出现「连接池 / 重启 / 告警 / api-gateway / 排查 / 运维」这类业务词，
``core/`` 与 ``plane/`` 不许 import ``agents.`` / ``demo.``。
"""
from __future__ import annotations

import sys
from pathlib import Path

# 允许 `python3 demo/fixtures.py` 直接跑（自检），也允许被包导入
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from adapters.llm_mock import CallableLLM, ScriptedLLM, usage          # noqa: E402
from contracts import (                                                # noqa: E402
    ExecCtx, LLMResponse, NormalizedToolCall, RiskTier, SideEffect,
    ToolManifest,
)
from plane.tool import ToolRegistry                                    # noqa: E402


def _call(name: str, args: dict, idx: int) -> NormalizedToolCall:
    """构造一个确定的工具调用（与 adapters 里的内部辅助同形状，刻意不跨层引用私有名）。"""
    return NormalizedToolCall(id=f"call_{idx}", name=name, args=args)


# ══════════════════════════════════════════════════════════════
# 预置业务脚本
# ══════════════════════════════════════════════════════════════
def goal_llm() -> ScriptedLLM:
    """正常流程：查日志 → 查告警 → 给终答。"""
    return ScriptedLLM([
        LLMResponse(
            text="先看最近日志。",
            tool_calls=(_call("query_logs", {"service": "api", "lines": 200}, 0),),
            usage=usage(820, 120),
        ),
        LLMResponse(
            text="日志指向连接池耗尽，再查告警确认。",
            tool_calls=(_call("query_alerts", {"service": "api"}, 1),),
            usage=usage(1100, 140),
        ),
        LLMResponse(
            text="根因：连接池上限 20，峰值并发 180，导致 acquire 超时。建议上调至 200 并加熔断。",
            tool_calls=(),
            usage=usage(1300, 260),
        ),
    ])


def endless_tool_llm(tool: str = "query_logs") -> CallableLLM:
    """无限调用工具 —— 用于触发预算 / 步数上限。

    参数必须**同时**满足两件事：① 合法（工具声明了 ``input_schema``，乱塞字段会在
    Dispatcher 第 2 步就被 ``invalid args`` 拦下，预算根本涨不上去，于是场景 2 会
    以 MAX_STEPS 而非 BUDGET 收口）；② 每轮字面不同（否则命中幂等缓存，同样不消耗
    预算）。所以变化落在已声明的 ``lines`` 上 —— 合法且天然不重复。
    """
    def fn(i: int, _p: str) -> LLMResponse:
        return LLMResponse(
            text=f"继续第 {i} 次尝试。",
            tool_calls=(_call(tool, {"service": "api", "lines": (i % 2000) + 1}, i),),
            usage=usage(700, 80),
        )
    return CallableLLM(fn)


def semantic_loop_llm() -> CallableLLM:
    """语义死锁：字面每轮都不同，但去掉易变部分后语义完全相同。

    对应现实里的「同一个查询换个说法反复查」——
    一级（字面计数）抓不住，必须靠二级（语义指纹）。
    """
    variants = [
        "port conflict on api-gateway",
        "port conflict on api-gateway please retry",
        "port conflict on api-gateway again",
        "port conflict on api-gateway once more",
        "port conflict on api-gateway retry now",
        "port conflict on api-gateway try again",
    ]

    def fn(i: int, _p: str) -> LLMResponse:
        q = variants[i % len(variants)]
        # 参数形状必须是合法的（query_logs 的 input_schema 只认 service/lines），
        # 否则 L2 判定还没轮到，调用就先被 invalid args 拦下。字面差异继续由
        # `lines` 承担 —— 语义指纹把数字折叠成 <NUM>，于是"字面不同、语义相同"
        # 这个要演示的性质一点没丢。
        return LLMResponse(
            text=f"再确认一次（{i}）：{q}。",
            tool_calls=(_call("query_logs", {"service": "api", "lines": (i % 2000) + 1}, i),),
            usage=usage(650, 70),
        )
    return CallableLLM(fn)


def confirmation_llm() -> ScriptedLLM:
    """先调一个 mutating 工具（需要人工确认），批准后给终答。"""
    return ScriptedLLM([
        LLMResponse(
            text="需要重启服务以释放连接池。",
            tool_calls=(_call("restart_service", {"service": "api"}, 0),),
            usage=usage(760, 110),
        ),
        LLMResponse(
            text="已重启 api 服务，连接池恢复，观察 5 分钟无异常。",
            tool_calls=(),
            usage=usage(880, 150),
        ),
    ])


# ══════════════════════════════════════════════════════════════
# 工具 fixture（SRE 演示场景；业务语义只在 demo 层）
# ══════════════════════════════════════════════════════════════
STATE = {"restarted": False, "log_bytes": 0}


def _query_logs(args: dict, e: ExecCtx):
    return {
        "service": args.get("service", "api"),
        "p99_ms": 3200,
        "errors": 41,
        "top_errors": ["acquire timeout after 3000ms", "pool exhausted"],
    }


def _query_alerts(args: dict, e: ExecCtx):
    return {"alert": "conn_pool_exhausted", "since": "2026-11-22T19:50:00Z", "severity": "P1"}


def _dump_logs(args: dict, e: ExecCtx):
    """故意返回 80KB，用来验证 Normalizer 的截断 + artifacts 落盘。"""
    blob = "2026-11-22T19:5x ERROR acquire timeout pool=20 active=180\n" * 1200
    STATE["log_bytes"] = len(blob)
    return {"raw": blob}


def _restart_service(args: dict, e: ExecCtx):
    STATE["restarted"] = True
    return {"service": args.get("service"), "restarted": True}


def build_registry() -> ToolRegistry:
    """演示工具集（模拟经 MCP 接入的能力）。

    每个工具都带 ``input_schema``：**入参契约是工具定义的一部分**，不是可选项。
    没有它，adapter 只能发保守形状 ``{"args": {}}``，模型只能靠猜参数名 —— 实测就
    会把 ``lines`` 写成 ``limit``；而 Dispatcher 没有契约也无从拒绝，工具拿着
    ``args.get()`` 兜底返回空结果，模型再基于空结果一本正经地分析。
    ``additionalProperties: False`` 是故意的：未知字段必须报错，不能被静默丢弃。
    """
    reg = ToolRegistry()
    reg.register(
        ToolManifest(name="query_logs", side_effect=SideEffect.READ_ONLY,
                     risk_tier=RiskTier.T0_SAFE, description="按服务查询最近日志",
                     input_schema={
                         "type": "object",
                         "properties": {
                             "service": {"type": "string"},
                             "lines": {"type": "integer", "minimum": 1, "maximum": 2000},
                         },
                         "required": ["service"],
                         "additionalProperties": False,
                     }),
        _query_logs,
    )
    reg.register(
        ToolManifest(name="query_alerts", side_effect=SideEffect.READ_ONLY,
                     risk_tier=RiskTier.T0_SAFE, description="查询当前告警",
                     input_schema={
                         "type": "object",
                         "properties": {"service": {"type": "string"}},
                         "required": ["service"],
                         "additionalProperties": False,
                     }),
        _query_alerts,
    )
    reg.register(
        ToolManifest(name="dump_logs", side_effect=SideEffect.READ_ONLY,
                     risk_tier=RiskTier.T1_LOW, max_result_bytes=8192,
                     description="导出全量日志（结果很大）",
                     # 无参数工具也要显式声明"我只吃空 object"，
                     # 否则模型会顺手塞个 service 进来而无人反对。
                     input_schema={
                         "type": "object",
                         "properties": {},
                         "additionalProperties": False,
                     }),
        _dump_logs,
    )
    reg.register(
        ToolManifest(name="restart_service", side_effect=SideEffect.MUTATING,
                     risk_tier=RiskTier.T2_HIGH, idempotent=False,
                     required_scope="tool.write", per_turn_limit=2,
                     description="重启服务（有副作用，需人工确认）",
                     input_schema={
                         "type": "object",
                         "properties": {"service": {"type": "string"}},
                         "required": ["service"],
                         "additionalProperties": False,
                     }),
        _restart_service,
    )
    return reg


if __name__ == "__main__":                       # 自检：fixture 自洽即可，不跑 Loop
    for factory in (goal_llm, endless_tool_llm, semantic_loop_llm, confirmation_llm):
        llm = factory()
        assert callable(llm.complete), factory
    assert len(build_registry().names()) >= 4
    print("demo/fixtures.py OK：4 套业务脚本 + 4 个工具 fixture 可用")
