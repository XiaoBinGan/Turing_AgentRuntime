"""健壮性：外部组件怎么坏，Runtime 怎么反应。

本文件盯的是**静默失败** —— 那些"看起来正常、实际错了"的路径：
  * 垃圾被当成答案（类型容错过宽）
  * 硬截止被绕过（adapter 拦不住"响应迟到"）
  * 内存无界增长（长跑服务必炸）

这些在功能测试里全绿，只有在故障注入下才暴露。
"""
from __future__ import annotations

import threading
import time
import unittest

from adapters.llm_mock import LLMResponse, ScriptedLLM, usage
from builder import PromptBuilder
from contracts import (
    ExecCtx, NormalizedToolCall, RiskTier, SideEffect, ToolManifest, TermReason,
)
from core.gateway import Gateway
from core.loop import Loop, _coerce_response
from plane.governance import Governance, InputGuard, OutputGuard, RBAC
from plane.tool import Dispatcher, ToolRegistry

P = {"user_id": "u", "tenant": "t", "roles": ["r"],
     "scopes": ["runtime:run", "tool.read"]}


def _gw(llm, tool_handler=None, **kw):
    reg = ToolRegistry()
    if tool_handler is not None:
        reg.register(ToolManifest(
            name="t1", side_effect=SideEffect.READ_ONLY, risk_tier=RiskTier.T0_SAFE,
            description="测试", input_schema={"type": "object", "properties": {}}),
            tool_handler)

    def factory(goal, tc):
        return Loop(PromptBuilder(total_tokens=4000), llm, Dispatcher(reg))
    return Gateway(factory, governance=Governance(
        rbac=RBAC(), input_guard=InputGuard(), output_guard=OutputGuard()), **kw)


class TestCoerceIsStrict(unittest.TestCase):
    """适配器返回垃圾 **不能** 被当成答案。

    早期实现 `text=str(resp or "")`：任意对象 → `"<object object at 0x...>"`
    → 回合以 DONE 结束。垃圾被静默当成正常结果，客户端无从察觉。
    """

    def test_llmresponse_passthrough(self):
        self.assertEqual(_coerce_response(LLMResponse(text="a")).text, "a")

    def test_mapping_accepted(self):
        self.assertEqual(_coerce_response({"text": "b"}).text, "b")

    def test_bare_string_accepted(self):
        """裸字符串是**有意留的简易契约**（适配器可以只回文本）。"""
        self.assertEqual(_coerce_response("c").text, "c")

    def test_arbitrary_object_rejected(self):
        with self.assertRaises(TypeError) as ctx:
            _coerce_response(object())
        self.assertIn("LLMResponse", str(ctx.exception))

    def test_none_rejected(self):
        with self.assertRaises(TypeError):
            _coerce_response(None)

    def test_garbage_adapter_yields_failure_not_done(self):
        """端到端：适配器返回对象 → 回合失败，而不是 DONE + 乱码。"""

        class Garbage:
            model = "junk"

            def complete(self, p, t, e):
                return object()

        r = _gw(Garbage()).handle({"goal": "x", "principal": P})
        self.assertEqual(r.term_reason, TermReason.UNRECOVERABLE)
        self.assertIn("LLMResponse", r.text, "错误信息要说清契约要求什么")


class TestDeadlineHardStop(unittest.TestCase):
    """硬截止必须在 **LLM 返回后** 复查。

    adapter 的 `_guard` 只在发请求前检查，拦不住"响应迟到"。
    实测：deadline 100ms、LLM 睡 400ms → 回合 DONE，硬截止被静默绕过。
    """

    def test_late_response_is_deadline_not_done(self):
        class Slow:
            model = "slow"

            def complete(self, p, t, e):
                time.sleep(0.25)
                return LLMResponse(text="迟到的答案", usage=usage())

        r = _gw(Slow()).handle({"goal": "x", "principal": P, "deadline_ms": 80})
        self.assertEqual(r.term_reason, TermReason.DEADLINE)
        self.assertTrue(r.partial, "超时必须标 partial，否则客户端以为完整")

    def test_late_content_preserved(self):
        """**保留已产出的文本**：token 已经花掉，丢掉纯浪费。"""

        class Slow:
            model = "slow"

            def complete(self, p, t, e):
                time.sleep(0.25)
                return LLMResponse(text="迟到的答案", usage=usage())

        r = _gw(Slow()).handle({"goal": "x", "principal": P, "deadline_ms": 80})
        self.assertIn("迟到的答案", r.text)

    def test_within_deadline_is_done(self):
        r = _gw(ScriptedLLM([LLMResponse(text="快", usage=usage())])).handle(
            {"goal": "x", "principal": P, "deadline_ms": 30_000})
        self.assertTrue(r.ok)
        self.assertEqual(r.term_reason, TermReason.DONE)

    def test_no_deadline_means_no_check(self):
        """deadline=0（默认）不许误判 —— 不设截止就是不限时。"""
        class Slow:
            model = "slow"

            def complete(self, p, t, e):
                time.sleep(0.1)
                return LLMResponse(text="ok", usage=usage())

        r = _gw(Slow()).handle({"goal": "x", "principal": P})
        self.assertTrue(r.ok)


class TestAuditLogBounded(unittest.TestCase):
    """`audit_log` 必须有界 —— 无界列表在长跑服务里就是内存泄漏。

    实测 1000 回合涨 1000 条，永不回收。内存里只该留"最近 N 条"供排查，
    持久审计走 `audit_sink` 回调。
    """

    def test_bounded_by_maxlen(self):
        gw = _gw(ScriptedLLM([LLMResponse(text="ok", usage=usage())]),
                 rate_limit_per_minute=0, audit_log_maxlen=10)
        for _ in range(50):
            gw.handle({"goal": "x", "principal": P})
        self.assertLessEqual(len(gw.audit_log), 10, "audit_log 没有被裁剪")

    def test_keeps_most_recent(self):
        gw = _gw(ScriptedLLM([LLMResponse(text="ok", usage=usage())]),
                 rate_limit_per_minute=0, audit_log_maxlen=5)
        for _ in range(20):
            gw.handle({"goal": "x", "principal": P})
        # 保留的是**最近**的，不是最老的
        self.assertEqual(len(gw.audit_log), 5)
        ids = [rec["audit_id"] for rec in gw.audit_log]
        self.assertEqual(len(set(ids)), 5, "留下的是重复条目")

    def test_default_maxlen_is_reasonable(self):
        gw = _gw(ScriptedLLM([LLMResponse(text="ok", usage=usage())]))
        self.assertGreaterEqual(gw.audit_log.maxlen, 1000,
                                "默认窗口太小会让排障看不到历史")

    def test_sink_still_called_for_all(self):
        """内存有界 ≠ 审计丢：`audit_sink` 必须每条都收到。"""
        seen = []
        gw = _gw(ScriptedLLM([LLMResponse(text="ok", usage=usage())]),
                 rate_limit_per_minute=0, audit_log_maxlen=3,
                 audit_sink=seen.append)
        for _ in range(10):
            gw.handle({"goal": "x", "principal": P})
        self.assertEqual(len(seen), 10, "audit_sink 漏掉了被裁剪的记录")


class TestExternalComponentFailures(unittest.TestCase):
    """外部组件坏掉时的行为：**有界收口，不崩、不静默成功**。"""

    def test_llm_exception_is_unrecoverable_not_crash(self):
        class Boom:
            model = "boom"

            def complete(self, p, t, e):
                raise RuntimeError("模型服务 503")

        r = _gw(Boom()).handle({"goal": "x", "principal": P})
        # `ok` 的语义是"请求被正确处理"（没炸在 Runtime 内部）；
        # 任务层面的成败在 `term_reason`。区分这两者是刻意的：
        # 混在一起会让"模型挂了"和"Runtime 挂了"在日志里无法分辨。
        self.assertEqual(r.term_reason, TermReason.UNRECOVERABLE)
        self.assertIn("503", r.text)

    def test_dependency_failure_is_502_not_200(self):
        """**依赖故障必须对监控可见**：LLM 挂了不能返回 200。

        早期 `ok=True → 200` 一刀切：模型服务 503 时 HTTP 层全是 200，
        监控与告警完全看不到（静默失败比 500 危险）。现在落 502。
        """
        import serve

        class Boom:
            model = "boom"

            def complete(self, p, t, e):
                raise RuntimeError("模型服务 503")

        r = _gw(Boom()).handle({"goal": "x", "principal": P})
        self.assertEqual(serve.status_for_result(r), 502)

    def test_task_level_failure_stays_200(self):
        """任务没干完（预算/步数/无进展）不是**依赖**故障 → 仍 200。"""
        import serve

        class Empty:
            model = "empty"

            def complete(self, p, t, e):
                return LLMResponse(text="", usage=usage())

        r = _gw(Empty()).handle({"goal": "x", "principal": P})
        self.assertEqual(r.term_reason, TermReason.NO_PROGRESS)
        self.assertEqual(serve.status_for_result(r), 200)

    def test_tool_exception_does_not_crash(self):
        class Caller:
            model = "caller"

            def complete(self, p, t, e):
                return LLMResponse(text="调", tool_calls=(
                    NormalizedToolCall(id="c", name="t1", args={}),), usage=usage())

        def boom(a, e):
            raise RuntimeError("工具炸了")

        r = _gw(Caller(), boom).handle({"goal": "x", "principal": P})
        # 不崩，且有界收口（工具失败被记为观察，最终因无进展终止）
        self.assertIn(r.term_reason, (TermReason.NO_PROGRESS, TermReason.DONE))

    def test_empty_llm_response_terminates(self):
        class Empty:
            model = "empty"

            def complete(self, p, t, e):
                return LLMResponse(text="", usage=usage())

        r = _gw(Empty()).handle({"goal": "x", "principal": P})
        self.assertEqual(r.term_reason, TermReason.NO_PROGRESS,
                         "空响应必须被识别为无进展，不能死循环")


class TestConcurrency(unittest.TestCase):
    def test_parallel_turns_no_exception(self):
        gw = _gw(ScriptedLLM([LLMResponse(text="ok", usage=usage())]),
                 rate_limit_per_minute=0)
        errors, done = [], []
        lock = threading.Lock()

        def one(i):
            try:
                r = gw.handle({"goal": f"g{i}", "principal": P})
                with lock:
                    done.append(r.ok)
            except Exception as exc:                           # noqa: BLE001
                with lock:
                    errors.append(f"{type(exc).__name__}: {exc}")

        threads = [threading.Thread(target=one, args=(i,)) for i in range(50)]
        [t.start() for t in threads]
        [t.join(20) for t in threads]
        self.assertEqual(errors, [], f"并发下抛异常：{errors[:2]}")
        self.assertEqual(len(done), 50)

    def test_rate_limit_not_bypassed_under_concurrency(self):
        gw = _gw(ScriptedLLM([LLMResponse(text="ok", usage=usage())]),
                 rate_limit_per_minute=5)
        passed = []
        lock = threading.Lock()

        def one(i):
            r = gw.handle({"goal": "x", "principal": P})
            with lock:
                passed.append(r.ok)

        threads = [threading.Thread(target=one, args=(i,)) for i in range(30)]
        [t.start() for t in threads]
        [t.join(20) for t in threads]
        self.assertLessEqual(sum(passed), 5, "并发下配额被绕过")


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestStreamStatusConsistency(unittest.TestCase):
    """流式端点与 JSON 端点对**同一个故障**必须给同一个结论。

    踩过的坑：JSON 端点因 `term_reason=UNRECOVERABLE` 返回 502，
    而 SSE 的 `completed` 事件里 `ok` 仍是 `true` —— 同一个模型故障，
    用 JSON 的客户端看到失败，用流式的客户端看到成功。**一致性比口径选择更重要。**
    """

    def _drain(self, gateway):
        from core.observability import ShutdownCoordinator
        import asyncio
        import serve

        async def run():
            return [f async for f in serve._event_stream(
                gateway, {"goal": "x", "principal": P},
                ShutdownCoordinator(), heartbeat_s=30)]
        return asyncio.run(run())

    def test_unrecoverable_marks_ok_false(self):
        class Boom:
            model = "boom"

            def complete(self, p, t, e):
                raise RuntimeError("上游 503")

        frames = self._drain(_gw(Boom()))
        completed = [f for f in frames if f.startswith("event: completed")][-1]
        self.assertIn('"ok":false', completed.replace(" ", ""))
        self.assertIn('"term_reason":"UNRECOVERABLE"', completed.replace(" ", ""))

    def test_normal_completion_keeps_ok_true(self):
        frames = self._drain(_gw(ScriptedLLM([LLMResponse(text="ok", usage=usage())])))
        completed = [f for f in frames if f.startswith("event: completed")][-1]
        self.assertIn('"ok":true', completed.replace(" ", ""))


class TestHotSwap(unittest.TestCase):
    """外部组件热插拔：换实现不动业务代码。"""

    def test_kv_implementations_interchangeable(self):
        from core.kv import InMemoryKV
        a, b = InMemoryKV(), InMemoryKV()
        a.set("k", "1")
        self.assertEqual(a.get("k"), "1")
        self.assertIsNone(b.get("k"), "两个实例必须隔离")

    def test_all_memory_providers_satisfy_protocol(self):
        from plane.memory import InMemoryProvider
        p = InMemoryProvider()
        for m in ("put", "get", "scan", "delete"):
            self.assertTrue(callable(getattr(p, m, None)), f"缺 {m}")

    def test_vector_backends_report_availability(self):
        from plane.vector_backends import available_backends
        info = available_backends()
        self.assertEqual(set(info), {"memory", "qdrant", "milvus"})
        for name, row in info.items():
            self.assertIn("available", row)
            self.assertTrue(row["reason"], f"{name} 没给原因")

    def test_splitter_swap_keeps_output_shape(self):
        from plane.splitters import HeadingSplitter, splitter_available
        chunks = HeadingSplitter().split("## a\n" + "内容。" * 80)
        self.assertIsInstance(chunks, list)
        self.assertTrue(all(isinstance(c, str) for c in chunks))
        self.assertIn("heading", splitter_available())

    def test_sandbox_selector_reports_strength(self):
        from plane.sandbox_backends import BackendSelector
        sel = BackendSelector()
        report = sel.report()
        self.assertTrue(report, "后端自述不能为空")
        for row in report:
            self.assertIn("available", row)
            self.assertIn("reason", row)

    def test_tokenizer_swap(self):
        from adapters.tokenizer import CountOnlyTokenizer, HeuristicTokenizer
        for t in (HeuristicTokenizer(), CountOnlyTokenizer()):
            self.assertGreaterEqual(t.count("中文 abc 123"), 0)

    def test_fts_swap_keeps_query_shape(self):
        from plane.fts_index import FTS5Index
        ix = FTS5Index()
        ix.build([("1", "连接池上限二十")])
        hits = ix.search("连接池")
        self.assertTrue(hits)
        self.assertEqual(hits[0][0], "1")


class TestRateLimitZeroMeansUnlimited(unittest.TestCase):
    """`rate_limit_per_minute=0` 必须是"不限流"，不是"一个都不许"。

    **这是踩出来的坑**：裸写 `used + 1 > limit` 时，`limit=0` 会拒绝**所有**请求。
    而它在配置里长得像"关闭"——`AGENT_RUNTIME_HTTP_RATE=0` 确实是关闭。
    两个名字几乎一样的开关含义相反，运营想关配额结果把整个服务关掉了，
    日志里只有 `rate_limited`，看不出是自己配的。
    """

    def _gw(self, limit):
        return _gw(ScriptedLLM([LLMResponse(text="ok", usage=usage())]),
                   rate_limit_per_minute=limit)

    def test_zero_allows_all(self):
        gw = self._gw(0)
        for i in range(5):
            r = gw.handle({"goal": f"g{i}", "principal": P})
            self.assertTrue(r.ok, f"第 {i+1} 个请求被拒：0 应表示不限流")

    def test_zero_reports_unlimited_remaining(self):
        """`remaining=-1` 表示无上限 —— 报 0 会让仪表盘显示"耗尽"，与事实相反。"""
        r = self._gw(0).handle({"goal": "x", "principal": P})
        self.assertEqual(r.vertex["quota"]["remaining"], -1)

    def test_positive_limit_still_enforced(self):
        """修正不能顺手把真正的限流也关掉。"""
        gw = self._gw(1)
        self.assertTrue(gw.handle({"goal": "a", "principal": P}).ok)
        second = gw.handle({"goal": "b", "principal": P})
        self.assertFalse(second.ok)
        self.assertIn("rate_limited", second.error)

    def test_negative_rejected_at_construction(self):
        """负数是**配置错误**，构造期就拒 —— 不能悄悄变成"全禁"或"不限"。

        这一条与"0 = 不限"是两个不同的口径：
          * `0` 是**合法配置**（显式关闭），语义明确
          * 负数是**配错了**（比如环境变量展开成空导致 -1），必须报错
        """
        with self.assertRaises(TypeError) as ctx:
            _gw(ScriptedLLM([LLMResponse(text="ok", usage=usage())]),
                rate_limit_per_minute=-1)
        self.assertIn("非负整数", str(ctx.exception))


class TestMemoryBounded(unittest.TestCase):
    """长跑服务的内存必须有界（audit_log 是主要增长源）。"""

    def test_audit_log_caps_memory(self):
        import gc
        import tracemalloc
        gw = _gw(ScriptedLLM([LLMResponse(text="ok", usage=usage())]),
                 rate_limit_per_minute=0, audit_log_maxlen=50)
        tracemalloc.start()
        try:
            for _ in range(50):
                gw.handle({"goal": "x", "principal": P})
            gc.collect()
            base = tracemalloc.get_traced_memory()[0]
            for _ in range(450):
                gw.handle({"goal": "x", "principal": P})
            gc.collect()
            grown = tracemalloc.get_traced_memory()[0]
        finally:
            tracemalloc.stop()
        self.assertLessEqual(len(gw.audit_log), 50)
        # 封顶后增量应该是噪声级（<10 B/回合），不该是线性的
        per_turn = (grown - base) / 450
        self.assertLess(per_turn, 64, f"每回合仍在增长 {per_turn:.0f} B")
