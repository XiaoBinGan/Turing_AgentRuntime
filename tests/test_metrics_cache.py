"""从 vLLM 的 `/metrics` 读缓存命中（`scrape_prefix_cache_tokens`）。

**为什么需要它**：vLLM 0.32 的 usage API **完全不报缓存**。实测
（`Qwen3-VL-Embedding-2B` + `--enable-prefix-caching`，同一 prompt 连发）：

    /metrics  prefix_cache_hits_total     0 → 1248
    /metrics  prompt_tokens_cached_total  1856
    usage.prompt_tokens_details           null        ← 关键

缓存真的命中了，但 `Usage.cache_hit_tokens` 拿不到，`billable()` 算不出折扣。

**为什么不自动**：请求前后各抓一次 metrics 求差值，在并发下无法归属到具体
请求，只能串行化那个窗口 —— 那会把高并发吞吐压垮。所以只提供累计值，
由调用方按时间窗口求增量。

实测（243 上的 vLLM，真实服务）：

    抓取前 2464 → 抓取后 3072，本次增量 608
    billable 全价 624 → 用增量后 77（省 87.7%）
"""
from __future__ import annotations

import unittest
from unittest import mock

from adapters.openai_compat import scrape_prefix_cache_tokens

METRICS = """# HELP vllm:prompt_tokens_cached_total Cached tokens
# TYPE vllm:prompt_tokens_cached_total counter
vllm:prompt_tokens_cached_total{engine="0",model_name="vlchat"} 1856.0
vllm:prompt_tokens_by_source_total{engine="0",model_name="vlchat",source="local_cache_hit"} 1856.0
vllm:num_requests_running{engine="0",model_name="vlchat"} 0.0
"""


class _FakeResp:
    def __init__(self, text: str) -> None:
        self._t = text

    def read(self) -> bytes:
        return self._t.encode()

    def __enter__(self):
        return self

    def __exit__(self, *a) -> None:
        return None


class TestScrape(unittest.TestCase):
    def _patch(self, text: str):
        return mock.patch("urllib.request.urlopen",
                          return_value=_FakeResp(text))

    def test_reads_total(self) -> None:
        with self._patch(METRICS):
            self.assertEqual(
                scrape_prefix_cache_tokens("http://x/metrics"), 1856)

    def test_filters_by_model(self) -> None:
        with self._patch(METRICS):
            self.assertEqual(
                scrape_prefix_cache_tokens("http://x/metrics",
                                           model_name="vlchat"), 1856)
            self.assertEqual(
                scrape_prefix_cache_tokens("http://x/metrics",
                                           model_name="other"), 0)

    def test_sums_multiple_engines(self) -> None:
        two = ('vllm:prompt_tokens_cached_total{engine="0",model_name="a"} 100\n'
               'vllm:prompt_tokens_cached_total{engine="1",model_name="a"} 250\n')
        with self._patch(two):
            self.assertEqual(
                scrape_prefix_cache_tokens("http://x/metrics"), 350)

    def test_empty_url_is_zero(self) -> None:
        """空 url 不抓，直接 0 —— 不联网、不报错。"""
        self.assertEqual(scrape_prefix_cache_tokens(""), 0)
        self.assertEqual(scrape_prefix_cache_tokens(None), 0)  # type: ignore[arg-type]

    def test_unreachable_is_zero(self) -> None:
        """**fail-open**：抓不到不能阻断业务，观测面永远不该成为单点。"""
        self.assertEqual(
            scrape_prefix_cache_tokens("http://127.0.0.1:1/metrics",
                                       timeout_s=0.5), 0)

    def test_missing_metric_is_zero(self) -> None:
        with self._patch("vllm:num_requests_running 3\n"):
            self.assertEqual(
                scrape_prefix_cache_tokens("http://x/metrics"), 0)

    def test_garbage_line_skipped(self) -> None:
        bad = ('vllm:prompt_tokens_cached_total garbage\n'
               'vllm:prompt_tokens_cached_total{model_name="a"} 42\n')
        with self._patch(bad):
            self.assertEqual(
                scrape_prefix_cache_tokens("http://x/metrics"), 42)

    def test_comments_ignored(self) -> None:
        with self._patch(METRICS):
            # HELP/TYPE 行里有指标名，不能被当成数据
            self.assertEqual(
                scrape_prefix_cache_tokens("http://x/metrics"), 1856)


class TestBillableWithScraped(unittest.TestCase):
    def test_discount_applied(self) -> None:
        """把 metrics 增量塞进 Usage 后，折扣确实生效。"""
        from contracts import Usage
        u = Usage(input_tokens=624, output_tokens=8, cache_hit_tokens=608)
        self.assertEqual(u.billable(cache_hit_ratio=0.1)["input_equivalent"], 77)

    def test_no_scraped_means_full_price(self) -> None:
        from contracts import Usage
        u = Usage(input_tokens=624, output_tokens=8, cache_hit_tokens=0)
        self.assertEqual(u.billable(cache_hit_ratio=0.1)["input_equivalent"], 624)


if __name__ == "__main__":
    unittest.main(verbosity=2)
