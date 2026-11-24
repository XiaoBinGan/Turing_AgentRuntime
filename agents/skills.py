"""agents/skills.py — 3 个多 Tool 编排技能（SOP 经验包）。

Skill 与 Tool 的分界（`docs/SPI.md` §2.3）
==========================================

    Tool   原子能力：一次查询、一次重启。Runtime 管权限 / 审批 / 归一化。
    Skill  多 Tool 编排：先查什么、再看什么、什么情况收手、结论怎么下。
           **步骤由技能自己拆**，Runtime 只给配额、审计、深度限制。

契约里没有「body」这个字段，别去找：`SkillSpec` 把「常驻」和「按需」拆成了两半 ——

    index_text  **属性**，由 name + trigger + description 拼出来的一行，进 Prompt 常驻；
    body_text() **方法**，由 steps / pitfalls / verification / tools 拼成的规程全文，
                只在 `SkillRuntime.match()` 命中后才装载（受 `max_body_chars` 约束）。

所以本文件里 `description` 必须**短**（它进索引、每轮都付钱），
真正的规程写在 `steps` / `pitfalls` / `verification` 里（按需付费）。

三个技能分别对应三类真实工况
============================

    investigate_latency  指标已经劣化 → 定位根因（读多写零）
    safe_rollback        要动生产 → 先取证、再评估不可逆代价、最后交人决定
    capacity_check       还没坏，但快满了 → 用水位与增速算出「扩到几」

写 SOP 的三条自我约束（比步骤本身更重要）：

1. **每一步都落到一个具体工具**。写「分析一下日志」等于没写；
   写 `query_logs(services=[...], level="ERROR", window_minutes=60)` 才是可执行规程
    （**多服务一次查完**：单查会逼出 N 次往返，实测是模型不收敛的主因之一）。
2. **避坑条目必须是「会真的做错」的事**。比如「别先重启，现场会没」是真的会
   发生（重启清掉连接池与 in-flight 请求），不是正确的废话。
3. **验收标准必须可判定**。写「结论正确」没法验；写「每条判断都能指到某次
   工具返回的字段」才验得了。
"""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from contracts import SkillSpec  # noqa: E402
from plane.skill import SkillLibrary  # noqa: E402

__all__ = [
    "capacity_check",
    "default_skills",
    "investigate_latency",
    "safe_rollback",
    "build_library",
    "skill_names",
]

#: 三个技能的版本号。改 SOP 就升版本：`SkillLibrary.add` 同名新版本会成为
#: current 且**保留旧版本**（审计时能查到「三个月前那版写了什么」）。
SKILL_VERSION = "1"


def investigate_latency() -> SkillSpec:
    """延迟劣化排查：查指标 → 查日志 → 查发布 → 给结论。"""
    return SkillSpec(
        name="investigate_latency",
        title="延迟劣化排查（p99 上涨 / 错误率上升）",
        # 索引常驻：一句话说清「什么时候用它」。控制在 60 字内。
        description="指标劣化时按 指标→日志→发布 三段定位根因并给结论",
        trigger=("p99", "延迟", "劣化", "排查"),
        tools=("query_metrics", "query_logs", "list_deployments", "get_config"),
        steps=(
            "确认劣化：query_metrics(service, metric=\"p99\", window_minutes=60)，"
            "看 stats.delta_ratio —— 大于 0.3 才算「在涨」，单点毛刺不算。",
            "区分类型：再查 error_rate 与 qps。错误率涨而 QPS 不涨 = 饱和或依赖故障；"
            "两者同涨 = 流量问题；两者都平 = 只是长尾抖动。",
            "找证据行：query_logs(services=[目标服务], level=\"ERROR\", window_minutes=60, limit=20)，"
            "把错误归到三类之一（池耗尽 / 超时 / 上游 5xx），记下原文以便复述。",
            "对齐时间线：list_deployments(service, limit=5)，看劣化起点前 30 分钟内"
            "有没有发布，尤其 suspect=true 的那条；有则把「变更内容」抄进结论。",
            "核对参数：get_config(service) 看 pool_max / acquire_timeout_ms 这类值是否"
            "与预期一致（敏感字段被打码，看不清就写「需人工确认」，不要猜）。",
            "下结论：按「现象 → 证据 → 根因 → 建议」四段写。建议若涉及变更，"
            "必须写明对应工具的副作用等级（restart_service=T2 需审批，回滚=T3 会被拒）。",
        ),
        pitfalls=(
            "不要一上来就 restart_service：它会清掉连接池状态与 in-flight 请求，"
            "现场没了就再也证明不了根因。",
            "p99 天然有毛刺。3000ms 的单点不等于劣化，要看连续点与 delta_ratio。",
            "没有发布也可能劣化（依赖方故障、流量结构变化、数据量增长），"
            "不要把「最近发布」当成唯一假设。",
            "get_config 里被打码的字段不代表「没配置」——只代表你看不到，"
            "需要原值时应当让人去看，而不是推断一个值。",
        ),
        verification=(
            "结论里每条判断都能指到某次工具返回的具体字段（service / metric / stats.latest）。",
            "若给出变更建议，必须包含：工具名、参数、副作用等级、是否需要审批。",
            "未验证的假设要显式标「未验证」，不许写成结论。",
        ),
        version=SKILL_VERSION,
        source="手工",
    )


def safe_rollback() -> SkillSpec:
    """安全回滚：前置检查 → 备份（取证） → 回滚 → 验证。"""
    return SkillSpec(
        name="safe_rollback",
        title="安全回滚（取证 → 评估不可逆代价 → 交人决定 → 验证）",
        description="回滚前先取证与评估不可逆代价，再交人工决定并验证",
        trigger=("回滚", "rollback", "回退"),
        tools=("list_deployments", "query_metrics", "query_logs", "get_config",
               "rollback_deployment"),
        steps=(
            "定坐标：list_deployments(service, limit=5)，写下 current 版本与目标版本"
            "（from → to）。目标版本必须来自真实历史，凭记忆写的版本号会被后端拒绝。",
            "取基线：query_metrics(service, \"p99\") 与 \"error_rate\"，把回滚前的数值"
            "抄下来 —— 回滚后的验收要跟这组数对比。",
            "现场取证：query_logs(services=[目标服务], level=\"ERROR\", window_minutes=60)，"
            "把关键错误行原文留下来。pod 换掉之后这些日志就没了。",
            "评估不可逆代价（**这一步决定要不要回滚**）：新版本是否跑过 schema 迁移、"
            "是否写过新字段、是否有异步任务在途。任何一项为「是」，回滚都会丢数据，"
            "必须由人明确接受这个代价。",
            "交人决定：rollback_deployment 是 IRREVERSIBLE / T3，Dispatcher 会直接"
            "拒绝（不是待确认）。正确做法是把方案、代价、验收标准交给人，由人走"
            "变更流程执行 —— 不要尝试绕过 Dispatcher 直连后端。",
            "验证：变更执行后重新查同一组指标（p99 / error_rate）与错误率，"
            "逐条对照「回滚前基线」写清是否达标。",
        ),
        pitfalls=(
            "回滚不是撤销。已执行的 schema 迁移与已写入的数据不会回来 —— 这是它被"
            "定成 T3（硬拒）的唯一理由，也是唯一不能被「审批一下」抵消的东西。",
            "没取证就回滚，等于把根因一起删掉：下次同样的劣化还得从头查。",
            "目标版本必须是 list_deployments 返回过的版本，别用「大概 v41 吧」。",
            "回滚会掩盖问题：回滚成功后仍要回答「为什么新版本会这样」，否则下次发布"
            "会再次触发。",
        ),
        verification=(
            "回滚前后同一指标（p99 / error_rate）都有具体数值，形成前后对照。",
            "不可逆代价有一句明确结论（有无 schema 迁移 / 有无数据丢失风险）。",
            "有「谁批准、批的是哪个 args_hash」的记录；没有就不算走完流程。",
        ),
        version=SKILL_VERSION,
        source="手工",
    )


def capacity_check() -> SkillSpec:
    """容量评估：查水位 → 算增速 → 给扩容建议。"""
    return SkillSpec(
        name="capacity_check",
        title="容量评估（水位 → 增速 → 扩容建议）",
        description="用水位与增速算余量，给出扩容/缩容建议与执行动作",
        trigger=("扩容", "容量", "水位", "缩容"),
        tools=("query_metrics", "get_config", "scale_service"),
        steps=(
            "量水位：query_metrics(service, \"cpu\", 30) 与 \"memory\"，取 stats.latest。"
            "口径：<0.6 宽松、0.75~0.85 偏高、>0.85 危险（OOM / 限流已经在路上）。",
            "算增速：用 cpu 的 stats.delta_ratio（窗口后半段 vs 前半段），换算成每小时"
            "涨幅，再算「还有几小时到 0.9」—— 这就是余量天数/小时数。",
            "算单副本承载：query_metrics(service, \"qps\") 的 latest 除以当前副本数，"
            "得到单副本 QPS。扩容后的副本数 = 峰值 QPS / 单副本安全承载。",
            "查上限：get_config(service) 看 pool_max、限流阈值、下游配额。若下游连接数"
            "已到顶，加副本只会把瓶颈推到数据库，此时应当先扩下游或改参数。",
            "给建议：保持 / 扩到 N 副本 / 先修热点。写明 N 的算法（峰值 QPS ÷ 单副本"
            "安全承载）与目标水位；执行动作是 scale_service（MUTATING / T2，需审批）。",
            "写复查点：明确「扩完后看哪个指标、多少分钟内复核」，避免扩完就没人管。",
        ),
        pitfalls=(
            "只看 CPU 不看内存：内存到 0.9 之后 OOM 往往比 CPU 打满更早发生。",
            "用平均值判断容量会漏峰；要看 latest / max，必要时看 p99。",
            "扩容前先确认下游能承接（数据库连接数、第三方配额），否则只是把瓶颈下移。",
            "水位高但单点热点（例如某个分片）时，加副本不会缓解 —— 要先修分片。",
            "scale_service 虽可回改，但它改的是生产副本数，仍属 T2，需要审批。",
        ),
        verification=(
            "建议的副本数能由「当前水位 + 增速 + 单副本承载」三个数一步步推出来。",
            "写明目标水位与复查时间点（谁在多久后看哪个指标）。",
            "若判定「扩容不解决问题」，必须给出根因（热点 / 下游配额 / 参数）。",
        ),
        version=SKILL_VERSION,
        source="手工",
    )


def default_skills() -> list[SkillSpec]:
    """三个技能的清单（演示 / 用例 / 注册表共用这一份）。"""
    return [investigate_latency(), safe_rollback(), capacity_check()]


def skill_names() -> list[str]:
    return [s.name for s in default_skills()]


def build_library() -> SkillLibrary:
    """造一个装了这三个技能的 `SkillLibrary`（配合 `SkillRuntime` 做双层装载）。"""
    library = SkillLibrary()
    for spec in default_skills():
        library.add(spec)
    return library
