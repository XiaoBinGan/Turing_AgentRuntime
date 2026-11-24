"""tests/test_context.py — Context 内核验收（W1）。

覆盖：四种 op 语义、单写者（INV-3）、未声明字段拒绝、版本/日志同步、
快照隔离、JSON round-trip、不可序列化对象、fork 隔离、merge 白名单、
预算超额、多级路径、日志顺序、replay 等价性。
"""
from __future__ import annotations

import io
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from contracts import (  # noqa: E402
    Budget,
    BudgetExceeded,
    ContextError,
    OwnershipViolation,
    Owner,
    Patch,
    Principal,
)
from core.context import ContextHandle  # noqa: E402


class Conn:
    """假连接对象：有 send，序列化必须被拒。"""

    def send(self, data):  # pragma: no cover - 不会被调用
        return None


class TestOps(unittest.TestCase):
    """四种 op 的语义。"""

    def setUp(self) -> None:
        self.ctx = ContextHandle()

    def test_set_overwrite_and_create(self):
        self.ctx.apply(Patch("set", "plan", Owner.PLANNER, {"steps": []}))
        self.ctx.apply(Patch("set", "plan", Owner.PLANNER, {"steps": [1]}))
        self.assertEqual(self.ctx.snapshot().get("plan"), {"steps": [1]})
        self.ctx.apply(Patch("set", "route_confidence", Owner.ROUTER, 0.91))
        self.assertAlmostEqual(self.ctx.snapshot().get("route_confidence"), 0.91)

    def test_append_single_element(self):
        self.ctx.apply(Patch("append", "history", Owner.LOOP, {"role": "user", "text": "hi"}))
        self.ctx.apply(Patch("append", "history", Owner.LOOP, {"role": "assistant", "text": "yo"}))
        self.assertEqual(
            self.ctx.snapshot().get("history"),
            [{"role": "user", "text": "hi"}, {"role": "assistant", "text": "yo"}],
        )

    def test_incr_default_and_explicit(self):
        self.ctx.apply(Patch("incr", "loop_count", Owner.LOOP))
        self.ctx.apply(Patch("incr", "loop_count", Owner.LOOP, 4))
        self.assertEqual(self.ctx.snapshot().get("loop_count"), 5)

    def test_merge_shallow(self):
        self.ctx.apply(Patch("set", "usage", Owner.LOOP, {"in": 10, "out": 3}))
        self.ctx.apply(Patch("merge", "usage", Owner.LOOP, {"out": 7, "cache": 2}))
        self.assertEqual(self.ctx.snapshot().get("usage"), {"in": 10, "out": 7, "cache": 2})

    def test_bad_op_rejected(self):
        with self.assertRaises(ContextError):
            self.ctx.apply(Patch("delete", "plan", Owner.PLANNER, None))
        self.assertEqual(self.ctx.version, 0)
        self.assertEqual(self.ctx.patch_log, ())

    def test_append_on_non_list_rejected(self):
        self.ctx.apply(Patch("set", "history", Owner.LOOP, "not-a-list"))
        with self.assertRaises(ContextError):
            self.ctx.apply(Patch("append", "history", Owner.LOOP, "x"))
        self.assertEqual(self.ctx.version, 1)

    def test_incr_on_non_numeric_rejected(self):
        self.ctx.apply(Patch("set", "loop_count", Owner.LOOP, "NaN-ish"))
        with self.assertRaises(ContextError):
            self.ctx.apply(Patch("incr", "loop_count", Owner.LOOP, 1))

    def test_values_are_deep_copied_on_write(self):
        payload = {"steps": [1, 2]}
        self.ctx.apply(Patch("set", "plan", Owner.PLANNER, payload))
        payload["steps"].append(999)
        self.assertEqual(self.ctx.snapshot().get("plan"), {"steps": [1, 2]})


class TestOwnership(unittest.TestCase):
    """INV-3：单写者 + 未声明字段拒绝。"""

    def setUp(self) -> None:
        self.ctx = ContextHandle()

    def test_non_owner_write_raises_with_expected_actual_path(self):
        with self.assertRaises(OwnershipViolation) as caught:
            self.ctx.apply(Patch("set", "plan", Owner.MEMORY, {"steps": []}))
        msg = str(caught.exception)
        self.assertIn("PLANNER", msg)   # 期望 owner
        self.assertIn("MEMORY", msg)    # 实际 owner
        self.assertIn("plan", msg)      # path
        self.assertEqual(self.ctx.version, 0)
        self.assertEqual(self.ctx.patch_log, ())

    def test_undeclared_field_rejected(self):
        with self.assertRaises(ContextError) as caught:
            self.ctx.apply(Patch("set", "hack_field", Owner.LOOP, 1))
        self.assertIsInstance(caught.exception, OwnershipViolation)
        self.assertIn("未声明", str(caught.exception))
        self.assertEqual(self.ctx.version, 0)

    def test_owner_check_uses_root_field_only(self):
        """多级路径按根字段判归属：plan.steps[0] 仍然只归 PLANNER。"""
        self.ctx.apply(Patch("set", "plan.steps", Owner.PLANNER, [{"n": 1}]))
        with self.assertRaises(OwnershipViolation):
            self.ctx.apply(Patch("set", "plan.steps", Owner.LOOP, []))


class TestVersionAndLog(unittest.TestCase):
    """版本单调递增 + patch_log 只追加且顺序一致。"""

    def setUp(self) -> None:
        self.ctx = ContextHandle()

    def test_version_monotonic_and_log_length_in_sync(self):
        patches = [
            Patch("set", "principal", Owner.GATEWAY, {"user_id": "u1"}),
            Patch("set", "phase", Owner.LOOP, "EXECUTING"),
            Patch("incr", "loop_count", Owner.LOOP, None),
            Patch("merge", "usage", Owner.LOOP, {"in": 1}),
            Patch("append", "history", Owner.LOOP, {"role": "user", "text": "hi"}),
        ]
        for i, patch in enumerate(patches, start=1):
            returned = self.ctx.apply(patch)
            self.assertEqual(returned, patch)
            self.assertEqual(self.ctx.version, i)
            self.assertEqual(len(self.ctx.patch_log), i)

    def test_patch_log_preserves_order_and_identity(self):
        seq = [
            Patch("set", "phase", Owner.LOOP, "RECEIVED"),
            Patch("set", "agent_state", Owner.ROUTER, "chat"),
            Patch("append", "history", Owner.LOOP, {"role": "user", "text": "q"}),
            Patch("incr", "loop_count", Owner.LOOP, 2),
        ]
        for patch in seq:
            self.ctx.apply(patch)
        log = self.ctx.patch_log
        self.assertEqual(len(log), len(seq))
        for got, want in zip(log, seq):
            self.assertEqual((got.op, got.path, got.by, got.value),
                             (want.op, want.path, want.by, want.value))
        self.assertEqual(self.ctx.version, len(seq))

    def test_failed_apply_does_not_touch_state(self):
        self.ctx.apply(Patch("set", "phase", Owner.LOOP, "EXECUTING"))
        before = (self.ctx.version, self.ctx.snapshot().data)
        with self.assertRaises(OwnershipViolation):
            self.ctx.apply(Patch("set", "memory_hits", Owner.LOOP, []))
        self.assertEqual(self.ctx.version, before[0])
        self.assertEqual(self.ctx.snapshot().data, before[1])


class TestSnapshot(unittest.TestCase):
    """快照是不可变视图。"""

    def setUp(self) -> None:
        self.ctx = ContextHandle()
        self.ctx.apply(Patch("set", "plan", Owner.PLANNER, {"steps": [{"n": 1}], "meta": {"k": "v"}}))
        self.ctx.apply(Patch("append", "history", Owner.LOOP, {"role": "user", "text": "hi"}))

    def test_snapshot_mutation_does_not_leak_into_context(self):
        snap = self.ctx.snapshot()
        snap.data["plan"]["steps"].append({"n": 2})
        snap.data["plan"]["meta"]["k"] = "hacked"
        snap.data["history"].append({"role": "x", "text": "y"})
        snap.data["phase"] = "DONE"

        fresh = self.ctx.snapshot()
        self.assertEqual(fresh.get("plan.steps"), [{"n": 1}])
        self.assertEqual(fresh.get("plan.meta.k"), "v")
        self.assertEqual(len(fresh.get("history")), 1)
        self.assertIsNone(fresh.get("phase"))

    def test_snapshot_version_matches_handle(self):
        self.assertEqual(self.ctx.snapshot().version, self.ctx.version)
        before = self.ctx.version
        self.ctx.apply(Patch("incr", "loop_count", Owner.LOOP, None))
        self.assertEqual(self.ctx.snapshot().version, before + 1)

    def test_get_returns_copy(self):
        got = self.ctx.get("plan")
        got["steps"].clear()
        self.assertEqual(self.ctx.get("plan.steps"), [{"n": 1}])


class TestSerialization(unittest.TestCase):
    """JSON round-trip + 不可序列化对象报错。"""

    def _build(self) -> ContextHandle:
        ctx = ContextHandle(budget=Budget(token_total=1000, cost_total_usd=0.25))
        ctx.apply(Patch("set", "principal", Owner.GATEWAY,
                        {"user_id": "u1", "roles": ["engineer"], "tenant": "acme"}))
        ctx.apply(Patch("set", "phase", Owner.LOOP, "EXECUTING"))
        ctx.apply(Patch("append", "history", Owner.LOOP, {"role": "user", "text": "重启服务"}))
        ctx.apply(Patch("incr", "loop_count", Owner.LOOP, 2))
        ctx.apply(Patch("set", "plan", Owner.PLANNER, {"steps": [{"n": 1, "tool": "query_logs"}]}))
        ctx.apply(Patch("incr", "budget.token_used", Owner.LOOP, 120))
        ctx.apply(Patch("incr", "retry_count", Owner.DISPATCHER, 1))
        return ctx

    def test_json_roundtrip_data_and_version(self):
        ctx = self._build()
        text = ctx.serialize()
        payload = json.loads(text)
        self.assertEqual(payload["schema_version"], 1)
        self.assertEqual(payload["version"], ctx.version)
        self.assertEqual(len(payload["patch_log"]), len(ctx.patch_log))

        restored = ContextHandle.restore(text)
        self.assertEqual(restored.version, ctx.version)
        self.assertEqual(restored.snapshot().data, ctx.snapshot().data)
        self.assertEqual(restored.patch_log, ctx.patch_log)
        self.assertEqual(restored.budget.to_dict(), ctx.budget.to_dict())

    def test_roundtrip_survives_second_generation(self):
        ctx = self._build()
        again = ContextHandle.restore(ctx.serialize())
        self.assertEqual(again.serialize(), ctx.serialize())

    def test_unserializable_callable_raises_context_error(self):
        ctx = ContextHandle()
        ctx.apply(Patch("set", "plan", Owner.PLANNER, {"steps": [], "hook": lambda: 1}))
        with self.assertRaises(ContextError) as caught:
            ctx.serialize()
        self.assertIn("<lambda>", str(caught.exception))

    def test_unserializable_connection_raises_context_error(self):
        for bad in (Conn(), io.BytesIO(b"x"), b"raw-bytes"):
            ctx = ContextHandle()
            ctx.apply(Patch("set", "error", Owner.LOOP, {"cause": bad}))
            with self.assertRaises(ContextError) as caught:
                ctx.serialize()
            self.assertIn("error.cause", str(caught.exception))

    def test_restore_rejects_bad_schema_version(self):
        with self.assertRaises(ContextError):
            ContextHandle.restore('{"schema_version": 2, "version": 0, "data": {}}')

    def test_restore_rejects_broken_json(self):
        with self.assertRaises(ContextError):
            ContextHandle.restore("{not json")

    def test_principal_dataclass_is_stored_as_dict(self):
        ctx = ContextHandle()
        ctx.apply(Patch("set", "principal", Owner.GATEWAY, Principal(user_id="u9", tenant="t9")))
        self.assertIsInstance(ctx.snapshot().get("principal"), dict)
        self.assertEqual(ctx.snapshot().get("principal.tenant"), "t9")


class TestForkMerge(unittest.TestCase):
    """fork 隔离 / merge 白名单。"""

    def setUp(self) -> None:
        self.parent = ContextHandle()
        self.parent.apply(Patch("set", "session_id", Owner.GATEWAY, "s1"))
        self.parent.apply(Patch("set", "plan", Owner.PLANNER, {"steps": [{"n": 1}]}))

    def test_fork_child_write_does_not_touch_parent(self):
        child = self.parent.fork()
        self.assertEqual(child.version, self.parent.version)
        self.assertEqual(child.patch_log, ())
        self.assertEqual(child.parent_version, self.parent.version)

        child.apply(Patch("append", "observations", Owner.LOOP, {"n": 1, "text": "子分支发现"}))
        child.apply(Patch("set", "plan", Owner.PLANNER, {"steps": []}))

        self.assertEqual(self.parent.get("observations"), None)
        self.assertEqual(self.parent.get("plan"), {"steps": [{"n": 1}]})
        self.assertEqual(child.get("plan"), {"steps": []})
        self.assertEqual(len(child.patch_log), 2)
        self.assertEqual(len(self.parent.patch_log), 2)

    def test_fork_parent_snapshot_mutation_does_not_touch_child(self):
        child = self.parent.fork()
        self.parent.apply(Patch("set", "plan", Owner.PLANNER, {"steps": []}))
        self.assertEqual(child.get("plan"), {"steps": [{"n": 1}]})

    def test_merge_whitelisted_fields(self):
        child = self.parent.fork()
        child.apply(Patch("append", "observations", Owner.LOOP, {"n": 1}))
        child.apply(Patch("set", "summary", Owner.LOOP, "子分支小结"))
        child.apply(Patch("append", "artifacts", Owner.NORMALIZER, {"name": "a.txt"}))
        before = self.parent.version

        applied = self.parent.merge(child, ["observations", "summary"])

        self.assertEqual(len(applied), 2)
        self.assertEqual(self.parent.version, before + 2)
        self.assertEqual(self.parent.get("observations"), [{"n": 1}])
        self.assertEqual(self.parent.get("summary"), "子分支小结")
        # 非白名单字段没有被拉过来
        self.assertIsNone(self.parent.get("artifacts"))
        # merge 产生的 patch by=LOOP
        self.assertTrue(all(p.by is Owner.LOOP for p in applied))
        self.assertEqual([p.path for p in applied], ["observations", "summary"])

    def test_merge_rejects_fields_outside_whitelist(self):
        child = self.parent.fork()
        with self.assertRaises(OwnershipViolation) as caught:
            self.parent.merge(child, ["hack_field"])
        self.assertIn("hack_field", str(caught.exception))
        self.assertIn("未声明", str(caught.exception))

    def test_merge_missing_path_is_skipped(self):
        child = self.parent.fork()
        self.assertEqual(self.parent.merge(child, ["summary"]), [])

    def test_replay_reproduces_state(self):
        child = self.parent.fork()
        child.apply(Patch("append", "observations", Owner.LOOP, {"n": 1}))
        child.apply(Patch("set", "summary", Owner.LOOP, "小结"))
        self.parent.merge(child, ["observations", "summary"])
        self.parent.apply(Patch("incr", "budget.token_used", Owner.LOOP, 50))

        replayed = ContextHandle.replay(self.parent.patch_log, version=self.parent.version)
        self.assertEqual(replayed.snapshot().data, self.parent.snapshot().data)
        self.assertEqual(replayed.version, self.parent.version)
        self.assertEqual(replayed.budget.token_used, 50)


class TestBudget(unittest.TestCase):
    """预算必须在越界前拦住（BudgetExceeded）。"""

    def setUp(self) -> None:
        self.ctx = ContextHandle(budget=Budget(token_total=100, tool_calls_total=2,
                                               cost_total_usd=0.10, max_steps=4))

    def test_budget_exceeded_on_token_overflow(self):
        self.ctx.apply(Patch("incr", "budget.token_used", Owner.LOOP, 60))
        self.assertEqual(self.ctx.get("budget.token_used"), 60)
        with self.assertRaises(BudgetExceeded) as caught:
            self.ctx.apply(Patch("incr", "budget.token_used", Owner.LOOP, 41))
        self.assertIn("token_used", str(caught.exception))
        # 失败不落盘、不涨版本
        self.assertEqual(self.ctx.get("budget.token_used"), 60)
        self.assertEqual(self.ctx.version, 1)
        self.assertEqual(len(self.ctx.patch_log), 1)

    def test_budget_exceeded_on_cost_and_tool_calls(self):
        with self.assertRaises(BudgetExceeded):
            self.ctx.apply(Patch("incr", "budget.cost_used_usd", Owner.LOOP, 0.25))
        self.ctx.apply(Patch("incr", "budget.tool_calls_used", Owner.LOOP, 2))
        with self.assertRaises(BudgetExceeded):
            self.ctx.apply(Patch("incr", "budget.tool_calls_used", Owner.LOOP, 1))
        self.assertIsNone(self.ctx.budget.exhausted())

    def test_budget_total_cannot_drop_below_used(self):
        self.ctx.apply(Patch("incr", "budget.token_used", Owner.LOOP, 80))
        with self.assertRaises(BudgetExceeded):
            self.ctx.apply(Patch("set", "budget.token_total", Owner.LOOP, 10))

    def test_budget_partial_dict_write_is_atomic(self):
        with self.assertRaises(BudgetExceeded):
            self.ctx.apply(Patch("set", "budget", Owner.LOOP,
                                 {"token_used": 10, "token_total": 5}))
        self.assertEqual(self.ctx.get("budget.token_used"), 0)
        self.assertEqual(self.ctx.get("budget.token_total"), 100)

    def test_budget_write_still_needs_owner(self):
        with self.assertRaises(OwnershipViolation):
            self.ctx.apply(Patch("incr", "budget.token_used", Owner.PLANNER, 1))

    def test_budget_adopted_from_initial_snapshot(self):
        seeded = ContextHandle(initial={"budget": Budget(token_total=7).to_dict()})
        self.assertEqual(seeded.budget.token_total, 7)
        self.assertEqual(seeded.get("budget.token_total"), 7)


class TestMultilevelPath(unittest.TestCase):
    """点号多级路径：不存在则逐级创建。"""

    def test_multilevel_set_get(self):
        ctx = ContextHandle()
        ctx.apply(Patch("set", "agent_state.route.debug.level", Owner.ROUTER, 3))
        self.assertEqual(ctx.snapshot().get("agent_state.route.debug.level"), 3)
        self.assertEqual(ctx.get("agent_state.route.debug"),
                         {"level": 3})
        ctx.apply(Patch("merge", "agent_state.route.debug", Owner.ROUTER, {"verbose": True}))
        self.assertEqual(ctx.get("agent_state.route.debug"),
                         {"level": 3, "verbose": True})

    def test_multilevel_append_and_incr(self):
        ctx = ContextHandle()
        ctx.apply(Patch("set", "skill_state.slots.a.items", Owner.SKILL, []))
        ctx.apply(Patch("append", "skill_state.slots.a.items", Owner.SKILL, "x"))
        ctx.apply(Patch("incr", "skill_state.slots.a.hits", Owner.SKILL, 2))
        self.assertEqual(ctx.get("skill_state.slots.a"),
                         {"items": ["x"], "hits": 2})

    def test_intermediate_non_dict_rejected(self):
        ctx = ContextHandle()
        ctx.apply(Patch("set", "agent_state.route", Owner.ROUTER, "chat"))
        with self.assertRaises(ContextError):
            ctx.apply(Patch("set", "agent_state.route.debug", Owner.ROUTER, 1))
        self.assertEqual(ctx.get("agent_state.route"), "chat")


if __name__ == "__main__":
    unittest.main(verbosity=2)
