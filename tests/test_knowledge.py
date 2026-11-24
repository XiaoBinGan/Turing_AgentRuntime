"""tests/test_knowledge.py — W6 Knowledge Plane 自带测试（stdlib unittest，零第三方依赖）。

跑法：
    cd /Users/supre/Documents/tmp/agent-runtime && python3 -m unittest tests.test_knowledge -v

覆盖三件事：
1. 写入侧闭环（add / rebuild / stats / 版本覆盖 / 下线）
2. **INV-11**：Agent 只能拿到 KnowledgeReader，它上面没有 add / write / delete
3. 混合检索的真实行为：双路打分、引用完整、版本即时生效、**零时间衰减**、确定性
"""
from __future__ import annotations

import unittest

from contracts import Chunk, now_ms
from plane.knowledge import (
    MAX_CHUNK_CHARS,
    MIN_CHUNK_CHARS,
    W_BM25,
    W_VECTOR,
    Document,
    KnowledgeBase,
    KnowledgeReader,
    _chunk_text,          # 测分块形态必须看全量块，不能走带过滤的 retrieve
)

# ══════════════════════════════════════════════════════════════
# 夹具
# ══════════════════════════════════════════════════════════════
DAY_MS = 86_400_000.0

# 一段真实形态的中文运维文档：三级标题 + 每节一段，符合现场 wiki 的样子
OPS_DOC = """# 3 数据库运维手册

## 3.1 连接池配置

应用侧连接池的 max_connections 需要与数据库实例的连接上限匹配。连接池耗尽时，请求会在获取连接阶段阻塞，表现为接口超时。排查顺序：先看连接池使用率，再看是否出现慢查询长期占用连接，最后检查是否存在连接泄漏。建议保留一定的空闲连接余量，并将连接超时设置为三秒以内，避免请求堆积。

## 3.2 主从切换

主从切换由高可用组件自动触发，也可以在计划内维护时手工执行。手工切换前必须先摘除写入流量，确认从库延迟为零，然后执行提升从库为主库。切换完成后需要刷新应用侧的数据源地址，否则应用会继续连接旧主库。连接失败时先检查域名解析是否已经更新，再检查网络策略是否放通了新主库端口。

## 3.3 回滚流程

变更失败后的回滚必须按预案执行。回滚前先冻结相关变更，保留现场日志。数据库结构回滚要使用版本化脚本，禁止手工修改表结构。回滚完成后验证核心链路可用，并在变更系统里记录回滚原因与影响范围。若回滚过程中再次失败，立即升级到值班主管处理。
"""


def make_kb(*docs: Document) -> KnowledgeBase:
    kb = KnowledgeBase()
    for d in docs:
        kb.add(d)
    kb.rebuild()
    return kb


def pairs(chunks: list[Chunk]) -> list[tuple[str, str, str]]:
    return [(c.doc_id, c.span, c.text) for c in chunks]


# ══════════════════════════════════════════════════════════════
# 1. 写入侧闭环
# ══════════════════════════════════════════════════════════════
class TestWriteSide(unittest.TestCase):
    def test_add_rebuild_retrieve_roundtrip(self):
        """1. add + rebuild + retrieve 基本闭环。"""
        kb = make_kb(
            Document("ops-db", OPS_DOC, title="数据库运维手册", version="3"),
            Document("ops-redis", "Redis 内存告警时先看大 key 与过期策略。"
                                   "maxmemory-policy 建议设为 allkeys-lru，"
                                   "避免写满后出现 OOM 拒绝写入。", version="1"),
        )
        built = kb.rebuild()
        self.assertEqual(built["docs"], 2)
        self.assertGreaterEqual(built["chunks"], 3)
        self.assertGreater(built["tokens"], 0)
        self.assertEqual(set(built), {"docs", "chunks", "tokens"})

        hits = kb.reader().retrieve("主从切换完成后应用连接失败", k=2)
        self.assertTrue(hits)
        self.assertIsInstance(hits[0], Chunk)
        self.assertEqual(hits[0].doc_id, "ops-db")
        self.assertEqual(hits[0].span, "3.2")
        self.assertIn("主从切换", hits[0].text)

    def test_reader_is_read_only_inv11(self):
        """2. INV-11：reader 上没有 add / write / delete，一个写口子都不许留。"""
        kb = make_kb(Document("a", "连接池耗尽的排查方法。"))
        reader = kb.reader()
        self.assertIsInstance(reader, KnowledgeReader)

        # 签名即契约：三个写方法名一个都不能存在
        self.assertFalse(hasattr(reader, "add"))
        self.assertFalse(hasattr(reader, "write"))
        self.assertFalse(hasattr(reader, "delete"))

        # 把所有「像写操作」的名字都扫一遍，防止换个名字溜回来
        for name in ("add", "write", "delete", "remove", "update", "insert",
                     "upsert", "put", "set", "drop", "clear", "retire", "rebuild"):
            self.assertFalse(hasattr(reader, name), f"reader 暴露了写方法: {name}")

        # 公开读方法集合是封闭的：只有 retrieve / explain / stats / doc_ids
        public = {n for n in dir(reader) if not n.startswith("_")}
        self.assertEqual(public, {"retrieve", "explain", "stats", "doc_ids"})

        # 连动态挂一个写方法都不行（__slots__）
        with self.assertRaises(AttributeError):
            reader.add = lambda doc: None  # type: ignore[attr-defined]

        # 对照：写侧必须真有 add（否则「只读」就没意义了）
        self.assertTrue(hasattr(kb, "add"))
        self.assertTrue(hasattr(kb, "rebuild"))

    def test_retire_removes_document(self):
        """19. 版本下线：retire 后该文档彻底退出检索，其他文档不受影响。"""
        kb = make_kb(
            Document("gone", "灰度发布前需要冻结变更窗口。"),
            Document("stay", "回滚预案必须包含数据一致性校验。"),
        )
        self.assertTrue(any(c.doc_id == "gone" for c in kb.reader().retrieve("冻结变更窗口", k=5)))
        self.assertTrue(kb.retire("gone"))
        self.assertFalse(kb.retire("gone"))          # 幂等：第二次就没得删了
        hits = kb.reader().retrieve("冻结变更窗口", k=5)
        self.assertFalse(any(c.doc_id == "gone" for c in hits))
        self.assertEqual(kb.doc_ids(), ("stay",))

    def test_add_validates_input(self):
        """20. 写入侧校验：空 doc_id / 错误类型直接拒绝，不留半条脏数据。"""
        kb = KnowledgeBase()
        with self.assertRaises(ValueError):
            kb.add(Document("", "没有主键的文档"))
        with self.assertRaises(TypeError):
            kb.add("not a document")  # type: ignore[arg-type]
        self.assertEqual(len(kb), 0)
        self.assertEqual(kb.reader().retrieve("任何查询"), [])


# ══════════════════════════════════════════════════════════════
# 2. 分块稳定性与形态
# ══════════════════════════════════════════════════════════════
class TestChunking(unittest.TestCase):
    def test_chunking_is_stable(self):
        """3. 同一文档两次 rebuild 得到相同 chunks（span 与正文都逐字节相同）。"""
        kb = make_kb(Document("ops-db", OPS_DOC, version="3"))
        full_a = [(c.piece.span, c.piece.text) for c in kb._chunks]
        first = pairs(kb.reader().retrieve("数据库", k=99))
        built_a = kb.rebuild()
        full_b = [(c.piece.span, c.piece.text) for c in kb._chunks]
        second = pairs(kb.reader().retrieve("数据库", k=99))
        built_b = kb.rebuild()
        self.assertEqual(full_a, full_b)          # 全量块（含被检索过滤掉的那些）
        self.assertEqual(full_a, _chunk_text(OPS_DOC))
        self.assertEqual(first, second)
        self.assertEqual(built_a, built_b)

        # 另一个进程/实例重建同一文档，span 也必须一模一样（引用不能漂）
        other = make_kb(Document("ops-db", OPS_DOC, version="3"))
        self.assertEqual(pairs(other.reader().retrieve("数据库", k=99)), first)

    def test_span_is_unique_and_deterministic(self):
        """22. 同一文档内 span 唯一，且编号来自标题（3.1 / 3.2 / 3.3）。"""
        kb = make_kb(Document("ops-db", OPS_DOC, version="3"))
        hits = kb.reader().retrieve("连接池 主从切换 回滚 变更", k=99)
        spans = [c.span for c in hits]
        self.assertEqual(len(spans), len(set(spans)))
        self.assertIn("3.2", spans)
        self.assertIn("3.3", spans)

    def test_chunk_size_bounds_and_merge(self):
        """17. 块长：硬上限 600；同节内的短块会被合并，不会切成一堆碎渣。"""
        para = ("回滚脚本必须版本化，禁止手工改表结构。执行前先做一致性快照，"
                "执行后核对行数与校验和，任何一步失败立即停止并保留现场。")
        doc = "\n\n".join(para for _ in range(5))
        chunks = [text for _, text in _chunk_text(doc)]
        self.assertTrue(all(len(t) <= MAX_CHUNK_CHARS for t in chunks), [len(t) for t in chunks])
        self.assertLess(len(chunks), 5, "同节短块应当被合并")
        self.assertTrue(any(len(t) >= MIN_CHUNK_CHARS for t in chunks))

    def test_long_paragraph_is_split(self):
        """18. 单个超长段落按句子边界切，每块 <= 600。"""
        long_para = "。".join(
            f"第{i}步检查数据库复制延迟并记录当前偏移量，确认无异常后继续" for i in range(60)
        ) + "。"
        self.assertGreater(len(long_para), MAX_CHUNK_CHARS * 2)
        chunks = [text for _, text in _chunk_text(long_para)]
        self.assertGreaterEqual(len(chunks), 3)
        self.assertTrue(all(len(t) <= MAX_CHUNK_CHARS for t in chunks))
        self.assertEqual("".join(chunks).replace("\n", ""), long_para)


# ══════════════════════════════════════════════════════════════
# 3. 检索质量
# ══════════════════════════════════════════════════════════════
class TestRetrieval(unittest.TestCase):
    def setUp(self):
        self.kb = make_kb(
            Document("ops-db", OPS_DOC, version="3"),
            Document("net-dns", "域名解析异常时，应用会连到错误的主库地址。"
                                "排查方式：dig 检查解析结果，确认 DNS 缓存已刷新，"
                                "再确认内网解析记录与实际实例地址一致。"),
            Document("cache", "缓存穿透会导致请求直接打到数据库，"
                              "需要用布隆过滤器前置拦截，并对空结果做短 TTL 缓存。"),
        )
        self.reader = self.kb.reader()

    def test_results_sorted_by_score_desc(self):
        """4. 检索排序按 score 降序。"""
        hits = self.reader.retrieve("主从切换 连接 数据库", k=10)
        self.assertGreaterEqual(len(hits), 2)
        scores = [c.score for c in hits]
        self.assertEqual(scores, sorted(scores, reverse=True))
        self.assertTrue(all(0.0 < s <= 1.0 for s in scores), scores)

    def test_citations_complete(self):
        """5. 引用完整：每条 Chunk 都有 doc_id / version / span / source。"""
        hits = self.reader.retrieve("回滚 预案 数据库", k=10)
        self.assertTrue(hits)
        for c in hits:
            self.assertTrue(c.doc_id)
            self.assertEqual(c.version, "3" if c.doc_id == "ops-db" else "1")
            self.assertTrue(c.span)
            self.assertEqual(c.source, "knowledge")
            self.assertEqual(c.kind, "")
            self.assertIsInstance(c.score, float)

    def test_bm25_exact_term_wins(self):
        """8. BM25 生效：查询词精确出现在文中时，该文档排第一且 bm25 拿满分。"""
        kb = make_kb(
            Document("retry", "遇到 503 时使用指数退避重试，初始间隔 200 毫秒，最多三次。"),
            Document("retry-generic", "重试策略统一由客户端库实现，重试时需要按指数间隔放大。"),
        )
        query = "指数退避重试"
        hits = kb.reader().retrieve(query, k=2)
        self.assertEqual(len(hits), 2)                       # 两个候选都过了证据门槛
        self.assertEqual(hits[0].doc_id, "retry")
        self.assertGreater(hits[0].score, hits[1].score)

        rows = kb.reader().explain(query, k=2)
        self.assertEqual(rows[0]["doc_id"], "retry")
        self.assertEqual(rows[0]["bm25_norm"], 1.0)          # 命中者拿到归一化峰值
        self.assertGreater(rows[0]["bm25"], rows[1]["bm25"])
        self.assertGreater(rows[0]["matched_terms"], rows[1]["matched_terms"])
        # 两路都真的参与了：分数 == 0.6*cos + 0.4*bm25_norm（容差取 explain 的四舍五入）
        for row in rows:
            expect = W_VECTOR * row["cos"] + W_BM25 * row["bm25_norm"]
            self.assertAlmostEqual(row["score"], expect, places=5)

    def test_vector_side_recalls_paraphrase(self):
        """9. 向量侧生效：换了说法（原句没出现过）也能召回。"""
        kb = make_kb(
            Document("primary", "主库连不上时，先确认域名解析是否指向新的主库，"
                                "再检查网络策略是否已放通端口。"),
            Document("cache", "缓存穿透需要用布隆过滤器前置拦截，"
                              "避免请求直接打到数据库。"),
        )
        query = "主库连接失败怎么排查"          # 「连不上」≠「连接失败」，无原句重合
        hits = kb.reader().retrieve(query, k=2)
        self.assertEqual(hits[0].doc_id, "primary")
        rows = kb.reader().explain(query, k=2)
        self.assertGreater(rows[0]["cos"], 0.0)               # 余弦侧确实给了分
        self.assertGreater(rows[0]["score"], 0.0)
        self.assertNotIn("连接失败", kb._docs["primary"].text)  # 原句确实不存在

    def test_no_time_decay(self):
        """10. 无时间衰减：400 天前的文档与今天的文档，相关性相同时分数一致。"""
        text = "索引重建期间禁止执行结构变更，避免锁等待堆积。"
        now = now_ms()
        kb = make_kb(
            Document("doc-old", text, version="1", updated_at=now - 400 * DAY_MS),
            Document("doc-new", text, version="1", updated_at=now),
        )
        hits = kb.reader().retrieve("索引重建期间的结构变更", k=2)
        self.assertEqual(len(hits), 2)
        self.assertEqual({c.doc_id for c in hits}, {"doc-old", "doc-new"})
        old = next(c for c in hits if c.doc_id == "doc-old")
        new = next(c for c in hits if c.doc_id == "doc-new")
        self.assertLess(abs(old.score - new.score), 1e-6,
                        "Knowledge 不应有任何时间衰减因子")

        # 把旧文档的时间戳改成今天，分数必须一分不变 → 时间戳完全不进打分
        kb.add(Document("doc-old", text, version="2", updated_at=now))
        hits2 = kb.reader().retrieve("索引重建期间的结构变更", k=2)
        refreshed = next(c for c in hits2 if c.doc_id == "doc-old")
        self.assertLess(abs(refreshed.score - old.score), 1e-6)
        self.assertEqual(refreshed.version, "2")

    def test_empty_query_returns_empty(self):
        """11. 空 query → 空列表，不抛异常。"""
        self.assertEqual(self.reader.retrieve(""), [])
        self.assertEqual(self.reader.retrieve("   \n\t "), [])
        self.assertEqual(self.reader.explain(""), [])

    def test_k_limit(self):
        """12. k 限制生效；k<=0 返回空。"""
        total = len(self.reader.retrieve("数据库", k=99))
        self.assertGreaterEqual(total, 3)
        self.assertEqual(len(self.reader.retrieve("数据库", k=1)), 1)
        self.assertEqual(len(self.reader.retrieve("数据库", k=2)), 2)
        self.assertEqual(self.reader.retrieve("数据库", k=0), [])
        self.assertEqual(self.reader.retrieve("数据库", k=-3), [])

    def test_empty_kb_returns_empty(self):
        """13. 空知识库 retrieve → 空列表，不抛异常。"""
        kb = KnowledgeBase()
        self.assertEqual(kb.reader().retrieve("任何查询"), [])
        self.assertEqual(kb.stats()["docs"], 0)
        self.assertEqual(kb.stats()["chunks"], 0)
        self.assertEqual(kb.rebuild(), {"docs": 0, "chunks": 0, "tokens": 0})

    def test_retrieve_deterministic(self):
        """14. 确定性：同 query 两次 retrieve 结果完全一致（含顺序与分数位）。"""
        q = "主从切换后应用连接失败"
        a = self.reader.retrieve(q, k=10)
        b = self.reader.retrieve(q, k=10)
        self.assertEqual(a, b)
        self.assertEqual([c.score for c in a], [c.score for c in b])
        self.assertEqual(self.reader.explain(q, k=10), self.reader.explain(q, k=10))

    def test_unrelated_query_returns_nothing(self):
        """21. 完全不相关时宁可不召回，也不拿零分段落污染 Context。"""
        self.assertEqual(self.reader.retrieve("偏微分方程的数值解法"), [])


# ══════════════════════════════════════════════════════════════
# 4. 版本驱动
# ══════════════════════════════════════════════════════════════
class TestVersioning(unittest.TestCase):
    def test_version_override(self):
        """6. 同 doc_id 新版本 add 后，retrieve 只返回新内容且 version 是新的。"""
        kb = KnowledgeBase()
        kb.add(Document("runbook", "主从切换前先摘除写入流量。标记 ZETAOLD", version="1"))
        kb.rebuild()
        v1 = kb.reader().retrieve("主从切换", k=5)
        self.assertEqual([c.version for c in v1], ["1"])

        kb.add(Document("runbook", "主从切换前先摘除写入流量。标记 ZETANEW", version="2"))
        v2 = kb.reader().retrieve("主从切换", k=5)
        self.assertTrue(v2)
        self.assertTrue(all(c.version == "2" for c in v2), [c.version for c in v2])
        self.assertTrue(any("ZETANEW" in c.text for c in v2))
        self.assertEqual(kb.rebuild()["chunks"], 1)      # 旧块没有多留一份

    def test_old_version_not_residual(self):
        """7. 旧版本不残留：被覆盖的旧文本无法再被检索出来。"""
        kb = KnowledgeBase()
        kb.add(Document("policy", "旧策略：生产变更无需审批，标记 LEGACYTOKEN", version="1"))
        kb.rebuild()
        self.assertTrue(kb.reader().retrieve("LEGACYTOKEN", k=5))

        kb.add(Document("policy", "新策略：生产变更必须双人审批并留痕。", version="2"))
        self.assertEqual(kb.reader().retrieve("LEGACYTOKEN", k=5), [])
        hits = kb.reader().retrieve("生产变更 审批", k=5)
        self.assertTrue(hits)
        for c in hits:
            self.assertNotIn("LEGACYTOKEN", c.text)
            self.assertNotIn("无需审批", c.text)
            self.assertEqual(c.version, "2")
        self.assertEqual(kb.stats()["chunks"], 1)          # 全库只剩新版这一块

    def test_add_without_rebuild_is_visible(self):
        """补充：写后读一致 —— add 之后不显式 rebuild 也不会读到旧版本。"""
        kb = KnowledgeBase()
        kb.add(Document("d", "第一版内容：备份保留七天。", version="1"))
        kb.rebuild()
        kb.add(Document("d", "第二版内容：备份保留三十天。", version="2"))
        hits = kb.reader().retrieve("备份保留", k=5)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].version, "2")
        self.assertIn("三十天", hits[0].text)


# ══════════════════════════════════════════════════════════════
# 5. 统计与中文语料
# ══════════════════════════════════════════════════════════════
class TestStatsAndChinese(unittest.TestCase):
    def test_stats_counts(self):
        """15. stats 返回正确计数（与 rebuild 返回值口径一致）。"""
        kb = make_kb(
            Document("ops-db", OPS_DOC, version="3"),
            Document("small", "只有一句话的短文档。", version="1"),
        )
        built = kb.rebuild()
        st = kb.stats()
        self.assertEqual(st["docs"], 2)
        self.assertEqual(st["chunks"], built["chunks"])
        self.assertEqual(st["tokens"], built["tokens"])
        self.assertEqual(st["doc_ids"], ("ops-db", "small"))
        self.assertGreater(st["tokens"], 0)
        self.assertGreaterEqual(st["vocab"], 1)
        # reader 看到的统计与写侧一致（只读，但口径必须同源）
        self.assertEqual(kb.reader().stats(), st)

    def test_chinese_ops_doc(self):
        """16. 中文内容正确分块与检索（真实运维文档形态）。"""
        kb = make_kb(Document("ops-db", OPS_DOC, title="数据库运维手册", version="3"))
        chunks = kb.reader().retrieve("数据库 连接池 主从切换 回滚", k=99)
        self.assertEqual(len(chunks), 3)                       # 3.1 / 3.2 / 3.3
        self.assertEqual(sorted(c.span for c in chunks), ["3.1", "3.2", "3.3"])
        for c in chunks:
            self.assertLessEqual(len(c.text), MAX_CHUNK_CHARS)
            self.assertNotIn("\n\n", c.text)

        first = kb.reader().retrieve("主从切换完成后应用连不上怎么办", k=1)
        self.assertEqual(first[0].span, "3.2")
        self.assertIn("主从切换", first[0].text)

        rollback = kb.reader().retrieve("回滚要保留现场日志吗", k=1)
        self.assertEqual(rollback[0].span, "3.3")

        pool = kb.reader().retrieve("连接池耗尽表现为接口超时", k=1)
        self.assertEqual(pool[0].span, "3.1")


if __name__ == "__main__":
    unittest.main(verbosity=2)
