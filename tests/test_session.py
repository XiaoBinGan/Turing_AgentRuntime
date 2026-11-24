"""tests/test_session.py — 会话层（`core/session.py` + `core/session_gateway.py`）自带用例。

覆盖三块：

  * **SessionStore（10）**：轮次累计、追加不覆盖、超长压缩/裁剪、summarizer 缺失降级、
    TTL 惰性清理、租户隔离、updated_at 降序、删除、骨架不变式（绝无 tool_results）、
    换实例可读同一底层 store。
  * **SessionGateway（12+）**：无 session_id 生成、二次请求预填、多轮累积、未知 id 新建、
    租户/用户不一致 403、vertex 字段、失败轮次不入历史、过期标记、流式承接、
    不重复追加、Context 不被工具回包污染。
  * **端到端（2+）**：真 Gateway + 真 SessionGateway + 真 Loop，三轮后第三轮 Context
    的 history 长度 == 4；第二轮 Prompt 里能看到第一轮问答。

跑法::

    cd /Users/supre/Documents/tmp/agent-runtime
    .venv/bin/python -m unittest tests.test_session -v

只 import `contracts.py` / `core/gateway.py` / `core/store.py` / `core/session.py` /
`core/session_gateway.py` / `core/loop.py`（读），不碰任何别人的文件。
"""
from __future__ import annotations

import copy
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from contracts import LLMResponse, Phase, Result, TermReason, Usage  # noqa: E402
from core.gateway import Gateway, TurnResult  # noqa: E402
from core.loop import Loop  # noqa: E402
from core.session import (  # noqa: E402
    ROLE_ASSISTANT, ROLE_USER, SKELETON_KEYS, Session, SessionCorrupt,
    SessionOwnershipError, SessionStore, new_session_id,
)
from core.session_gateway import (  # noqa: E402
    SESSION_STATUS_BAD_REQUEST, SESSION_STATUS_FORBIDDEN, SessionGateway,
)
from core.store import MemoryStore  # noqa: E402
from core.summary import HistoryCompressor  # noqa: E402


# ══════════════════════════════════════════════════════════════
# 测试替身
# ══════════════════════════════════════════════════════════════
class FakeClock:
    """可推的秒级时钟（SessionStore / TTL 用）。"""

    def __init__(self, t: float = 1_000.0) -> None:
        self.t = float(t)

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += float(seconds)


class FakeSummary:
    """`HistoryCompressor.compress()` 的最小产物（只带本模块消费的两个字段）。"""

    def __init__(self, summary: str = "【摘要】早期对话已压缩",
                 compressed_count: int = 1) -> None:
        self.summary = summary
        self.compressed_count = compressed_count


class SpySummarizer:
    """记录调用参数的压缩器。"""

    def __init__(self, summary: str = "【摘要】", compressed_count: int = 1) -> None:
        self.calls: list[tuple[list[dict], str]] = []
        self.summary = summary
        self.compressed_count = compressed_count

    def compress(self, history, existing_summary: str = ""):
        self.calls.append(([dict(m) for m in history], existing_summary))
        return FakeSummary(self.summary, self.compressed_count)


class BoomSummarizer:
    def compress(self, history, existing_summary: str = ""):
        raise RuntimeError("summarizer 炸了")


def principal_dict(**kw) -> dict:
    """默认：一个能进运行时的主体（带 runtime:run）。"""
    base = {"user_id": "u_1", "roles": ["engineer"], "tenant": "acme",
            "scopes": ["runtime:run"]}
    base.update(kw)
    return base


def request(goal: str = "连接池超时怎么办", **kw) -> dict:
    base = {"goal": goal, "principal": principal_dict()}
    base.update(kw)
    return base


class RecordingLoop:
    """假 Loop：记录拿到的 Context（用于断言 history 是否被预填）。"""

    def __init__(self, text: str = "好的。", exc: BaseException | None = None) -> None:
        self.text = text
        self.exc = exc
        self.contexts: list = []
        self.goals: list[str] = []

    def run(self, goal, ctx, e):
        self.goals.append(goal)
        self.contexts.append(ctx)
        if self.exc is not None:
            raise self.exc
        return Result(phase=Phase.DONE, term_reason=TermReason.DONE,
                      text=self.text, partial=False, steps=1)

    def resume(self, ctx, e, decision=True, reason=""):
        self.contexts.append(ctx)
        if self.exc is not None:
            raise self.exc
        return Result(phase=Phase.DONE, term_reason=TermReason.DONE,
                      text=self.text, partial=False, steps=1)


class RecordingContextFactory:
    """包一层 context_factory：记下每次**新建** Context 时的 initial 字典。"""

    def __init__(self) -> None:
        self.initials: list[dict] = []

    def __call__(self, initial=None, budget=None):
        self.initials.append(copy.deepcopy(initial) if initial is not None else None)
        from core.context import ContextHandle
        return ContextHandle(initial=initial, budget=budget)


class FakeGateway:
    """最小 Gateway 替身（**没有** `_context_factory`）。

    用来证明「请求字段通道」独立可用：它自己从 `request["session"]` 里读历史。
    """

    def __init__(self, ok: bool = True, text: str = "好的。") -> None:
        self.ok = ok
        self.text = text
        self.requests: list[dict] = []
        self.contexts: list[dict] = []

    def _ctx_of(self, req: dict) -> dict:
        payload = req.get("session") or {}
        return {"history": [dict(m) for m in payload.get("history") or ()],
                "summary": payload.get("summary") or "",
                "session_id": payload.get("session_id") or ""}

    def handle(self, req: dict):
        self.requests.append(dict(req))
        self.contexts.append(self._ctx_of(req))
        return TurnResult(ok=self.ok, text=self.text, phase="done",
                          term_reason="done", steps=1)

    def handle_stream(self, req: dict):
        self.requests.append(dict(req))
        self.contexts.append(self._ctx_of(req))
        yield {"type": "accepted", "seq": 0, "data": {}}
        yield {"type": "started", "seq": 1, "data": {}}
        yield {"type": "completed", "seq": 2, "data": {
            "text": self.text, "steps": 1, "ok": self.ok, "phase": "done",
            "term_reason": "done", "error": "", "audit_id": ""}}


# ══════════════════════════════════════════════════════════════
# 一、SessionStore
# ══════════════════════════════════════════════════════════════
class TestSessionStore(unittest.TestCase):
    """会话存储：轮次、追加、压缩、TTL、隔离、持久化。"""

    def setUp(self) -> None:
        self.clock = FakeClock()
        self.store = SessionStore(clock=self.clock)

    # 1
    def test_upsert_creates_session_and_counts_turns(self):
        session = self.store.upsert_turn(
            session_id="sess_a", tenant="acme", user_id="u_1",
            goal="连接池超时怎么办", answer="先把最大连接数调大。")
        self.assertEqual(session.turns, 1)
        self.assertEqual(session.tenant, "acme")
        self.assertEqual(session.user_id, "u_1")
        self.assertEqual(session.created_at, session.updated_at)
        self.assertEqual(len(session.history), 2)
        # 再读一次仍然是同一份（已落库/已入内存）
        again = self.store.get("sess_a")
        self.assertIsNotNone(again)
        self.assertEqual(again.turns, 1)

    # 2
    def test_second_upsert_appends_and_keeps_old_turns(self):
        self.store.upsert_turn(session_id="sess_b", tenant="acme", user_id="u_1",
                               goal="问题一", answer="答一")
        self.clock.advance(5)
        session = self.store.upsert_turn(session_id="sess_b", tenant="acme",
                                         user_id="u_1", goal="问题二", answer="答二")
        self.assertEqual(session.turns, 2)
        self.assertEqual(len(session.history), 4)
        self.assertEqual(session.history[0]["text"], "问题一")
        self.assertEqual(session.history[2]["text"], "问题二")
        # created_at 不被后续写入改写（否则「这段对话活了多久」就查不出来了）
        self.assertLess(session.created_at, session.updated_at)

    # 3
    def test_max_history_over_limit_calls_summarizer_and_trims(self):
        spy = SpySummarizer("【摘要】前两轮已压")
        store = SessionStore(clock=self.clock, max_history=4, summarizer=spy)
        for i in range(4):                      # 4 轮 → 8 条消息 > 4
            store.upsert_turn(session_id="sess_c", tenant="acme", user_id="u_1",
                              goal=f"问题{i}", answer=f"答{i}")
        session = store.get("sess_c")
        self.assertTrue(spy.calls, "超过 max_history 必须调用 summarizer")
        self.assertEqual(len(session.history), 4, "history 必须裁到 max_history")
        self.assertEqual(session.history[-1]["text"], "答3")
        self.assertIn("前两轮已压", session.summary)
        self.assertEqual(store.compressed_total, len(spy.calls))

    # 4
    def test_without_summarizer_only_trims_and_warns(self):
        store = SessionStore(clock=self.clock, max_history=2)
        for i in range(3):
            store.upsert_turn(session_id="sess_d", tenant="acme", user_id="u_1",
                              goal=f"问题{i}", answer=f"答{i}")
        with self.assertLogs("core.session", level="WARNING") as logs:
            # 第三次写入之后的历史已经超限，再写一次必然触发滚动裁剪
            store.upsert_turn(session_id="sess_d", tenant="acme", user_id="u_1",
                              goal="问题3", answer="答3")
        session = store.get("sess_d")
        self.assertEqual(len(session.history), 2)
        self.assertEqual(session.summary, "", "没有 summarizer 就不该凭空造摘要")
        self.assertTrue(any("未配置 summarizer" in line for line in logs.output))

    # 5
    def test_ttl_expiry_returns_none_and_deletes(self):
        store = SessionStore(clock=self.clock, ttl_s=60)
        store.upsert_turn(session_id="sess_e", tenant="acme", user_id="u_1",
                          goal="问", answer="答")
        self.assertIsNotNone(store.get("sess_e"))
        self.clock.advance(61)
        self.assertIsNone(store.get("sess_e"), "过期会话必须读不到")
        self.assertTrue(store.was_expired("sess_e"), "过期要被记住（区别于从未存在）")
        self.assertEqual(store.list_ids(), [], "惰性清理必须真的删掉")
        self.assertEqual(store.expired_total, 1)

    # 6
    def test_list_by_user_isolates_tenants(self):
        self.store.upsert_turn(session_id="sess_f1", tenant="acme", user_id="u_1",
                               goal="a", answer="b")
        self.store.upsert_turn(session_id="sess_f2", tenant="globex", user_id="u_1",
                               goal="a", answer="b")
        mine = self.store.list_by_user("acme", "u_1")
        self.assertEqual([s.session_id for s in mine], ["sess_f1"])
        self.assertEqual(self.store.list_by_user("globex", "u_1")[0].session_id, "sess_f2")
        self.assertEqual(self.store.list_by_user("acme", "u_2"), [])
        with self.assertRaises(ValueError):
            self.store.list_by_user("", "u_1")

    # 7
    def test_list_by_user_orders_by_updated_at_desc(self):
        for name in ("sess_g1", "sess_g2", "sess_g3"):
            self.store.upsert_turn(session_id=name, tenant="acme", user_id="u_1",
                                   goal="问", answer="答")
            self.clock.advance(10)
        ordered = [s.session_id for s in self.store.list_by_user("acme", "u_1")]
        self.assertEqual(ordered, ["sess_g3", "sess_g2", "sess_g1"])
        self.assertEqual(len(self.store.list_by_user("acme", "u_1", limit=2)), 2)

    # 8
    def test_delete(self):
        self.store.upsert_turn(session_id="sess_h", tenant="acme", user_id="u_1",
                               goal="问", answer="答")
        self.assertTrue(self.store.delete("sess_h"))
        self.assertIsNone(self.store.get("sess_h"))
        self.assertFalse(self.store.delete("sess_h"), "重复删除必须幂等")
        self.assertEqual(self.store.deletes, 1)

    # 9
    def test_history_carries_only_role_and_text(self):
        # 即使调用方塞进来带工具回包的消息，落库后也必须只剩骨架
        raw = Session(
            session_id="sess_i", tenant="acme", user_id="u_1",
            created_at=1.0, updated_at=1.0, turns=1,
            history=(
                {"role": "user", "text": "查一下", "tool_results": [{"tool": "x"}],
                 "observations": {"a": 1}},
                {"role": "assistant", "text": "查完了", "tool_calls": [{"id": "c1"}]},
                {"role": "system", "text": "重规划"},
                {"role": "tool", "text": "原始回包"},
            ))
        for message in raw.history:
            self.assertEqual(set(message.keys()), set(SKELETON_KEYS))
        self.store.put(raw)
        loaded = self.store.get("sess_i")
        for message in loaded.history:
            self.assertEqual(set(message.keys()), set(SKELETON_KEYS))
            self.assertNotIn("tool_results", message)
            self.assertNotIn("observations", message)
        # 非问答角色统一降级成 user（下游渲染器不该看到陌生角色）
        self.assertEqual([m["role"] for m in loaded.history],
                         [ROLE_USER, ROLE_ASSISTANT, ROLE_USER, ROLE_USER])

    # 10
    def test_persistence_across_store_instances(self):
        backing = MemoryStore()
        first = SessionStore(backing, clock=self.clock)
        first.upsert_turn(session_id="sess_j", tenant="acme", user_id="u_1",
                          goal="问", answer="答")
        second = SessionStore(backing, clock=self.clock)
        loaded = second.get("sess_j")
        self.assertIsNotNone(loaded, "换一个 SessionStore 实例必须能读到同一底层 store")
        self.assertEqual(loaded.turns, 1)
        self.assertEqual(loaded.history[0]["text"], "问")
        # 键空间前缀：不会与 Context 的 ctx_id 混在一起
        self.assertIn("session-sess_j", backing.list_pending())

    # 附加：存储层的租户/用户归属是数据完整性约束，不只靠上层校验
    def test_store_level_ownership_guard(self):
        self.store.upsert_turn(session_id="sess_k", tenant="acme", user_id="u_1",
                               goal="问", answer="答")
        with self.assertRaises(SessionOwnershipError):
            self.store.upsert_turn(session_id="sess_k", tenant="globex",
                                   user_id="u_1", goal="问", answer="答")
        with self.assertRaises(SessionOwnershipError):
            self.store.upsert_turn(session_id="sess_k", tenant="acme",
                                   user_id="u_2", goal="问", answer="答")

    # 附加：坏信封 / 版本不认识 → 抛错而不是猜
    def test_corrupt_payload_raises(self):
        backing = MemoryStore()
        backing.save("session-sess_bad", "{\"schema_version\": 999, \"session\": {}}")
        store = SessionStore(backing, clock=self.clock)
        with self.assertRaises(SessionCorrupt):
            store.get("sess_bad")

    # 附加：summarizer 自己炸了 → 降级为只裁剪，不把异常抛给业务
    def test_summarizer_failure_degrades_to_trim(self):
        store = SessionStore(clock=self.clock, max_history=2,
                             summarizer=BoomSummarizer())
        for i in range(3):
            store.upsert_turn(session_id="sess_l", tenant="acme", user_id="u_1",
                              goal=f"问{i}", answer=f"答{i}")
        session = store.get("sess_l")
        self.assertEqual(len(session.history), 2)
        self.assertEqual(session.summary, "")

    # 附加：len(history) 恰好等于上限时不压缩（避免无谓改写）
    def test_no_compress_when_exactly_at_limit(self):
        spy = SpySummarizer()
        store = SessionStore(clock=self.clock, max_history=4, summarizer=spy)
        store.upsert_turn(session_id="sess_m", tenant="acme", user_id="u_1",
                          goal="问", answer="答")
        store.upsert_turn(session_id="sess_m", tenant="acme", user_id="u_1",
                          goal="问2", answer="答2")
        self.assertEqual(spy.calls, [])

    # 附加：接真 HistoryCompressor（`core/summary.py`）跑一次端到端压缩
    def test_real_history_compressor_integration(self):
        store = SessionStore(clock=self.clock, max_history=4,
                             summarizer=HistoryCompressor(max_keep=4))
        session = None
        for i in range(5):
            session = store.upsert_turn(
                session_id="sess_real", tenant="acme", user_id="u_1",
                goal=f"问题{i} 关于连接池", answer=f"答案{i}")
        self.assertEqual(len(session.history), 4)
        self.assertIn("历史摘要", session.summary)
        self.assertIn("问题0 关于连接池", session.summary)
        self.assertNotIn("问题4", session.summary, "最近这段必须留在 history 原文里")
        self.assertEqual(session.history[-1]["text"], "答案4")

    # 附加：new_session_id 形态与合法性
    def test_new_session_id_is_safe(self):
        sid = new_session_id()
        self.assertTrue(sid.startswith("sess_"))
        store = SessionStore(clock=self.clock)
        store.upsert_turn(session_id=sid, tenant="acme", user_id="u_1",
                          goal="问", answer="答")
        self.assertIsNotNone(store.get(sid))


# ══════════════════════════════════════════════════════════════
# 二、SessionGateway
# ══════════════════════════════════════════════════════════════
class TestSessionGateway(unittest.TestCase):
    """会话网关：预填、回写、隔离、失败不入账、流式。"""

    def setUp(self) -> None:
        self.clock = FakeClock()
        self.sessions = SessionStore(clock=self.clock)
        self.loop = RecordingLoop("答：先调大连接数。")
        self.factory = RecordingContextFactory()
        self.gw = Gateway(lambda goal, tenant_ctx: self.loop,
                          context_factory=self.factory)
        self.sgw = SessionGateway(self.gw, self.sessions)

    # 11
    def test_first_request_without_session_id_generates_one(self):
        result = self.sgw.handle(request())
        self.assertTrue(result.ok, result.error)
        sid = result.vertex["session_id"]
        self.assertTrue(sid.startswith("sess_"))
        self.assertEqual(result.vertex["turn_index"], 0)
        self.assertTrue(result.vertex["session_new"])
        self.assertFalse(result.vertex["session_expired"])
        self.assertTrue(result.vertex["session_persisted"])
        self.assertEqual(self.sessions.get(sid).turns, 1)

    # 12
    def test_second_request_prefills_history_via_request_field(self):
        fake = FakeGateway()
        sgw = SessionGateway(fake, self.sessions)
        self.assertFalse(sgw.history_injected,
                         "替身没有 _context_factory：注入只能走请求字段")
        first = sgw.handle(request(goal="问题一"))
        self.assertEqual(fake.contexts[0]["history"], [])
        sid = first.vertex["session_id"]
        second = sgw.handle(request(goal="问题二", session_id=sid))
        self.assertEqual(second.vertex["turn_index"], 1)
        history = fake.contexts[1]["history"]
        self.assertEqual([m["text"] for m in history], ["问题一", "好的。"])

    # 12b：真 Gateway 的 context_factory 通道也预填（两条通道都验证）
    def test_second_request_prefills_history_via_context_factory(self):
        self.assertTrue(self.sgw.history_injected)
        first = self.sgw.handle(request(goal="问题一"))
        sid = first.vertex["session_id"]
        self.sgw.handle(request(goal="问题二", session_id=sid))
        self.assertEqual(self.factory.initials[0].get("history"), None)
        prefilled = self.factory.initials[1]
        self.assertEqual([m["text"] for m in prefilled["history"]],
                         ["问题一", "答：先调大连接数。"])
        self.assertEqual(prefilled["session_id"], sid)

    # 13
    def test_third_turn_context_contains_previous_two_turns(self):
        sid = None
        for i, goal in enumerate(("问题一", "问题二", "问题三")):
            result = self.sgw.handle(request(goal=goal, session_id=sid) if sid
                                     else request(goal=goal))
            self.assertTrue(result.ok, result.error)
            sid = result.vertex["session_id"]
            self.assertEqual(result.vertex["turn_index"], i)
        third_ctx_history = self.loop.contexts[2].get("history")
        self.assertEqual(len(third_ctx_history), 4)
        self.assertEqual([m["text"] for m in third_ctx_history],
                         ["问题一", "答：先调大连接数。",
                          "问题二", "答：先调大连接数。"])
        self.assertEqual(self.sessions.get(sid).turns, 3)

    # 14
    def test_unknown_session_id_creates_new_session(self):
        result = self.sgw.handle(request(session_id="sess_never_seen"))
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.vertex["session_id"], "sess_never_seen")
        self.assertEqual(result.vertex["turn_index"], 0)
        self.assertTrue(result.vertex["session_new"])
        self.assertFalse(result.vertex["session_expired"],
                         "从未存在 ≠ 过期")
        self.assertEqual(self.sessions.get("sess_never_seen").turns, 1)

    # 15
    def test_tenant_mismatch_forbidden(self):
        sid = self.sgw.handle(request()).vertex["session_id"]
        bad = request(session_id=sid,
                      principal=principal_dict(tenant="globex"))
        result = self.sgw.handle(bad)
        self.assertFalse(result.ok)
        self.assertIn("forbidden", result.error)
        self.assertEqual(result.vertex["http_status"], SESSION_STATUS_FORBIDDEN)
        self.assertEqual(result.vertex["session_id"], sid)
        # 拒绝不能顺手把这一轮写进别人的会话
        self.assertEqual(self.sessions.get(sid).turns, 1)

    # 16
    def test_user_mismatch_forbidden(self):
        sid = self.sgw.handle(request()).vertex["session_id"]
        bad = request(session_id=sid, principal=principal_dict(user_id="u_2"))
        result = self.sgw.handle(bad)
        self.assertFalse(result.ok)
        self.assertIn("forbidden", result.error)
        self.assertEqual(result.vertex["http_status"], SESSION_STATUS_FORBIDDEN)
        self.assertEqual(self.sessions.get(sid).turns, 1)

    # 17
    def test_vertex_carries_session_id_and_turn_index(self):
        first = self.sgw.handle(request())
        self.assertIn("session_id", first.vertex)
        self.assertIn("turn_index", first.vertex)
        second = self.sgw.handle(request(session_id=first.vertex["session_id"]))
        self.assertEqual(second.vertex["session_id"], first.vertex["session_id"])
        self.assertEqual(second.vertex["turn_index"], 1)
        self.assertEqual(second.vertex["session_turns"], 2)
        # to_dict 也要能带出去（客户端靠它拿到下一次的 session_id）
        self.assertEqual(second.to_dict()["vertex"]["session_id"],
                         first.vertex["session_id"])

    # 18
    def test_failed_turn_is_not_recorded(self):
        first = self.sgw.handle(request(goal="问题一"))
        sid = first.vertex["session_id"]
        # 让 Loop 炸掉 → Gateway 返回 ok=False（loop_error）
        self.loop.exc = RuntimeError("llm 挂了")
        failed = self.sgw.handle(request(goal="问题二", session_id=sid))
        self.assertFalse(failed.ok)
        session = self.sessions.get(sid)
        self.assertEqual(session.turns, 1, "失败轮次不得计入 turns")
        self.assertEqual([m["text"] for m in session.history],
                         ["问题一", "答：先调大连接数。"])
        self.assertFalse(failed.vertex["session_persisted"])

    # 19
    def test_expired_session_becomes_new_one_with_flag(self):
        sessions = SessionStore(clock=self.clock, ttl_s=30)
        sgw = SessionGateway(self.gw, sessions)
        sid = sgw.handle(request()).vertex["session_id"]
        self.clock.advance(31)
        result = sgw.handle(request(goal="问题二", session_id=sid))
        self.assertTrue(result.ok, result.error)
        self.assertTrue(result.vertex["session_expired"], "过期必须被标记出来")
        self.assertTrue(result.vertex["session_new"])
        self.assertEqual(result.vertex["turn_index"], 0)
        # 过期会话的历史不能漏进新一轮
        self.assertEqual(len(self.loop.contexts[-1].get("history") or []), 0)
        self.assertEqual(sessions.get(sid).turns, 1)

    # 20
    def test_handle_stream_carries_history(self):
        events1 = list(self.sgw.handle_stream(request(goal="问题一")))
        self.assertEqual([e["type"] for e in events1],
                         ["accepted", "started", "completed"])
        sid = events1[-1]["data"]["session_id"]
        self.assertEqual(events1[-1]["data"]["turn_index"], 0)

        events2 = list(self.sgw.handle_stream(request(goal="问题二", session_id=sid)))
        self.assertEqual(events2[-1]["data"]["session_id"], sid)
        self.assertEqual(events2[-1]["data"]["turn_index"], 1)
        self.assertTrue(events2[-1]["data"]["session_persisted"])
        prefilled = self.factory.initials[1]["history"]
        self.assertEqual([m["text"] for m in prefilled],
                         ["问题一", "答：先调大连接数。"])

    # 21
    def test_history_not_duplicated_between_turns(self):
        sid = None
        for goal in ("问题一", "问题二"):
            result = self.sgw.handle(request(goal=goal, session_id=sid) if sid
                                     else request(goal=goal))
            sid = result.vertex["session_id"]
        session = self.sessions.get(sid)
        self.assertEqual(len(session.history), 4, "同一轮不能被追加两次")
        self.assertEqual(session.turns, 2)
        self.assertEqual([m["role"] for m in session.history],
                         [ROLE_USER, ROLE_ASSISTANT, ROLE_USER, ROLE_ASSISTANT])

    # 22
    def test_context_not_polluted_by_tool_results(self):
        sid = None
        for goal in ("问题一", "问题二", "问题三"):
            result = self.sgw.handle(request(goal=goal, session_id=sid) if sid
                                     else request(goal=goal))
            sid = result.vertex["session_id"]
        ctx = self.loop.contexts[-1]
        self.assertFalse(ctx.get("tool_results"),
                         "会话只带问答骨架：tool_results 必须仍为空")
        self.assertFalse(ctx.get("observations"))
        for message in ctx.get("history") or []:
            self.assertEqual(set(message.keys()), set(SKELETON_KEYS))
        # Context 里确实挂上了 session_id（owner=GATEWAY），但没多带别的
        self.assertEqual(ctx.get("session_id"), sid)

    # 23（附加）：会话摘要（被压掉的那段历史）也要进 Context
    def test_session_summary_reaches_context(self):
        sid = self.sgw.handle(request(goal="问题一")).vertex["session_id"]
        # 模拟「早期历史已被压缩」：直接补给会话一份摘要
        self.sessions.upsert_turn(session_id=sid, tenant="acme", user_id="u_1",
                                  goal="问题一", answer="答一",
                                  summary="【摘要】用户关心连接池上限")
        self.sgw.handle(request(goal="问题二", session_id=sid))
        self.assertIn("连接池上限", self.loop.contexts[-1].get("summary") or "")

    # 24（附加）：续跑（ctx_id）不预填、不重复记账
    def test_resume_with_ctx_id_does_not_prefill_or_append(self):
        ctx_store = MemoryStore()
        gw = Gateway(lambda goal, tenant_ctx: self.loop,
                     store=ctx_store, context_factory=self.factory)
        sgw = SessionGateway(gw, self.sessions)
        first = sgw.handle(request(goal="问题一"))
        sid = first.vertex["session_id"]
        ctx_id = first.vertex["ctx_id"]
        # 手工把这次任务的 Context 落盘，模拟「挂起后可续跑」
        from core.store import save_checkpoint
        save_checkpoint(self.loop.contexts[0], ctx_store, ctx_id)
        before = self.sessions.get(sid).turns
        result = sgw.handle(request(goal="问题一", session_id=sid, ctx_id=ctx_id))
        self.assertTrue(result.ok, result.error)
        self.assertTrue(result.vertex["session_resumed"])
        self.assertFalse(result.vertex["session_persisted"])
        self.assertEqual(self.sessions.get(sid).turns, before,
                         "续跑同一任务不能再记一轮")

    # 25（附加）：session_id 类型错 → 400，且不去碰 Gateway
    def test_bad_session_id_type_rejected(self):
        fake = FakeGateway()
        sgw = SessionGateway(fake, self.sessions)
        result = sgw.handle(request(session_id=123))
        self.assertFalse(result.ok)
        self.assertIn("bad_request", result.error)
        self.assertEqual(result.vertex["http_status"], SESSION_STATUS_BAD_REQUEST)
        self.assertEqual(fake.requests, [], "会话层拒绝的请求不该进 Gateway")

    # 26（附加）：principal 缺失时交给 Gateway 判（会话层不抢鉴权）
    def test_missing_principal_delegates_to_gateway(self):
        result = self.sgw.handle({"goal": "随便问问"})
        self.assertFalse(result.ok)
        self.assertIn("principal", result.error)
        self.assertNotIn("session_id", result.vertex,
                         "未通过身份解析的请求不产生会话")
        self.assertEqual(self.sessions.list_ids(), [])

    # 27（附加）：流式拒绝也是单条事件（客户端状态机可收口）
    def test_stream_rejects_mismatched_tenant_with_single_event(self):
        sid = self.sgw.handle(request()).vertex["session_id"]
        events = list(self.sgw.handle_stream(
            request(session_id=sid, principal=principal_dict(tenant="globex"))))
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["type"], "rejected")
        self.assertEqual(events[0]["data"]["http_status"], SESSION_STATUS_FORBIDDEN)

    # 28（附加）：失败流不写历史
    def test_stream_failure_not_recorded(self):
        fake = FakeGateway(ok=False)
        sgw = SessionGateway(fake, self.sessions)
        events = list(sgw.handle_stream(request(goal="问题一")))
        sid = events[-1]["data"]["session_id"]
        self.assertFalse(events[-1]["data"]["ok"])
        self.assertFalse(events[-1]["data"]["session_persisted"])
        self.assertEqual(self.sessions.list_ids(), [], "失败轮次不创建/不写入会话")


# ══════════════════════════════════════════════════════════════
# 三、端到端：真 Gateway + 真 SessionGateway + 真 Loop
# ══════════════════════════════════════════════════════════════
class EchoBuilder:
    """把 Context 的 history 原样摊进 Prompt（真 Loop 用的最小 builder）。"""

    def __init__(self) -> None:
        self.prompts: list[str] = []
        self.summaries: list[str] = []

    def build(self, snap, tools=()):
        history = snap.get("history") or []
        summary = snap.get("summary") or ""
        self.summaries.append(summary if isinstance(summary, str) else "")
        lines = [f"{m.get('role')}: {m.get('text')}" for m in history]
        prompt = "\n".join(lines) or "（空历史）"
        self.prompts.append(prompt)
        return prompt


class AnswerLLM:
    """按顺序吐出预设答案，并记下每一次拿到的 Prompt。"""

    def __init__(self, answers) -> None:
        self.answers = list(answers)
        self.prompts: list[str] = []

    def complete(self, prompt, tools, e):
        self.prompts.append(prompt)
        text = self.answers.pop(0) if self.answers else "好的。"
        return LLMResponse(text=text, usage=Usage(input_tokens=5, output_tokens=3))


class NoToolsDispatcher:
    manifests: list = []

    def invoke(self, name, args, e):          # pragma: no cover - 永远不该被调用
        raise AssertionError("本用例不涉及工具调用")


class TestEndToEnd(unittest.TestCase):
    """三轮真实对话：验证「会话承接」在完整链路上真的生效。"""

    def _build(self, answers):
        builder = EchoBuilder()
        llm = AnswerLLM(answers)
        factory = RecordingContextFactory()

        def loop_factory(goal, tenant_ctx):
            return Loop(builder, llm, NoToolsDispatcher(), tools=[])

        gw = Gateway(loop_factory, context_factory=factory)
        sessions = SessionStore(clock=FakeClock())
        return gw, sessions, SessionGateway(gw, sessions), builder, llm, factory

    # 23
    def test_end_to_end_three_turns_context_history_len_4(self):
        answers = ["把最大连接数调到 50。", "上限是 128。", "够用了。"]
        gw, sessions, sgw, builder, llm, factory = self._build(answers)

        sid = None
        goals = ["连接池超时怎么办", "那它上限多少", "再看看别的"]
        for i, goal in enumerate(goals):
            payload = {"goal": goal, "principal": principal_dict()}
            if sid:
                payload["session_id"] = sid
            result = sgw.handle(payload)
            self.assertTrue(result.ok, result.error)
            sid = result.vertex["session_id"]
            self.assertEqual(result.vertex["turn_index"], i)

        # 第三轮新建 Context 时的 initial 里正好有前两轮 4 条骨架
        self.assertEqual(len(factory.initials[2]["history"]), 4)
        self.assertEqual(len(factory.initials[1]["history"]), 2)
        self.assertEqual(len(factory.initials[0].get("history") or []), 0)

        session = sessions.get(sid)
        self.assertEqual(session.turns, 3)
        self.assertEqual(len(session.history), 6)     # 3 轮 × 2 条

    # 24
    def test_end_to_end_followup_prompt_contains_previous_turn(self):
        answers = ["把最大连接数从 20 调到 50。", "上限是 128。"]
        gw, sessions, sgw, builder, llm, factory = self._build(answers)

        first = sgw.handle({"goal": "连接池超时怎么办",
                            "principal": principal_dict()})
        self.assertTrue(first.ok, first.error)
        sid = first.vertex["session_id"]

        second = sgw.handle({"goal": "那它上限多少", "principal": principal_dict(),
                             "session_id": sid})
        self.assertTrue(second.ok, second.error)

        followup_prompt = llm.prompts[-1]
        self.assertIn("连接池超时怎么办", followup_prompt)
        self.assertIn("把最大连接数从 20 调到 50。", followup_prompt)
        self.assertIn("那它上限多少", followup_prompt)
        # 第一轮的 Prompt 里当然不该有第二轮的影子
        self.assertNotIn("那它上限多少", llm.prompts[0])
        # 会话里记的是骨架，不是 Prompt 全量
        session = sessions.get(sid)
        self.assertEqual(len(session.history), 4)
        for message in session.history:
            self.assertEqual(set(message.keys()), set(SKELETON_KEYS))

    # 25（端到端附加）：两段会话互不串味
    def test_end_to_end_sessions_are_isolated(self):
        answers = ["答一", "答二", "答三"]
        gw, sessions, sgw, builder, llm, factory = self._build(answers)
        a = sgw.handle({"goal": "会话A的问题", "principal": principal_dict()})
        sid_a = a.vertex["session_id"]
        b = sgw.handle({"goal": "会话B的问题", "principal": principal_dict()})
        sid_b = b.vertex["session_id"]
        self.assertNotEqual(sid_a, sid_b)
        sgw.handle({"goal": "会话A追问", "principal": principal_dict(),
                    "session_id": sid_a})
        prompt = llm.prompts[-1]
        self.assertIn("会话A的问题", prompt)
        self.assertNotIn("会话B的问题", prompt)


if __name__ == "__main__":              # pragma: no cover
    unittest.main(verbosity=2)
