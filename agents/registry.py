"""agents/registry.py — Agent 注册表（3 个 Agent）+ 路由装配。

Router 属于 Runtime（`plane/router.py`），**选择逻辑不该在这里重写**；
这里只提供两样东西：`AgentSpec`（描述 + 权限 + 工具 + 技能 + priority），
以及一个把 spec 灌进 `AgentRouter` 的 `build_router()`。

三个 Agent 的分工（业务边界，不是关键词表）
==========================================

    sre       排障：指标劣化 → 日志 → 发布，定位根因。**只读**。
    release   发布/回滚：变更窗口内的一切判断，唯一持有 `tool.write` 的角色。
    capacity  容量：水位 / 增速 / 扩容建议。只读 +（审批后的）扩缩容。

`sre` 与 `capacity` 都只需要 `tool.read`；只有 `release` 要 `tool.write` ——
这不是"顺手写全"，而是 Router 的权限闸门（`required_scopes ⊄ principal.scopes`
→ 该 spec **被剔除**，不是扣分）。一个只有 `tool.read` 的服务账号不可能被
路由到 release，因为 release 需要的写权限它没有。

关于 `tags`：为什么挂得这么少
=============================

`tag_match` 的分母是**这个 Agent 的 tag 数**（`plane/router.py::_tag_ratio`）：

    tag_match = 命中的 tag 数 / 去重后的 tag 数

给一个 Agent 挂 8 个"看着全面"的 tag，等于把每个 tag 的权重稀释成 1/8；
而用户真实问句里往往只出现其中 1 个词。实测：`sre` 挂
`("排障","排查","延迟","p99","线上故障")` 时，「p99 上涨怎么排查」的
tag_match 只有 0.4（0.20 × 0.4 = 0.08 分），加上 text_match 的
bigram Jaccard（短中文问句天然偏低，约 0.05~0.10）后总分 0.38 左右，
**低于默认阈值 0.45 → 回澄清**，等于"有 Agent 却选不出来"。

所以这里的纪律是：**tag 只放"用户真的会说的高精度触发词"**，
其余语义交给 `description`（参与 text_match）和技能触发词（`SkillSpec.trigger`，
另一套匹配口径）。三个 Agent 目前的 tag 集：

    sre       ("排查", "p99")       ← 「p99 上涨怎么排查」两个都命中
    release   ("回滚", "版本")       ← 「帮我回滚上个版本」两个都命中
    capacity  ("扩容",)             ← 「要不要扩容」命中

关于 `priority`：为什么三个都是 50
==================================

`priority_norm` 是**相对序**（min-max 归一化到 0..1）。一旦三个 Agent 的
priority 不同，最高分那个白拿 0.10、最低分那个白丢 0.10 —— 这 0.10 恰好
略大于 `GAP_MIN=0.08`，于是"优先级最高的 Agent 会把所有空闲请求都吸走"。
实测代价很具体：把 release 的 priority 调低后，「帮我回滚上个版本」的
release 分数从 0.47 掉到 0.35（低于阈值 → 回澄清），而一个无关 goal
反而会稳定命中 sre。

所以这里显式声明**三者同级**：路由只由「文本 + 标签」决定，人工排序不参与。
将来真要表达"谁的活更该先接"，应当调 priority 并**同时重跑用例**
（`tests/test_agent_tools.py` 里那三条路由断言就是这条纪律的守门人）。
"""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from contracts import AgentSpec  # noqa: E402
from plane.router import AgentRouter  # noqa: E402

__all__ = [
    "DEFAULT_CONFIDENCE_THRESHOLD",
    "FULL_SCOPES",
    "agent_names",
    "build_router",
    "default_agents",
]

#: Router 的置信度阈值。用 Runtime 的默认值而不是自己另设一个：
#: 换了阈值就等于换了一套"什么叫够匹配"的语义，而这条语义经过
#: `plane/router.py` 的标定（0.45 = 文本过半 + 一次 tag 命中 + 权限 + 排序的合理下限）。
DEFAULT_CONFIDENCE_THRESHOLD = 0.45

#: 一个"完整的运维服务账号"应有的 scope。用例与 demo 用它造 principal；
#: 生产里当然应该按角色拆分（只读账号不该有 tool.write）。
FULL_SCOPES = frozenset({"tool.read", "tool.write"})

#: 三个 Agent 同级（理由见模块 docstring「关于 priority」）。
_SHARED_PRIORITY = 50


def default_agents() -> list[AgentSpec]:
    """返回三个 AgentSpec。**每次调用都新建**，调用方可以随意改而不影响别人。"""
    return [
        AgentSpec(
            name="sre",
            description=("线上排障：p99 延迟上涨、错误率升高、日志排查、最近发布与配置定位。"
                         "拿到「哪里慢/哪里错/是不是发布引起的」这类问题归它。"),
            tags=("排查", "p99"),
            # 只读：排障本身不该改动生产。要动手（重启/扩容）必须换到 release
            required_scopes=("tool.read",),
            tools=("query_metrics", "query_logs", "list_deployments", "get_config"),
            skills=("investigate_latency",),
            priority=_SHARED_PRIORITY,
        ),
        AgentSpec(
            name="release",
            description=("发布与回滚：灰度上线、版本回退、变更风险与回滚验证。"
                         "涉及「改生产」的判断归它，它是唯一持有写权限的角色。"),
            tags=("回滚", "版本"),
            # 写权限只给这一个 Agent：权限在 Router 里是**剔除**，不是扣分
            required_scopes=("tool.read", "tool.write"),
            tools=("list_deployments", "query_metrics", "query_logs", "get_config",
                   "restart_service", "scale_service", "rollback_deployment"),
            skills=("safe_rollback",),
            priority=_SHARED_PRIORITY,
        ),
        AgentSpec(
            name="capacity",
            description=("容量规划：水位（cpu/内存）、QPS 增速与扩容/缩容建议。"
                         "「要不要加机器」「什么时候会满」这类问题归它。"),
            tags=("扩容",),
            required_scopes=("tool.read",),
            tools=("query_metrics", "get_config", "scale_service"),
            skills=("capacity_check",),
            priority=_SHARED_PRIORITY,
        ),
    ]


def agent_names() -> list[str]:
    """三个 Agent 的名字（与 `default_agents()` 同序）。"""
    return [a.name for a in default_agents()]


def build_router(agents: list[AgentSpec] | None = None, *,
                 confidence_threshold: float = DEFAULT_CONFIDENCE_THRESHOLD
                 ) -> AgentRouter:
    """造一个装好这些 Agent 的 `AgentRouter`。

    默认用 `default_agents()`，也允许传入自定义集合（用例要测"权限不足时全部
    被剔除 → 澄清"这类边界时会这么用）。
    """
    router = AgentRouter(confidence_threshold=confidence_threshold)
    for spec in (default_agents() if agents is None else agents):
        router.register(spec)
    return router
