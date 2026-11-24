"""token 计量 SPI 的验收：按模型选对计量器，比无脑用 tiktoken 准。"""
from __future__ import annotations

import unittest

from adapters.tokenizer import (
    CountOnlyTokenizer, HeuristicTokenizer, TiktokenTokenizer, describe, for_model,
)
from builder import PromptBuilder, estimate_tokens
from contracts import ContextSnapshot

ZH = "生产环境连接池耗尽导致请求排队等待，首字节延迟升高。" * 15   # 实测 235~256 tok
EN = " ".join(["system"] * 200)                                  # 实测 200~230 tok


class TestSelection(unittest.TestCase):
    def test_openai_models_use_tiktoken(self):
        self.assertIsInstance(for_model("gpt-4o"), TiktokenTokenizer)
        self.assertIsInstance(for_model("o3-mini"), TiktokenTokenizer)
        self.assertIsInstance(for_model("text-embedding-3-large"), TiktokenTokenizer)

    def test_non_openai_models_use_heuristic(self):
        for m in ("qwen3.5-plus", "GLM-5.2", "deepseek-flash",
                  "gemini-3.8-flash", "claude-opus-4-8", "kimi-k3.1-pro"):
            self.assertIsInstance(for_model(m), HeuristicTokenizer, m)

    def test_unknown_model_falls_back_to_heuristic(self):
        self.assertIsInstance(for_model(""), HeuristicTokenizer)
        self.assertIsInstance(for_model("some-unknown-model"), HeuristicTokenizer)

    def test_describe_reports_dependencies(self):
        d = describe()
        self.assertIn("tiktoken", d)
        self.assertIn("note", d)


class TestAccuracy(unittest.TestCase):
    def test_heuristic_matches_real_for_cjk(self):
        """标定启发式对中文的误差应在 15% 内（实测真实值 235~256）。"""
        n = HeuristicTokenizer().count(ZH)
        self.assertLess(abs(n - 250) / 250, 0.15, f"{n} 偏离太远")

    def test_heuristic_matches_real_for_ascii(self):
        n = HeuristicTokenizer().count(EN)
        self.assertLess(abs(n - 215) / 215, 0.15, f"{n} 偏离太远")

    def test_tiktoken_is_accurate_for_ascii(self):
        n = TiktokenTokenizer().count(EN)
        self.assertLess(abs(n - 215) / 215, 0.12)

    def test_tiktoken_overcounts_cjk_vs_real(self):
        """这是选择启发式的**原因**：中文场景 tiktoken 高估近 2 倍。"""
        t = TiktokenTokenizer().count(ZH)
        h = HeuristicTokenizer().count(ZH)
        self.assertGreater(t, h * 1.6, f"tiktoken={t} 启发式={h}")

    def test_empty_text_is_zero(self):
        for tk in (HeuristicTokenizer(), CountOnlyTokenizer()):
            self.assertEqual(tk.count(""), 0)


class TestBuilderIntegration(unittest.TestCase):
    def _snap(self, hist_chars: int = 3000):
        return ContextSnapshot(1, {
            "history": [{"role": "user", "text": "z" * hist_chars}],
            "phase": "EXECUTING",
        })

    def test_tokenizer_overrides_profile(self):
        """给了 tokenizer 就用它，而不是 profile 启发式。"""
        b = PromptBuilder(total_tokens=500, tokenizer=CountOnlyTokenizer())
        out = b.build(self._snap(), [])
        # 1 字符 = 1 token 的计量器下，3000 字符的 history 必然被大幅裁剪
        self.assertLess(len(out), 3000)

    def test_no_tokenizer_keeps_old_behavior(self):
        a = PromptBuilder(total_tokens=500)
        b = PromptBuilder(total_tokens=500, tokenizer=None)
        self.assertEqual(a.build(self._snap(), []), b.build(self._snap(), []))

    def test_broken_tokenizer_falls_back(self):
        class Boom:
            name = "boom"
            exact = False

            def count(self, text):
                raise RuntimeError("boom")

        out = PromptBuilder(total_tokens=500, tokenizer=Boom()).build(self._snap(), [])
        # 没崩、产出了 Prompt（退回 profile 启发式）；默认模板 system 是空串，
        # 所以这里断言 HISTORY 段存在，而不是 SYSTEM 段。
        self.assertTrue(out.strip())
        self.assertIn("<<<HISTORY>>>", out)

    def test_count_matches_estimate_without_tokenizer(self):
        b = PromptBuilder()
        self.assertEqual(b._count(ZH), estimate_tokens(ZH, b.profile))


if __name__ == "__main__":
    unittest.main(verbosity=2)
