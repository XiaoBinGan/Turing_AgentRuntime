"""agents/extractor.py —— 运维域的记忆抽取器（Agent 层，纯业务内容）。

================================================================================
它负责的那一半
================================================================================
`plane/memory.py` 只做机械动作：抽取（委托）→ 去重 → 冲突覆写 → 落库。
它把「这句话是什么意思」整包委托给本模块，因为 **Runtime 不该知道**
「连接池耗尽」比「今天天气不错」更值得记：

    问题                              谁回答
    ────────────────────────────────  ──────────────────────────────
    这句属于 PROFILE/SEMANTIC/EPISODIC 本模块（运维域的表达习惯）
    算几分重要、理由是什么              本模块（`explain()` 必须说得出来）
    两条记录说的是不是同一件事          本模块（`fact_key` 的口径）
    「好的 / 收到 / 嗯」要不要入库       本模块（Memory Swamp 的第一现场）
    什么时候淘汰、怎么覆写、怎么隔离      Runtime（与本领域无关）

换一个领域（法务 / 客服 / 代码评审），换的是本文件里的词典与规则，
`plane/memory.py` 一行不动。

================================================================================
为什么不是「一条大正则」
================================================================================
`agents/demo_extractor.py`（保留为对比基线）只有三条正则，能跑通但会漏：

    「灰度发布先过回归」   → 三条正则全不命中 → 整句被丢
    「日志盘占用 92%」     → 没有「是/上限/默认」这类判断词 → 整句被丢
    「p99 3120ms」        → 纯量纲事实 → 整句被丢

漏的原因不是正则写得不好，而是**把语义压进了一个模式串**：一旦句子的语法外壳
变了（「先过回归」没有判断词、「92%」是量纲不是判断），单条正则就无从下手。
所以这里按「**一句话在讲什么**」拆成八条互相独立、各自可读的规则，每条规则只
回答一个问题，注释写明它在抓什么、为什么这么抓：

    ① 分句（含长句保护）      ② 寒暄/确认过滤        ③ 代指过滤
    ④ 时间锚点 → 事件         ⑤ 偏好/规范            ⑥ 事实（可量化/配置类）
    ⑦ 重要度打分（带理由）     ⑧ 事实键（同一事实的口径）
    ⑨ 工具回包 → 指标读数

任何一条改坏了，只影响它负责的那一类句子，其余七条不受牵连——这是拆开的收益。
"""
from __future__ import annotations

import re
from typing import Any, Iterable

from contracts import RETENTION_MATRIX, MemoryKind, MemoryRecord, Subject
from plane.memory import normalize_text

__all__ = ["OpsExtractor", "MAX_CLAUSE_CHARS"]


# ══════════════════════════════════════════════════════════════
# ① 分句：中文标点断句 + 长句保护
# ══════════════════════════════════════════════════════════════
#: 断句符：句末标点 + 逗号顿号 + 换行。
#: 逗号也切，是因为中文一句话里常并置两条独立陈述（「灰度发布先过回归，p99 3120ms
#: 才算达标」），不切会让后半句的量纲事实被前半句的规范语气吞掉。
_CLAUSE_SPLIT = re.compile(r"[。！？!?；;\n\r，,、]+")

#: 切句的**目标**粒度（软上限）。Runtime 落库时会截到 240 字，取 200 留余量。
#: 注意它**不是硬约束**：没有任何标点的超长串不按字符硬切——硬切会造出
#: 「连接池上限要」这种半句，比长句本身更坏。这类串原样入库，交给 Runtime 截断。
MAX_CLAUSE_CHARS = 200


def _split_clauses(text: str) -> list[str]:
    """把一段话切成「原子陈述」。切点永远落在标点上。

    「不被截断成垃圾」的具体做法：只在标点处切；没有标点的超长串**保留整句**
    （自己按字符硬切反而会造出「连接池上限要」这种半句，那是更坏的垃圾）。
    """
    return [c.strip() for c in _CLAUSE_SPLIT.split(text or "") if c and c.strip()]


# ══════════════════════════════════════════════════════════════
# ② 寒暄 / 确认过滤：入库即 Memory Swamp
# ══════════════════════════════════════════════════════════════
#: 纯社交应答。它们有价值，但价值在对话流里，不在长期记忆里（下周再想起「收到」毫无用处）。
_FILLER_WORDS: tuple[str, ...] = (
    "好的好的", "好的吧", "好的", "好滴", "好嘞", "收到收到", "收到", "谢谢啦", "谢谢你",
    "谢谢", "感谢", "麻烦了", "辛苦了", "嗯嗯", "嗯呢", "嗯", "哦哦", "哦", "了解了解",
    "了解", "明白", "明白了", "知道了", "晓得", "没问题", "可以可以", "行吧", "行",
    "在吗", "在么", "你好呀", "你好", "您好", "早", "早上好", "okay", "ok", "k",
    "thanks", "thank you", "thx", "hello", "hi", "hey", "got it", "sure", "cool",
)

#: 有这些字符就说明句子里还有实质内容（数字/英文/汉字实词），不能因短而丢
_MEANINGFUL_RE = re.compile(r"[0-9A-Za-z\u4e00-\u9fff]")


def _is_low_value(clause: str) -> bool:
    """寒暄与低信息量碎片的判定：**削掉社交词后还剩什么**。

    只剩 1~2 个字符（「好」「嗯」「收到」）→ 丢弃；
    削完还有 3 个以上字符（「好的，连接池扩到 40」）→ 留下，正文才是要抽的东西。
    """
    compact = normalize_text(clause)
    if not compact:
        return True
    rest = compact
    for word in sorted(_FILLER_WORDS, key=len, reverse=True):
        rest = rest.replace(normalize_text(word), "")
    if not rest:
        return True
    if len(rest) <= 2 and not re.search(r"\d", rest):
        return True          # 「好」「嗯」「ok啦」这类剩余，不值得占一条记忆
    return not _MEANINGFUL_RE.search(rest)


# ══════════════════════════════════════════════════════════════
# ③ 代指过滤：无法就地消解就整句丢
# ══════════════════════════════════════════════════════════════
#: 句首代指词。长期记忆没有「上文」可回指，存下来只会变成「它又挂了」这种垃圾。
_ANAPHORA_HEADS: tuple[str, ...] = ("它的", "它又", "它", "这个", "那个", "这些", "那些",
                                    "上面", "刚才那个", "刚才那", "该服务", "该实例")
#: 短句阈值：句首是代指且整句很短 → 无从消解，宁可丢。长句里代指往往有同位语，
#: 保留正文比丢整句划算（例如「那个连接池的 max 是 40」仍能读出事实）。
_ANAPHORA_MAX_CHARS = 12


def _anaphora_unresolved(clause: str) -> bool:
    head = clause.lstrip(" \t，,、")
    if len(clause) > _ANAPHORA_MAX_CHARS:
        return False
    return any(head.startswith(word) for word in _ANAPHORA_HEADS)


# ══════════════════════════════════════════════════════════════
# ④⑤⑥ 三类内容判断：每条规则只回答一个问题
# ══════════════════════════════════════════════════════════════
def _hit(text: str, words: Iterable[str]) -> bool:
    """词典命中（大小写不敏感；中文不受影响）。词典是「可读的」，不是正则迷宫。"""
    lowered = text.lower()
    return any(word.lower() in lowered for word in words)


#: 时间锚点：把句子钉在「某一次发生过的事」上。这是 Episodic 最强的信号，
#: 因为它回答的是「什么时候」，而长期事实/规范不依赖时间。
_TIME_ANCHORS: tuple[str, ...] = (
    "上次", "上回", "上一次", "昨天", "前天", "上周", "上上周", "上个月", "去年",
    "刚才", "刚刚", "今早", "今天凌晨", "凌晨", "本周一", "周一凌晨", "前几天", "那次",
    "yesterday", "last week", "last night", "last time", "this morning", "earlier today",
)

#: 事件动词：没有时间锚点时，它们是「发生过/发生过故障」的次强信号。
_EVENT_VERBS: tuple[str, ...] = (
    "失败", "报错", "崩溃", "挂了", "宕机", "超时", "回滚", "恢复", "重启", "误删",
    "抖动", "雪崩", "打满", "耗尽", "没跑", "没有跑", "漏了", "触发", "告警", "炸",
    "事故", "演练", "修好", "修复", "定位到", "踩坑", "扩容完成", "上线完成",
    "crashed", "failed", "rolled back", "rollback", "restarted", "outage", "incident",
)

#: 偏好（个人习惯）：说的是「我一直怎么做」，不是「这次发生了什么」。
_PROFILE_PERSONAL: tuple[str, ...] = (
    "我习惯", "我通常", "我一般", "我偏好", "我更喜欢", "我喜欢", "我一向", "我们习惯",
    "我们通常", "我的习惯", "我的原则", "照老规矩", "老规矩", "按惯例", "沿用",
    "i prefer", "i usually", "i always", "we usually", "my rule", "by convention",
)

#: 偏好（硬性约束）：违反代价最高的那一类，必须永久保留、永不衰减。
_PROFILE_MODAL: tuple[str, ...] = (
    "必须", "一律", "禁止", "严禁", "不要", "绝不", "永远不", "不允许", "不许",
    "只能用", "只能", "只用", "务必", "不得", "must", "must not", "never",
    "do not", "don't", "forbidden", "shall not", "always use",
)

#: 变更动作：发布/灰度/回滚/扩容…… 运维域里**「某类变更的固定顺序」就是规范**。
_CHANGE_ACTIONS: tuple[str, ...] = (
    "发布", "发版", "上线", "灰度", "回滚", "扩容", "缩容", "变更", "重启", "部署",
    "迁移", "切流", "割接", "演练", "review", "release", "deploy", "rollout",
    "migration", "failover",
)

#: 顺序词：和变更动作同时出现时，句子在讲「流程该按什么顺序走」＝ 规范。
_ORDER_WORDS: tuple[str, ...] = (
    "先", "再", "然后", "之后", "分批", "依次", "按顺序", "第一步", "第二步",
    "before", "then", "in batches", "step 1", "phase",
)

#: 配置/度量词：这些词出现说明句子在描述系统的**稳定属性**（阈值、容量、默认值）。
_FACT_WORDS: tuple[str, ...] = (
    "上限", "下限", "阈值", "默认", "配置", "约定", "规则", "容量", "配额", "水位",
    "端口", "版本", "占用", "耗时", "延迟", "并发", "副本", "分片", "超时", "重试",
    "比例", "占比", "错误率", "使用率", "命中率", "内存", "磁盘", "带宽", "核数",
    "连接数", "保留期", "限流", "qps", "tps", "p99", "p95", "p90",
    "limit", "threshold", "default", "quota", "capacity", "replica", "timeout",
    "retry", "max", "min", "size", "count", "version",
)

#: 量纲：数字 + 单位 = 可核对的量（92%、3120ms、20 台、7 天）。
_UNIT_RE = re.compile(
    r"\d+(?:\.\d+)?\s*(?:%|‰|毫秒|ms|秒|分钟|小时|天|周|个月|GB|MB|KB|TB|G|M|T|核|"
    r"条|个|台|次|万|亿|qps|tps)",
    re.IGNORECASE,
)
_NUM_RE = re.compile(r"\d")
#: 判断词：「X 是/为/等于 Y」——中文事实句最常见的骨架。
_JUDGE_RE = re.compile(r"(?:等于|是|为|＝|=)")

#: 影响面词：涉及生产与核心链路的记忆，值得更高的重要度。
_BLAST_RADIUS: tuple[str, ...] = (
    "生产", "线上", "主库", "主从", "核心链路", "资金", "支付", "订单", "全量",
    "所有用户", "prod", "production", "primary", "critical",
)


def _is_hard_constraint(text: str) -> bool:
    """硬性约束（禁止 / 必须 / 一律 / 绝不 …）。它优先于其它一切判断。"""
    return _hit(text, _PROFILE_MODAL)


def _is_profile(text: str) -> bool:
    """偏好与规范：① 个人习惯 ② 硬性约束 ③ 「变更动作 + 顺序词」的流程规范。

    第 ③ 条是本文件与 demo 的关键差距之一：「灰度发布先过回归」既没有「我习惯」
    也没有「必须」，但它显然是一条规范——它讲的是**流程顺序**。demo 的三条正则
    里没有任何一条能看见「顺序」这个语义，于是整句被丢。
    """
    if _hit(text, _PROFILE_PERSONAL) or _hit(text, _PROFILE_MODAL):
        return True
    return _hit(text, _CHANGE_ACTIONS) and _hit(text, _ORDER_WORDS)


def _is_fact(text: str) -> bool:
    """稳定事实：① 有量纲 ② 有数字且有配置/度量词 ③ 判断词 + 配置/度量词。

    第 ① 条覆盖 demo 漏掉的纯量纲句：「日志盘占用 92%」「p99 3120ms」——
    它们没有「是/上限」，但「92%」本身就是可核对的事实。
    """
    if _UNIT_RE.search(text):
        return True
    has_number = bool(_NUM_RE.search(text))
    has_config_word = _hit(text, _FACT_WORDS)
    if has_number and has_config_word:
        return True
    return has_config_word and bool(_JUDGE_RE.search(text))


def _is_event(text: str) -> bool:
    """事件：有时间锚点，或描述了「发生过的事」（报错/回滚/宕机…）。"""
    return _hit(text, _TIME_ANCHORS) or _hit(text, _EVENT_VERBS)


def _has_time_anchor(text: str) -> bool:
    return _hit(text, _TIME_ANCHORS)


def classify(clause: str) -> MemoryKind | None:
    """分类裁决（顺序本身就是业务判断，值得写清楚）：

    1. **带时间锚点且不是硬约束 → 事件**。时间锚点把句子钉在「某一次」上，
       哪怕里面含阈值也只是一次观测（「上周 p99 到了 3120ms」是事件，不是配置）。
       硬约束例外：「上周五禁止发布」讲的是纪律，不是事件经过。
    2. **偏好/规范 → PROFILE**。刚性约束优先于普通事实：宁可多活 180 天，
       也不能因为一句「必须」「禁止」被 TTL 淘汰（与 demo 的裁决一致）。
    3. **可量化 / 配置类 → SEMANTIC**。
    4. **发生过的事 → EPISODIC**。
    5. 其余（无法归类的碎片）→ None，不入库。
    """
    if _has_time_anchor(clause) and not _is_hard_constraint(clause):
        return MemoryKind.EPISODIC
    if _is_profile(clause):
        return MemoryKind.PROFILE
    if _is_fact(clause):
        return MemoryKind.SEMANTIC
    if _is_event(clause):
        return MemoryKind.EPISODIC
    return None


# ══════════════════════════════════════════════════════════════
# ⑦ 重要度打分：每个加减分都要说得出理由
# ══════════════════════════════════════════════════════════════
#: 基线。数值刻意与 `contracts.RETENTION_MATRIX` 的阈值对齐且**可解释**：
#:   * PROFILE 0.86 > Semantic 归档阈值 0.70 —— 偏好本来就该免疫衰减；
#:   * SEMANTIC 0.60 + 量纲 +0.10 = 0.70 —— 有量纲的事实刚好够到「不归档」线；
#:   * EPISODIC 0.40 < 删除阈值 0.50 —— 普通事件 60 天后该走；
#:     带故障词 +0.08 = 0.48 仍在线下，只有「影响面 + 有量纲」的事故复盘才够线，
#:     这是故意的：事故复盘的价值应当由人（或 Skill 库）显式沉淀，不是靠自动加分。
_BASE_IMPORTANCE: dict[MemoryKind, float] = {
    MemoryKind.PROFILE: 0.86,
    MemoryKind.SEMANTIC: 0.60,
    MemoryKind.EPISODIC: 0.40,
}

#: 各条加分规则的说明文案（同时是 `explain()` 的输出）
_BOOST_NUMERIC = 0.10
_BOOST_HARD = 0.06
_BOOST_BLAST = 0.08
_BOOST_DENSE = 0.03
_DENSE_CHARS = 24


def score_clause(clause: str, kind: MemoryKind) -> tuple[float, tuple[str, ...]]:
    """返回 (importance, 理由列表)。理由不是装饰：线上调规则时靠它解释「为什么 0.78」。"""
    score = _BASE_IMPORTANCE[kind]
    reasons: list[str] = [f"基线 {score:.2f}（{kind.value}）"]
    if _NUM_RE.search(clause):
        score += _BOOST_NUMERIC
        reasons.append(f"含量纲/数值（可核对、可回归验证）+{_BOOST_NUMERIC:.2f}")
    if _is_hard_constraint(clause):
        score += _BOOST_HARD
        reasons.append(f"硬性约束（禁止/必须/一律）：违反代价高 +{_BOOST_HARD:.2f}")
    if _hit(clause, _BLAST_RADIUS):
        score += _BOOST_BLAST
        reasons.append(f"涉及生产/核心链路：影响面大 +{_BOOST_BLAST:.2f}")
    if len(clause) >= _DENSE_CHARS:
        score += _BOOST_DENSE
        reasons.append(f"信息密度高（>={_DENSE_CHARS} 字）+{_BOOST_DENSE:.2f}")
    return round(min(0.99, max(0.05, score)), 4), tuple(reasons)


# ══════════════════════════════════════════════════════════════
# ⑧ fact_key：「同一事实」的口径
# ══════════════════════════════════════════════════════════════
#: 判断词与语气词：不进事实骨架（「上限是 20」和「上限 20」是同一件事）
_JUDGE_WORDS: tuple[str, ...] = ("等于", "是", "为", "大约", "大概", "约", "左右")
#: 紧跟数字的量词/单位：不进骨架（「20 个」和「20」是同一个取值）
_UNIT_AFTER_NUM = re.compile(
    r"(?<=\d)(?:毫秒|分钟|小时|个月|gb|mb|kb|tb|ms|qps|tps|ms|s|h|d|g|m|t|k|"
    r"个|条|台|次|位|核|块|只|秒|分|天|周)",
    re.IGNORECASE,
)
_DIGITS = re.compile(r"\d+(?:\.\d+)?")
#: 连续的数字占位符合并成一个（「20 到 30」不该产生 `##` 这种噪声键）
_HASH_RUN = re.compile(r"#+")
#: 骨架最短长度：太短的键（`p#`、`#`）会把不相干的事实并到一起，宁可不合并
_MIN_SKELETON_CHARS = 3


def skeleton(text: str) -> str:
    """事实骨架：去标点、去判断词、去量词、把数值抹成占位符。

    数值被抹成 ``#`` 是**故意**的：
        连接池上限是 20  →  连接池上限#
        连接池上限是 30  →  连接池上限#
    同一个配置的新旧取值 = **同一条事实的新版本**，由 Runtime 做版本覆写
    （旧记录留痕、recall 只取最新）。若把数值留在键里，20 和 30 会变成两条
    互相矛盾的「事实」长期共存——这正是冲突消解要避免的。

    两个已知边界（诚实记录，不假装没有）：
      * 英文判断词不做剥离：归一化去空格后 ``is`` 会命中 ``disk`` / ``this``
        的词内，剥离的代价大于收益；
      * 只有指标名没有标签词的句子（``p99 3120ms`` → ``p#``）骨架太短，返回空串，
        Runtime 退化用文本哈希 —— 同一句重复仍然幂等，只是不做版本覆写。
    """
    flat = normalize_text(text)
    for word in _JUDGE_WORDS:
        flat = flat.replace(word, "")
    flat = _UNIT_AFTER_NUM.sub("", flat)
    flat = _DIGITS.sub("#", flat)
    flat = _HASH_RUN.sub("#", flat)
    flat = flat[:_MIN_SKELETON_CHARS * 24]
    return flat if len(flat) >= _MIN_SKELETON_CHARS else ""


def fact_key_for(kind: MemoryKind, text: str) -> str:
    """按类别给出事实键。

    * **SEMANTIC**：参与版本覆写，需要键（这是键存在的主要理由）；
    * **PROFILE**：同一条规范换种说法不该存两遍，也给键（kind 前缀保证跨类不撞）；
    * **EPISODIC**：一次性事件没有「同一事实」，给空串 —— Runtime 退化为
      「文本完全相同才算重复」，只做幂等，不做覆写。事件被「更新的版本覆盖」
      在语义上是错的。
    """
    if kind is MemoryKind.EPISODIC:
        return ""
    core = skeleton(text)
    return f"{kind.value}:{core}" if core else ""


# ══════════════════════════════════════════════════════════════
# ⑨ 工具回包 → 指标读数（只抽明确字段，绝不整包入库）
# ══════════════════════════════════════════════════════════════
#: 密钥类字段一律不进 Memory（Gateway 的 PII / 凭据线在更前面，这里是第二道）
_SECRET_KEY_RE = re.compile(
    r"(password|passwd|secret|token|api[_-]?key|credential|auth|cookie|session[_-]?id)",
    re.IGNORECASE,
)

#: 度量字段 → 运维人话。白名单外的字段不编造翻译，原样保留字段名。
_METRIC_LABELS: dict[str, str] = {
    "p99_ms": "p99 延迟(ms)",
    "p95_ms": "p95 延迟(ms)",
    "p50_ms": "p50 延迟(ms)",
    "latency_ms": "请求延迟(ms)",
    "error_rate": "错误率",
    "error_count": "错误数",
    "errors": "错误数",
    "cpu_usage": "CPU 使用率",
    "cpu_util": "CPU 使用率",
    "mem_usage": "内存使用率",
    "memory_usage": "内存使用率",
    "disk_used_percent": "磁盘使用率",
    "disk_usage": "磁盘使用率",
    "disk_used_gb": "磁盘已用(GB)",
    "pool_limit": "连接池上限",
    "pool_max": "连接池上限",
    "pool_used": "连接池已用",
    "qps": "QPS",
    "tps": "TPS",
    "replicas": "副本数",
    "shard_count": "分片数",
    "restarts": "重启次数",
    "alert_count": "告警条数",
    "timeout_ms": "超时毫秒",
    "retry_count": "重试次数",
    "status": "状态",
    "version": "版本",
    "region": "区域",
    "zone": "可用区",
    "instance": "实例",
    "host": "主机",
}

#: 白名单之外，长得像度量的字段也收（`node_disk_free_gb` 这类自定义名很常见）
_METRIC_SUFFIX_RE = re.compile(
    r"(?:_ms|_pct|_percent|_rate|_count|_bytes|_gb|_mb|_sec|_qps|_tps|"
    r"_used|_usage|_util|_limit|_max|_min|_total|_free)$",
    re.IGNORECASE,
)


def _is_metric_key(key: str) -> bool:
    """白名单按**末段**匹配：`meta.region` 与 `region` 是同一个度量。"""
    tail = key.rsplit(".", 1)[-1].lower()
    return tail in _METRIC_LABELS or bool(_METRIC_SUFFIX_RE.search(tail))


def _label_for(field: str) -> str:
    """字段名 → 人话。白名单命中就翻译，否则原样保留（不凭空造词）。"""
    tail = field.rsplit(".", 1)[-1]
    return _METRIC_LABELS.get(tail.lower(), field)


def _scalar_text(value: Any) -> str:
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def _flatten_metrics(payload: Any, prefix: str = "") -> list[tuple[str, str]]:
    """把回包压成 (字段路径, 值) 的原子读数列表；嵌套用 `.` 连接。"""
    facts: list[tuple[str, str]] = []
    if isinstance(payload, dict):
        for raw_key, value in payload.items():
            key = str(raw_key)
            if _SECRET_KEY_RE.search(key):
                continue                                   # 凭据永远不进记忆
            facts.extend(_flatten_metrics(value, f"{prefix}.{key}" if prefix else key))
        return facts
    if isinstance(payload, (list, tuple)):
        scalars = [_scalar_text(v) for v in payload
                   if isinstance(v, (str, int, float, bool)) and v is not None]
        if scalars and prefix:
            facts.append((prefix, "、".join(scalars)[:80]))
        return facts
    if payload is None or not prefix:
        return facts
    if not _is_metric_key(prefix):
        return facts                                        # 不认识的字段不进记忆
    text = _scalar_text(payload)
    if not text:
        return facts
    return [(prefix, text[:60])]


# ══════════════════════════════════════════════════════════════
# Extractor 实现（contracts.Extractor）
# ══════════════════════════════════════════════════════════════
#: 各类记忆的默认置信度。事件是「有人这么说过」的一次性陈述，置信度最低；
#: 结构化回包最高（机器读出来的，不是转述）。
_CONFIDENCE: dict[MemoryKind, float] = {
    MemoryKind.PROFILE: 0.88,
    MemoryKind.SEMANTIC: 0.66,
    MemoryKind.EPISODIC: 0.56,
}
_TOOL_CONFIDENCE = 0.75


def _ttl_from_matrix(kind: MemoryKind) -> float | None:
    """默认 TTL 直接读 `contracts.RETENTION_MATRIX`，避免与 Runtime 的口径漂移。

    保留矩阵是 Runtime 的（几天淘汰、归档还是删除），本模块只负责「按类别取它」。
    """
    for rule in RETENTION_MATRIX:
        if rule.kind == kind:
            return rule.ttl_days
    return None


class OpsExtractor:
    """运维域的记忆抽取器。实现 `contracts.Extractor`。

    **这是业务判断**：Runtime 不知道「连接池耗尽」比「今天天气不错」更值得记。
    本类的全部知识都是运维域的：哪些词代表硬约束、哪些是量纲、指标字段叫什么、
    「灰度发布先过回归」其实是一条流程规范。

    Args:
        semantic_ttl_days / episodic_ttl_days: 覆盖默认 TTL（默认取保留矩阵）。
            生产上想「事件留 90 天」就传 90，不必改 Runtime。

    用法::

        MemoryRuntime(extractor=OpsExtractor())
    """

    def __init__(self, *, semantic_ttl_days: float | None = None,
                 episodic_ttl_days: float | None = None) -> None:
        self._ttl: dict[MemoryKind, float | None] = {
            MemoryKind.PROFILE: None,          # 永久：仅主体可显式删除
            MemoryKind.SEMANTIC: (semantic_ttl_days
                                  if semantic_ttl_days is not None
                                  else _ttl_from_matrix(MemoryKind.SEMANTIC)),
            MemoryKind.EPISODIC: (episodic_ttl_days
                                  if episodic_ttl_days is not None
                                  else _ttl_from_matrix(MemoryKind.EPISODIC)),
        }

    # ── 调试口：为什么这句话被抽 / 被打这个分 ──────────────────
    def explain(self, clause: str) -> dict:
        """单句诊断：给线上调规则用（返回 None 类 = 被丢弃）。"""
        if _is_low_value(clause):
            return {"clause": clause, "kind": None, "dropped": "寒暄或低信息量"}
        if _anaphora_unresolved(clause):
            return {"clause": clause, "kind": None, "dropped": "代指无法就地消解"}
        kind = classify(clause)
        if kind is None:
            return {"clause": clause, "kind": None, "dropped": "无法归类（非偏好/事实/事件）"}
        importance, reasons = score_clause(clause, kind)
        return {"clause": clause, "kind": kind.value, "importance": importance,
                "reasons": list(reasons), "fact_key": fact_key_for(kind, clause)}

    # ── SPI ──────────────────────────────────────────────
    def extract(self, turn: dict, subject: Subject) -> list[MemoryRecord]:
        """一轮对话 → 若干条记忆。

        只产出「形状」字段（kind / text / importance / fact_key …），
        `id` 与 `created_at` 留空，由 `MemoryRuntime` 盖章（那是机械动作）。
        """
        if not isinstance(turn, dict):
            raise TypeError(f"turn 必须是 dict，收到 {type(turn).__name__}")
        turn_id = str(turn.get("turn_id") or turn.get("id") or "turn")

        records: list[MemoryRecord] = []
        #: 同一轮内去重：同一句话说了两遍不该产生两条记忆。
        #: 键优先用 fact_key（语义去重），没有键就退回归一化文本（幂等）。
        seen: dict[tuple[str, str], int] = {}

        for role in ("user", "assistant"):
            raw = turn.get(role) or ""
            if not isinstance(raw, str):
                raw = str(raw)
            for clause in _split_clauses(raw):
                if _is_low_value(clause) or _anaphora_unresolved(clause):
                    continue
                kind = classify(clause)
                if kind is None:
                    continue            # 无法归类的碎片不入库
                record = self._make_record(kind, clause, f"{role}:{turn_id}", subject)
                if role == "assistant":
                    # 助手自述是二手转述：置信度与重要度都打折，但类别不变
                    record.confidence = round(record.confidence * 0.9, 4)
                    record.importance = round(record.importance * 0.95, 4)
                self._emit(records, seen, record)

        for entry in turn.get("tools") or []:
            if not isinstance(entry, dict):
                continue
            tool = str(entry.get("tool") or entry.get("name") or "tool")
            payload = entry.get("payload", entry.get("result"))
            for field, value in _flatten_metrics(payload):
                label = _label_for(field)
                text = f"{label} {value}"
                importance, _ = score_clause(text, MemoryKind.SEMANTIC)
                region = ".".join(field.split(".")[:-1])
                record = MemoryRecord(
                    id="",
                    kind=MemoryKind.SEMANTIC,
                    text=text,
                    subject=subject,
                    importance=importance,
                    confidence=_TOOL_CONFIDENCE,
                    created_at=0.0,
                    last_hit_at=0.0,
                    hits=0,
                    version=1,
                    superseded_by=None,
                    ttl_days=self._ttl[MemoryKind.SEMANTIC],
                    source=f"tool:{tool}:{turn_id}",
                    # 工具回包是**读数不是事件**：同一指标的新读数应当覆写旧读数
                    # （version 递增、旧值留痕），而不是堆成 N 条互相矛盾的记忆。
                    # 需要读数历史请走时序库，不是 Memory。
                    fact_key=f"semantic:metric:{region + '.' if region else ''}{label}",
                )
                self._emit(records, seen, record)
        return records

    # ── 内部 ─────────────────────────────────────────────
    def _make_record(self, kind: MemoryKind, text: str, source: str,
                     subject: Subject) -> MemoryRecord:
        importance, _reasons = score_clause(text, kind)
        return MemoryRecord(
            id="",
            kind=kind,
            text=text,
            subject=subject,            # 主体透传：INV-12 由 Runtime 复核
            importance=importance,
            confidence=_CONFIDENCE[kind],
            created_at=0.0,
            last_hit_at=0.0,
            hits=0,
            version=1,
            superseded_by=None,
            ttl_days=self._ttl[kind],
            source=source,
            fact_key=fact_key_for(kind, text),
        )

    @staticmethod
    def _emit(records: list[MemoryRecord], seen: dict[tuple[str, str], int],
              record: MemoryRecord) -> bool:
        """轮内去重 + 主体透传。返回 True 表示「已合并进既有记录」。"""
        key = (record.kind.value, record.fact_key or normalize_text(record.text))
        index = seen.get(key)
        if index is None:
            seen[key] = len(records)
            records.append(record)
            return False
        existing = records[index]
        # 同一事实重复出现：保留信息量更大的那条（重要度取高，文本取长）
        if record.importance > existing.importance:
            existing.importance = record.importance
        if len(record.text) > len(existing.text):
            existing.text = record.text
        existing.confidence = max(existing.confidence, record.confidence)
        return True
