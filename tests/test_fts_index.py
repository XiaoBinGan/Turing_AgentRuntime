"""FTS5 索引的验收：换索不换结果。"""
from __future__ import annotations

import unittest

from plane.fts_index import (
    FTS5Index, FTS5Unavailable, bigram_terms, fts_available, fts_knowledge_base,
)

DOCS = [
    ("d0#p1", "连接池默认上限 20，并发超过 150 会 acquire timeout"),
    ("d1#p1", "灰度发布必须先过全量回归，再逐步放量"),
    ("d2#p1", "新人入职第一天要装 uv 和 python，并申请仓库权限"),
    ("d3#p1", "日志盘占用 92%，按当前增速 6 天写满"),
]


@unittest.skipUnless(fts_available(), "当前 SQLite 不支持 FTS5")
class TestBigramTerms(unittest.TestCase):
    def test_ascii_whole_words(self):
        self.assertEqual(bigram_terms("acquire timeout"), ["acquire", "timeout"])

    def test_cjk_bigrams(self):
        self.assertEqual(bigram_terms("连接池"), ["连接", "接池"])

    def test_mixed(self):
        self.assertEqual(bigram_terms("uv 权限"), ["uv", "权限"])

    def test_empty(self):
        self.assertEqual(bigram_terms(""), [])

    def test_single_cjk_char_is_kept(self):
        """单字也要能检索 —— 退回原字符而不是丢空。"""
        self.assertEqual(bigram_terms("池"), ["池"])


@unittest.skipUnless(fts_available(), "当前 SQLite 不支持 FTS5")
class TestFTS5Index(unittest.TestCase):
    def setUp(self) -> None:
        self.idx = FTS5Index()
        self.idx.build(DOCS)

    def tearDown(self) -> None:
        self.idx.close()

    def test_build_reports_count(self):
        self.assertEqual(self.idx.stats()["docs"], 4)

    def test_exact_term_wins(self):
        hits = self.idx.search("连接池上限")
        self.assertEqual(hits[0][0], "d0#p1")

    def test_search_returns_descending(self):
        hits = self.idx.search("发布 回归 灰度")
        scores = [s for _, s in hits]
        self.assertEqual(scores, sorted(scores, reverse=True))

    def test_best_hit_normalizes_to_one(self):
        hits = self.idx.search("连接池上限")
        self.assertAlmostEqual(hits[0][1], 1.0, places=6)

    def test_unrelated_query_returns_nothing(self):
        self.assertEqual(self.idx.search("量子纠缠与超导"), [])

    def test_empty_query(self):
        self.assertEqual(self.idx.search(""), [])
        self.assertEqual(self.idx.search("   "), [])

    def test_rebuild_is_idempotent(self):
        before = self.idx.search("连接池")
        self.idx.build(DOCS)
        after = self.idx.search("连接池")
        self.assertEqual(before, after)
        self.assertEqual(self.idx.stats()["docs"], 4)

    def test_rebuild_replaces_content(self):
        """版本驱动语义：重建后旧内容必须消失。"""
        self.idx.build([("d0#p1", "连接池默认上限已调整为 200")])
        self.assertEqual(self.idx.stats()["docs"], 1)
        # 旧内容里独有的词（acquire / timeout）在重建后必须消失
        self.assertEqual(self.idx.search("acquire timeout"), [])
        # 新内容仍可检索
        self.assertEqual(self.idx.search("连接池上限")[0][0], "d0#p1")

    def test_ascii_term(self):
        hits = self.idx.search("acquire")
        self.assertEqual(hits[0][0], "d0#p1")

    def test_prefix_and_quotes_do_not_inject(self):
        # 引号与 FTS 运算符不该让查询炸掉
        for bad in ('"', 'a OR b', "NEAR(a b)", "*"):
            self.idx.search(bad)          # 不抛即通过

    def test_stats_shape(self):
        st = self.idx.stats()
        for k in ("backend", "docs", "built", "sqlite"):
            self.assertIn(k, st)
        self.assertEqual(st["backend"], "sqlite-fts5")

    def test_determinism(self):
        a = self.idx.search("回归 灰度 发布")
        b = self.idx.search("回归 灰度 发布")
        self.assertEqual(a, b)


@unittest.skipUnless(fts_available(), "当前 SQLite 不支持 FTS5")
class TestKnowledgeBaseWiring(unittest.TestCase):
    def _kb(self):
        from plane.knowledge import Document, KnowledgeBase
        KB = fts_knowledge_base(KnowledgeBase)
        kb = KB()
        kb.add(Document(doc_id="runbook", version="1", title="运维手册",
                        text="连接池默认上限 20。\n\n灰度发布必须先过全量回归。"))
        kb.rebuild()
        return kb

    def test_subclass_still_works(self):
        kb = self._kb()
        hits = kb.reader().retrieve("连接池上限", k=2)
        self.assertTrue(hits)
        self.assertTrue(all(h.doc_id and h.version for h in hits))

    def test_fts_stats_exposed(self):
        kb = self._kb()
        st = kb.fts_stats()
        self.assertEqual(st["backend"], "sqlite-fts5")
        self.assertGreater(st["docs"], 0)

    def test_bm25_norm_range(self):
        kb = self._kb()
        scores = kb.bm25_norm("连接池上限 灰度发布")
        self.assertTrue(scores)
        self.assertLessEqual(max(scores.values()), 1.0)
        self.assertGreaterEqual(min(scores.values()), 0.0)

    def test_read_only_facade_preserved(self):
        """INV-11：换索引不能打开写通道。"""
        r = self._kb().reader()
        self.assertFalse(any(hasattr(r, m) for m in ("add", "write", "delete")))

    def test_version_override_still_wins(self):
        from plane.knowledge import Document
        kb = self._kb()
        kb.add(Document(doc_id="runbook", version="2", title="运维手册",
                        text="连接池默认上限已调整为 200。\n\n灰度发布流程不变。"))
        kb.rebuild()
        hits = kb.reader().retrieve("连接池上限", k=2)
        self.assertTrue(hits)
        self.assertFalse(any("默认上限 20。" in h.text for h in hits))


class TestAvailabilityProbe(unittest.TestCase):
    def test_probe_returns_bool(self):
        self.assertIsInstance(fts_available(), bool)

    def test_unavailable_error_type_exists(self):
        self.assertTrue(issubclass(FTS5Unavailable, RuntimeError))


if __name__ == "__main__":
    unittest.main(verbosity=2)
