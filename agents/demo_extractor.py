"""agents/demo_extractor.py —— 示例 Agent 的抽取策略。

放在 agents/ 而不是 plane/，是因为它包含**领域语义**（中文表达习惯、
运维业务词汇），属于 Agent 的能力。Runtime 只提供 Extractor SPI。

===============================================================================
这里有什么、为什么不该在 Runtime 里
===============================================================================
本模块承载的全是「内容判断」，因此**可替换**：

1. 低价值过滤：哪些句子是寒暄/确认（好的、收到、谢谢…），入库即 Memory Swamp；
2. 代词过滤：`它 / 这个 / 那个 / 上面 / 刚才` 无法就地消解时整句丢弃；
3. 子句切分：中文标点 `。！？；，` 断句；
4. 三条分类正则（PROFILE / SEMANTIC / EPISODIC）——org 原来的那三条，原样搬来；
5. 各类记忆的默认重要度 / 置信度 / TTL（算几分重要、能活多久）；
6. 工具回包的字段 → 人话映射（`pool_limit` → 连接池上限），只看明确事实；
7. `fact_key`：**什么算同一事实**（只有 Semantic 参与版本覆写）。

第 7 条最典型：英文客户说 `pool limit is 20` / `max pool = 30`，中文正则和
`等于|是|为` 骨架全部失效；而「同 key 覆写 + 旧版留痕」的机制与语言无关。

Runtime 侧只保留机械动作：抽取（委托）→ 去重 → 冲突覆写 → 落库。
"""
from __future__ import annotations

import re
from typing import Any

from contracts import MemoryKind, MemoryRecord, Subject
from plane.memory import fact_key, normalize_text

__all__ = ["RuleBasedExtractor"]


# ══════════════════════════════════════════════════════════════
# ① 分句 / 低价值 / 代词 —— 中文表达习惯（领域知识）
# ══════════════════════════════════════════════════════════════
_CLAUSE_SPLIT = re.compile(r"[。！？!?；;\n\r，,、]+")

#: 纯寒暄/确认。这类东西入库 = Memory Swamp 的第一现场
_FILLER = (
    "好的好的", "好的吧", "好的", "收到收到", "收到", "谢谢啦", "谢谢", "感谢",
    "okay", "ok", "嗯嗯", "嗯呢", "嗯", "哦哦", "哦", "了解了解", "了解", "明白",
    "知道了", "晓得", "没问题", "可以可以", "行吧", "好滴", "thanks", "thx",
    "hello", "hi", "你好", "在吗", "好",
)
_STOP_TOKENS = {
    "这个", "那个", "一下", "什么", "怎么", "就是", "然后", "现在", "可以",
    "我们", "你们", "他们", "这样", "那样", "一个", "没有", "不是", "还是",
    "已经", "应该", "需要", "时候", "东西", "问题", "地方",
}
_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]{1,}|[一-鿿]{2,}")

#: 代指词。无法消解时整句丢弃（宁缺毋滥，别把「它坏了」这种垃圾存成事实）
_ANAPHORA = ("它", "这个", "那个", "上面", "刚才")
#: 「刚才」只有在这些组合里才是代指，否则是时间副词
#: （裁决：单独作时间副词时不拦 ——「刚才重启后报错了」必须能进 Episodic，
#:   只有「刚才那个 / 刚才它 / 刚才的…」这种真代指才丢。否则两条规则互相吃掉。）
_DEMONSTRATIVE_TAIL = ("那个", "这个", "它", "他", "她", "的", "这事", "这件事", "那次")
#: 紧跟这些字说明后面是谓词/情态词，不是一个具体名词 → 代指无法消解
_NON_NOUN_HEAD = set(
    "应该需要可以把被在是有没不也就都还更将由让使改删加用弄搞修跑看查做写存取设换停"
    "开关算给拿放点选报坏挂崩重理想知觉得到维护支那这它他她们呀吧啊么什怎"
)


def _split_clauses(text: str) -> list[str]:
    return [c.strip() for c in _CLAUSE_SPLIT.split(text or "") if c and c.strip()]


def _has_substantive_token(text: str) -> bool:
    for tok in _TOKEN_RE.findall(text or ""):
        if tok in _STOP_TOKENS:
            continue
        return True
    return False


def _is_low_value(text: str) -> bool:
    """低价值过滤：寒暄、确认、以及「太短且无实质名词」的碎片。"""
    compact = normalize_text(text)
    if not compact:
        return True
    rest = compact
    for w in sorted(_FILLER, key=len, reverse=True):
        rest = rest.replace(w, "")
    if len(rest) < 2:
        return True
    if len(rest) < 6 and not _has_substantive_token(rest):
        return True
    return False


def _anaphora_unresolved(text: str) -> bool:
    """代指过滤：含代指词且无法就地消解 → 整句丢弃。"""
    verdict = False
    for word in _ANAPHORA:
        idx = text.find(word)
        if idx < 0:
            continue
        tail = text[idx + len(word):]
        if word == "上面":
            # 「上面」没有指代锚点，永远不可消解
            return True
        if word == "刚才":
            if not tail.startswith(_DEMONSTRATIVE_TAIL):
                continue          # 纯时间副词，交给 Episodic 正则处理
        head = tail.lstrip(" 　")
        if not head:
            return True
        ch = head[0]
        if re.match(r"[A-Za-z0-9]", ch):
            continue              # 紧跟英文/数字 → 视为具体名词
        if ch in _NON_NOUN_HEAD:
            return True
        verdict = verdict or False
    return verdict


# ══════════════════════════════════════════════════════════════
# ② 三条分类正则 —— org 原来的语义判断，原样搬来
# ══════════════════════════════════════════════════════════════
_PROFILE_RE = re.compile(
    r"我(?:更)?(?:喜欢|偏好|习惯)|不要|禁止|必须|一律|永远|绝不|只能|只用|"
    r"\balways\b|\bnever\b",
    re.IGNORECASE,
)
_SEMANTIC_RE = re.compile(
    r"等于|上限|默认|规则|阈值|限制|约定|(?<!因)为|(?<![还就都但可])是",
)
_EPISODIC_RE = re.compile(
    r"上次|昨天|前天|上周|刚才|重启后|修好了|修好|失败|报错|崩溃|挂了|超时|回滚|"
    r"踩坑|试了",
)


def _classify(text: str) -> MemoryKind | None:
    """分类优先级：PROFILE > SEMANTIC > EPISODIC > 丢弃。

    Profile 优先是硬的：一句里既有「必须」又有「默认」时，按刚性约束处理，
    宁可多活 180 天也不能被 TTL 淘汰。
    """
    if _PROFILE_RE.search(text):
        return MemoryKind.PROFILE
    if _SEMANTIC_RE.search(text):
        return MemoryKind.SEMANTIC
    if _EPISODIC_RE.search(text):
        return MemoryKind.EPISODIC
    return None


#: 各类记忆的默认置信度。重要度 / TTL 走构造参数（部署方可按业务调）。
_CONFIDENCE: dict[MemoryKind, float] = {
    MemoryKind.PROFILE: 0.90,
    MemoryKind.SEMANTIC: 0.60,
    MemoryKind.EPISODIC: 0.55,
}


# ══════════════════════════════════════════════════════════════
# ③ 工具回包抽取：只抽明确事实，绝不整包入库（运维词汇 → 人话）
# ══════════════════════════════════════════════════════════════
_SECRET_KEY_RE = re.compile(
    r"(password|passwd|secret|token|api[_-]?key|credential|auth|cookie)", re.IGNORECASE)
#: 字段名 → 人话。落在词表外的 key 原样保留（不做无根据的翻译）
_FIELD_NAMES = {
    "pool_limit": "连接池上限",
    "pool_size": "连接池大小",
    "max_connections": "最大连接数",
    "max_conn": "最大连接数",
    "timeout_ms": "超时毫秒",
    "timeout": "超时时间",
    "retry": "重试次数",
    "retry_count": "重试次数",
    "shard": "分片数",
    "shard_count": "分片数",
    "replicas": "副本数",
    "batch_size": "批大小",
    "version": "版本",
    "status": "状态",
    "region": "区域",
    "port": "端口",
}


def _scalar_to_text(value: Any) -> str:
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _flatten_payload(payload: Any, prefix: str = "") -> list[tuple[str, str]]:
    """把 payload 压成 (字段名, 值) 的原子事实列表。嵌套用 `.` 连接。"""
    facts: list[tuple[str, str]] = []
    if isinstance(payload, dict):
        for key, value in payload.items():
            key_s = str(key)
            if _SECRET_KEY_RE.search(key_s):
                continue                      # 密钥类字段不进 Memory（Gateway 的 PII 线）
            facts.extend(_flatten_payload(value, f"{prefix}.{key_s}" if prefix else key_s))
        return facts
    if isinstance(payload, (list, tuple)):
        scalars = [_scalar_to_text(v) for v in payload
                   if isinstance(v, (str, int, float, bool)) and v is not None]
        if scalars and prefix:
            facts.append((prefix, "、".join(scalars)[:120]))
        return facts
    if payload is None or prefix == "":
        return facts
    text = _scalar_to_text(payload).strip()
    if not text:
        return facts
    name = _FIELD_NAMES.get(prefix.rsplit(".", 1)[-1], prefix)
    facts.append((name, text[:120]))
    return facts


# ══════════════════════════════════════════════════════════════
# ④ Extractor 实现
# ══════════════════════════════════════════════════════════════
class RuleBasedExtractor:
    """把 org 里原来的三条中文正则原样搬过来，行为不变。

    只做三件事：切原子陈述 → 分三级 → 给每条打分（重要度/置信度/TTL/fact_key）。
    id 与 created_at 留空，由 MemoryRuntime 盖章（那是机械动作，不是语义判断）。

    注意 `fact_key` 只给 **Semantic** 写：Profile 是「永久约束」、Episodic 是
    「一次性事件」，都不参与「同一事实的新旧版本覆写」。这与重构前的
    `_persist()` 行为完全一致（旧实现里 `fk = fact_key(text) if kind is SEMANTIC else ""`）。
    """

    def __init__(self, *, profile_importance: float = 0.9,
                 semantic_importance: float = 0.6,
                 episodic_importance: float = 0.4,
                 semantic_ttl_days: float = 180.0,
                 episodic_ttl_days: float = 60.0) -> None:
        self._importance: dict[MemoryKind, float] = {
            MemoryKind.PROFILE: profile_importance,
            MemoryKind.SEMANTIC: semantic_importance,
            MemoryKind.EPISODIC: episodic_importance,
        }
        #: ttl_days=None 仅 Profile 合法（永久，免疫衰减）
        self._ttl: dict[MemoryKind, float | None] = {
            MemoryKind.PROFILE: None,
            MemoryKind.SEMANTIC: semantic_ttl_days,
            MemoryKind.EPISODIC: episodic_ttl_days,
        }

    # ── SPI ──────────────────────────────────────────────
    def extract(self, turn: dict, subject: Subject) -> list[MemoryRecord]:
        if not isinstance(turn, dict):
            raise TypeError(f"turn 必须是 dict，收到 {type(turn).__name__}")
        turn_id = str(turn.get("turn_id") or turn.get("id") or "turn")

        # (文本, source, kind_hint)  kind_hint 非空 = 形状已知，不再跑分类正则
        atoms: list[tuple[str, str, MemoryKind | None]] = []
        for role in ("user", "assistant"):
            raw = turn.get(role) or ""
            if not isinstance(raw, str):
                raw = str(raw)
            for clause in _split_clauses(raw):
                if _is_low_value(clause):
                    continue
                if _anaphora_unresolved(clause):
                    continue
                atoms.append((clause, f"{role}:{turn_id}", None))

        for entry in turn.get("tools") or []:
            if not isinstance(entry, dict):
                continue
            tool = str(entry.get("tool") or "tool")
            for name, value in _flatten_payload(entry.get("payload")):
                atoms.append((f"{name} {value}", f"tool:{tool}:{turn_id}",
                              MemoryKind.SEMANTIC))

        records: list[MemoryRecord] = []
        for text, source, hint in atoms:
            kind = hint or _classify(text)
            if kind is None:
                continue                       # 无法归类的碎片不入库
            importance = float(self._importance[kind])
            confidence = float(_CONFIDENCE[kind])
            if hint is None and source.startswith("assistant:"):
                # 助手自述是二手转述，置信度略低（不改变 kind 与 TTL）
                confidence = round(confidence * 0.9, 4)
                importance = round(importance * 0.95, 4)
            records.append(MemoryRecord(
                id="",
                kind=kind,
                text=text,
                subject=subject,
                importance=importance,
                confidence=confidence,
                created_at=0.0,                # Runtime 按自己的时钟盖章
                last_hit_at=0.0,
                hits=0,
                version=1,
                superseded_by=None,
                ttl_days=self._ttl[kind],
                source=source,
                fact_key=fact_key(text) if kind is MemoryKind.SEMANTIC else "",
            ))
        return records
