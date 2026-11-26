"""推理模型的思维链剥离。

**为什么需要它**：Qwen3.5 这类推理模型会在 `content` 里返回
`思维链</think>答案`，而 `message["reasoning"]` 是 None（服务端没配
`--reasoning-parser` 时不会单独给字段）。

不剥的后果不是"多显示几行"：

1. 思维链被当成最终答案写进 history，下一轮模型看到自己的独白当前文，判断会漂
2. 用户读到的是"我们需要回答用户……"这种内心独白
3. 思维链里的 token 已经付过钱，但把它当答案会让"回合文本"失去检索价值

实测（vllm + Qwen3.8-27B + --tool-call-parser qwen3_xml）：

    content = "用户问'2+2等于几' 需要只回答数字。答案是4。\\n</think>\\n\\n4"

剥掉之后是 `"4"`。
"""
from __future__ import annotations

import unittest

from adapters.openai_compat import OpenAICompatAdapter


class TestStripReasoning(unittest.TestCase):
    def test_marker_then_answer(self):
        got = OpenAICompatAdapter._strip_reasoning(
            "We need answer.\n</think>\n\n4")
        self.assertEqual(got, "4")

    def test_multiline_reasoning(self):
        got = OpenAICompatAdapter._strip_reasoning(
            "多行\n思考\n</think>\n\n最终答案在这里")
        self.assertEqual(got, "最终答案在这里")

    def test_no_marker_untouched(self):
        """没有标记的模型（普通 chat 模型）文本必须原样保留。"""
        text = "p99 是第 99 百分位延迟。"
        self.assertEqual(OpenAICompatAdapter._strip_reasoning(text), text)

    def test_empty_marker_tail_keeps_original(self):
        """标记之后为空 → **保留原文**。

        推理吃光 max_tokens 时 `content` 可能只剩标记前的内容。
        剥掉会得到空字符串，把"模型没答完"变成"模型答了空"，
        那是更糟的失败。
        """
        text = "只有标记结尾 a</think>"
        self.assertEqual(OpenAICompatAdapter._strip_reasoning(text), text)

    def test_marker_at_start(self):
        got = OpenAICompatAdapter._strip_reasoning("</think>\n\n直接答案")
        self.assertEqual(got, "直接答案")

    def test_alternative_markers(self):
        for marker in ("</thinking>", "[/THINK]"):
            with self.subTest(marker=marker):
                got = OpenAICompatAdapter._strip_reasoning(f"想{marker}答案")
                self.assertEqual(got, "答案")

    def test_none_safe(self):
        self.assertEqual(OpenAICompatAdapter._strip_reasoning(""), "")

    def test_default_on(self):
        a = OpenAICompatAdapter(base_url="http://127.0.0.1:1/v1", model="m")
        self.assertTrue(a.strip_reasoning, "默认应当剥离")

    def test_can_disable(self):
        a = OpenAICompatAdapter(base_url="http://127.0.0.1:1/v1", model="m",
                                strip_reasoning=False)
        self.assertFalse(a.strip_reasoning)


if __name__ == "__main__":
    unittest.main(verbosity=2)
