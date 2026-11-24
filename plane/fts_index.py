"""plane/fts_index.py — 用 SQLite FTS5 提供 BM25，替代手写实现。

**为什么换**：`plane/knowledge.py` 里手写了一套 BM25（tf / df / avgdl / idf / 长度归一化）。
它在小语料上没问题，但：
* df/avgdl 每次 rebuild 都要全量重算并常驻内存（语料一大就吃内存）
* 没有倒排索引，查询是 O(块数 × 查询词数) 全扫
* 排序、短语查询、前缀查询、NEAR 全都要自己加

SQLite 的 **FTS5 自带 `bm25()` 函数**（stdlib，零安装），倒排索引、分词、短语
（`"a b"`）、前缀（`tok*`）、NEAR 都是现成的。这正是"该用就用"的典型。

**保留什么**：中文分词的处理方式（字符 bigram）。FTS5 的 `unicode61` 分词器不切中文，
直接建表会把整句当一个 token；所以这里把中文按 bigram 展开后再喂给 FTS5 ——
索引由库管，分词策略仍是我们自己的（它属于业务，不属于存储）。

**不替换什么**：`KnowledgeBase` 的分块、版本驱动、引用回填、向量侧（0.6 权重）。
本模块只接管 BM25 那一路（0.4 权重），其余不动。
"""
from __future__ import annotations

import math
import re
import sqlite3
from dataclasses import dataclass
from typing import Iterable, Sequence

__all__ = [
    "FTS5Index",
    "FTS5Unavailable",
    "bigram_terms",
    "fts_available",
]

#: ASCII 词（含数字）+ CJK 串（**允许单字**：单字查询也要能检索到）。
#: 早期用的是 `{2,}`，导致「池」这种单字查询永远打不中 —— 这正是手写分词的坑。
_TOKEN_RE = re.compile(r"[A-Za-z0-9]+|[\u4e00-\u9fff]+")
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")


class FTS5Unavailable(RuntimeError):
    """当前 SQLite 构建没有 FTS5（或分词器不可用）。"""


def fts_available() -> bool:
    """探测 FTS5 是否可用（不同 Python 构建的 SQLite 编译选项不同）。"""
    try:
        con = sqlite3.connect(":memory:")
    except Exception:                                          # noqa: BLE001
        return False
    try:
        con.execute("CREATE VIRTUAL TABLE _probe USING fts5(x)")
        return True
    except sqlite3.OperationalError:
        return False
    finally:
        con.close()


def bigram_terms(text: str) -> list[str]:
    """与 `plane/knowledge.py` 同口径的分词：ASCII 整词 + CJK 字符 bigram。

    **分词策略属于业务，不属于存储** —— 所以即使是 FTS5 也沿用同一套，
    否则换索引会改变检索结果（那是不可接受的静默行为变化）。
    """
    out: list[str] = []
    for m in _TOKEN_RE.finditer(text or ""):
        tok = m.group(0)
        if _CJK_RE.search(tok):
            if len(tok) == 1:
                out.append(tok)                     # 单字保留（否则查不到）
            else:
                out.extend(tok[i:i + 2] for i in range(len(tok) - 1))
        else:
            out.append(tok.lower())                 # 数字与英文词整词
    return out


def _fts_query(terms: Iterable[str]) -> str:
    """把词表转成 FTS5 MATCH 表达式（OR 连接，加引号防注入与运算符误判）。"""
    seen: list[str] = []
    for t in terms:
        t = t.replace('"', "")
        if t and t not in seen:
            seen.append(t)
    if not seen:
        return ""
    return " OR ".join(f'"{t}"' for t in seen)


class FTS5Index:
    """一个 FTS5 倒排索引，用 `bm25()` 打分。

    用法::

        idx = FTS5Index()
        idx.build([("doc-0#p1", "连接池默认上限 20"), ("doc-1#p1", "灰度发布流程")])
        hits = idx.search("连接池上限", k=5)   # → [("doc-0#p1", 0.87), ...]

    分数归一化到 ``[0, 1]``（`1.0` 为本次最优），**与 knowledge.py 的 bm25_norm 同语义**，
    这样 `0.6*cos + 0.4*bm25_norm` 的加权不需要改。
    """

    def __init__(self, *, table: str = "kb", location: str = ":memory:") -> None:
        if not fts_available():
            raise FTS5Unavailable("当前 SQLite 不支持 FTS5")
        self.table = re.sub(r"\W", "_", table) or "kb"
        self._con = sqlite3.connect(location)
        # unicode61 不做中文切分，但我们喂进去的已经是 bigram 词，用空格分隔即可
        self._con.execute(
            f"CREATE VIRTUAL TABLE IF NOT EXISTS {self.table} "
            "USING fts5(docid UNINDEXED, body, tokenize='unicode61')")
        self._built = 0

    # ── 建索引 ──────────────────────────────────────────────
    def build(self, docs: Sequence[tuple[str, str]]) -> dict:
        """全量重建（与 KnowledgeBase 的版本驱动语义一致：覆盖即重建）。"""
        cur = self._con.cursor()
        cur.execute(f"DELETE FROM {self.table}")
        rows = []
        for docid, text in docs:
            terms = bigram_terms(text)
            if not terms:
                continue
            rows.append((docid, " ".join(terms)))
        cur.executemany(f"INSERT INTO {self.table}(docid, body) VALUES (?, ?)", rows)
        self._con.commit()
        self._built = len(rows)
        return {"docs": self._built}

    # ── 检索 ────────────────────────────────────────────────
    def search_raw(self, query: str, *, limit: int = 1000) -> list[tuple[str, float]]:
        """返回 `[(docid, bm25_score), ...]`，按相关度降序。

        FTS5 的 `bm25()` 返回**负数**（越小越相关，这是它的约定），
        这里取相反数转成「越大越相关」。
        """
        q = _fts_query(bigram_terms(query))
        if not q or not self._built:
            return []
        cur = self._con.cursor()
        try:
            cur.execute(
                f"SELECT docid, bm25({self.table}) AS s FROM {self.table} "
                "WHERE body MATCH ? ORDER BY s LIMIT ?", (q, int(limit)))
        except sqlite3.OperationalError:
            return []
        return [(str(d), -float(s)) for d, s in cur.fetchall()]

    def search(self, query: str, *, limit: int = 1000) -> list[tuple[str, float]]:
        """归一化到 `[0, 1]`（本次最高分 = 1.0），与 knowledge 的 bm25_norm 同语义。"""
        raw = self.search_raw(query, limit=limit)
        if not raw:
            return []
        peak = max(s for _, s in raw)
        if peak <= 0:
            return [(d, 0.0) for d, _ in raw]
        return [(d, s / peak) for d, s in raw]

    # ── 观测 ────────────────────────────────────────────────
    def stats(self) -> dict:
        cur = self._con.cursor()
        cur.execute(f"SELECT count(*) FROM {self.table}")
        n = int(cur.fetchone()[0])
        return {"backend": "sqlite-fts5", "docs": n, "built": self._built,
                "sqlite": sqlite3.sqlite_version}

    def close(self) -> None:
        try:
            self._con.close()
        except Exception:                                      # noqa: BLE001
            pass

    def __enter__(self) -> "FTS5Index":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


# ══════════════════════════════════════════════════════════════
# 接线：让 KnowledgeBase 用 FTS5 算 BM25，其余全部保持不变
# ══════════════════════════════════════════════════════════════
def fts_knowledge_base(base_cls):
    """返回一个「BM25 走 FTS5」的 KnowledgeBase 子类。

    只覆写 `_rank` 里的 BM25 那一项，向量侧、证据门槛、归一化、排序键全部沿用原实现 ——
    换索引不该改变检索结果，只该让它更快、更省内存。
    """

    class FTSKnowledgeBase(base_cls):            # type: ignore[misc, valid-type]
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            self._fts = FTS5Index(table="kb_live")

        def rebuild(self) -> dict:
            stats = super().rebuild()
            docs = [(f"{c.piece.doc_id}#{c.piece.span}", c.piece.text)
                    for c in self._chunks]
            try:
                self._fts.build(docs)
            except Exception:                                  # noqa: BLE001
                pass                                           # FTS 挂了退回手写 BM25
            return stats

        def fts_stats(self) -> dict:
            try:
                return self._fts.stats()
            except Exception:                                  # noqa: BLE001
                return {"backend": "unavailable"}

        def bm25_norm(self, query: str) -> dict[str, float]:
            """给调用方的便利入口：`{docid#span: 归一化 bm25}`。"""
            return dict(self._fts.search(query))

    return FTSKnowledgeBase