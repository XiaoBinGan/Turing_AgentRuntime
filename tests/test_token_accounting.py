"""token 记账：估算档、窗口口径 vs 计费口径、压缩不切孤儿。

这三条都是「文档/直觉说一件事，代码做另一件事」的坑：

  1. `DEFAULT_PROFILE` 刻意高估（CJK 1.0 tok/字），实测是 0.638。
     生产路径若用它，纯中文预算高估 1.57×，白扔 36%。
  2. `Usage.total` 是**窗口**口径（缓存命中仍占上下文），
     不能拿它算钱 —— 缓存按折扣价计。用一个字段喂两个用途必有一边错。
  3. 压缩按索引盲切会切出"没有来由的 tool 输出"。实测 59.2% 概率。
"""
from __future__ import annotations

import random
import unittest

from builder import CALIBRATED, DEFAULT_PROFILE, estimate_tokens
from contracts import Usage
from core.summary import HistoryCompressor, _safe_cut


class TestCalibratedProfile(unittest.TestCase):
    def test_default_overestimates_cjk(self):
        """记录事实：DEFAULT_PROFILE 是保守档，确实高估。"""
        zh = "线上服务响应变慢需要排查根因并给出结论" * 10
        est_default = estimate_tokens(zh, DEFAULT_PROFILE)
        est_cal = estimate_tokens(zh, CALIBRATED)
        self.assertGreater(est_default, est_cal,
                           "保守档应当高于标定档")
        self.assertAlmostEqual(est_default / est_cal, 1.47, delta=0.15)

    def test_serve_uses_calibrated_by_default(self):
        """**核心**：生产路径必须用标定档，不是保守档。"""
        from serve import build_gateway_from_env
        gw = build_gateway_from_env(env={})
        loop = gw.loop_factory("probe", None)
        self.assertAlmostEqual(loop._builder.profile.cjk_per_token,
                               CALIBRATED.cjk_per_token, places=9)

    def test_conservative_switch_works(self):
        from serve import build_gateway_from_env
        gw = build_gateway_from_env(env={"AGENT_RUNTIME_TOKEN_PROFILE": "conservative"})
        loop = gw.loop_factory("probe", None)
        self.assertAlmostEqual(loop._builder.profile.cjk_per_token,
                               DEFAULT_PROFILE.cjk_per_token, places=9)

    def test_calibration_saves_budget(self):
        """量化收益：同样预算下，标定档能装更多中文。"""
        zh = "线上服务响应变慢需要排查根因并给出结论" * 10
        self.assertLess(estimate_tokens(zh, CALIBRATED),
                        estimate_tokens(zh, DEFAULT_PROFILE) * 0.75)


class TestWindowVsBillable(unittest.TestCase):
    def test_total_counts_cache_as_window(self):
        """`total` 是窗口口径：缓存命中仍然占上下文。"""
        u = Usage(input_tokens=1000, output_tokens=100, cache_hit_tokens=800)
        self.assertEqual(u.total, 1100)

    def test_billable_discounts_cache(self):
        u = Usage(input_tokens=1000, output_tokens=100, cache_hit_tokens=800)
        bill = u.billable(cache_hit_ratio=0.1)
        self.assertEqual(bill["input_miss"], 200)
        self.assertEqual(bill["input_hit"], 800)
        self.assertEqual(bill["input_equivalent"], 280)   # 200 + 800*0.1

    def test_billable_clamps_absurd_cache(self):
        """缓存命中数超过输入数（上游会返这种脏数据）→ 钳住，不当负数。"""
        u = Usage(input_tokens=100, output_tokens=10, cache_hit_tokens=999)
        bill = u.billable()
        self.assertEqual(bill["input_miss"], 0)
        self.assertEqual(bill["input_hit"], 100)

    def test_zero_cache_equals_full_price(self):
        u = Usage(input_tokens=1000, output_tokens=100, cache_hit_tokens=0)
        self.assertEqual(u.billable()["input_equivalent"], 1000)

    def test_cost_of_accounts_for_cache(self):
        """适配器的成本函数必须真的按缓存打折。"""
        from adapters.openai_compat import OpenAICompatAdapter
        a = OpenAICompatAdapter.__new__(OpenAICompatAdapter)
        a.price_in_per_m = 1.0
        a.price_out_per_m = 2.0
        a.cache_hit_ratio = 0.1
        full = a.cost_of(1_000_000, 0, 0)
        half_hit = a.cost_of(1_000_000, 0, 500_000)
        self.assertAlmostEqual(full, 1.0, places=6)
        self.assertAlmostEqual(half_hit, 0.55, places=6)   # 0.5 + 0.5*0.1


class TestCompactionCut(unittest.TestCase):
    def test_never_starts_tail_with_tool(self):
        hist = [{"role": "user"}, {"role": "assistant"}, {"role": "tool"},
                {"role": "assistant"}, {"role": "tool"}, {"role": "user"}]
        for want in range(len(hist) + 1):
            tail = hist[_safe_cut(hist, want):]
            if tail:
                self.assertNotEqual(tail[0]["role"], "tool",
                                    f"want={want} 切出了孤儿")

    def test_fuzz_no_orphans(self):
        """随机真实形态：assistant + 1~3 个 tool 结果。"""
        random.seed(7)
        orphans = 0
        for _ in range(500):
            hist = [{"role": "user"}]
            for _ in range(random.randint(3, 12)):
                hist.append({"role": "assistant"})
                for _ in range(random.randint(1, 3)):
                    hist.append({"role": "tool"})
                if random.random() < 0.3:
                    hist.append({"role": "user"})
            cut = _safe_cut(hist, max(1, len(hist) - 8))
            tail = hist[cut:]
            if tail and tail[0]["role"] == "tool":
                orphans += 1
        self.assertEqual(orphans, 0)

    def test_cut_is_monotone_and_bounded(self):
        hist = [{"role": "tool"}] * 5
        self.assertEqual(_safe_cut(hist, 3), 0, "全是 tool 时只能退到 0")
        self.assertEqual(_safe_cut(hist, 0), 0)
        self.assertEqual(_safe_cut(hist, 99), 5, "want 超界要钳住")

    def test_compress_output_has_no_orphan(self):
        hist = [{"role": "user", "text": "go"}]
        for i in range(8):
            hist.append({"role": "assistant", "text": f"[CALL] t{i}"})
            hist.append({"role": "tool", "tool": f"t{i}", "kind": "ok", "text": "r"})
        c = HistoryCompressor(max_keep=5)
        res = c.compress(hist)
        self.assertGreater(res.compressed_count, 0)
        tail = hist[_safe_cut(hist, len(hist) - 5):]
        self.assertNotEqual(tail[0]["role"], "tool")


if __name__ == "__main__":
    unittest.main(verbosity=2)
