"""tests/test_blackboard.py — Workflow Plane 共享黑板自带用例（W8）。

覆盖：
   ① 读路径：create→read、缺失返回 None、防御性副本、遮蔽解析
   ② 写路径：create 冲突不静默覆盖、CAS 成功/冲突/不存在、OCC 重试闭环
   ③ 分区：PRIVATE 对他人不可见、私有列表隔离、自动前缀、GLOBAL 全体可读、
          三区同名互不干扰、GLOBAL 必须 publish_global、publish 一次成型
   ④ 删除：带版本删除、版本不符 conflict、不存在 conflict
   ⑤ 订阅：精确订阅触发一次、通配收到全部、fail-open 计数、notifier 汇、退订
   ⑥ 审计：history 还原演进、writers 记录所有写者、delete 墓碑仍在 history
   ⑦ 快照：json.dumps round-trip、restore 还原、不可序列化 value 指名 key 抛 TypeError
   ⑧ 并发模拟：两个 Agent 交替 CAS，只有一成功，另一个必须重读

跑法：
    cd /Users/supre/Documents/tmp/agent-runtime && python3 -m unittest tests.test_blackboard -v
只依赖 plane/blackboard.py + contracts.py，不 import core/*，不碰别人的文件。
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from contracts import CASResult, Partition  # noqa: E402
from plane.blackboard import (  # noqa: E402
    WILDCARD,
    Blackboard,
    BlackboardError,
    PartitionViolation,
)

T0 = 1_700_000_000_000.0


class _Clock:
    """确定性时钟：每次读 +1ms，让 updated_at 可断言（不依赖真实时间）。"""

    def __init__(self, start: float = T0, step: float = 1.0):
        self.t = start
        self.step = step

    def __call__(self) -> float:
        self.t += self.step
        return self.t


def make_bb(**kw) -> Blackboard:
    return Blackboard(now=_Clock(), **kw)


# ══════════════════════════════════════════════════════════════
# ① 读路径
# ══════════════════════════════════════════════════════════════
class TestReadPath(unittest.TestCase):
    def test_01_create_then_read(self):
        bb = make_bb()
        r = bb.create("plan", {"steps": ["A"]}, "leader")
        self.assertTrue(r.ok)
        self.assertEqual(r.version, 1)

        entry = bb.read("plan", "coder_a")
        self.assertIsNotNone(entry)
        self.assertEqual(entry.value, {"steps": ["A"]})
        self.assertEqual(entry.version, 1)
        self.assertEqual(entry.owner, "leader")
        self.assertIs(entry.partition, Partition.WORKING)
        self.assertGreater(entry.updated_at, 0.0)

    def test_02_read_missing_returns_none(self):
        bb = make_bb()
        self.assertIsNone(bb.read("nope"))
        self.assertIsNone(bb.read("nope", "alice", Partition.PRIVATE))
        # 不存在的 key 绝不返回「空条目」——None 与「值为 None 的条目」必须可区分
        bb.create("nil", None, "leader")
        entry = bb.read("nil")
        self.assertIsNotNone(entry)
        self.assertIsNone(entry.value)

    def test_03_read_returns_defensive_copy(self):
        """读出来的是副本：改它不能改坏内部版本表（否则 CAS 可被绕过）。"""
        bb = make_bb()
        bb.create("plan", {"steps": ["A"]}, "leader")
        entry = bb.read("plan")
        entry.version = 99
        entry.value["steps"].append("HACK")
        again = bb.read("plan")
        self.assertEqual(again.version, 1)
        self.assertEqual(again.value, {"steps": ["A"]})
        # 版本没被改坏 → 原本的 CAS 仍然按 v1 判定
        self.assertTrue(bb.compare_and_set("plan", 1, {"steps": ["B"]}, "coder_a").ok)

    def test_04_default_resolution_shadows(self):
        """不带 partition：私有 → 工作 → 全局 遮蔽顺序（越私有越先命中）。"""
        bb = make_bb()
        bb.publish_global("style", "global-style", "leader")
        self.assertEqual(bb.read("style", "alice").value, "global-style")

        bb.create("style", "working-style", "leader")
        self.assertEqual(bb.read("style", "alice").value, "working-style")

        bb.write_private("style", "alice-style", "alice")
        self.assertEqual(bb.read("style", "alice").value, "alice-style")
        # bob 没有私有副本 → 仍然看见 working
        self.assertEqual(bb.read("style", "bob").value, "working-style")
        # 显式 partition 时遮蔽不生效
        self.assertEqual(bb.read("style", "alice", Partition.GLOBAL).value, "global-style")
        self.assertEqual(bb.read("style", "alice", Partition.WORKING).value, "working-style")


# ══════════════════════════════════════════════════════════════
# ② 写路径 / CAS
# ══════════════════════════════════════════════════════════════
class TestCAS(unittest.TestCase):
    def test_05_duplicate_create_conflicts(self):
        bb = make_bb()
        self.assertTrue(bb.create("plan", {"steps": ["A"]}, "leader").ok)
        r = bb.create("plan", {"steps": ["B"]}, "coder_a")
        self.assertFalse(r.ok)
        self.assertEqual(r.current_version, 1)
        self.assertIn("already exists", r.reason)
        self.assertEqual(bb.conflicts, 1)
        # 原值纹丝不动
        self.assertEqual(bb.read("plan").value, {"steps": ["A"]})
        self.assertEqual(bb.read("plan").owner, "leader")

    def test_06_cas_success_bumps_version(self):
        bb = make_bb()
        bb.create("plan", {"steps": ["A"]}, "leader")
        r = bb.compare_and_set("plan", 1, {"steps": ["A", "B"]}, "coder_a")
        self.assertTrue(r.ok)
        self.assertEqual(r.version, 2)
        entry = bb.read("plan")
        self.assertEqual(entry.version, 2)
        self.assertEqual(entry.value, {"steps": ["A", "B"]})
        self.assertEqual(entry.owner, "coder_a")

    def test_07_cas_conflict_leaves_value_untouched(self):
        bb = make_bb()
        bb.create("plan", {"steps": ["A"]}, "leader")
        bb.compare_and_set("plan", 1, {"steps": ["A", "B"]}, "coder_a")   # → v2
        r = bb.compare_and_set("plan", 1, {"steps": ["A", "C"]}, "coder_b")  # 基于过期 v1
        self.assertFalse(r.ok)
        self.assertEqual(r.current_version, 2)
        self.assertIn("expected v1", r.reason)
        self.assertIn("current v2", r.reason)
        # 关键：冲突的写不得留下任何痕迹
        entry = bb.read("plan")
        self.assertEqual(entry.version, 2)
        self.assertEqual(entry.value, {"steps": ["A", "B"]})
        self.assertEqual(entry.writers, ("leader", "coder_a"))
        self.assertEqual(bb.conflicts, 1)

    def test_08_cas_key_not_found(self):
        bb = make_bb()
        r = bb.compare_and_set("ghost", 1, "x", "coder_a")
        self.assertFalse(r.ok)
        self.assertEqual(r.current_version, 0)
        self.assertIn("key not found", r.reason)
        self.assertIsNone(bb.read("ghost"))
        self.assertEqual(bb.conflicts, 1)

    def test_09_occ_retry_flow(self):
        """标准 OCC 闭环：冲突 → 重读 → 语义合并 → 再提交成功。"""
        bb = make_bb()
        bb.create("plan", {"steps": ["A"]}, "leader")
        self.assertTrue(bb.compare_and_set("plan", 1, {"steps": ["A", "B"]}, "coder_a").ok)
        stale = bb.read("plan")
        stale.version = 1                     # 模拟 coder_b 手上那份过期的副本
        r = bb.compare_and_set("plan", stale.version, {"steps": ["A", "C"]}, "coder_b")
        self.assertFalse(r.ok)

        cur = bb.read("plan")                 # 重读
        merged = {"steps": cur.value["steps"] + ["C"]}   # 语义合并（调用方负责）
        r2 = bb.compare_and_set("plan", cur.version, merged, "coder_b")
        self.assertTrue(r2.ok)
        self.assertEqual(r2.version, 3)
        self.assertEqual(bb.read("plan").value, {"steps": ["A", "B", "C"]})
        self.assertEqual(bb.conflicts, 1)

    def test_10_cas_partition_scoped(self):
        """CAS 可以显式打分区；三区同名 key 的版本各自独立。"""
        bb = make_bb()
        bb.publish_global("k", "g", "leader")
        bb.create("k", "w", "leader")
        bb.write_private("k", "p", "alice")

        self.assertTrue(bb.compare_and_set("k", 1, "g2", "leader", Partition.GLOBAL).ok)
        self.assertTrue(bb.compare_and_set("k", 1, "w2", "leader", Partition.WORKING).ok)
        self.assertTrue(bb.compare_and_set("k", 1, "p2", "alice", Partition.PRIVATE).ok)
        self.assertEqual(bb.read("k", "alice", Partition.GLOBAL).value, "g2")
        self.assertEqual(bb.read("k", "alice", Partition.WORKING).value, "w2")
        self.assertEqual(bb.read("k", "alice", Partition.PRIVATE).value, "p2")


# ══════════════════════════════════════════════════════════════
# ③ 分区
# ══════════════════════════════════════════════════════════════
class TestPartitions(unittest.TestCase):
    def test_11_private_invisible_to_others(self):
        bb = make_bb()
        bb.write_private("scratch", {"note": "只有我能看"}, "alice")
        self.assertIsNotNone(bb.read("scratch", "alice"))
        self.assertEqual(bb.read("scratch", "alice").value, {"note": "只有我能看"})
        # 三种「别人」的读法统统拿不到
        self.assertIsNone(bb.read("scratch", "bob"))
        self.assertIsNone(bb.read("scratch", "bob", Partition.PRIVATE))
        self.assertIsNone(bb.read("scratch", ""))          # 匿名
        self.assertIsNone(bb.read("@alice:scratch", "bob", Partition.PRIVATE))
        # 而本人用带前缀的名字也能读到同一个条目（前缀幂等）
        self.assertEqual(bb.read("@alice:scratch", "alice").key, "@alice:scratch")

    def test_12_private_list_is_scoped(self):
        bb = make_bb()
        bb.write_private("a", 1, "alice")
        bb.write_private("b", 2, "alice")
        bb.write_private("c", 3, "bob")
        mine = bb.list(Partition.PRIVATE, "alice")
        self.assertEqual([e.key for e in mine], ["@alice:a", "@alice:b"])
        self.assertEqual([e.key for e in bb.list(Partition.PRIVATE, "bob")], ["@bob:c"])
        self.assertEqual(bb.list(Partition.PRIVATE, "carol"), [])
        # 不带分区时也只含自己的私有
        self.assertEqual([e.key for e in bb.list(agent="alice")], ["@alice:a", "@alice:b"])

    def test_13_private_key_auto_prefix(self):
        bb = make_bb()
        r = bb.write_private("plan_v2", {"draft": True}, "alice")
        self.assertTrue(r.ok)
        self.assertEqual(r.version, 1)
        entries = bb.list(Partition.PRIVATE, "alice")
        self.assertEqual(len(entries), 1)
        self.assertTrue(entries[0].key.startswith("@alice:"))
        self.assertEqual(entries[0].key, "@alice:plan_v2")
        self.assertIs(entries[0].partition, Partition.PRIVATE)
        # 前缀幂等：重复写不会出现 "@alice:@alice:..."
        bb.write_private("@alice:plan_v2", {"draft": False}, "alice")
        self.assertEqual(bb.list(Partition.PRIVATE, "alice")[0].key, "@alice:plan_v2")
        self.assertEqual(len(bb.list(Partition.PRIVATE, "alice")), 1)
        # 私有写是 upsert，版本继续涨（单写者，无竞态可言）
        self.assertEqual(bb.read("plan_v2", "alice").version, 2)

    def test_14_global_readable_by_everyone(self):
        bb = make_bb()
        r = bb.publish_global("coding_style", {"indent": 4}, "leader")
        self.assertTrue(r.ok)
        self.assertEqual(r.version, 1)
        for agent in ("leader", "coder_a", "coder_b", ""):
            entry = bb.read("coding_style", agent, Partition.GLOBAL)
            self.assertIsNotNone(entry, msg=f"agent={agent!r}")
            self.assertEqual(entry.value, {"indent": 4})
        self.assertIn("coding_style", [e.key for e in bb.list(Partition.GLOBAL)])

    def test_15_global_requires_publish_global(self):
        bb = make_bb()
        with self.assertRaises(PartitionViolation) as ctx:
            bb.create("spec", {"a": 1}, "leader", Partition.GLOBAL)
        self.assertIn("publish_global", str(ctx.exception))
        self.assertTrue(issubclass(PartitionViolation, BlackboardError))
        self.assertIsNone(bb.read("spec", "leader", Partition.GLOBAL))
        self.assertEqual(len(bb.list(Partition.GLOBAL)), 0)

    def test_16_publish_global_is_write_once(self):
        """GLOBAL 一次成型：二次 publish 冲突，改版必须 CAS 升版。"""
        bb = make_bb()
        self.assertTrue(bb.publish_global("style", {"indent": 4}, "leader").ok)
        r = bb.publish_global("style", {"indent": 2}, "coder_a")
        self.assertFalse(r.ok)
        self.assertEqual(r.current_version, 1)
        self.assertIn("一次成型", r.reason)
        self.assertEqual(bb.read("style", "coder_a", Partition.GLOBAL).value, {"indent": 4})

        upd = bb.compare_and_set("style", 1, {"indent": 2}, "coder_a", Partition.GLOBAL)
        self.assertTrue(upd.ok)
        self.assertEqual(upd.version, 2)
        self.assertEqual(bb.read("style", "coder_b", Partition.GLOBAL).value, {"indent": 2})

    def test_17_same_key_in_three_partitions_is_independent(self):
        bb = make_bb()
        bb.publish_global("k", "g", "leader")
        bb.create("k", "w", "leader")
        bb.write_private("k", "p", "alice")
        self.assertEqual(bb.read("k", "bob", Partition.GLOBAL).value, "g")
        self.assertEqual(bb.read("k", "bob", Partition.WORKING).value, "w")
        self.assertIsNone(bb.read("k", "bob", Partition.PRIVATE))
        self.assertEqual(bb.read("k", "alice", Partition.PRIVATE).value, "p")
        # 三条独立轨迹
        self.assertEqual(len(bb.snapshot()["versions"]), 3)
        self.assertEqual(bb.read("k", "alice", Partition.GLOBAL).version, 1)
        self.assertEqual(bb.read("k", "alice", Partition.WORKING).version, 1)
        self.assertEqual(bb.read("k", "alice", Partition.PRIVATE).version, 1)

    def test_18_list_across_partitions_uses_stable_order(self):
        bb = make_bb()
        bb.publish_global("z_global", 1, "leader")
        bb.create("b_work", 2, "leader")
        bb.create("a_work", 3, "leader")
        bb.write_private("m_priv", 4, "alice")
        order = [e.key for e in bb.list(agent="alice")]
        self.assertEqual(order, ["@alice:m_priv", "a_work", "b_work", "z_global"])


# ══════════════════════════════════════════════════════════════
# ④ 删除
# ══════════════════════════════════════════════════════════════
class TestDelete(unittest.TestCase):
    def test_19_delete_requires_matching_version(self):
        bb = make_bb()
        bb.create("plan", {"steps": ["A"]}, "leader")
        bad = bb.delete("plan", 7, "coder_a")
        self.assertFalse(bad.ok)
        self.assertEqual(bad.current_version, 1)
        self.assertIn("expected v7", bad.reason)
        self.assertIsNotNone(bb.read("plan"))      # 版本不符 → 什么都没删

        ok = bb.delete("plan", 1, "leader")
        self.assertTrue(ok.ok)
        self.assertEqual(ok.version, 1)            # 返回被删除时的版本
        self.assertIsNone(bb.read("plan"))
        self.assertEqual(bb.deletes, 1)
        self.assertIsNone(bb.read("plan", "alice", Partition.PRIVATE))

    def test_20_delete_missing_key_conflicts(self):
        bb = make_bb()
        r = bb.delete("ghost", 1, "leader")
        self.assertFalse(r.ok)
        self.assertEqual(r.current_version, 0)
        self.assertIn("key not found", r.reason)
        self.assertEqual(bb.conflicts, 1)

    def test_21_delete_private_uses_prefixed_scope(self):
        bb = make_bb()
        bb.write_private("scratch", 1, "alice")
        # bob 删不了 alice 的私有 key（前缀不同 → 个人前缀下根本没这个条目）
        self.assertFalse(bb.delete("scratch", 1, "bob").ok)
        self.assertTrue(bb.delete("scratch", 1, "alice").ok)
        self.assertEqual(bb.list(Partition.PRIVATE, "alice"), [])


# ══════════════════════════════════════════════════════════════
# ⑤ 订阅
# ══════════════════════════════════════════════════════════════
class TestSubscribe(unittest.TestCase):
    def test_22_callback_fires_once_after_cas(self):
        bb = make_bb()
        seen: list[dict] = []
        bb.create("plan", {"steps": ["A"]}, "leader")
        bb.subscribe("plan", seen.append)
        self.assertEqual(seen, [])                      # create 早于订阅 → 不补发

        bb.compare_and_set("plan", 1, {"steps": ["A", "B"]}, "coder_a")
        self.assertEqual(len(seen), 1)
        payload = seen[0]
        self.assertEqual(payload["key"], "plan")
        self.assertEqual(payload["version"], 2)
        self.assertEqual(payload["owner"], "coder_a")
        self.assertEqual(payload["value"], {"steps": ["A", "B"]})
        self.assertEqual(payload["op"], "update")
        self.assertEqual(bb.notifications, 1)
        # 订阅只认自己的 key
        bb.create("other", 1, "leader")
        self.assertEqual(len(seen), 1)

    def test_23_wildcard_receives_everything(self):
        bb = make_bb()
        seen: list[dict] = []
        bb.subscribe(WILDCARD, seen.append)
        bb.create("a", 1, "leader")
        bb.create("b", 2, "leader")
        bb.compare_and_set("a", 1, 11, "coder_a")
        bb.write_private("p", 3, "alice")
        bb.publish_global("g", 4, "leader")
        bb.delete("b", 1, "leader")
        self.assertEqual([p["key"] for p in seen],
                         ["a", "b", "a", "@alice:p", "g", "b"])
        self.assertEqual([p["version"] for p in seen], [1, 1, 2, 1, 1, 1])
        self.assertEqual([p["op"] for p in seen],
                         ["create", "create", "update", "private", "publish", "delete"])
        self.assertIsNone(seen[-1]["value"])            # delete 事件 value 为 None

    def test_24_callback_exception_is_fail_open(self):
        bb = make_bb()
        boom: list[str] = []

        def bad(payload: dict) -> None:
            boom.append(payload["key"])
            raise RuntimeError("订阅者挂了，但黑板不许挂")

        ok_seen: list[dict] = []
        bb.subscribe("plan", bad)
        bb.subscribe("plan", ok_seen.append)            # 坏回调之后的好回调必须照常收到
        bb.create("plan", {"steps": ["A"]}, "leader")

        self.assertEqual(bb.callback_errors, 1)
        self.assertEqual(boom, ["plan"])
        self.assertEqual(len(ok_seen), 1)
        # 写入没有因为回调异常失败
        self.assertEqual(bb.read("plan").value, {"steps": ["A"]})
        self.assertEqual(bb.read("plan").version, 1)
        # 后续写入照样能成功
        self.assertTrue(bb.compare_and_set("plan", 1, {"steps": ["B"]}, "coder_a").ok)
        self.assertEqual(bb.callback_errors, 2)
        self.assertEqual(bb.read("plan").version, 2)

    def test_25_notifier_sink_is_fail_open_too(self):
        seen: list[dict] = []

        def notifier(payload: dict) -> None:
            seen.append(payload)
            raise ValueError("trace 后端抖动")

        bb = make_bb(notifier=notifier)
        bb.create("plan", 1, "leader")
        self.assertTrue(bb.compare_and_set("plan", 1, 2, "coder_a").ok)
        self.assertEqual([p["version"] for p in seen], [1, 2])
        self.assertEqual(bb.callback_errors, 2)
        self.assertEqual(bb.read("plan").value, 2)

    def test_26_unsubscribe(self):
        bb = make_bb()
        seen: list[dict] = []
        cb = seen.append
        bb.subscribe("plan", cb)
        self.assertTrue(bb.unsubscribe("plan", cb))
        self.assertFalse(bb.unsubscribe("plan", cb))
        bb.create("plan", 1, "leader")
        self.assertEqual(seen, [])
        with self.assertRaises(ValueError):
            bb.subscribe("", cb)
        with self.assertRaises(TypeError):
            bb.subscribe("plan", None)   # type: ignore[arg-type]


# ══════════════════════════════════════════════════════════════
# ⑥ 审计
# ══════════════════════════════════════════════════════════════
class TestAudit(unittest.TestCase):
    def test_27_history_replays_evolution(self):
        bb = make_bb()
        bb.create("plan", {"steps": ["A"]}, "leader")
        bb.compare_and_set("plan", 1, {"steps": ["A", "B"]}, "coder_a")
        bb.compare_and_set("plan", 2, {"steps": ["A", "B", "C"]}, "coder_b")
        trail = bb.history("plan")
        self.assertEqual(len(trail), 3)
        self.assertEqual([e["version"] for e in trail], [1, 2, 3])
        self.assertEqual([e["owner"] for e in trail], ["leader", "coder_a", "coder_b"])
        self.assertEqual([e["event"] for e in trail], ["create", "update", "update"])
        self.assertEqual(trail[-1]["value"], {"steps": ["A", "B", "C"]})
        self.assertEqual([e["at"] for e in trail], sorted(e["at"] for e in trail))
        # 返回的是副本：改它不影响内部历史
        trail[-1]["value"]["steps"].append("HACK")
        self.assertEqual(bb.history("plan")[-1]["value"], {"steps": ["A", "B", "C"]})

    def test_28_history_survives_delete_and_recreate(self):
        bb = make_bb()
        bb.create("plan", 1, "leader")
        bb.delete("plan", 1, "leader")
        bb.create("plan", 2, "coder_a")
        trail = bb.history("plan")
        self.assertEqual([e["event"] for e in trail], ["create", "delete", "create"])
        self.assertEqual([e["version"] for e in trail], [1, 1, 1])
        self.assertEqual(bb.read("plan").version, 1)
        self.assertEqual(bb.history("ghost"), [])

    def test_29_writers_records_every_writer(self):
        bb = make_bb()
        bb.create("plan", {"steps": []}, "leader")
        bb.compare_and_set("plan", 1, {"steps": ["A"]}, "coder_a")
        bb.compare_and_set("plan", 2, {"steps": ["A", "B"]}, "coder_b")
        bb.compare_and_set("plan", 3, {"steps": ["A", "B", "C"]}, "coder_a")   # 回头写者
        entry = bb.read("plan")
        self.assertEqual(entry.writers, ("leader", "coder_a", "coder_b", "coder_a"))
        self.assertEqual(entry.owner, "coder_a")
        self.assertEqual(entry.version, 4)
        # 私有写的写者链也在
        bb.write_private("s", 1, "alice")
        bb.write_private("s", 2, "alice")
        self.assertEqual(bb.read("s", "alice").writers, ("alice", "alice"))


# ══════════════════════════════════════════════════════════════
# ⑦ 快照
# ══════════════════════════════════════════════════════════════
class TestSnapshot(unittest.TestCase):
    def _populate(self) -> Blackboard:
        bb = make_bb()
        bb.publish_global("style", {"indent": 4}, "leader")
        bb.create("plan", {"steps": ["A"]}, "leader")
        bb.compare_and_set("plan", 1, {"steps": ["A", "B"]}, "coder_a")
        bb.write_private("scratch", {"note": "n"}, "alice")
        return bb

    def test_30_snapshot_json_roundtrip(self):
        bb = self._populate()
        snap = bb.snapshot()
        self.assertEqual(set(snap), {"schema", "entries", "partitions", "versions"})
        blob = json.dumps(snap, ensure_ascii=False)          # 必须可序列化
        back = json.loads(blob)
        self.assertEqual(back, snap)
        self.assertEqual(back["versions"]["working:plan"], 2)
        self.assertEqual(back["versions"]["global:style"], 1)
        self.assertEqual(back["versions"]["private:@alice:scratch"], 1)
        self.assertEqual(back["partitions"]["working"], ["working:plan"])
        self.assertEqual(back["partitions"]["private"], ["private:@alice:scratch"])
        self.assertEqual(back["entries"]["working:plan"]["value"], {"steps": ["A", "B"]})
        self.assertEqual(back["entries"]["working:plan"]["writers"], ["leader", "coder_a"])

    def test_31_snapshot_restore_roundtrip(self):
        bb = self._populate()
        blob = json.dumps(bb.snapshot(), ensure_ascii=False)
        reborn = Blackboard.restore(json.loads(blob), now=_Clock())
        self.assertEqual(len(reborn), 3)
        self.assertEqual(reborn.read("plan").value, {"steps": ["A", "B"]})
        self.assertEqual(reborn.read("plan").version, 2)
        self.assertEqual(reborn.read("plan").writers, ("leader", "coder_a"))
        self.assertEqual(reborn.read("style", "bob", Partition.GLOBAL).value, {"indent": 4})
        self.assertEqual(reborn.read("scratch", "alice").value, {"note": "n"})
        self.assertIsNone(reborn.read("scratch", "bob"))
        # 恢复后的 CAS 语义仍然成立（版本表跟着回来了）
        self.assertTrue(reborn.compare_and_set("plan", 2, {"steps": []}, "coder_b").ok)

    def test_32_snapshot_rejects_unserializable_value_with_key(self):
        bb = make_bb()
        bb.create("bad", {"fn": object()}, "leader")
        with self.assertRaises(TypeError) as ctx:
            bb.snapshot()
        msg = str(ctx.exception)
        self.assertIn("bad", msg)              # 必须指名 key
        self.assertIn("working", msg)          # 顺带给出分区，便于定位
        # 写入本身不被拒（快照时才校验：黑板不替调用方决定 value 形状）
        self.assertIsNotNone(bb.read("bad"))


# ══════════════════════════════════════════════════════════════
# ⑧ 并发模拟（顺序调用模拟两个 Agent 交替提交）
# ══════════════════════════════════════════════════════════════
class TestConcurrentSimulation(unittest.TestCase):
    def test_33_two_agents_race_one_wins(self):
        bb = make_bb()
        bb.create("plan", {"steps": ["A"]}, "leader")
        a = bb.compare_and_set("plan", 1, {"steps": ["A", "B"]}, "coder_a")
        b = bb.compare_and_set("plan", 1, {"steps": ["A", "C"]}, "coder_b")
        self.assertTrue(a.ok)
        self.assertFalse(b.ok)
        self.assertEqual(a.version, 2)
        self.assertEqual(b.current_version, 2)
        self.assertEqual(bb.conflicts, 1)
        # 输的一方必须重读（不允许自动重试合并）
        self.assertEqual(bb.read("plan").value, {"steps": ["A", "B"]})

    def test_34_alternating_rounds_conflict_count(self):
        """两 Agent 每轮都基于同一快照提交：每轮恰好一个成功，另一个必冲突。"""
        bb = make_bb()
        bb.create("plan", {"who": []}, "leader")
        wins: list[str] = []
        order = [("coder_a", "coder_b"), ("coder_b", "coder_a"), ("coder_a", "coder_b")]
        for rnd, pair in enumerate(order):
            base = bb.read("plan")                 # 两人在这一轮都只看到这一份快照
            version, value = base.version, base.value
            for agent in pair:
                mine = {"who": value["who"] + [agent]}
                r = bb.compare_and_set("plan", version, mine, agent)
                if r.ok:
                    wins.append(agent)
                else:
                    # 输的一方拿到的是当前版本，必须重读后才能再提交
                    self.assertEqual(r.current_version, bb.read("plan").version)
            self.assertEqual(len(wins), rnd + 1)   # 每轮只有一个赢家

        self.assertEqual(wins, ["coder_a", "coder_b", "coder_a"])
        self.assertEqual(bb.conflicts, 3)
        self.assertEqual(bb.read("plan").version, 4)
        self.assertEqual([e["owner"] for e in bb.history("plan")],
                         ["leader", "coder_a", "coder_b", "coder_a"])

    def test_35_publish_and_private_do_not_count_conflicts(self):
        """私有 upsert 永不冲突；GLOBAL 首次发布也不冲突（冲突计数只认真冲突）。"""
        bb = make_bb()
        bb.publish_global("g", 1, "leader")
        bb.write_private("p", 1, "alice")
        bb.write_private("p", 2, "alice")
        self.assertEqual(bb.conflicts, 0)
        self.assertTrue(bb.publish_global("g2", 2, "leader").ok)
        self.assertEqual(bb.stats()["conflicts"], 0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main(verbosity=2)
