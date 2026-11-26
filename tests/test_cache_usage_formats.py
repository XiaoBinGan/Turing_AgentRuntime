"""`usage` 里缓存命中的三种字段格式。

**为什么需要它**：不同厂商把 cache hit 放在不同位置，而"读不到"的表现不是
报错，是**当成没命中按全价计费** —— 静默算错钱，未必有人会发现。

| 厂商 | 路径 |
|---|---|
| DeepSeek | `usage.prompt_cache_hit_tokens` |
| OpenAI Chat Completions | `usage.prompt_tokens_details.cached_tokens` |
| OpenAI Responses | `usage.input_tokens_details.cached_tokens` |
| Anthropic | `usage.cache_read_input_tokens` |

早先只读顶层，OpenAI 官方格式被完全忽略：2006 prompt / 1920 cached
会算成 `input_equivalent=2006` 而不是 `278`，差价 7 倍。
"""
from __future__ import annotations

import unittest

from adapters.openai_compat import OpenAICompatAdapter


def _adapter(ratio: float = 0.1) -> OpenAICompatAdapter:
    return OpenAICompatAdapter(base_url="http://127.0.0.1:1/v1", model="m",
                               cache_hit_ratio=ratio)


class TestCacheFieldFormats(unittest.TestCase):
    def test_openai_chat_completions(self):
        u = _adapter()._usage({
            "prompt_tokens": 2006, "completion_tokens": 300,
            "prompt_tokens_details": {"cached_tokens": 1920},
        }, "p", [], "t", "m")
        self.assertEqual(u.cache_hit_tokens, 1920)
        b = u.billable(cache_hit_ratio=0.1)
        # 1920 次命中按 0.1 计价 = 192，加 86 次未命中 = 278
        self.assertEqual(b["input_equivalent"], 278)

    def test_openai_responses_api(self):
        u = _adapter()._usage({
            "input_tokens": 1000, "output_tokens": 50,
            "input_tokens_details": {"cached_tokens": 800},
        }, "p", [], "t", "m")
        self.assertEqual(u.cache_hit_tokens, 800)

    def test_deepseek_top_level(self):
        u = _adapter()._usage({
            "prompt_tokens": 1000, "completion_tokens": 50,
            "prompt_cache_hit_tokens": 600,
        }, "p", [], "t", "m")
        self.assertEqual(u.cache_hit_tokens, 600)

    def test_anthropic_style(self):
        u = _adapter()._usage({
            "input_tokens": 1000, "output_tokens": 50,
            "cache_read_input_tokens": 400,
        }, "p", [], "t", "m")
        self.assertEqual(u.cache_hit_tokens, 400)

    def test_no_cache_reported(self):
        u = _adapter()._usage({"prompt_tokens": 1000, "completion_tokens": 50},
                              "p", [], "t", "m")
        self.assertEqual(u.cache_hit_tokens, 0)

    def test_null_details(self):
        """有的实现给 `prompt_tokens_details: null`（vLLM 就是这样）。"""
        u = _adapter()._usage({
            "prompt_tokens": 100, "completion_tokens": 5,
            "prompt_tokens_details": None,
        }, "p", [], "t", "m")
        self.assertEqual(u.cache_hit_tokens, 0)

    def test_empty_details_dict(self):
        u = _adapter()._usage({
            "prompt_tokens": 100, "prompt_tokens_details": {},
        }, "p", [], "t", "m")
        self.assertEqual(u.cache_hit_tokens, 0)

    def test_total_vs_billable_are_different(self):
        """缓存命中**占窗口**但**不算全价** —— 两个口径必须分开。"""
        u = _adapter()._usage({
            "prompt_tokens": 10000, "completion_tokens": 500,
            "prompt_tokens_details": {"cached_tokens": 8000},
        }, "p", [], "t", "m")
        self.assertEqual(u.total, 10500, "窗口口径要把缓存算进去")
        self.assertEqual(u.billable(cache_hit_ratio=0.1)["input_equivalent"], 2800,
                         "计费口径要把缓存打折")


if __name__ == "__main__":
    unittest.main(verbosity=2)
