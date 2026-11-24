"""``core/summary.py`` 的验收测试（Summary Memory / 滚动摘要）。

设计要点：全部用 ``unittest``（本机无 pytest），只依赖标准库 + ``contracts``。
``apply_to_context`` 的测试用 **fake ctx**（只实现 ``snapshot`` / ``apply``）验证鸭子
类型契约，另有一条用真 ``ContextHandle`` 做集成（owner 校验、patch_log、回读一致）。

跑法：
    python3 -m unittest tests.test_summary -v
"""

from __future__ import annotations

import copy
import json
import unittest

from contracts import Owner, Patch
from core.summary import (
    ENTRY_BULLET,
    HEADER_FORMAT,
    MAX_ENTRY_CHARS,
    OMITTED_FORMAT,
    REASON_KEEP_CHARS,
    HistoryCompressor,
    SummaryResult,
    apply_to_context,
    render_entry,
    summary_section,
)


# ══════════════════════════════════════════════════════════════
# 测试夹具
# ══════════════════════════════════════════════════════════════
def user(text: str) -> dict:
    return {"role": "user", "text": text}


def assistant(text: str, tool_calls=None) -> dict:
    msg = {"role": "assistant", "text": text}
    if tool_calls:
        msg["tool_calls"] = tool_calls
    return msg


def tool_ok(name: str, text: str = "ok") -> dict:
    return {"role": "tool", "tool": name, "kind": "ok", "text": text}


def tool_failed(name: str, reason: str) -> dict:
    return {"role": "tool", "tool": name, "kind": "failed", "reason": reason,
            "text": f"工具 {name} 失败：{reason}"}


def lines_of(summary: str) -> list[str]:
    """摘要正文行（去掉首行计数与省略标记）。"""
    out = []
    for line in summary.split("\n"):
        s = line.strip()
        if not s or s.startswith("【历史摘要"):
            continue
        if s.startswith("…（更早"):
            continue
        out.append(s)
    return out


class FakeCtx:
    """只满足 ``snapshot()`` / ``apply()`` 的鸭子 Context，用来钉契约。"""

    def __init__(self, data: dict | None = None) -> None:
        self.data = copy.deepcopy(data or {})
        self.patches: list[Patch] = []

    def snapshot(self):
        class _Snap:
            def __init__(self, data: dict) -> None:
                self.version = 0
                self.data = copy.deepcopy(data)

            def get(self, path, default=None):
                cur = self.data
                for seg in path.split("."):
                    if not isinstance(cur, dict) or seg not in cur:
                        return default
                    cur = cur[seg]
                return cur

        return _Snap(self.data)

    def apply(self, patch):  # 只接受 set，且必须写 LOOP 字段
        assert isinstance(patch, Patch), type(patch)
        assert patch.op == "set", patch.op
        assert patch.by is Owner.LOOP, patch.by
        assert patch.path in ("summary", "history"), patch.path
        self.patches.append(patch)
        self.data[patch.path] = copy.deepcopy(patch.value)
        return patch


def long_history(n: int, *, chars: int = 180) -> list[dict]:
    """n 条足够长的历史：每条 user/assistant 交替，用来算 token 账。"""
    out: list[dict] = []
    for i in range(n):
        if i % 2 == 0:
            out.append(user(f"第{i}轮诉求：" + "需要排查的现场信息" * (chars // 9)))
        else:
            out.append(assistant(f"第{i}轮结论：" + "已定位到连接池配置" * (chars // 9)))
    return out


# ══════════════════════════════════════════════════════════════
# 1) 不需要压缩的边界
# ══════════════════════════════════════════════════════════════
class TestNoCompression(unittest.TestCase):
    def test_01_short_history_returns_empty_summary(self):
        """用例 1：history 短于 max_keep → 空摘要、压缩 0 条、ref 全 0。"""
        history = [user("看下 p99"), assistant("好")]
        res = HistoryCompressor(max_keep=8).compress(history)
        self.assertEqual(res.summary, "")
        self.assertEqual(res.compressed_count, 0)
        self.assertEqual(res.kept_count, 2)
        self.assertEqual(res.ref, {"from_index": 0, "to_index": 0})
        self.assertFalse(res.compressed)

    def test_16_empty_history_does_not_crash(self):
        """用例 16：空 history → 不崩，各计数为 0。"""
        res = HistoryCompressor(max_keep=4).compress([])
        self.assertEqual((res.summary, res.compressed_count, res.kept_count),
                         ("", 0, 0))
        self.assertEqual(res.tokens_before, 0)
        self.assertEqual(res.tokens_after, 0)

        # None 也当空处理（Loop 早期 snapshot 里没有该字段）
        res2 = HistoryCompressor(max_keep=4).compress(None)  # type: ignore[arg-type]
        self.assertEqual((res2.summary, res2.compressed_count), ("", 0))

    def test_28_should_compress_helper(self):
        """条数刚好等于 max_keep 不该压（压了也只是改写、纯亏一次）。"""
        c = HistoryCompressor(max_keep=3)
        self.assertFalse(c.should_compress(long_history(3)))
        self.assertTrue(c.should_compress(long_history(4)))


# ══════════════════════════════════════════════════════════════
# 2) 切分与计数
# ══════════════════════════════════════════════════════════════
class TestSplit(unittest.TestCase):
    def test_02_ten_messages_max_keep_four(self):
        """用例 2 + 10/11：10 条 + max_keep=4 → 压 6 留 4，ref=(0,6)。"""
        history = long_history(10)
        before = copy.deepcopy(history)
        res = HistoryCompressor(max_keep=4).compress(history)

        self.assertEqual(res.compressed_count, 6)
        self.assertEqual(res.kept_count, 4)
        self.assertEqual(res.ref["from_index"], 0)
        self.assertEqual(res.ref["to_index"], 6)
        self.assertEqual(res.ref["to_index"] - res.ref["from_index"],
                         res.compressed_count)
        # 入参一条都没动（深比较）
        self.assertEqual(history, before)
        # 首行计数与 compressed_count 一致
        self.assertTrue(res.summary.startswith(
            HEADER_FORMAT.format(n=res.compressed_count)))

    def test_26_kept_tail_equals_original_tail(self):
        """保留的必须是**原始的最后 max_keep 条**，且顺序不变。"""
        history = long_history(9)
        res = HistoryCompressor(max_keep=3).compress(history)
        tail = history[-3:]
        self.assertEqual(res.kept_count, len(tail))
        # 头部进了摘要（前 20 字没被截掉），尾部必须留在 history 里、不进摘要
        for msg in history[:6]:
            self.assertIn(msg["text"][:20], res.summary)
        for msg in tail:
            self.assertNotIn(msg["text"][:20], res.summary)


# ══════════════════════════════════════════════════════════════
# 3) 按类型提炼
# ══════════════════════════════════════════════════════════════
class TestExtraction(unittest.TestCase):
    def test_03_goal_segment_from_user(self):
        """用例 3：user → 「目标：」。"""
        # max_keep=0 = 「全部送进摘要」，这样能隔离「提炼」这一件事
        history = [user("排查 api p99 升高"), assistant("先看日志"), user("再确认下")]
        res = HistoryCompressor(max_keep=0).compress(history)
        self.assertEqual(res.kept_count, 0)
        self.assertIn("- 目标：排查 api p99 升高", res.summary)
        self.assertIn("- 目标：再确认下", res.summary)

    def test_04_done_segment_from_tool_ok(self):
        """用例 4：tool + kind=ok → 「已执行 <tool>」。"""
        history = [tool_ok("query_logs"), assistant("好"), user("继续")]
        res = HistoryCompressor(max_keep=0).compress(history)
        self.assertIn("- 已执行 query_logs", res.summary)

    def test_05_fail_segment_includes_reason(self):
        """用例 5：tool 非 ok → 「失败 <tool>: <reason 前 60 字>」。"""
        reason = "timeout after 30s while calling alerts-api in region cn-north-1 extra detail"
        history = [tool_failed("query_alerts", reason), user("换个工具")]
        res = HistoryCompressor(max_keep=0).compress(history)
        self.assertIn("- 失败 query_alerts: ", res.summary)
        # reason 被保留（前 60 字），但不该整段照抄
        kept = [ln for ln in lines_of(res.summary) if ln.startswith("- 失败")][0]
        detail = kept.split(": ", 1)[1]
        self.assertTrue(reason.startswith(detail.rstrip("…")),
                        f"detail={detail!r}")
        self.assertLessEqual(len(detail), REASON_KEEP_CHARS + 1)  # +1 = 省略号

    def test_06_conclusion_from_assistant_without_tool_calls(self):
        """用例 6：assistant 无 tool_calls → 「结论：」。"""
        history = [assistant("连接池上限 20 导致 acquire 超时"), user("确认")]
        res = HistoryCompressor(max_keep=0).compress(history)
        self.assertIn("- 结论：连接池上限 20 导致 acquire 超时", res.summary)

    def test_19_assistant_with_tool_calls_is_skipped(self):
        """带 tool_calls 的 assistant 是「发起动作」不是结论，记了就是重复预算。"""
        history = [assistant("我先查日志", tool_calls=[{"id": "c1", "name": "query_logs",
                                                      "args": {}}]),
                   user("确认")]
        res = HistoryCompressor(max_keep=0).compress(history)
        self.assertNotIn("我先查日志", res.summary)
        self.assertEqual(res.compressed_count, 2)  # 仍然算被压缩，只是没提炼出条目

    def test_20_system_role_is_skipped(self):
        """system 消息不是历史决议：跳过，不进摘要。"""
        history = [{"role": "system", "text": "检测到死锁，强制 replan 一次"},
                   {"role": "user", "text": "继续"}]
        res = HistoryCompressor(max_keep=0).compress(history)
        self.assertNotIn("死锁", res.summary)
        self.assertIn("- 目标：继续", res.summary)

    def test_27_render_entry_unknown_kind_is_empty(self):
        """未知 kind 返回空串（调用方据此跳过），而不是塞一行噪音。"""
        self.assertEqual(render_entry("whatever", text="x"), "")
        self.assertEqual(render_entry("done", tool="query_logs"), "已执行 query_logs")
        self.assertEqual(render_entry("fail", tool="t"), "失败 t")
        self.assertEqual(render_entry("goal", text=""), "")


# ══════════════════════════════════════════════════════════════
# 4) 去重 / 合并 / 截断
# ══════════════════════════════════════════════════════════════
class TestDedupeMergeTruncate(unittest.TestCase):
    def test_07_duplicate_conclusions_kept_once(self):
        """用例 7：同样结论出现 3 次，摘要里只 1 次。"""
        same = "连接池上限 20 导致 acquire 超时"
        history = [assistant(same), assistant(same), assistant(same), user("确认")]
        res = HistoryCompressor(max_keep=1).compress(history)
        self.assertEqual(res.summary.count(same), 1)
        self.assertEqual(len([ln for ln in lines_of(res.summary)
                              if ln == f"- 结论：{same}"]), 1)

    def test_08_existing_summary_merged_at_front(self):
        """用例 8：已有摘要原样放在最前，且计数累加（12 + 新压的）。"""
        existing = ("【历史摘要｜已压缩 12 条】\n"
                    "- 目标：昨天的目标任务\n"
                    "- 结论：昨天定下的结论")
        history = [user("今天的新目标"), assistant("今天的新结论"), user("确认")]
        res = HistoryCompressor(max_keep=1).compress(history, existing_summary=existing)

        body = res.summary.split("\n")
        self.assertEqual(body[0], HEADER_FORMAT.format(n=12 + 2))
        # 旧内容在最前，且顺序保持
        self.assertEqual(body[1], "- 目标：昨天的目标任务")
        self.assertEqual(body[2], "- 结论：昨天定下的结论")
        # 新条目追加在后
        self.assertIn("- 目标：今天的新目标", body[3:])
        # 只压了 2 条（第 3 条留作尾部）
        self.assertEqual(res.compressed_count, 2)

    def test_21_recompress_merges_old_omission_marker(self):
        """二次压缩：旧省略标记被吸收进新计数，不出现两个标记、不重复计数。"""
        old = ("【历史摘要｜已压缩 9 条】\n"
               "- 目标：很早的目标\n"
               + OMITTED_FORMAT.format(n=5))
        res = HistoryCompressor(max_keep=1).compress(
            [user("新的诉求"), user("留在尾部的一条")], existing_summary=old)
        # 只出现一个省略标记（旧标记被吸收后重渲染，不是叠加）
        self.assertEqual(res.summary.count("…（更早"), 1)
        # 旧标记说的是「更早 5 条」，这一轮没有再丢，所以数字保持 5；
        # 但首行累计计数必须 9 + 1 = 10（增量压了 1 条）
        self.assertIn(OMITTED_FORMAT.format(n=5), res.summary)
        self.assertTrue(res.summary.startswith(HEADER_FORMAT.format(n=10)))
        # 被省略的旧条目不再掉，因为新摘要很长……这里上限充足，只剩省略标记语义
        self.assertIn("- 目标：很早的目标", res.summary)

    def test_09_over_limit_drops_oldest_and_marks_omitted(self):
        """用例 9：超 max_summary_chars → 从最旧丢 + 末尾省略标记。"""
        history = [user(f"第{i}条诉求：" + "很长很长的说明" * 8) for i in range(10)]
        res = HistoryCompressor(max_keep=2, max_summary_chars=260).compress(history)
        lines = lines_of(res.summary)
        self.assertLessEqual(len(res.summary), 260)
        # 保留的必然是最新的那些（最旧的被丢掉；第 8/9 条属于 tail，本来就不进摘要）
        self.assertFalse(any("第0条诉求" in ln for ln in lines))
        self.assertTrue(any("第7条诉求" in ln for ln in lines), lines)
        # 末尾省略标记，数字 = 被丢条目数，且 > 0
        tail = res.summary.split("\n")[-1]
        self.assertTrue(tail.startswith("…（更早"))
        self.assertIn("条已省略）", tail)
        dropped = int(tail.removeprefix("…（更早").removesuffix("条已省略）"))
        self.assertGreater(dropped, 0)
        self.assertEqual(dropped + len(lines), res.compressed_count)

    def test_23_zero_budget_returns_header_only(self):
        """max_summary_chars=0 是退化配置：只留首行，保证下游知道这是摘要。"""
        res = HistoryCompressor(max_keep=0, max_summary_chars=0).compress(
            [user("目标一"), user("目标二")])
        self.assertEqual(res.summary, HEADER_FORMAT.format(n=2))

    def test_31_token_budget_trims_summary(self):
        """预算联动：max_summary_tokens 生效时也按「从最旧丢」收敛，而不是超窗。"""
        from core.summary import estimate_tokens as est

        history = [user(f"第{i}条诉求：确认修复方案并给出回滚步骤") for i in range(12)]
        res = HistoryCompressor(max_keep=2, max_summary_chars=10_000,
                                max_summary_tokens=60).compress(history)
        lines = lines_of(res.summary)
        self.assertLessEqual(est(res.summary), 60)       # 真的卡在 token 上限内
        self.assertIn("…（更早", res.summary)             # 丢过东西就必须说明
        self.assertTrue(0 < len(lines) < 10, lines)      # 还留了条目，不是一刀全丢
        self.assertIn("第9条诉求", "".join(lines))        # 留下的是最新的
        self.assertNotIn("第0条诉求", "".join(lines))

    def test_32_token_budget_too_tight_falls_back_to_header(self):
        """token 上限比首行还小 → 退回首行（格式契约优先，不返回半截行）。"""
        res = HistoryCompressor(max_keep=1, max_summary_chars=10_000,
                                max_summary_tokens=1).compress(
            [user("甲"), user("乙"), user("丙")])
        self.assertEqual(res.summary, HEADER_FORMAT.format(n=2))
        with self.assertRaises(TypeError):
            HistoryCompressor(max_summary_tokens="40")   # type: ignore[arg-type]

    def test_18_cjk_long_text_truncated_on_char_boundary(self):
        """用例 18：中文长文本截断不切坏字符，且截断是「原串前缀 + 省略号」。"""
        text = "排查" + "连接池配置与上游超时" * 60   # 远超 MAX_ENTRY_CHARS
        res = HistoryCompressor(max_keep=0).compress([user(text), user("尾部")])
        goal_line = [ln for ln in lines_of(res.summary) if ln.startswith("- 目标：")][0]
        body = goal_line[len("- 目标："):]

        self.assertTrue(body.endswith("…"))
        self.assertLessEqual(len(body), MAX_ENTRY_CHARS)
        prefix = body[:-1].rstrip()
        self.assertTrue(text.startswith(prefix), "截断必须落在原串前缀上")
        # 每个字符都还能编码/解码，没有半个码点
        self.assertEqual(prefix.encode("utf-8").decode("utf-8"), prefix)
        self.assertTrue(all(ch in text for ch in prefix))
        # 第二行（尾部的 user）没被这行挤掉
        self.assertIn("- 目标：尾部", res.summary)


# ══════════════════════════════════════════════════════════════
# 5) 纯函数性：确定性 / 不改入参 / token 账
# ══════════════════════════════════════════════════════════════
class TestPurityAndAccounting(unittest.TestCase):
    def test_11_input_history_not_mutated(self):
        """用例 11：入参 history 深比较前后一致（压缩只读）。"""
        history = long_history(12)
        snapshot_before = copy.deepcopy(history)
        json_before = json.dumps(history, ensure_ascii=False, sort_keys=True)
        ids_before = [id(m) for m in history]

        HistoryCompressor(max_keep=5).compress(history, existing_summary="旧摘要")

        self.assertEqual(history, snapshot_before)
        self.assertEqual(json.dumps(history, ensure_ascii=False, sort_keys=True),
                         json_before)
        # 元素对象本身没被替换/改写（同一批 dict 对象，内容也没变）
        self.assertEqual([id(m) for m in history], ids_before)

    def test_12_deterministic_same_input(self):
        """用例 12：同输入两次结果完全相同（无时钟、无随机）。"""
        history = long_history(14)
        a = HistoryCompressor(max_keep=4).compress(history)
        b = HistoryCompressor(max_keep=4).compress(history)
        self.assertEqual(a, b)
        self.assertEqual(a.to_dict(), b.to_dict())
        # 换一个新实例也必须一样
        c = HistoryCompressor(max_keep=4).compress(list(history))
        self.assertEqual(a, c)

    def test_13_tokens_before_greater_than_after(self):
        """用例 13：真省了 token（before > after），且 method=rule。"""
        history = [user("x" * 400) for _ in range(16)]
        res = HistoryCompressor(max_keep=2, max_summary_chars=400).compress(history)

        self.assertEqual(res.method, "rule")
        self.assertGreater(res.tokens_before, 0)
        self.assertGreater(res.tokens_before, res.tokens_after)
        self.assertGreater(res.saved_tokens, 0)
        # 省下的量级要对：压掉 14 条长消息，不可能只省几个 token
        self.assertGreater(res.tokens_before - res.tokens_after, 100)

    def test_24_result_fields_and_to_dict(self):
        """SummaryResult 全字段可序列化，saved_tokens 与计数自洽。"""
        res = HistoryCompressor(max_keep=2).compress(long_history(6))
        self.assertIsInstance(res, SummaryResult)
        d = res.to_dict()
        self.assertEqual(set(d), {"summary", "compressed_count", "kept_count",
                                  "tokens_before", "tokens_after", "method", "ref"})
        self.assertEqual(d["ref"], {"from_index": 0, "to_index": 4})
        self.assertEqual(res.saved_tokens, res.tokens_before - res.tokens_after)
        json.dumps(d, ensure_ascii=False)   # 必须能进 patch_log / artifacts
        with self.assertRaises(Exception):
            res.summary = "改不动"          # frozen dataclass

    def test_25_injected_estimator_is_used(self):
        """estimator 可注入且被真正调用（单一职责：不绑死 builder 的口径）。"""
        calls: list[str] = []

        def fake_est(text: str) -> int:
            calls.append(text)
            return len(text)  # 1 char = 1 token

        c = HistoryCompressor(max_keep=1, estimator=fake_est)
        res = c.compress([user("abc"), user("de")])
        # before = 全部 history 的字符数（1 char = 1 token）
        self.assertEqual(res.tokens_before, 5)
        # after = 保留的「de」（2） + 摘要文本长度
        self.assertEqual(res.tokens_after, 2 + len(res.summary))
        # 摘要与尾部都被估算过（证明 estimator 没被绕过）
        self.assertIn("de", calls)
        self.assertIn(res.summary, calls)


# ══════════════════════════════════════════════════════════════
# 6) 畸形输入
# ══════════════════════════════════════════════════════════════
class TestMalformed(unittest.TestCase):
    def test_17_malformed_messages_skipped(self):
        """用例 17：缺 role / 缺 text / 非 dict / None → 跳过不崩。"""
        history = [
            {"text": "没有 role"},
            {"role": "user"},                       # 缺 text
            {"role": "user", "text": ""},           # 空 text
            {"role": "user", "text": 12345},        # text 非字符串
            "裸字符串",
            None,
            42,
            {"role": "tool", "tool": "x", "kind": "ok"},   # tool 缺 text 但可推
            user("这条是好的"),
        ]
        res = HistoryCompressor(max_keep=0).compress(history)
        # 不崩、计数仍然覆盖全部被压缩的条数（含畸形）
        self.assertEqual(res.compressed_count, 9)
        self.assertEqual(res.kept_count, 0)
        self.assertIn("- 目标：这条是好的", res.summary)
        self.assertNotIn("没有 role", res.summary)
        self.assertNotIn("12345", res.summary)

    def test_17b_non_string_existing_summary_is_ignored(self):
        """existing_summary 传了非字符串（例如 None）→ 当空处理。"""
        res = HistoryCompressor(max_keep=1).compress(
            [user("甲"), user("乙")], existing_summary=None)  # type: ignore[arg-type]
        self.assertIn("- 目标：甲", res.summary)

    def test_29_bad_constructor_args(self):
        """构造参数校验：类型错立刻炸，负值收成 0（不产生难猜的负索引）。"""
        with self.assertRaises(TypeError):
            HistoryCompressor(max_keep="8")       # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            HistoryCompressor(max_summary_chars=1.5)   # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            HistoryCompressor(estimator=42)       # type: ignore[arg-type]
        c = HistoryCompressor(max_keep=-3)
        self.assertEqual(c.max_keep, 0)


# ══════════════════════════════════════════════════════════════
# 7) apply_to_context（鸭子类型 ctx）
# ══════════════════════════════════════════════════════════════
class TestApplyToContext(unittest.TestCase):
    def test_14_writes_summary_and_converges_history(self):
        """用例 14：summary 被写入，history 收敛为「最后 max_keep 条」。"""
        history = long_history(10)
        ctx = FakeCtx({"history": copy.deepcopy(history), "summary": ""})
        res = apply_to_context(ctx, HistoryCompressor(max_keep=4))

        self.assertIsNotNone(res)
        self.assertEqual([p.path for p in ctx.patches], ["summary", "history"])
        self.assertEqual(ctx.data["summary"], res.summary)
        self.assertEqual(len(ctx.data["history"]), 4)
        self.assertEqual(ctx.data["history"], history[-4:])
        self.assertEqual(res.kept_count, 4)
        self.assertEqual(res.compressed_count, 6)
        # 用 set 而不是 append：append 会把旧消息又拼回去，不叫收敛
        self.assertTrue(all(p.op == "set" for p in ctx.patches))
        # 写入值的类型干净
        self.assertIsInstance(ctx.data["summary"], str)

    def test_15_short_history_returns_none_without_patches(self):
        """用例 15：history 不足 max_keep → 返回 None 且一个 patch 都不发。"""
        ctx = FakeCtx({"history": long_history(3), "summary": "旧摘要"})
        before = copy.deepcopy(ctx.data)
        res = apply_to_context(ctx, HistoryCompressor(max_keep=8))
        self.assertIsNone(res)
        self.assertEqual(ctx.patches, [])
        self.assertEqual(ctx.data, before)

    def test_15b_exactly_max_keep_returns_none(self):
        """边界：条数 == max_keep 也不压（没有可压的头部）。"""
        ctx = FakeCtx({"history": long_history(4), "summary": ""})
        self.assertIsNone(apply_to_context(ctx, HistoryCompressor(max_keep=4)))
        self.assertEqual(ctx.patches, [])

    def test_14b_existing_summary_is_carried_over(self):
        """Context 里已有的 summary 会被合并进来，不是覆盖丢掉。"""
        ctx = FakeCtx({"history": long_history(6),
                       "summary": HEADER_FORMAT.format(n=7) + "\n- 目标：更早的目标"})
        res = apply_to_context(ctx, HistoryCompressor(max_keep=2))
        self.assertIsNotNone(res)
        self.assertIn("- 目标：更早的目标", ctx.data["summary"])
        self.assertTrue(ctx.data["summary"].startswith(HEADER_FORMAT.format(n=7 + 4)))

    def test_25b_second_apply_is_noop(self):
        """收敛后再跑一次：条数已 <= max_keep → None，history 不会再短。"""
        ctx = FakeCtx({"history": long_history(12), "summary": ""})
        c = HistoryCompressor(max_keep=4)
        self.assertIsNotNone(apply_to_context(ctx, c))
        first = copy.deepcopy(ctx.data)
        self.assertIsNone(apply_to_context(ctx, c))
        self.assertEqual(ctx.data, first)

    def test_30_real_context_handle_owner_is_loop(self):
        """集成：真 ContextHandle 上 owner=LOOP 可写 summary/history，回读一致。"""
        from core.context import ContextHandle

        history = long_history(9)
        ctx = ContextHandle({"history": copy.deepcopy(history), "summary": ""})
        version_before = ctx.version
        res = apply_to_context(ctx, HistoryCompressor(max_keep=3))

        self.assertIsNotNone(res)
        snap = ctx.snapshot()
        self.assertEqual(snap.get("summary"), res.summary)
        self.assertEqual(snap.get("history"), history[-3:])
        self.assertEqual(len(ctx.patch_log), version_before + 2)
        for patch in ctx.patch_log:
            self.assertIs(patch.by, Owner.LOOP)
            self.assertEqual(patch.op, "set")

        # 序列化再回放：摘要与收敛后的 history 都能 round-trip
        restored = ContextHandle.restore(ctx.serialize())
        self.assertEqual(restored.snapshot().get("summary"), res.summary)
        self.assertEqual(restored.snapshot().get("history"), history[-3:])


# ══════════════════════════════════════════════════════════════
# 8) Prompt 接线：summary_section（Builder 加一段时只需 1 行）
# ══════════════════════════════════════════════════════════════
class TestSummarySection(unittest.TestCase):
    def test_33_summary_section_for_ctx_and_snapshot(self):
        """有摘要就原样给出、没有就空串（Builder 跳过空段，不留空行）。"""
        ctx = FakeCtx({"history": [], "summary": ""})
        self.assertEqual(summary_section(ctx), "")
        self.assertEqual(summary_section(ctx.snapshot()), "")

        ctx.apply(Patch("set", "summary", Owner.LOOP, HEADER_FORMAT.format(n=3)
                        + "\n- 目标：甲"))
        expected = HEADER_FORMAT.format(n=3) + "\n- 目标：甲"
        self.assertEqual(summary_section(ctx), expected)
        self.assertEqual(summary_section(ctx.snapshot()), expected)

        # 真 ContextHandle 也吃（它的 summary 是 str）
        from core.context import ContextHandle

        real = ContextHandle({"history": [], "summary": "  "})
        self.assertEqual(summary_section(real), "")   # 全空白当没有
        real.apply(Patch("set", "summary", Owner.LOOP, "摘要正文"))
        self.assertEqual(summary_section(real), "摘要正文")

    def test_33b_summary_section_tolerates_garbage(self):
        """畸形输入（无 get / summary 非字符串 / snapshot 抛异常）→ 返回空串。"""
        self.assertEqual(summary_section(object()), "")
        self.assertEqual(summary_section(None), "")
        self.assertEqual(summary_section({"summary": 123}), "")

        class Boom:
            def snapshot(self):
                raise RuntimeError("boom")

        self.assertEqual(summary_section(Boom()), "")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
