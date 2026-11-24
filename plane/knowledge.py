"""plane/knowledge.py — Knowledge Plane（W6）：客观知识 / RAG。

================================================================================
核心立场：Knowledge 与 Memory 的分离依据是「生命周期」，不是「内容」
================================================================================

把 RAG 库和长期记忆放一起，最常见的理由是「它们都是向量检索」—— 这是内容视角，
是错的。正确的判据是**生命周期**（架构文档 §2）：

    维度            Memory（长期记忆）                 Knowledge（客观知识）
    ─────────────────────────────────────────────────────────────────────────
    回答什么        这个主体「经历过」什么              世界上「有什么」
    生命周期        分层：永久 / 180 天 / 60 天         文档版本驱动，无 TTL
    淘汰机制        确定性保留矩阵，可审计              版本下线 / 重建索引
    写者            Memory Runtime（运行时回写）        知识管理员 / pipeline（离线）
    读通道          recall：语义 + 时效 + 重要度         retrieve：向量 + BM25
    归属            属于主体（user/team/tenant）        全组织共享
    合规            GDPR：主体可要求删除                不在删除请求权范围内
    失败后果        召回污染 → 毒化后续所有 turn        检索错 → 单次答错，可纠正

由此推出本模块的三条硬性形态（架构文档 §4.3）：

1. **单向只读（INV-11）**：Agent / Loop 侧只拿得到 :class:`KnowledgeReader`。
   它上面**没有** ``add`` / ``write`` / ``delete`` —— 不是「忘了实现」，
   是「签名即契约」。写入只能走 :class:`KnowledgeBase`，那是 pipeline 的活。
   混用一套 API 的后果是：用户行使删除权时无法只删自己那份（§2 推论 2）。

2. **版本驱动**：同一 ``doc_id`` 被新版本覆盖后，检索**只能**返回新版本的内容，
   ``Chunk.version`` 必须是新版本号。Knowledge 不做条目级衰减、不做时间加权 ——
   「400 天前写的运维手册」不会因为旧就不相关。这一点与 Memory 的 Episodic
   形成刻意对照，测试里有专门的 test_no_time_decay 盯着。

3. **必须带引用**：每条命中都回填 ``doc_id / version / span``，供输出护栏与溯源。
   检索错可以纠正，前提是你能顺着引用找到原文。

================================================================================
混合检索（deterministic，零第三方依赖）
================================================================================

    最终分 = 0.6 * cosine(query, chunk) + 0.4 * bm25_normalized(query, chunk)

- **两路并行，权重 0.6 / 0.4**：向量侧管「换了说法也能召回」（同义近形），
  BM25 侧管「精确术语必须置顶」（错误码、命令名、字段名）。任何单侧都会漏。
- **向量侧 = mock embedding**：字符 bigram 计数向量 + 余弦。中文没有空格，
  bigram 是天然的 term；英文按整词切。确定性、可复现、无模型依赖。
  换真实 embedding 时只需替换向量化那一段，排序与融合逻辑不动。
- **BM25 标准式**：``k1=1.5, b=0.75``；``idf = ln(1 + (N-df+0.5)/(df+0.5))``。
- **bm25 归一化**：除以本次候选集的最高分，映射到 ``[0,1]`` 后才能与 cos 加权相加。
  归一化只在**单次查询内部**进行，跨查询绝对分不可比 —— 这是刻意的：
  Knowledge 没有时效因子，绝对分本来就没有语义。
- **证据门槛**（``EVIDENCE_MIN_TERMS``）：归一化有个副作用 —— 任何 1 个 term
  的偶然重合都会把 0.4 权重顶满，于是「偏微分方程的数值解法」能靠一个 bigram
  拿到 0.4 的假高分。所以在**过滤层**（不是打分公式里）要求命中 >=2 个不同 term
  （查询本身只有 1 个 term 时降级为 1）。宁可少召回，也不拿无关段落污染 Context。

================================================================================
分块
================================================================================

按 Markdown 标题 / 空行切块，目标块长 200~600 字符；块之间的归属用 ``span`` 表达：

- 有编号标题（``## 3.2 主从切换``）→ ``span = "3.2"``；该节切成多块 → ``"3.2#1"``、``"3.2#2"``
- 无编号 → ``span = "p1"``、``"p2"``（全局块序号）
- 只有标题没有正文的块（文档大标题）折叠进下一个块，避免 9 字的无信息块

同一文档、同一版本，两次 ``rebuild()`` 必须得到完全相同的 span 与正文 ——
引用是给人顺着查的，不能今天 ``3.2`` 明天 ``p7``。
注意 200 是**同节内合并**的目标值，硬上限 600：节本身只有 120 字时不会去跨节拼凑，
因为跨节拼接会把 span 弄脏，引用就失去意义了。
"""
from __future__ import annotations

import math
import re
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

# 允许在任意工作目录下 import（demo/run.py 可能从别处启动）
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from contracts import Chunk  # noqa: E402

__all__ = [
    "Document",
    "KnowledgeBase",
    "KnowledgeReader",
    "MIN_CHUNK_CHARS",
    "MAX_CHUNK_CHARS",
    "W_VECTOR",
    "W_BM25",
    "EVIDENCE_MIN_TERMS",
]

# ══════════════════════════════════════════════════════════════
# 常量
# ══════════════════════════════════════════════════════════════
MIN_CHUNK_CHARS = 200          # 同节内合并目标：低于此值尝试吸收下一个块
MAX_CHUNK_CHARS = 600          # 硬上限，超出按句子边界再切
BM25_K1 = 1.5
BM25_B = 0.75
W_VECTOR = 0.6                 # 向量侧权重
W_BM25 = 0.4                   # BM25 侧权重
SOURCE = "knowledge"
EVIDENCE_MIN_TERMS = 2         # 命中多少个不同 term 才算候选（防止单 bigram 假命中）

_CJK = r"\u4e00-\u9fff"
_RUN = re.compile(rf"[0-9a-zA-Z]+|[{_CJK}]+")
_HEADING_MD = re.compile(r"^\s{0,3}#{1,6}\s+(.*\S)\s*$")
_HEADING_DOTTED = re.compile(r"^(\d+(?:\.\d+)+)[.、)）]?\s+(\S.*)$")
_HEADING_TOP = re.compile(r"^(\d+)[.、)）]?\s+(\S.*)$")
_SENTENCE_END = "。！？；!?;\n"
_SENT_SPLIT = re.compile(rf"[^{_SENTENCE_END}]*[{_SENTENCE_END}]?")


# ══════════════════════════════════════════════════════════════
# 文档（写入侧的输入形状）
# ══════════════════════════════════════════════════════════════
@dataclass(frozen=True)
class Document:
    """一篇知识文档。``doc_id`` 是版本线的主键，``version`` 是同一条线上的新旧。"""
    doc_id: str
    text: str
    title: str = ""
    version: str = "1"
    updated_at: float = 0.0     # epoch ms，仅用于展示/审计，**不参与打分**（无衰减）


# ══════════════════════════════════════════════════════════════
# 分词（BM25 term 与 mock embedding 共用同一套 term，保证两路口径一致）
# ══════════════════════════════════════════════════════════════
def _terms(text: str) -> list[str]:
    """英文/数字按整词切，中文按字符 bigram 切。

    中文单字成词信息量太低（「的」「了」），bigram 才是最小可用单元；
    英文整词比 bigram 精确（避免 inject / injection 靠 bigram 混为一谈）。
    顺序保留出现顺序 —— Counter 的插入顺序参与浮点累加顺序，这是确定性的前提。
    """
    out: list[str] = []
    for m in _RUN.finditer(text.lower()):
        run = m.group(0)
        if run[0].isascii():
            out.append(run)
        elif len(run) == 1:
            out.append(run)
        else:
            out.extend(run[i:i + 2] for i in range(len(run) - 1))
    return out


# ══════════════════════════════════════════════════════════════
# 分块
# ══════════════════════════════════════════════════════════════
def _as_heading(line: str) -> tuple[str, str] | None:
    """识别标题行，返回 (编号, 标题正文)；不是标题返回 None。

    支持 ``## 3.2 主从切换``（Markdown）与 ``3.2 主从切换``（纯文本）。
    纯文本形态收紧条件（短行、不以句读结尾），避免把列表项 "1. xxx" 误当标题。
    """
    m = _HEADING_MD.match(line)
    if m:
        title = m.group(1).strip()
        hit = _HEADING_DOTTED.match(title)
        if hit:
            return hit.group(1), title
        lead = _HEADING_TOP.match(title)      # "# 3 数据库运维手册" 这类单级编号
        if lead:
            return lead.group(1), title
        return "", title
    stripped = line.strip()
    hit = _HEADING_DOTTED.match(stripped)
    if hit and len(stripped) <= 60 and not stripped.endswith(("。", "：", ":")):
        return hit.group(1), stripped
    return None


def _blocks(text: str) -> list[tuple[str, str, bool]]:
    """切成 (节编号, 块正文, 是否仅有标题)。

    标题行并入它所在节的第一块（标题本身是极好的检索信号，不能丢）。
    某节只有标题没有正文时标记 heading_only，后面尝试折叠进下一块，
    避免产出 "3 数据库运维手册" 这种 9 字的无信息块。
    """
    out: list[tuple[str, str, bool]] = []
    section = ""
    buf: list[str] = []
    pending: str | None = None

    def flush() -> None:
        nonlocal buf, pending
        heading, pending = pending, None
        body = "\n".join(buf).strip()
        buf = []
        if heading is None and not body:
            return
        if heading is None:
            out.append((section, body, False))
        elif body:
            out.append((section, heading + "\n" + body, False))
        else:
            out.append((section, heading, True))

    for raw in text.splitlines():
        line = raw.rstrip()
        head = _as_heading(line)
        if head is not None:
            flush()
            section = head[0]
            pending = head[1]
            continue
        if not line.strip():
            flush()
            continue
        buf.append(line)
    flush()
    return out


def _split_long(block: str) -> list[str]:
    """超长块按句子边界切，单句仍超限则硬切。保证每片 <= MAX_CHUNK_CHARS。"""
    if len(block) <= MAX_CHUNK_CHARS:
        return [block]
    pieces: list[str] = []
    cur = ""
    for m in _SENT_SPLIT.finditer(block):
        sent = m.group(0)
        if not sent:
            continue
        while len(sent) > MAX_CHUNK_CHARS:              # 单句超长：硬切
            if cur:
                pieces.append(cur)
                cur = ""
            pieces.append(sent[:MAX_CHUNK_CHARS])
            sent = sent[MAX_CHUNK_CHARS:]
        if cur and len(cur) + len(sent) > MAX_CHUNK_CHARS:
            pieces.append(cur)
            cur = sent
        else:
            cur += sent
    if cur:
        pieces.append(cur)
    return [p for p in (p.strip() for p in pieces) if p]


def _chunk_text(text: str) -> list[tuple[str, str]]:
    """返回 [(span, 正文)]，顺序即文档顺序，完全确定性。"""
    # 1) 折叠「只有标题没有正文」的块：优先并入下一块（父标题 → 子节），否则并入上一块
    folded: list[tuple[str, str]] = []
    carry = ""
    for section, body, heading_only in _blocks(text):
        if carry:
            body = carry + "\n" + body
            carry = ""
            heading_only = False
        if heading_only and len(body) < MIN_CHUNK_CHARS:
            carry = body
            continue
        folded.append((section, body))
    if carry:
        if folded:
            folded[-1] = (folded[-1][0], folded[-1][1] + "\n" + carry)
        else:
            folded.append(("", carry))

    # 2) 超长块按句子边界切
    parts: list[tuple[str, str]] = []
    for section, block in folded:
        for piece in _split_long(block):
            parts.append((section, piece))

    # 3) 小节内合并：块长 < MIN 时向后吸收同节相邻块，但不越过 MAX
    merged: list[tuple[str, str]] = []
    for section, piece in parts:
        if merged and merged[-1][0] == section:
            prev_section, prev = merged[-1]
            if len(prev) < MIN_CHUNK_CHARS and len(prev) + 1 + len(piece) <= MAX_CHUNK_CHARS:
                merged[-1] = (prev_section, prev + "\n" + piece)
                continue
        merged.append((section, piece))

    # 4) span 生成：本节唯一块用节号，多块用 "#序号"；无节号用全局 p{n}
    per_section = Counter(sec for sec, _ in merged if sec)
    seen: Counter[str] = Counter()
    used: set[str] = set()
    out: list[tuple[str, str]] = []
    for seq, (section, piece) in enumerate(merged, 1):
        if section:
            seen[section] += 1
            span = section if per_section[section] == 1 else f"{section}#{seen[section]}"
        else:
            span = f"p{seq}"
        if span in used:                                # 极端撞名兜底，仍然确定性
            span = f"{span}-{seq}"
        used.add(span)
        out.append((span, piece))
    return out


# ══════════════════════════════════════════════════════════════
# 索引
# ══════════════════════════════════════════════════════════════
@dataclass(frozen=True)
class _Piece:
    """一个块 + 它的引用信息。"""
    doc_id: str
    version: str
    span: str
    text: str


@dataclass(frozen=True)
class _Indexed:
    """块 + 预计算的检索结构（tf / 文档长度 / 向量模）。"""
    piece: _Piece
    tf: dict[str, int]
    dl: int
    norm: float


@dataclass(frozen=True)
class _Scored:
    """一次查询里某个块的分数明细。"""
    idx: int
    matched: int
    cos: float
    bm25: float
    bm25_norm: float
    score: float


class KnowledgeBase:
    """写入侧：知识 pipeline 用它灌文档、重建索引。

    Agent / Loop **不应**拿到这个对象，只应拿到 :meth:`reader`。见 INV-11。

    版本驱动语义：``add()`` 同 ``doc_id`` 直接覆盖（不做多版本共存）。
    覆盖后旧块立刻从索引里消失 —— 「世界上有什么」只有当前版本算数。
    """

    def __init__(self) -> None:
        self._docs: dict[str, Document] = {}
        self._chunks: tuple[_Indexed, ...] = ()
        self._df: dict[str, int] = {}
        self._avgdl: float = 1.0
        self._dirty = True
        self._rebuilds = 0

    # ---------- 写 ----------
    def add(self, doc: Document) -> None:
        """写入/覆盖一篇文档。同 doc_id 的新版本覆盖旧版本（版本驱动）。"""
        if not isinstance(doc, Document):
            raise TypeError("add() 只接受 Document")
        if not doc.doc_id:
            raise ValueError("doc_id 不能为空")
        self._docs[doc.doc_id] = doc
        self._dirty = True

    def add_many(self, docs: Iterable[Document]) -> None:
        for d in docs:
            self.add(d)

    def retire(self, doc_id: str) -> bool:
        """版本下线：整篇文档退出知识库（不是条目级衰减）。返回是否真的删掉了。"""
        existed = self._docs.pop(doc_id, None) is not None
        if existed:
            self._dirty = True
        return existed

    # ---------- 建索引 ----------
    def rebuild(self) -> dict:
        """重建全量索引。返回 {"docs": n, "chunks": n, "tokens": n}。

        重建而非增量：知识同步是离线批处理，确定性比增量速度重要，
        而且「全量重建」天然满足「版本下线即生效」（不留旧块残影）。
        """
        chunks: list[_Indexed] = []
        df: Counter[str] = Counter()
        for doc_id in sorted(self._docs):                 # 排序 → 重建结果稳定
            doc = self._docs[doc_id]
            for span, text in _chunk_text(doc.text):
                counts = Counter(_terms(text))
                if not counts:
                    continue                              # 纯标点块不值得进索引
                chunks.append(_Indexed(
                    piece=_Piece(doc_id=doc_id, version=doc.version, span=span, text=text),
                    tf=dict(counts),
                    dl=sum(counts.values()),
                    norm=math.sqrt(sum(v * v for v in counts.values())),
                ))
                for term in counts:
                    df[term] += 1

        self._chunks = tuple(chunks)
        self._df = dict(df)
        self._avgdl = (sum(c.dl for c in chunks) / len(chunks)) if chunks else 1.0
        if self._avgdl <= 0:
            self._avgdl = 1.0
        self._dirty = False
        self._rebuilds += 1
        return {"docs": len(self._docs),
                "chunks": len(self._chunks),
                "tokens": sum(c.dl for c in self._chunks)}

    def _ensure_fresh(self) -> None:
        """写后读一致性：脏了就自动重建，避免「忘了 rebuild 就检索」读到旧版本。"""
        if self._dirty:
            self.rebuild()

    # ---------- 只读元信息 ----------
    def stats(self) -> dict:
        self._ensure_fresh()
        return {
            "docs": len(self._docs),
            "chunks": len(self._chunks),
            "tokens": sum(c.dl for c in self._chunks),
            "vocab": len(self._df),
            "avg_chunk_tokens": round(self._avgdl, 4),
            "rebuilds": self._rebuilds,
            "doc_ids": tuple(sorted(self._docs)),
        }

    def doc_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._docs))

    def __len__(self) -> int:
        return len(self._docs)

    # ---------- 读门面 ----------
    def reader(self) -> "KnowledgeReader":
        """返回只读门面。Agent 侧唯一能拿到的 Knowledge 句柄（INV-11）。"""
        self._ensure_fresh()
        return KnowledgeReader(self)


class KnowledgeReader:
    """读取侧门面：只有检索能力，**没有任何写方法**。

    INV-11 不是靠注释保证的，是靠「这个类上根本没有 add / write / delete 这三个名字」。
    想写入？去拿 KnowledgeBase —— 而 Agent 拿不到。签名即契约。
    """

    __slots__ = ("_kb",)

    def __init__(self, kb: KnowledgeBase) -> None:
        self._kb = kb

    # ---------- 检索 ----------
    def retrieve(self, query: str, k: int = 4) -> list[Chunk]:
        """混合检索，返回按分数降序的 Chunk（带 doc_id / version / span 引用）。

        - 空 query / 空库 / k<=0 → 空列表（检索失败是「答得差」，不是「崩」）
        - 证据不足（见 EVIDENCE_MIN_TERMS）或分数为 0 的块不返回：
          宁可少召回，也不拿无关段落污染 Context
        - 排序键 (-score, doc_id, span) 保证同分时顺序也确定
        """
        if k <= 0 or not query or not query.strip():
            return []
        self._kb._ensure_fresh()
        out: list[Chunk] = []
        for r in self._ranked(query)[:k]:
            p = self._chunks[r.idx].piece
            out.append(Chunk(
                doc_id=p.doc_id, text=p.text, score=r.score, span=p.span,
                version=p.version, source=SOURCE, kind="",
            ))
        return out

    def explain(self, query: str, k: int = 4) -> list[dict]:
        """两路分数拆解，只用于调试/评测（只读，不是写口子）。"""
        if k <= 0 or not query or not query.strip():
            return []
        self._kb._ensure_fresh()
        rows = []
        for r in self._ranked(query)[:k]:
            p = self._chunks[r.idx].piece
            rows.append({"doc_id": p.doc_id, "version": p.version, "span": p.span,
                         "cos": round(r.cos, 6), "bm25": round(r.bm25, 6),
                         "bm25_norm": round(r.bm25_norm, 6),
                         "matched_terms": r.matched, "score": round(r.score, 6)})
        return rows

    # ---------- 只读元信息 ----------
    def stats(self) -> dict:
        return self._kb.stats()

    def doc_ids(self) -> tuple[str, ...]:
        return self._kb.doc_ids()

    def __len__(self) -> int:
        return len(self._kb)

    def __repr__(self) -> str:  # pragma: no cover - 仅调试用
        return f"KnowledgeReader(docs={len(self._kb)}, read_only=True)"

    # ---------- 内部 ----------
    @property
    def _chunks(self) -> tuple[_Indexed, ...]:
        return self._kb._chunks

    def _ranked(self, query: str) -> list[_Scored]:
        """打分 → 去零分 → 按 (-score, doc_id, span) 稳定排序。"""
        rows = [r for r in self._rank(query) if r.score > 0.0]
        rows.sort(key=lambda r: (-r.score, self._chunks[r.idx].piece.doc_id,
                                 self._chunks[r.idx].piece.span))
        return rows

    def _rank(self, query: str) -> list[_Scored]:
        """每个块的打分明细。retrieve / explain 共用，保证「说的分」和「用的分」一致。"""
        items = self._kb._chunks
        q_terms = _terms(query)
        if not items or not q_terms:
            return []
        q_counts = Counter(q_terms)
        q_norm = math.sqrt(sum(v * v for v in q_counts.values()))
        uniq = sorted(q_counts)          # 排序 → 浮点累加顺序固定
        n = len(items)
        avgdl = self._kb._avgdl
        need = min(EVIDENCE_MIN_TERMS, len(uniq))

        bm25_raw: list[float] = []
        matched: list[int] = []
        for ic in items:
            s = 0.0
            hits = 0
            for t in uniq:
                tf = ic.tf.get(t, 0)
                if not tf:
                    continue
                hits += 1
                df = self._kb._df.get(t, 0)
                idf = math.log(1.0 + (n - df + 0.5) / (df + 0.5))
                denom = tf + BM25_K1 * (1.0 - BM25_B + BM25_B * ic.dl / avgdl)
                s += idf * tf * (BM25_K1 + 1.0) / denom
            bm25_raw.append(s)
            matched.append(hits)

        peak = max(bm25_raw) if bm25_raw else 0.0
        rows: list[_Scored] = []
        for i, ic in enumerate(items):
            if matched[i] < need:
                continue                              # 证据不足，不进候选
            dot = sum(c * ic.tf.get(t, 0) for t, c in q_counts.items())
            cos = dot / (q_norm * ic.norm) if (q_norm > 0 and ic.norm > 0) else 0.0
            bmn = (bm25_raw[i] / peak) if peak > 0 else 0.0
            rows.append(_Scored(idx=i, matched=matched[i], cos=cos, bm25=bm25_raw[i],
                                bm25_norm=bmn, score=W_VECTOR * cos + W_BM25 * bmn))
        return rows
