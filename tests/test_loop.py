"""tests/test_loop.py — W2 的 unittest（stdlib only，无 pytest）。

覆盖：
- 6 类终止条件各一例（取消 / 硬截止 / 预算 / 步数上限 / 无进展 / 完成）
- 正常完成 2 轮工具调用后终答 → DONE 且 steps == 3
- WAITING_USER 挂起（needs_confirmation）+ Context 状态
- resume(True) 续跑至 DONE；resume(False) 拒绝原因进 history 且继续循环
- 三级死锁：L1 硬迭代计数、L2 语义指纹（"客套乒乓"，真构造）、L3 仲裁接管
- partial=True 时 text 带原因说明
- 记账顺序（incr loop_count → append history → set usage → tool_results）

约定：只依赖 contracts.py 的 Protocol 形状，全部 fake；**不 import core.context**。
"""
from __future__ import annotations

import copy
import json
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from contracts import (  # noqa: E402
    OWNER_TABLE,
    Approval,
    Budget,
    ContextSnapshot,
    ExecCtx,
    LLMResponse,
    NormalizedToolCall,
    Owner,
    OwnershipViolation,
    Patch,
    Phase,
    Principal,
    Result,
    SideEffect,
    TermReason,
    ToolManifest,
    ToolOutcome,
    ToolResult,
    Usage,
    args_hash,
    now_ms,
)
from core.loop import (  # noqa: E402
    DeadlockDetector,
    Loop,
    action_repr,
    class_of,
    semantic_fingerprint,
)


# ══════════════════════════════════════════════════════════════
# fakes
# ══════════════════════════════════════════════════════════════
class FakeContext:
    """最小 ContextHandle：snapshot() / apply(Patch)，含 OWNER_TABLE 归属校验。"""

    def __init__(self, data: dict | None = None):
        self.data: dict = copy.deepcopy(data or {})
        self.version = 0
        self.patch_log: list[Patch] = []

    def apply(self, patch: Patch) -> int:
        assert isinstance(patch, Patch), f"apply 只吃 Patch，收到 {type(patch)}"
        root = patch.path.split(".")[0]
        owner = OWNER_TABLE.get(root)
        if owner is not None and owner is not patch.by:
            raise OwnershipViolation(f"{patch.by.value} 不能写 {patch.path}（owner={owner.value}）")

        segs = patch.path.split(".")
        container = self.data
        for seg in segs[:-1]:
            nxt = container.get(seg)
            if not isinstance(nxt, dict):
                nxt = {}
                container[seg] = nxt
            container = nxt
        leaf = segs[-1]

        if patch.op == "set":
            container[leaf] = copy.deepcopy(patch.value)
        elif patch.op == "incr":
            container[leaf] = (container.get(leaf) or 0) + patch.value
        elif patch.op == "append":
            cur = container.get(leaf)
            if not isinstance(cur, list):
                cur = []
                container[leaf] = cur
            cur.append(copy.deepcopy(patch.value))
        elif patch.op == "merge":
            cur = container.get(leaf)
            if not isinstance(cur, dict):
                cur = {}
            cur.update(copy.deepcopy(patch.value))
            container[leaf] = cur
        else:
            raise AssertionError(f"未知 op {patch.op}")

        self.version += 1
        self.patch_log.append(patch)
        return self.version

    def snapshot(self) -> ContextSnapshot:
        # via json round-trip 保证快照与 Context 内部状态隔离
        return ContextSnapshot(self.version,
                               json.loads(json.dumps(self.data, ensure_ascii=False, default=str)))


def resp(text: str = "", calls=(), finish: str = "stop", itok: int = 10, otok: int = 5):
    """构造一条 LLMResponse；calls 形如 [(id, name, args), ...]。"""
    norm = tuple(NormalizedToolCall(id=c[0], name=c[1], args=c[2]) for c in calls)
    return LLMResponse(
        text=text,
        tool_calls=norm,
        usage=Usage(input_tokens=itok, output_tokens=otok, cache_hit_tokens=0, model="mock-1"),
        finish_reason=finish,
    )


class ScriptedLLM:
    """按脚本吐响应；cycle=True 时循环脚本（死锁用例需要）。"""

    def __init__(self, script, cycle: bool = False):
        self.script = list(script)
        self.cycle = cycle
        self.calls = 0
        self.prompts: list[str] = []
        self.tools_seen = None
        self.exec_ctxs: list[ExecCtx] = []

    def complete(self, prompt, tools, e):
        index = self.calls
        self.calls += 1
        self.prompts.append(prompt)
        self.tools_seen = tools
        self.exec_ctxs.append(e)
        if not self.script:
            item = resp(text="（脚本为空）")
        elif index < len(self.script):
            item = self.script[index]
        elif self.cycle:
            item = self.script[index % len(self.script)]
        else:
            item = self.script[-1]
        if callable(item):
            item = item(prompt, e, index)
        return item


class FakeDispatcher:
    manifests = [
        ToolManifest(name="lookup", side_effect=SideEffect.READ_ONLY),
        ToolManifest(name="write_note", side_effect=SideEffect.MUTATING),
        ToolManifest(name="send_message", side_effect=SideEffect.READ_ONLY),
    ]

    def __init__(self, table: dict | None = None):
        self.table = dict(table or {})
        self.calls: list[tuple[str, dict]] = []

    def invoke(self, name, args, e):
        self.calls.append((name, dict(args)))
        entry = self.table.get(name)
        if entry is None:
            return ToolOutcome.ok(ToolResult(tool=name, payload={"echo": args}))
        if callable(entry):
            return entry(name, args, e)
        if isinstance(entry, list):
            return entry.pop(0) if len(entry) > 1 else entry[0]
        return entry

    def count(self, name: str) -> int:
        return sum(1 for n, _ in self.calls if n == name)


class FakeBuilder:
    def __init__(self):
        self.snapshots: list[ContextSnapshot] = []
        self.tools_seen = None

    def build(self, snap, tools):
        self.snapshots.append(snap)
        self.tools_seen = tools
        history = snap.get("history") or []
        joined = " ".join(str(m.get("text") or "") for m in history if isinstance(m, dict))
        obs = snap.get("observations")
        extra = json.dumps(obs, ensure_ascii=False, default=str) if obs else ""
        return f"GOAL>{joined}#OBS>{extra}"


class FakePlanner:
    def __init__(self, result=None):
        self.calls: list[str] = []
        self.result = result

    def replan(self, snap, reason):
        self.calls.append(reason)
        if self.result is not None:
            return self.result
        return Patch("append", "plan", Owner.PLANNER, {"replan": reason})


# ── 便捷构造 ────────────────────────────────────────────────────
def make_exec(**budget_kw) -> ExecCtx:
    kw = dict(budget_kw)
    budget = Budget(**kw) if kw else Budget()
    return ExecCtx(principal=Principal(), budget=budget)


def make_loop(script, table=None, cycle=False, planner=None, window=4, tracer=None):
    llm = ScriptedLLM(script, cycle=cycle)
    dispatcher = FakeDispatcher(table)
    builder = FakeBuilder()
    loop = Loop(builder, llm, dispatcher, tracer=tracer, planner=planner, window=window)
    return loop, llm, dispatcher, builder


LOOKUP = resp(text="我先查一下。", calls=[("c1", "lookup", {"q": "alpha"})])
WRITE = resp(text="我记一条。", calls=[("c2", "write_note", {"text": "alpha done"})])
FINAL = resp(text="任务完成：alpha 已处理。", finish="stop")


# ══════════════════════════════════════════════════════════════
# 1~6：六类终止条件
# ══════════════════════════════════════════════════════════════
class TestTerminationConditions(unittest.TestCase):
    def test_01_aborted(self):
        loop, llm, dispatcher, _ = make_loop([LOOKUP, FINAL])
        ctx, e = FakeContext(), make_exec()
        e.abort()
        r = loop.run("查 alpha", ctx, e)
        self.assertIsInstance(r, Result)
        self.assertEqual(r.phase, Phase.ABORTED)
        self.assertEqual(r.term_reason, TermReason.ABORTED)
        self.assertFalse(r.partial)
        self.assertEqual(r.steps, 0)
        self.assertEqual(llm.calls, 0, "取消后不应再调 LLM")
        self.assertEqual(dispatcher.calls, [])
        self.assertEqual(ctx.data["phase"], Phase.ABORTED.value)

    def test_02_deadline(self):
        loop, llm, dispatcher, _ = make_loop([LOOKUP, FINAL])
        ctx = FakeContext()
        e = make_exec()
        e.deadline = now_ms() - 1000.0        # 已过期
        r = loop.run("查 alpha", ctx, e)
        self.assertEqual(r.term_reason, TermReason.DEADLINE)
        self.assertEqual(r.phase, Phase.FAILED)
        self.assertTrue(r.partial)
        self.assertIn("硬截止", r.text)
        self.assertEqual(llm.calls, 0)

    def test_03_budget(self):
        loop, llm, dispatcher, _ = make_loop([LOOKUP, FINAL])
        ctx = FakeContext()
        e = make_exec(token_total=100, token_used=1000)   # token_left < 0
        r = loop.run("查 alpha", ctx, e)
        self.assertEqual(r.term_reason, TermReason.BUDGET)
        self.assertTrue(r.partial)
        self.assertIn("预算耗尽", r.text)
        self.assertIn("token", r.text)
        self.assertEqual(llm.calls, 0)

    def test_04_max_steps(self):
        loop, llm, dispatcher, _ = make_loop([LOOKUP], cycle=True)
        ctx = FakeContext()
        e = make_exec(max_steps=2)
        r = loop.run("查 alpha", ctx, e)
        self.assertEqual(r.term_reason, TermReason.MAX_STEPS)
        self.assertTrue(r.partial)
        self.assertEqual(r.steps, 2)
        self.assertEqual(llm.calls, 2, "上限是 2 步，就只允许 2 次 LLM 调用")
        self.assertIn("步数上限", r.text)

    def test_05_no_progress(self):
        loop, llm, dispatcher, _ = make_loop([LOOKUP], cycle=True)   # 永远同一个动作
        ctx = FakeContext()
        e = make_exec()
        r = loop.run("查 alpha", ctx, e)
        self.assertEqual(r.term_reason, TermReason.NO_PROGRESS)
        self.assertTrue(r.partial)
        self.assertEqual(r.steps, 4, "window=4：第 4 次观察命中，第 5 轮开头退出")
        self.assertIn("无进展", r.text)

    def test_06_done_immediately(self):
        loop, llm, dispatcher, _ = make_loop([FINAL])
        ctx = FakeContext()
        e = make_exec()
        r = loop.run("查 alpha", ctx, e)
        self.assertEqual(r.term_reason, TermReason.DONE)
        self.assertEqual(r.phase, Phase.DONE)
        self.assertFalse(r.partial)
        self.assertEqual(r.steps, 1)
        self.assertEqual(r.text, FINAL.text)
        self.assertEqual(ctx.data["phase"], Phase.DONE.value)

    def test_07_done_after_two_tool_rounds_steps_is_three(self):
        loop, llm, dispatcher, _ = make_loop([LOOKUP, WRITE, FINAL])
        ctx = FakeContext()
        e = make_exec()
        r = loop.run("处理 alpha", ctx, e)
        self.assertEqual(r.term_reason, TermReason.DONE)
        self.assertEqual(r.steps, 3)
        self.assertEqual(llm.calls, 3)
        self.assertEqual([n for n, _ in dispatcher.calls], ["lookup", "write_note"])
        self.assertEqual(len(ctx.data["tool_results"]), 2)
        self.assertEqual(ctx.data["loop_count"], 3)
        # token 记账：3 轮 × 15 = 45
        self.assertEqual(e.budget.token_used, 45)
        self.assertEqual(ctx.data["usage"]["total_tokens"], 45)
        self.assertEqual(ctx.data["usage"]["calls"], 3)


# ══════════════════════════════════════════════════════════════
# 7~9：WAITING_USER 挂起 / 恢复
# ══════════════════════════════════════════════════════════════
class TestWaitingUser(unittest.TestCase):
    def _suspending_loop(self, script_tail):
        table = {"write_note": [
            ToolOutcome.needs_confirmation("写操作需要人工确认"),
            ToolOutcome.ok(ToolResult(tool="write_note", payload={"written": True})),
        ]}
        return make_loop([LOOKUP, WRITE, *script_tail], table=table)

    def test_08_needs_confirmation_suspends(self):
        loop, llm, dispatcher, _ = self._suspending_loop([])
        ctx, e = FakeContext(), make_exec()
        r = loop.run("处理 alpha", ctx, e)

        self.assertEqual(r.phase, Phase.WAITING_USER)
        self.assertEqual(r.term_reason, TermReason.NEEDS_CONFIRMATION)
        self.assertEqual(r.steps, 2)
        self.assertEqual(ctx.data["phase"], Phase.WAITING_USER.value)

        pending = ctx.data.get("pending_confirmation")
        self.assertIsInstance(pending, dict)
        self.assertTrue(pending, "pending_confirmation 不能为空")
        self.assertEqual(pending["tool"], "write_note")
        self.assertEqual(pending["args_hash"], args_hash({"text": "alpha done"}))
        self.assertEqual(pending["args"], {"text": "alpha done"})
        # 挂起后不应继续循环：LLM 只被调了 2 次
        self.assertEqual(llm.calls, 2)
        # 被挂起的工具确实被问过一次
        self.assertEqual(dispatcher.count("write_note"), 1)

    def test_09_resume_approved_runs_to_done(self):
        loop, llm, dispatcher, _ = self._suspending_loop([FINAL])
        ctx, e = FakeContext(), make_exec()
        r1 = loop.run("处理 alpha", ctx, e)
        self.assertEqual(r1.phase, Phase.WAITING_USER)

        e2 = make_exec()                                   # 新一轮 ExecCtx
        # 编排者通常会把审批记录带进 e.approvals；这里先给空，验证 Loop 会补齐
        r2 = loop.resume(ctx, e2, decision=True, reason="已人工审核")

        self.assertEqual(r2.term_reason, TermReason.DONE, r2.text)
        self.assertEqual(r2.phase, Phase.DONE)
        self.assertEqual(dispatcher.count("write_note"), 2, "批准后应真正执行一次")
        self.assertTrue(e2.approved("write_note", args_hash({"text": "alpha done"})))
        self.assertIsNone(ctx.data.get("pending_confirmation"))
        history_text = " ".join(str(m.get("text") or "") for m in ctx.data["history"])
        self.assertIn("批准执行 write_note", history_text)
        # 挂起的那一轮 + 重放 + 终答
        self.assertGreaterEqual(r2.steps, 3)
        self.assertEqual(r2.term_reason, TermReason.DONE)

    def test_09b_resume_with_preexisting_approval(self):
        loop, llm, dispatcher, _ = self._suspending_loop([FINAL])
        ctx, e = FakeContext(), make_exec()
        loop.run("处理 alpha", ctx, e)

        e2 = make_exec()
        e2.approvals = (Approval(tool="write_note",
                                 args_hash=args_hash({"text": "alpha done"}),
                                 approved=True),)
        r = loop.resume(ctx, e2, decision=True)
        self.assertEqual(r.term_reason, TermReason.DONE)
        self.assertEqual(len(e2.approvals), 1, "已存在的批准不应被重复追加")

    def test_10_resume_rejected_writes_history_and_continues(self):
        loop, llm, dispatcher, _ = self._suspending_loop(
            [resp(text="那我改用只读方案。", calls=[("c3", "lookup", {"q": "alpha-readonly"})]), FINAL])
        ctx, e = FakeContext(), make_exec()
        r1 = loop.run("处理 alpha", ctx, e)
        self.assertEqual(r1.phase, Phase.WAITING_USER)

        r2 = loop.resume(ctx, make_exec(), decision=False, reason="不允许写库")

        self.assertEqual(r2.term_reason, TermReason.DONE, r2.text)
        history_text = " ".join(str(m.get("text") or "") for m in ctx.data["history"])
        self.assertIn("拒绝", history_text)
        self.assertIn("不允许写库", history_text)
        self.assertIsNone(ctx.data.get("pending_confirmation"))
        self.assertEqual(dispatcher.count("write_note"), 1, "被拒绝的动作不应重放")
        self.assertEqual(dispatcher.count("lookup"), 2, "拒绝后应换方案继续跑")


# ══════════════════════════════════════════════════════════════
# 10~13：三级死锁
# ══════════════════════════════════════════════════════════════
class TestDeadlock(unittest.TestCase):
    def test_11_level1_identical_actions(self):
        # 完全相同的动作重复 4 轮 → L1
        same = resp(text="再查一次。", calls=[("c9", "lookup", {"q": "alpha"})])
        loop, llm, dispatcher, _ = make_loop([same], cycle=True)
        ctx, e = FakeContext(), make_exec()
        r = loop.run("查 alpha", ctx, e)

        self.assertEqual(r.term_reason, TermReason.NO_PROGRESS)
        self.assertTrue(r.partial)
        self.assertIn("L1", r.text)
        self.assertIn("连续 4 轮动作完全相同", r.text)
        self.assertEqual(r.steps, 4)
        self.assertEqual(loop.detector.hits, 1)
        self.assertEqual(loop.detector.last_level, "L1")

    def test_12_level2_semantic_pingpong(self):
        """真实构造"客套乒乓"：字面全不同，语义指纹归一 → L2 死锁。

        A 说「感谢建议」→ B 说「不客气，还有什么需要」→ A 说「多谢指教」→ …
        四轮全部是 send_message 的自然语言短句，收件人也在两人之间来回。
        """
        pleasantries = [
            ("A", "感谢建议"),
            ("B", "不客气，还有什么需要吗"),
            ("A", "多谢指教，先这样"),
            ("B", "好的，随时联系我"),
            ("A", "感谢配合"),
            ("B", "应该的，不用客气"),
        ]
        script = [
            resp(text=f"{who} 回了一句客套话。",
                 calls=[(f"p{i}", "send_message", {"to": who, "text": phrase})])
            for i, (who, phrase) in enumerate(pleasantries)
        ]
        loop, llm, dispatcher, _ = make_loop(script, cycle=True)
        ctx, e = FakeContext(), make_exec()
        r = loop.run("让两个 agent 对接", ctx, e)

        self.assertEqual(r.term_reason, TermReason.NO_PROGRESS, r.text)
        self.assertTrue(r.partial)
        self.assertIn("L2", r.text)
        self.assertIn("语义指纹", r.text)
        self.assertIn("字面不同", r.text)
        self.assertEqual(loop.detector.hits, 1)
        self.assertEqual(loop.detector.last_level, "L2")
        # 前 4 步真的各不相同（字面不同才能证明是 L2 而不是 L1）
        literals = [action_repr("send_message", {"to": w, "text": p}) for w, p in pleasantries[:4]]
        self.assertEqual(len(set(literals)), 4, "构造的字面必须互不相同")
        self.assertEqual(dispatcher.count("send_message"), 4)

    def test_12b_semantic_fingerprint_unit(self):
        def fp(to, text):
            return semantic_fingerprint(action_repr("send_message", {"to": to, "text": text}))

        # 客套乒乓：语义归一
        self.assertEqual(fp("A", "感谢建议"), fp("B", "不客气，还有什么需要吗"))
        self.assertEqual(fp("A", "多谢指教"), fp("B", "好的，随时联系我"))
        # 数字/时间戳/路径这些易变部分被吸收
        self.assertEqual(
            semantic_fingerprint(action_repr("read", {"path": "/tmp/a/b.txt", "line": 3})),
            semantic_fingerprint(action_repr("read", {"path": "/var/x/y.log", "line": 88})),
        )
        self.assertEqual(
            semantic_fingerprint(action_repr("wait", {"ms": 500, "at": "2026-11-22T20:00:00Z"})),
            semantic_fingerprint(action_repr("wait", {"ms": 900, "at": "2026-11-23T01:02:03Z"})),
        )
        # 但不同动作仍需区分（否则太粗会把真进展当死锁）
        self.assertNotEqual(semantic_fingerprint(action_repr("read", {"line": 1})),
                            semantic_fingerprint(action_repr("write", {"line": 1})))
        # 字面指纹本身必须不同（L1 不会误报 L2 的用例）
        self.assertNotEqual(action_repr("send_message", {"to": "A", "text": "感谢建议"}),
                            action_repr("send_message", {"to": "B", "text": "不客气"}))
        self.assertEqual(class_of(123), "<NUM>")
        self.assertEqual(class_of("感谢建议"), class_of("不客气，还有什么需要吗"))

    def test_13_level3_forces_replan_then_recovers(self):
        """L3：命中后先强制 replan；replan 后换方案 → 能跑到 DONE。"""
        ping = resp(text="客套一句。",
                    calls=[("q0", "send_message", {"to": "A", "text": "感谢建议"})])
        variants = [
            resp(text="客套。", calls=[("q1", "send_message", {"to": "B", "text": "不客气"})]),
            resp(text="客套。", calls=[("q2", "send_message", {"to": "A", "text": "多谢"})]),
            resp(text="客套。", calls=[("q3", "send_message", {"to": "B", "text": "好的没问题"})]),
            resp(text="客套。", calls=[("q4", "send_message", {"to": "A", "text": "再见"})]),
        ]
        script = [ping, *variants]

        def after_replan(prompt, e, index):
            if "replan" in prompt:                 # builder 把 history 拼进 prompt
                return resp(text="换方案后完成：直接给结论。")   # 终答 → DONE
            return resp(text="客套。",
                        calls=[("qz", "send_message", {"to": "A", "text": "感谢建议"})])

        planner = FakePlanner()
        loop, llm, dispatcher, builder = make_loop(script, cycle=False, planner=planner)
        # 脚本耗尽后都走 after_replan
        llm.script.extend([after_replan] * 12)

        ctx, e = FakeContext(), make_exec()
        r = loop.run("让两个 agent 对接", ctx, e)

        self.assertEqual(len(planner.calls), 1, "死锁命中后必须强制 replan 一次")
        self.assertIn("L", planner.calls[0])
        self.assertTrue(any("replan" in p for p in llm.prompts))
        self.assertEqual(r.term_reason, TermReason.DONE, r.text)
        # replan 写了 planner 自己的字段（plan），Loop 不该拦；
        # observations 归 LOOP（观测事实的权威源），也不该被 planner 写
        self.assertTrue(ctx.data.get("plan"))

    def test_13b_detector_unit_hard_limit(self):
        det = DeadlockDetector(window=2, hard_limit=2)
        locked, detail = det.observe(action_repr("lookup", {"q": "a"}))
        self.assertFalse(locked)
        locked, detail = det.observe(action_repr("lookup", {"q": "a"}))
        self.assertTrue(locked)
        self.assertIn("L1", detail)
        self.assertEqual(det.hits, 1)
        locked, detail = det.observe(action_repr("lookup", {"q": "a"}))
        self.assertTrue(locked)
        self.assertIn("L3", detail)
        self.assertIn("人工仲裁", detail)
        self.assertGreaterEqual(det.hits, det.hard_limit)

    def test_13c_detector_level2_needs_literal_difference(self):
        det = DeadlockDetector(window=3)
        # 同一语义、字面不同 → L2
        for i in range(3):
            locked, detail = det.observe(
                action_repr("send_message", {"to": "A", "text": f"感谢建议{i}"}))
        self.assertTrue(locked)
        self.assertIn("L2", detail)
        # reset_window 后不残留
        det.reset_window()
        locked, _ = det.observe(action_repr("lookup", {"q": "a"}))
        self.assertFalse(locked)
        self.assertEqual(det.hits, 1, "reset_window 保留累计 hits")

    def test_13d_replan_once_then_still_locked_exits_no_progress(self):
        """L3 出口：planner 只被强制调用一次；replan 无效 → NO_PROGRESS（而非死循环）。"""
        phrases = [("A", "感谢建议"), ("B", "不客气，还有什么需要吗"),
                   ("A", "多谢指教"), ("B", "好的随时联系"), ("A", "感谢配合"),
                   ("B", "应该的"), ("A", "先这样"), ("B", "随时找我")]
        script = [resp(text="客套。", calls=[(f"r{i}", "send_message", {"to": w, "text": t})])
                  for i, (w, t) in enumerate(phrases)]
        planner = FakePlanner()
        loop, llm, dispatcher, _ = make_loop(script, cycle=True, planner=planner)
        ctx, e = FakeContext(), make_exec(max_steps=12)
        r = loop.run("让两个 agent 对接", ctx, e)

        self.assertEqual(len(planner.calls), 1, "只强制一次 replan")
        self.assertEqual(r.term_reason, TermReason.NO_PROGRESS, r.text)
        self.assertTrue(r.partial)
        self.assertIn("无进展", r.text)
        self.assertEqual(loop.detector.hits, 2, "replan 后再次命中")
        self.assertTrue(any("replan" in str(m.get("text") or "") for m in ctx.data["history"]))


# ══════════════════════════════════════════════════════════════
# 14~：partial 文案 / 记账顺序 / 装配
# ══════════════════════════════════════════════════════════════
class TestPartialAndAccounting(unittest.TestCase):
    def test_14_partial_results_carry_reason(self):
        cases = []

        # deadline
        loop, *_ = make_loop([LOOKUP, FINAL])
        e = make_exec()
        e.deadline = now_ms() - 5
        cases.append((loop.run("x", FakeContext(), e), "硬截止"))

        # budget
        loop, *_ = make_loop([LOOKUP, FINAL])
        cases.append((loop.run("x", FakeContext(), make_exec(cost_total_usd=0.0,
                                                             cost_used_usd=1.0)), "预算耗尽"))

        # max_steps
        loop, *_ = make_loop([LOOKUP], cycle=True)
        cases.append((loop.run("x", FakeContext(), make_exec(max_steps=1)), "步数上限"))

        # no_progress
        loop, *_ = make_loop([resp(text="", calls=[("d", "lookup", {"q": "z"})])], cycle=True)
        cases.append((loop.run("x", FakeContext(), make_exec()), "无进展"))

        for result, keyword in cases:
            self.assertTrue(result.partial, f"{result.term_reason} 应为 partial")
            self.assertTrue(result.text.startswith("未完成，原因："), result.text)
            self.assertIn(keyword, result.text)
            self.assertIn("已完成", result.text)
            self.assertNotEqual(result.term_reason, TermReason.DONE)
            self.assertIsInstance(result.artifacts, tuple)

    def test_15_accounting_order(self):
        loop, llm, dispatcher, _ = make_loop([LOOKUP, FINAL])
        ctx, e = FakeContext(), make_exec()
        loop.run("查 alpha", ctx, e)

        def first_index(predicate):
            for i, patch in enumerate(ctx.patch_log):
                if predicate(patch):
                    return i
            return -1

        i_incr = first_index(lambda p: p.op == "incr" and p.path == "loop_count")
        i_assistant = first_index(lambda p: p.op == "append" and p.path == "history"
                                  and isinstance(p.value, dict)
                                  and p.value.get("role") == "assistant")
        i_usage = first_index(lambda p: p.op == "set" and p.path == "usage")
        i_tool = first_index(lambda p: p.op == "append" and p.path == "tool_results")

        self.assertGreater(i_incr, -1)
        self.assertGreater(i_assistant, i_incr, "记账顺序：incr loop_count 先于 history")
        self.assertGreater(i_usage, i_assistant, "记账顺序：history 先于 usage")
        self.assertGreater(i_tool, i_usage, "记账顺序：usage 先于 tool_results")
        # 所有 patch 都必须是 Owner.LOOP（Context 归属校验也没炸）
        self.assertTrue(all(p.by is Owner.LOOP for p in ctx.patch_log))
        # assistant 消息里带了 tool_calls
        assistants = [m for m in ctx.data["history"] if m.get("role") == "assistant"]
        self.assertEqual(len(assistants), 2)
        self.assertEqual(assistants[0]["tool_calls"][0]["name"], "lookup")

    def test_16_wiring_and_owner_safety(self):
        loop, llm, dispatcher, builder = make_loop([LOOKUP, FINAL])
        ctx, e = FakeContext(), make_exec()
        r = loop.run("查 alpha", ctx, e)
        self.assertEqual(r.term_reason, TermReason.DONE)

        # builder / llm 都拿到 dispatcher 的 manifests（Loop 自动推断）
        self.assertEqual([m.name for m in builder.tools_seen],
                         ["lookup", "write_note", "send_message"])
        self.assertIs(builder.tools_seen, llm.tools_seen)
        # builder 拿到的是 snapshot（不可变视图），能看到 user goal
        self.assertIsInstance(builder.snapshots[0], ContextSnapshot)
        self.assertIn("查 alpha", builder.snapshots[0].get("history")[0]["text"])
        # llm 拿到同一个 ExecCtx
        self.assertTrue(all(ec is e for ec in llm.exec_ctxs))
        # tool_calls_used 由 Loop 兜底记账（dispatcher 没记）
        self.assertEqual(e.budget.tool_calls_used, 1)
        # 幂等：同一 Context 再跑不会重置计数（loop_count 继续累加）
        before = ctx.data["loop_count"]
        loop2, *_ = make_loop([FINAL])
        loop2.run("继续", ctx, e)
        self.assertGreater(ctx.data["loop_count"], before)

    def test_17_empty_response_is_bounded(self):
        loop, llm, dispatcher, _ = make_loop([resp(text="")], cycle=True)
        ctx, e = FakeContext(), make_exec(max_steps=3)
        r = loop.run("x", ctx, e)
        self.assertIn(r.term_reason, (TermReason.MAX_STEPS, TermReason.NO_PROGRESS))
        self.assertTrue(r.partial)
        self.assertEqual(llm.calls, 3)

    def test_18_llm_exception_is_bounded(self):
        class BoomLLM:
            def __init__(self):
                self.calls = 0

            def complete(self, prompt, tools, e):
                self.calls += 1
                raise RuntimeError("上游 502")

        loop = Loop(FakeBuilder(), BoomLLM(), FakeDispatcher())
        r = loop.run("x", FakeContext(), make_exec())
        self.assertEqual(r.term_reason, TermReason.UNRECOVERABLE)
        self.assertTrue(r.partial)
        self.assertIn("502", r.text)

    def test_19_resume_without_pending_runs_forward(self):
        loop, llm, dispatcher, _ = make_loop([FINAL])
        ctx, e = FakeContext(), make_exec()
        r = loop.resume(ctx, e, decision=True)
        self.assertEqual(r.term_reason, TermReason.DONE)
        self.assertTrue(any("没有 pending_confirmation" in str(m.get("text"))
                            for m in ctx.data["history"]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
