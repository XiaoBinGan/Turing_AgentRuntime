"""plane/memory.py — Memory Plane：**Memory 不是存储，是有状态的 Runtime。**

================================================================================
边界：Runtime 判断「形状」，Agent 判断「内容」
================================================================================
本模块只提供**与领域无关**的机械动作：

    Runtime 知道                                     Agent（Extractor）知道
    ─────────────────────────────────────────        ────────────────────────────────
    记忆有 3 类（Profile/Semantic/Episodic）          这句话属于哪一类
    管道有 5 段（write→recall→reflect→consolidate→sweep）
    保留矩阵怎么淘汰、什么时候归档 / 删              算几分重要、活多久（ttl_days）
    同一 fact_key 的新记录怎么覆写旧记录              什么算「同一事实」（fact_key）
    多租户隔离 / 预算裁剪 / 命中续命                  「好的/收到」该不该丢、代指能不能消解

所以这里**没有任何分类正则**。`plane/memory.py` 里跑中文正则可以跑通，
换成英文客户 / 法务 Agent / 代码 Agent 就全部失效 —— 那不是管道的问题，
是领域知识放错了层。领域策略见 `agents/demo_extractor.py`（示例 Agent），
契约见 `contracts.Extractor`，本模块只认那个 Protocol。

没有 Agent 注入策略时，`PassthroughExtractor` 兜底：把一轮对话当一条 Episodic，
不做任何语义判断。它唯一的作用是让 Runtime 能独立工作，**而不是替 Agent 做判断**。

================================================================================
核心立场（管道本身的立场，与领域无关）
================================================================================
存储只是载体。把记忆做成一张表 + 一个 `search()`，得到的只是「更慢的日志」。
真正的价值在 **代谢链**：

    write(抽取) → recall(召回重排) → reflect(反思) → consolidate(巩固) → sweep(遗忘)
         ↑                                                                      │
         └──────────────── 每条被召回的记录续命（hits/last_hit_at）──────────────┘

这条链是**有状态**的：同一条记忆在不同时间点的分数不同，被召回的次数会改变它的
寿命，反思会把离散事件压成规则，遗忘会按确定性矩阵把它送进冷存或真删。
换句话说，Memory 的价值不在「存了多少」，而在「**下一步该想起什么、该忘掉什么**」。

架构依据（agent-runtime-core-arch.md）：
- §3 七层记忆归位裁决：Working / Conversation / Summary 归 Context；
  Episodic / Semantic / Profile 归 Memory Plane；
  **Procedural 归 Skill 库**——所以本模块的 `consolidate()` 只产出 *候选*，不写 Skill 库。
- §4.2 五阶段代谢管道 + 确定性保留矩阵 + 五维评估。

================================================================================
设计约束
================================================================================
1. 只依赖 `contracts.py`（Extractor / MemoryKind / Subject / MemoryRecord / Chunk /
   RetentionRule / RETENTION_MATRIX / MemoryMetrics / now_ms），不 import core/*。
   换 Provider（Mem0 / Qdrant / Hermes）不改一行 Runtime 逻辑。
2. **确定性地遗忘**（INV-13）：`sweep()` 的每一次淘汰都必须能指回
   `RETENTION_MATRIX` 里的某一条 `rule.reason`，返回值里带出处。
   不用概率衰减公式——概率衰减在合规审计面前无法解释「为什么删了这条」。
3. **INV-12 无主不入库**：`write()` 缺 subject（或 tenant 为空）直接 `ValueError`。
4. **多租户强隔离**：跨 tenant 永久不可见；同 tenant 内 user 精确匹配，
   或 team+project 同层共享。这层做了两道（Provider + Runtime），纵深防御。
5. **召回是只读语义，评估必须纯净**：`metrics()` 内部召回 `activate=False`，
   不污染 hits，否则「评估」本身会改变记忆寿命，指标失去复现性。
6. **可替换的只有「判断」，不是「机制」**：换 Extractor 只改内容口径，
   `fact_key` 覆写规则、重排公式、保留矩阵、隔离层次全部不变。

================================================================================
一处刻意的语义裁决（实现里写在代码注释上）
================================================================================
`superseded_by` 的两种取值语义不同：
- 指向**一条存在的记录** → 被新版本覆写，recall 硬排除（冲突消解：取最新）；
- `"__archived__"`（墓碑）→ 归档冷存，**仍可召回**（audit trail 的意义），
  但会被 `recency_correctness` 记账成「误用过期记忆」。
这正是 `recency_correctness` 不会恒等于 1.0 的原因。

（另一处「刚才」是代词还是时间锚点的裁决，属于领域语义，已随分类正则一起
搬到 `agents/demo_extractor.py`。）
"""
from __future__ import annotations

import hashlib
import math
import re
import uuid
from typing import Callable, Protocol, Sequence, runtime_checkable

from contracts import (
    RETENTION_MATRIX,
    Chunk,
    Extractor,
    MemoryKind,
    MemoryMetrics,
    MemoryRecord,
    RetentionRule,
    Subject,
    now_ms,
)

__all__ = [
    "ARCHIVED_TOMBSTONE",
    "PROMPT_BUDGET_TOKENS",
    "PassthroughExtractor",
    "Provider",
    "InMemoryProvider",
    "MemoryRuntime",
    "fact_key",
    "normalize_text",
    "similarity",
    "text_similarity",
]

MS_PER_DAY = 86_400_000.0
#: 假定 Prompt 预算（token_overhead 的分母）
PROMPT_BUDGET_TOKENS = 4000
#: Profile 的刚性加成：硬约束优先注入
PROFILE_BONUS = 0.30
#: 重排权重（架构文档：语义 + 重要度 + 时效）
W_SIMILARITY = 0.50
W_IMPORTANCE = 0.30
W_RECENCY = 0.20
#: archive 动作的墓碑标记（不是 id，指向一条不存在的记录）
ARCHIVED_TOMBSTONE = "__archived__"


# ══════════════════════════════════════════════════════════════
# 文本工具：全部纯函数，便于复现（重排公式可手算验证）
# 这些是**与领域无关**的度量：字符 bigram 对任何语言都只是「便宜的近似语义单元」
# ══════════════════════════════════════════════════════════════
_PUNCT_RE = re.compile(r"[\s\W_]+", re.UNICODE)


def normalize_text(text: str) -> str:
    """归一化：小写 + 去空白/标点，保留 CJK 与字母数字。

    公开给 Agent 侧复用（`agents/*` 做低价值过滤时用同一把尺子），
    避免两侧各写一份归一化然后慢慢漂移。
    """
    return _PUNCT_RE.sub("", (text or "").lower())


#: 内部别名，保持本模块历史调用点不变
_norm = normalize_text


def _bigrams(text: str) -> set[str]:
    """字符 bigram 集合。中文无空格，字符二元组是最便宜的近似语义单元。"""
    s = _norm(text)
    if not s:
        return set()
    if len(s) == 1:
        return {s}
    return {s[i:i + 2] for i in range(len(s) - 1)}


def similarity(a: set[str], b: set[str]) -> float:
    """Jaccard 相似度（mock 语义相似度，零依赖、可手算）。"""
    if not a or not b:
        return 0.0
    inter = len(a & b)
    return inter / float(len(a | b)) if inter else 0.0


def text_similarity(a: str, b: str) -> float:
    return similarity(_bigrams(a), _bigrams(b))


_FACT_KEY_STRIP = re.compile(r"(等于|是|为|＝|=|[\d.%]+)")


def fact_key(text: str) -> str:
    """事实骨架：去掉数值/判断词/标点后剩下的部分。

    ⚠️ 这是**兜底归一化**，不是领域判断：`等于|是|为` 只覆盖中文的一种说法。
    真正的「什么算同一事实」由 Agent 的 `record.fact_key` 决定
    （示例见 `agents/demo_extractor.py`）；这里保留它有两个用途：
      ① 给不需要语义判断的调用方一个便宜口径；
      ② 让 `_fact_key_of()` 的退化路径可读、可测试。
    """
    return _FACT_KEY_STRIP.sub("", _norm(text))


def _fact_key_of(rec: MemoryRecord) -> str:
    """一条记录的「同一事实」判定键（conflict 检测唯一需要的元数据）。

    ① 优先用 Extractor 写在 record 上的 `fact_key` —— 那是 Agent 的语义口径；
    ② 为空时退化为 (kind, 归一化文本) 的**稳定哈希**：只能抓完全相同的文本，
       因此跨领域安全（不会把「连接池上限 20」和「连接池上限 30」混为一谈）。

    用 sha1 而不是内置 `hash()`：字符串哈希受 `PYTHONHASHSEED` 影响，跨进程不可复现，
    而「同一事实怎么判定」必须是可解释、可重放的。
    """
    if rec.fact_key:
        return rec.fact_key
    material = f"{rec.kind.value}\x00{normalize_text(rec.text)}".encode()
    return "auto:" + hashlib.sha1(material).hexdigest()[:16]


def _visible(rec_subject: Subject, viewer: Subject) -> bool:
    """多租户强隔离 + 分层共享。租户不同一律不可见。"""
    if viewer is None or not viewer.tenant or not rec_subject.tenant:
        return False
    if rec_subject.tenant != viewer.tenant:
        return False
    if rec_subject.user_id == viewer.user_id and viewer.user_id:
        return True
    # 同 team + 同 project 才允许跨 user 共享；缺一不可
    return bool(viewer.team and viewer.project
                and rec_subject.team == viewer.team
                and rec_subject.project == viewer.project)


def _require_subject(subject: Subject | None) -> Subject:
    """INV-12：无主记忆拒绝入库。"""
    if not isinstance(subject, Subject):
        raise ValueError(
            f"INV-12: 记忆必须有归属主体 Subject，收到 {type(subject).__name__}")
    if not subject.tenant:
        raise ValueError("INV-12: Subject.tenant 为空，拒绝入库")
    return subject


def _rule_for(kind: MemoryKind) -> RetentionRule | None:
    """保留矩阵里第一条 kind 匹配的规则（INV-13 的锚点）。"""
    for rule in RETENTION_MATRIX:
        if rule.kind == kind:
            return rule
    return None


# ══════════════════════════════════════════════════════════════
# ① 抽取：Runtime 只定义 SPI，不实现语义
# ══════════════════════════════════════════════════════════════
class PassthroughExtractor:
    """Runtime 自带的中性兜底：不做任何语义判断。

    把一轮对话整体当作一条 Episodic 记录（importance 取中位 0.5），
    fact_key 用 (kind, text) 哈希。它唯一的作用是让 MemoryRuntime
    在没有 Agent 注入策略时仍能工作，而不是替 Agent 做判断。

    注入 `agents.demo_extractor.RuleBasedExtractor()`（或任何实现了
    `contracts.Extractor` 的类）即可获得真正的领域分级。
    """

    #: 中位重要度：既不当硬约束，也不当垃圾
    IMPORTANCE = 0.5
    CONFIDENCE = 0.5

    def extract(self, turn: dict, subject: Subject) -> list[MemoryRecord]:
        if not isinstance(turn, dict):
            raise TypeError(f"turn 必须是 dict，收到 {type(turn).__name__}")
        turn_id = str(turn.get("turn_id") or turn.get("id") or "turn")

        parts: list[str] = []
        for role in ("user", "assistant"):
            raw = turn.get(role) or ""
            if not isinstance(raw, str):
                raw = str(raw)
            if raw.strip():
                parts.append(raw.strip())
        if not parts:                          # 只有工具回包的一轮，也照原样存
            for entry in turn.get("tools") or []:
                if not isinstance(entry, dict) or entry.get("payload") is None:
                    continue
                parts.append(f"{entry.get('tool') or 'tool'}: {entry.get('payload')}")
        text = " ".join(" ".join(parts).split())
        if not text:
            return []

        kind = MemoryKind.EPISODIC
        rule = _rule_for(kind)
        rec = MemoryRecord(
            id="",
            kind=kind,
            text=text,
            subject=subject,
            importance=self.IMPORTANCE,
            confidence=self.CONFIDENCE,
            created_at=0.0,                    # Runtime 按自己的时钟盖章
            last_hit_at=0.0,
            hits=0,
            version=1,
            superseded_by=None,
            ttl_days=(rule.ttl_days if rule is not None else None),
            source=f"passthrough:{turn_id}",
            fact_key="",
        )
        rec.fact_key = _fact_key_of(rec)       # 显式写出「退化口径」＝ (kind, text) 哈希
        return [rec]


# ══════════════════════════════════════════════════════════════
# Provider SPI：可替换的存储后端
# ══════════════════════════════════════════════════════════════
@runtime_checkable
class Provider(Protocol):
    """统一 Provider SPI。Mem0 / Qdrant / Hermes 都只需要实现这四个方法。

    约定（重要）：`get()` / `scan()` 返回的 `MemoryRecord` 被视为**活句柄**，
    Runtime 会就地修改 `hits` / `last_hit_at` / `superseded_by` 然后 `put()` 回写。
    向量库适配器请在 `get/scan` 里返回可变副本、在 `put` 里落库，
    不要依赖「对象引用即持久化」。
    """

    def put(self, rec: MemoryRecord) -> None: ...

    def get(self, rec_id: str) -> MemoryRecord | None: ...

    def scan(self, subject: Subject) -> list[MemoryRecord]:
        """只返回该主体可见范围内的记录（多租户隔离的第一道闸门）。"""
        ...

    def delete(self, rec_id: str) -> bool: ...


class InMemoryProvider:
    """默认实现：一个 dict。够跑测试、够当参考适配器的骨架。"""

    def __init__(self) -> None:
        self._store: dict[str, MemoryRecord] = {}
        self.calls: dict[str, int] = {"put": 0, "get": 0, "scan": 0, "delete": 0}

    # ── SPI ──────────────────────────────────────────────
    def put(self, rec: MemoryRecord) -> None:
        self.calls["put"] += 1
        self._store[rec.id] = rec

    def get(self, rec_id: str) -> MemoryRecord | None:
        self.calls["get"] += 1
        return self._store.get(rec_id)

    def scan(self, subject: Subject) -> list[MemoryRecord]:
        self.calls["scan"] += 1
        return [r for r in self._store.values() if _visible(r.subject, subject)]

    def delete(self, rec_id: str) -> bool:
        self.calls["delete"] += 1
        return self._store.pop(rec_id, None) is not None

    # ── 可选能力：全量枚举（sweep(None) 用；没有它 Runtime 会退回主体注册表）──
    def all(self) -> list[MemoryRecord]:
        return list(self._store.values())

    def __len__(self) -> int:
        return len(self._store)


# ══════════════════════════════════════════════════════════════
# MemoryRuntime：五阶段代谢链
# ══════════════════════════════════════════════════════════════
class MemoryRuntime:
    """有状态的记忆运行时。所有方法都以 `Subject` 为边界。

    `extractor` 是**唯一**的领域入口：不给就用 `PassthroughExtractor` 兜底，
    语义判断一律不在这里发生。
    """

    def __init__(self, provider: Provider | None = None,
                 extractor: Extractor | None = None,
                 now: Callable[[], float] = now_ms) -> None:
        self.provider: Provider = provider if provider is not None else InMemoryProvider()
        self._extractor: Extractor = extractor if extractor is not None else PassthroughExtractor()
        self.now = now
        #: 没有 `all()` 能力的 Provider 上，用注册表兜底 sweep(None)
        self._subjects: set[Subject] = set()

    # ── 内部：机械盖章（id / 时间 / 归属 / 取值范围）──────────
    def _stamp(self, rec: MemoryRecord, subject: Subject) -> MemoryRecord:
        """把 Extractor 产出的记录补齐成为**可落库的形状**。

        这些都不是语义判断：id 是身份、created_at 是 Runtime 自己的时钟、
        subject 缺省时以 `write(subject)` 为准（INV-12）、数值夹到 0..1。
        """
        if not isinstance(rec, MemoryRecord):
            raise TypeError(
                f"Extractor.extract 必须返回 MemoryRecord，收到 {type(rec).__name__}")
        if not rec.id:
            rec.id = f"mem_{uuid.uuid4().hex[:12]}"
        if not isinstance(rec.subject, Subject) or not rec.subject.tenant:
            rec.subject = subject               # INV-12：无主时以调用方主体为准
        if not rec.created_at:
            rec.created_at = self.now()
        rec.text = (rec.text or "")[:240]
        rec.importance = round(max(0.0, min(1.0, float(rec.importance))), 4)
        rec.confidence = round(max(0.0, min(1.0, float(rec.confidence))), 4)
        if rec.version < 1:
            rec.version = 1
        return rec

    # ── 内部：写入落库（去重 + 版本冲突）────────────────────
    def _new_record(self, kind: MemoryKind, text: str, subject: Subject,
                    importance: float, confidence: float, ttl_days: float | None,
                    source: str, version: int = 1) -> MemoryRecord:
        """管道自身产物（reflect 的共识）用；语义抽取一律走 Extractor。"""
        return MemoryRecord(
            id=f"mem_{uuid.uuid4().hex[:12]}",
            kind=kind,
            text=text[:240],
            subject=subject,
            importance=round(max(0.0, min(1.0, importance)), 4),
            confidence=round(max(0.0, min(1.0, confidence)), 4),
            created_at=self.now(),
            last_hit_at=0.0,
            hits=0,
            version=version,
            superseded_by=None,
            ttl_days=ttl_days,
            source=source,
        )

    def _persist(self, rec: MemoryRecord) -> MemoryRecord | None:
        """落库前的守门：完全重复丢弃，同 `fact_key` 则版本覆写（旧记录留痕不删）。

        「同一事实」的口径来自 `record.fact_key`（Agent 决定）；
        为空时退化为 (kind, 归一化文本) 哈希 —— 只能抓完全相同的文本。
        Runtime 不解释文本含义，只比较键。
        """
        key = _fact_key_of(rec)
        for old in self.provider.scan(rec.subject):
            if old.kind != rec.kind or old.subject != rec.subject:
                continue
            if old.superseded_by is not None:
                continue                       # 已死记录不参与去重/冲突
            if old.text == rec.text:
                return None                    # 幂等：同一句话不重复入库
            if _fact_key_of(old) == key:
                old.superseded_by = rec.id     # 旧版本指向新 id，version+1
                rec.version = max(rec.version, old.version + 1)
                self.provider.put(old)
        self.provider.put(rec)
        return rec

    # ── ① write ─────────────────────────────────────────
    def write(self, turn: dict, subject: Subject) -> list[MemoryRecord]:
        """只做机械动作：抽取（委托）→ 去重 → 冲突覆写 → 落库。不猜语义。"""
        subj = _require_subject(subject)
        if not isinstance(turn, dict):
            raise TypeError(f"turn 必须是 dict，收到 {type(turn).__name__}")
        self._subjects.add(subj)

        extracted = self._extractor.extract(turn, subj)
        if extracted is None:
            return []
        if isinstance(extracted, MemoryRecord):
            raise TypeError("Extractor.extract 必须返回 MemoryRecord 列表")

        created: list[MemoryRecord] = []
        for rec in extracted:
            self._stamp(rec, subj)
            saved = self._persist(rec)
            if saved is not None:
                created.append(saved)
        return created

    # ── ② recall ────────────────────────────────────────
    def _score(self, rec: MemoryRecord, query_bg: set[str], t: float) -> float:
        """重排公式（可复现，手算即得）：

            score = 0.50*sim + 0.30*importance + 0.20*recency (+0.30 if PROFILE)
            recency = 1.0                              kind == PROFILE（免疫衰减）
                    = exp(-age_days / ttl_days)        其他
            age_days 自 last_hit_at（命中续命）或 created_at 起算
        """
        sim = similarity(query_bg, _bigrams(rec.text))
        if rec.kind == MemoryKind.PROFILE:
            recency = 1.0
        else:
            ttl = rec.ttl_days
            if ttl is None:
                rule = _rule_for(rec.kind)
                ttl = (rule.ttl_days if rule and rule.ttl_days else 90.0)
            anchor = rec.last_hit_at or rec.created_at or t
            age_days = max(0.0, (t - anchor) / MS_PER_DAY)
            recency = math.exp(-age_days / max(ttl, 1e-9))
        score = W_SIMILARITY * sim + W_IMPORTANCE * rec.importance + W_RECENCY * recency
        if rec.kind == MemoryKind.PROFILE:
            score += PROFILE_BONUS
        return score

    def _retrievable(self, rec: MemoryRecord) -> bool:
        """被新版本覆写的记录硬排除；归档墓碑仍可召回（冷存留痕）。"""
        if rec.superseded_by is None:
            return True
        return self.provider.get(rec.superseded_by) is None

    def _rank(self, query: str, subject: Subject, k: int,
              budget_tokens: int, activate: bool) -> list[Chunk]:
        t = self.now()
        query_bg = _bigrams(query or "")
        ranked: list[tuple[MemoryRecord, float]] = []
        for rec in self.provider.scan(subject):
            if not _visible(rec.subject, subject):
                continue                        # 纵深防御：Provoder 之外再筛一遍
            if not self._retrievable(rec):
                continue
            ranked.append((rec, self._score(rec, query_bg, t)))

        # 排序：Profile 是硬约束，一级序；分数二级序。这样「Profile 一定在最前」
        # 不依赖经验常数，哪怕查询与它零相似度也不会被语义命中的记录挤下去。
        ranked.sort(key=lambda pair: (
            0 if pair[0].kind == MemoryKind.PROFILE else 1, -pair[1], pair[0].id))

        chunks: list[Chunk] = []
        used = 0
        for rec, score in ranked:
            if len(chunks) >= k:
                break
            cost = len(rec.text)
            if used + cost > budget_tokens:
                break                           # token 预算裁剪：超了就停
            used += cost
            chunks.append(Chunk(
                doc_id=rec.id,
                text=rec.text,
                score=round(score, 6),
                span=f"{rec.kind.value}:v{rec.version}",
                version=str(rec.version),
                source="memory",
                kind=rec.kind.value,
            ))

        if activate:
            for chunk in chunks:
                rec = self.provider.get(chunk.doc_id)
                if rec is None:
                    continue
                rec.hits += 1                   # 命中即续命
                rec.last_hit_at = t
                self.provider.put(rec)
        return chunks

    def recall(self, query: str, subject: Subject, k: int = 5,
               budget_tokens: int = 800) -> list[Chunk]:
        """每轮前置：召回 + 多因子重排 + 预算裁剪。返回统一 Chunk 形状。"""
        subj = _require_subject(subject)
        self._subjects.add(subj)
        return self._rank(query, subj, k, budget_tokens, activate=True)

    # ── ③ reflect ───────────────────────────────────────
    def _cluster(self, records: Sequence[MemoryRecord]) -> list[dict]:
        """按 bigram 交集 ≥2 做单链聚类（union 增长 → 允许链式归并）。"""
        clusters: list[dict] = []
        for rec in records:
            bg = _bigrams(rec.text)
            for cluster in clusters:
                if len(bg & cluster["union"]) >= 2:
                    cluster["items"].append(rec)
                    cluster["union"] |= bg
                    break
            else:
                clusters.append({"items": [rec], "union": set(bg)})
        return clusters

    @staticmethod
    def _keywords(records: Sequence[MemoryRecord], top: int = 4) -> list[str]:
        counts: dict[str, int] = {}
        for rec in records:
            for bg in _bigrams(rec.text):
                counts[bg] = counts.get(bg, 0) + 1
        ordered = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
        return [bg for bg, n in ordered if n >= 2][:top]

    def reflect(self, subject: Subject) -> list[MemoryRecord]:
        """离线：≥3 条同主题 Episodic → 1 条 Semantic 共识（原事件保留）。"""
        subj = _require_subject(subject)
        self._subjects.add(subj)
        episodics = [r for r in self.provider.scan(subj)
                     if r.kind == MemoryKind.EPISODIC and r.is_alive()
                     and _visible(r.subject, subj)]
        produced: list[MemoryRecord] = []
        for cluster in self._cluster(episodics):
            items = cluster["items"]
            if len(items) < 3:
                continue
            keywords = self._keywords(items) or ["同主题事件"]
            text = f"共识：{'/'.join(keywords)} 反复出现（由 {len(items)} 条事件归纳）"
            rec = self._new_record(
                MemoryKind.SEMANTIC, text, subj,
                importance=max(0.5, max(r.importance for r in items)),
                confidence=0.85, ttl_days=180.0,
                source="reflect:" + ",".join(sorted(r.id for r in items)))
            saved = self._persist(rec)
            if saved is not None:
                produced.append(saved)
        return produced

    # ── ④ consolidate ───────────────────────────────────
    def consolidate(self, subject: Subject) -> list[dict]:
        """离线：高置信 Semantic → **Skill 候选**。

        架构裁决（§3）：Procedural 内容归 Skill 库，Memory 只产出候选，
        不做 code review、不做灰度、不落 SKILL.md。这里返回的数据是交接口径。
        """
        subj = _require_subject(subject)
        self._subjects.add(subj)
        eligible = [r for r in self.provider.scan(subj)
                    if r.kind == MemoryKind.SEMANTIC and r.is_alive()
                    and _visible(r.subject, subj)
                    and r.confidence >= 0.8 and r.hits >= 2]
        candidates: list[dict] = []
        for cluster in self._cluster(eligible):
            items = cluster["items"]
            if not items:
                continue
            ids = sorted(r.id for r in items)
            keywords = self._keywords(items) or ["经验沉淀"]
            skill_id = "skill_" + hashlib.sha1("|".join(ids).encode()).hexdigest()[:10]
            candidates.append({
                "skill_id": skill_id,
                "title": f"自动沉淀：{'/'.join(keywords)}",
                "from_memory": ids,
                "reason": (f"{len(ids)} 条高置信语义（confidence>={min(r.confidence for r in items):.2f}, "
                           f"hits>={min(r.hits for r in items)}）共同指向该模式，"
                           f"建议交由 Skill 库评审后落 SKILL.md"),
                "draft": "；".join(r.text for r in items),
            })
        return candidates

    # ── ⑤ sweep ─────────────────────────────────────────
    def _all_records(self) -> list[MemoryRecord]:
        all_fn = getattr(self.provider, "all", None)
        if callable(all_fn):
            return list(all_fn())
        seen: dict[str, MemoryRecord] = {}
        for subj in sorted(self._subjects, key=lambda s: s.key()):
            for rec in self.provider.scan(subj):
                seen.setdefault(rec.id, rec)
        return list(seen.values())

    def sweep(self, subject: Subject | None = None) -> dict:
        """定时 Sweeper：严格按 RETENTION_MATRIX 确定性淘汰（INV-13）。

        决策顺序：找到第一条 kind 匹配的规则 → ttl_days is None 跳过 →
        未超期或重要度够 → 保留 → 否则按 rule.action 处理。
        """
        if subject is None:
            records = self._all_records()
        else:
            subj = _require_subject(subject)
            self._subjects.add(subj)
            records = self.provider.scan(subj)

        t = self.now()
        deleted = archived = kept = 0
        details: list[dict] = []
        seen: set[str] = set()
        for rec in records:
            if rec.id in seen:
                continue
            seen.add(rec.id)
            if rec.superseded_by is not None:
                kept += 1                      # 已死（覆写/归档）记录不重复处理
                continue
            rule = _rule_for(rec.kind)
            if rule is None:
                kept += 1
                details.append({"id": rec.id, "rule": "无匹配保留规则 → 保守保留",
                                "action": "keep"})
                continue
            if rule.ttl_days is None or rule.action == "keep":
                kept += 1                      # Profile：永不按时间淘汰
                continue
            anchor = rec.last_hit_at or rec.created_at or t
            age_days = max(0.0, (t - anchor) / MS_PER_DAY)
            if age_days <= rule.ttl_days or rec.importance >= rule.min_importance:
                kept += 1
                continue
            if rule.action == "delete":
                self.provider.delete(rec.id)
                deleted += 1
            else:                              # archive：墓碑 + 保留
                rec.superseded_by = ARCHIVED_TOMBSTONE
                self.provider.put(rec)
                archived += 1
            details.append({
                "id": rec.id,
                "rule": rule.reason,           # INV-13：说清命中哪条规则的哪句话
                "action": rule.action,
                "kind": rec.kind.value,
                "age_days": round(age_days, 2),
                "importance": rec.importance,
            })
        return {"deleted": deleted, "archived": archived,
                "kept": kept, "details": details}

    # ── ⑥ metrics：五维评估 ──────────────────────────────
    def _dominant_subject(self) -> Subject | None:
        counts: dict[Subject, int] = {}
        for rec in self._all_records():
            counts[rec.subject] = counts.get(rec.subject, 0) + 1
        if not counts:
            return None
        return max(counts.items(), key=lambda kv: (kv[1], kv[0].key()))[0]

    def _conflict_groups(self, subject: Subject) -> dict[str, dict]:
        """事实键 → 版本组（判定「取到最新版本」用）。

        键的口径与 `_persist()` 完全一致（`_fact_key_of`）：Extractor 给了就听它的，
        没给就用 (kind, 归一化文本) 哈希。这样「覆写时算同一事实」与
        「评估时算同一事实」不会各说各话。
        """
        groups: dict[str, dict] = {}
        for rec in self.provider.scan(subject):
            if rec.kind != MemoryKind.SEMANTIC or not _visible(rec.subject, subject):
                continue
            key = _fact_key_of(rec)
            if not key:
                continue
            group = groups.setdefault(key, {"ids": set(), "latest": None, "max_v": -1})
            group["ids"].add(rec.id)
            if rec.is_alive() and rec.version > group["max_v"]:
                group["max_v"], group["latest"] = rec.version, rec.id
        return {k: v for k, v in groups.items() if len(v["ids"]) > 1 and v["latest"]}

    def metrics(self, probes: Sequence[tuple[str, str]], k: int = 3,
                subject: Subject | None = None) -> MemoryMetrics:
        """五维：recall_precision / profile_consistency / token_overhead /
        recency_correctness / conflict_resolution。

        `probes = [(query, 期望命中的文本片段), ...]`
        内部召回用 `activate=False`：评估不能改变记忆的 hits 与寿命。
        """
        probe_list = list(probes or [])
        if not probe_list:
            return MemoryMetrics()
        subj = _require_subject(subject) if subject is not None else self._dominant_subject()
        if subj is None:
            return MemoryMetrics()

        profiles = {r.id for r in self.provider.scan(subj)
                    if r.kind == MemoryKind.PROFILE and r.is_alive()
                    and _visible(r.subject, subj)}
        groups = self._conflict_groups(subj)

        hit_count = fresh_count = 0
        tokens = 0
        profile_cover: list[float] = []
        checked_groups: set[str] = set()
        resolved_groups: set[str] = set()

        for query, expected in probe_list:
            chunks = self._rank(query, subj, k, PROMPT_BUDGET_TOKENS, activate=False)
            texts = [c.text for c in chunks]
            ids = {c.doc_id for c in chunks}
            if any(expected in t for t in texts):
                hit_count += 1
            tokens += sum(len(t) for t in texts)

            stale = False
            for cid in ids:
                rec = self.provider.get(cid)
                if rec is not None and rec.superseded_by is not None:
                    stale = True               # 召回到了覆写/归档记录 → 时效失分
                    break
            if not stale:
                fresh_count += 1

            for key, group in groups.items():
                if not (group["ids"] & ids):
                    continue
                checked_groups.add(key)
                if group["latest"] in ids:
                    resolved_groups.add(key)

            profile_cover.append(
                len(profiles & ids) / float(len(profiles)) if profiles else 1.0)

        n = float(len(probe_list))
        return MemoryMetrics(
            recall_precision=hit_count / n,
            profile_consistency=sum(profile_cover) / n,
            token_overhead=(tokens / n) / float(PROMPT_BUDGET_TOKENS),
            recency_correctness=fresh_count / n,
            conflict_resolution=(len(resolved_groups) / float(len(checked_groups))
                                 if checked_groups else 1.0),
        )
