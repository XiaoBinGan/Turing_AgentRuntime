"""副作用落盘：崩溃后能知道「哪一步已经真做了」。

**为什么需要**：原来的 checkpoint **只在 `WAITING_USER` 挂起时**落盘。
长回合（真模型实测 4-6 步 / 26 次工具 / 40 秒）中途崩了，前几步全丢 ——
而其中可能有"已经改了生产"的操作。

**为什么只在「非只读工具成功之后」落**：
  * 只读取证丢了重跑就行（幂等且便宜）
  * 已经发生的写操作丢了是灾难 —— **你不知道它到底做没做**

所以写入成本只付在真正需要的地方，不是每步全量序列化。
"""
from __future__ import annotations

import unittest

from builder import PromptBuilder
from contracts import (
    ExecCtx, LLMResponse, NormalizedToolCall, RiskTier, SideEffect, ToolManifest,
    Usage,
)
from core.gateway import Gateway
from core.loop import Loop
from core.store import MemoryStore, list_resumable, load_checkpoint
from plane.governance import Governance, InputGuard, OutputGuard, RBAC
from plane.tool import Dispatcher, ToolRegistry

P = {"user_id": "u", "tenant": "acme", "roles": ["r"],
     "scopes": ["runtime:run", "tool.read", "tool.write"]}


def manifest(name, effect=SideEffect.READ_ONLY, tier=RiskTier.T0_SAFE, **kw):
    return ToolManifest(name=name, side_effect=effect, risk_tier=tier,
                        description=name, input_schema={"type": "object",
                                                        "properties": {}}, **kw)


def registry(*manifests_and_fns):
    reg = ToolRegistry()
    for m, fn in manifests_and_fns:
        reg.register(m, fn)
    return reg


class ScriptedLLM:
    """按脚本逐次返回；脚本用完就抛（模拟崩溃）。"""

    model = "scripted"

    def __init__(self, responses, then_raise=None):
        self._responses = list(responses)
        self._then = then_raise
        self.i = 0

    def complete(self, prompt, tools, e):
        self.i += 1
        if self.i <= len(self._responses):
            return self._responses[self.i - 1]
        if self._then is not None:
            raise self._then
        return LLMResponse(text="完成", usage=Usage())


def call(name, args=None):
    return LLMResponse(text="调", tool_calls=(
        NormalizedToolCall(id=f"c_{name}", name=name, args=args or {}),),
        usage=Usage())


def gateway(reg, llm, store):
    def factory(goal, tc):
        return Loop(PromptBuilder(total_tokens=4000), llm, Dispatcher(reg))
    return Gateway(factory, governance=Governance(
        rbac=RBAC(), input_guard=InputGuard(), output_guard=OutputGuard()),
        store=store)


class TestReadOnlyDoesNotCheckpoint(unittest.TestCase):
    def test_read_only_skipped(self):
        """只读工具**不**落盘 —— 重跑就行，不值得付写入成本。"""
        store = MemoryStore()
        reg = registry((manifest("read1"), lambda a, e: {"v": 1}))
        gw = gateway(reg, ScriptedLLM([call("read1")]), store)
        gw.handle({"goal": "x", "principal": P})
        self.assertEqual(list_resumable(store), [],
                         "只读工具不该触发落盘")


class TestSideEffectCheckpoints(unittest.TestCase):
    def test_mutating_triggers(self):
        store = MemoryStore()
        reg = registry((manifest("deploy", SideEffect.MUTATING, RiskTier.T1_LOW),
                        lambda a, e: {"ok": 1}))
        gw = gateway(reg, ScriptedLLM([call("deploy", {"env": "prod"})]), store)
        gw.handle({"goal": "x", "principal": P})
        self.assertEqual(len(list_resumable(store)), 1, "写工具成功后必须落盘")

    def test_multiple_side_effects_each_checkpoint(self):
        """每次写操作各落一份 —— 不是"一次就够"。

        计数看 `store.saves` 而不是 `list_resumable()`：同一个 `ctx_id`
        落三次只会有**一个键**（后覆盖前），但保存动作发生了三次。
        """
        store = MemoryStore()
        reg = registry((manifest("deploy", SideEffect.MUTATING, RiskTier.T1_LOW),
                        lambda a, e: {"ok": 1}))
        gw = gateway(reg, ScriptedLLM([
            call("deploy", {"n": 1}),
            call("deploy", {"n": 2}),
            call("deploy", {"n": 3}),
        ]), store)
        gw.handle({"goal": "x", "principal": P})
        self.assertEqual(store.saves, 3, f"应落 3 次，实际 {store.saves}")
        self.assertEqual(len(list_resumable(store)), 1, "同一 ctx_id 只一个键")

    def test_irreversible_triggers(self):
        store = MemoryStore()
        reg = registry((manifest("drop_db", SideEffect.IRREVERSIBLE, RiskTier.T1_LOW),
                        lambda a, e: {"ok": 1}))
        gw = gateway(reg, ScriptedLLM([call("drop_db")]), store)
        gw.handle({"goal": "x", "principal": P})
        self.assertEqual(len(list_resumable(store)), 1, "不可逆操作更要落盘")

    def test_failed_side_effect_no_checkpoint(self):
        """**失败**的写操作不落盘 —— 它没生效，落盘只会误导恢复。"""
        store = MemoryStore()
        reg = registry((manifest("deploy", SideEffect.MUTATING, RiskTier.T1_LOW),
                        lambda a, e: (_ for _ in ()).throw(RuntimeError("boom"))))
        gw = gateway(reg, ScriptedLLM([call("deploy")]), store)
        gw.handle({"goal": "x", "principal": P})
        self.assertEqual(list_resumable(store), [])


class TestCrashRecoveryValue(unittest.TestCase):
    """**核心价值**：崩溃后恢复要能看出「这次写操作做没做」。"""

    def test_resume_shows_completed_side_effect(self):
        store = MemoryStore()
        reg = registry((manifest("deploy", SideEffect.MUTATING, RiskTier.T1_LOW),
                        lambda a, e: {"ok": 1, "env": "prod"}))
        llm = ScriptedLLM([call("deploy", {"env": "prod"})],
                          then_raise=RuntimeError("LLM 第 2 步崩了"))
        gw = gateway(reg, llm, store)
        gw.handle({"goal": "部署", "principal": P})

        rows = list_resumable(store)
        self.assertTrue(rows, "崩溃前那次写操作没有落盘 → 丢的就是灾难")
        ctx = load_checkpoint(store, rows[0]["ctx_id"])
        tools = [t.get("tool") for t in (ctx.get("tool_results") or [])]
        self.assertIn("deploy", tools, "恢复后看不出 deploy 做过")
        obs = [(o["tool"], o["kind"]) for o in (ctx.get("observations") or [])]
        self.assertIn(("deploy", "ok"), obs)

    def test_reason_mentions_tool(self):
        """落盘 reason 要写明**哪个工具** —— 恢复时第一眼要能看懂。"""
        store = MemoryStore()
        reg = registry((manifest("restart_svc", SideEffect.MUTATING, RiskTier.T1_LOW),
                        lambda a, e: {"ok": 1}))
        gw = gateway(reg, ScriptedLLM([call("restart_svc")]), store)
        gw.handle({"goal": "x", "principal": P})
        ctx_id = list_resumable(store)[0]["ctx_id"]
        ctx = load_checkpoint(store, ctx_id)
        marks = [h.get("text", "") for h in (ctx.get("history") or [])
                 if h.get("role") == "system"]
        self.assertTrue(any("restart_svc" in m for m in marks),
                        f"reason 里没有工具名：{marks}")


class TestCheckpointErrorsSurfaced(unittest.TestCase):
    """落盘失败必须**报出去** —— 吞掉等于「没落盘还谎称落了」。"""

    def test_error_visible_in_vertex(self):
        class BrokenStore:
            def save(self, *a, **k):
                raise RuntimeError("disk full")

            def load(self, *a, **k):
                return None

            def list_pending(self):
                return []

        reg = registry((manifest("deploy", SideEffect.MUTATING, RiskTier.T1_LOW),
                        lambda a, e: {"ok": 1}))
        gw = gateway(reg, ScriptedLLM([call("deploy")]), BrokenStore())
        r = gw.handle({"goal": "x", "principal": P})
        self.assertTrue(r.ok, "回合本身成功了，不该因为落盘失败改成失败")
        self.assertEqual((r.vertex or {}).get("checkpoint_errors_count"), 1)
        self.assertIn("disk full", str((r.vertex or {}).get("checkpoint_errors")))


class TestNoStoreNoCheckpoint(unittest.TestCase):
    def test_without_store_is_noop(self):
        reg = registry((manifest("deploy", SideEffect.MUTATING, RiskTier.T1_LOW),
                        lambda a, e: {"ok": 1}))
        gw = gateway(reg, ScriptedLLM([call("deploy")]), None)
        r = gw.handle({"goal": "x", "principal": P})
        self.assertTrue(r.ok)
        self.assertEqual((r.vertex or {}).get("checkpoint_errors_count", 0), 0)


class TestContractShape(unittest.TestCase):
    def test_child_does_not_inherit_checkpoint(self):
        """子 Context 是 fork 的另一个 Context —— 用父 ctx_id 落盘会搅乱状态。"""
        cb = lambda *a: None                                  # noqa: E731
        child = ExecCtx(checkpoint=cb).child()
        self.assertIsNone(child.checkpoint)

    def test_loop_collects_errors(self):
        loop = Loop(PromptBuilder(total_tokens=4000), ScriptedLLM([]),
                    Dispatcher(ToolRegistry()))
        self.assertEqual(loop.checkpoint_errors, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
