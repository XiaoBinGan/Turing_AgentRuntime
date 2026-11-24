"""切分器 SPI：默认零依赖实现 + LlamaIndex 可选实现，语义一致可换。"""
from __future__ import annotations

import unittest

from plane.splitters import (
    HeadingSplitter, LlamaIndexSplitter, configure_splitter, make_splitter,
    splitter_available,
)

TEXT = ("## 3.1 连接池\n连接池默认上限 20。并发超过 150 会 acquire timeout。"
        * 8 + "\n\n## 3.2 灰度\n灰度发布先过全量回归。" * 5)


class TestHeadingSplitter(unittest.TestCase):
    def setUp(self):
        self.s = HeadingSplitter()

    def test_respects_max_chars(self):
        for c in self.s.split(TEXT, max_chars=300):
            self.assertLessEqual(len(c), 300 + 60)   # 允许句子边界轻微超出

    def test_deterministic(self):
        a = self.s.split(TEXT, max_chars=300)
        b = HeadingSplitter().split(TEXT, max_chars=300)
        self.assertEqual(a, b)

    def test_short_text_single_chunk(self):
        self.assertEqual(self.s.split("一句话。"), ["一句话。"])

    def test_empty_text(self):
        self.assertEqual(self.s.split(""), [])

    def test_whitespace_only(self):
        self.assertEqual(self.s.split("   \n\n  "), [])

    def test_long_single_sentence_hard_split(self):
        chunks = self.s.split("甲" * 2000, max_chars=300)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(c) <= 300 for c in chunks))

    def test_merges_short_blocks(self):
        """短块向后合并 —— 避免「3 数据库手册」这种垃圾块。"""
        chunks = self.s.split("短。\n\n## 2 下一节\n" + "内容。" * 60, max_chars=600)
        self.assertLess(len(chunks), 4)

    def test_heading_boundaries(self):
        chunks = self.s.split(TEXT, max_chars=300)
        self.assertTrue(any("连接池" in c for c in chunks))
        self.assertTrue(any("灰度" in c for c in chunks))


class TestFactory(unittest.TestCase):
    def test_default_is_heading(self):
        self.assertIsInstance(make_splitter(""), HeadingSplitter)
        self.assertIsInstance(make_splitter("heading"), HeadingSplitter)
        self.assertIsInstance(make_splitter("auto"), HeadingSplitter)

    def test_unknown_raises_with_options(self):
        with self.assertRaises(ValueError) as ctx:
            make_splitter("semantic-nope")
        self.assertIn("heading", str(ctx.exception))

    def test_availability_reports_reason(self):
        info = splitter_available()
        self.assertEqual(set(info), {"heading", "llama-index"})
        for row in info.values():
            self.assertTrue(row["reason"])


@unittest.skipUnless(splitter_available()["llama-index"]["available"],
                     "llama-index-core 未安装")
class TestLlamaIndexSplitter(unittest.TestCase):
    def setUp(self):
        self.s = LlamaIndexSplitter(max_chars=300, overlap=60)

    def test_returns_chunks(self):
        chunks = self.s.split(TEXT, max_chars=300)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(c.strip() for c in chunks))

    def test_deterministic(self):
        self.assertEqual(self.s.split(TEXT, max_chars=300),
                         LlamaIndexSplitter(max_chars=300, overlap=60).split(TEXT, max_chars=300))

    def test_overlap_produces_context(self):
        """重叠窗口：相邻块应有共享内容（这是它的价值）。"""
        chunks = self.s.split(TEXT, max_chars=200)
        if len(chunks) >= 2:
            tail = chunks[0][-20:]
            self.assertTrue(any(tail[:8] in c for c in chunks[1:]))

    def test_respects_size_roughly(self):
        for c in self.s.split(TEXT, max_chars=300):
            self.assertLess(len(c), 300 * 2.5)

    def test_empty_text(self):
        self.assertEqual(self.s.split(""), [])

    def test_factory_creates_it(self):
        self.assertIsInstance(make_splitter("llama-index", max_chars=300),
                              LlamaIndexSplitter)


class TestEnvConfig(unittest.TestCase):
    def test_default_heading(self):
        cfg = configure_splitter({})
        self.assertEqual(cfg["name"], "heading")
        self.assertEqual(cfg["reason"], "ok")

    def test_explicit_heading(self):
        cfg = configure_splitter({"SPLITTER": "heading", "SPLITTER_MAX_CHARS": "300"})
        self.assertIsNotNone(cfg["splitter"])

    def test_llama_requested_but_missing_reports_reason(self):
        """没装 llama-index 却配置了它 —— 必须说清原因，不能静默用 heading。"""
        cfg = configure_splitter({"SPLITTER": "llama-index"})
        if splitter_available()["llama-index"]["available"]:
            self.assertEqual(cfg["name"], "llama-index")
        else:
            self.assertIsNone(cfg["splitter"])
            self.assertIn("llama", cfg["reason"].lower())


if __name__ == "__main__":
    unittest.main(verbosity=2)
