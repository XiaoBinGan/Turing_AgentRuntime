"""tests/test_distributed.py — 跨副本一致性的自带用例（W29）。

================================================================================
这三个缺口有一个共同点：**单进程全绿，部署成 N 副本后静默失效**
================================================================================
    ① 黑板 CAS 靠 RLock      → 跨副本丢更新（后写覆盖前写，无日志）
    ② 配额窗口在进程内       → 限额 × N（限流配了等于没配）
    ③ resources 的 LRU       → 并发首次读同一 uri，reader 被调 N 次（下游被打穿）

所以本文件的两条验收原则：
  - **不用「看起来对」的断言**：每个跨副本结论都要求「两个实例共享一个 KV」，
    并断言**合计**（不是单个实例的计数）；
  - **带负向对照**：直接跑一个「每副本独立窗口」的 `QuotaGuard`，让它的
    放行数（20）与集中式配额（10）并排出现在同一个用例里 ——
    没有负向对照的「已修复」是自我声明。

================================================================================
假 KV 为什么自己写（不用 core/kv.py）
================================================================================
并行施工期 `core/kv.py` 可能还没落盘；而且本模块的用例要**独立可测**：
`FakeKV` 用一把 `RLock` 把每个方法做成真原子（`get/set/set_nx/compare_and_set/
incr/delete/expire`），语义与 `contracts.KV` 对齐。
另有一个 `RealKVTest`：`core.kv.InMemoryKV` 存在时用真后端再跑一遍同样的结论，
不存在则 `skipTest`（不 Fake 成功）。

跑法：
    cd /Users/supre/Documents/tmp/agent-runtime
    python3 -m unittest tests.test_distributed -v
只依赖 plane/distributed.py + contracts.py + plane/resources.py（只读）+ 标准库。
"""
from __future__ import annotations

import contextlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest

from contracts import BlackboardEntry, CASResult, ExecCtx, GuardVerdict, Partition, Principal
from contracts import ResourceRef
from plane.distributed import (
    TOMBSTONE,
    DistributedBlackboard,
    DistributedQuota,
    PartitionViolation,
    SingleFlight,
    SingleFlightTimeout,
    SingleFlightResourceRuntime,
    wrap_with_singleflight,
)
from plane.governance import QuotaGuard          # 只读引用：做负向对照用
from plane.resources import (
    ResourceError,
    ResourcePermissionError,
    ResourceRegistry,
    ResourceRuntime,
)


# ══════════════════════════════════════════════════════════════
# 假 KV：每个方法都是一把锁保护下的完整动作（原子）
# ══════════════════════════════════════════════════════════════
class FakeKV:
    """最小 KV：语义对齐 `contracts.KV`。

    `shared = False` 是**诚实自述**：进程内 dict 跨副本不可见。
    两个 DistributedBlackboard 在本进程里共用它确实互相可见（用例 5/6 靠这个），
    但把它当分布式用就是「配置在、效果不在」。
    """

    shared = False
    atomic = True

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._data: dict[str, str] = {}
        self._exp: dict[str, float] = {}
        #: 操作轨迹：用例 10 用它证明「没有读-判断-写」
        self.ops: list[str] = []

    # ── 内部 ────────────────────────────────────────────────
    def _live(self, key: str) -> str | None:
        exp = self._exp.get(key)
        if exp is not None and time.time() >= exp:
            self._data.pop(key, None)
            self._exp.pop(key, None)
            return None
        return self._data.get(key)

    def _put(self, key: str, value: str, ttl_s: float | None) -> None:
        self._data[key] = value
        if ttl_s is None:
            self._exp.pop(key, None)
        else:
            self._exp[key] = time.time() + float(ttl_s)

    # ── KV 协议 ─────────────────────────────────────────────
    def get(self, key: str) -> str | None:
        with self._lock:
            self.ops.append("get")
            return self._live(key)

    def set(self, key: str, value: str, *, ttl_s: float | None = None) -> bool:
        with self._lock:
            self.ops.append("set")
            self._put(key, value, ttl_s)
            return True

    def set_nx(self, key: str, value: str, *, ttl_s: float | None = None) -> bool:
        with self._lock:
            self.ops.append("set_nx")
            if self._live(key) is not None:
                return False
            self._put(key, value, ttl_s)
            return True

    def compare_and_set(self, key: str, expected: str, value: str,
                        *, ttl_s: float | None = None) -> bool:
        with self._lock:
            self.ops.append("compare_and_set")
            if self._live(key) != expected:
                return False
            self._put(key, value, ttl_s)
            return True

    def incr(self, key: str, delta: int = 1, *, ttl_s: float | None = None) -> int:
        with self._lock:
            self.ops.append("incr")
            cur = self._live(key)
            new = (int(cur) if cur is not None else 0) + int(delta)
            self._data[key] = str(new)
            if key not in self._exp and ttl_s is not None:
                self._exp[key] = time.time() + float(ttl_s)
            return new

    def delete(self, key: str) -> bool:
        with self._lock:
            self.ops.append("delete")
            existed = self._live(key) is not None
            self._data.pop(key, None)
            self._exp.pop(key, None)
            return existed

    def expire(self, key: str, ttl_s: float) -> bool:
        with self._lock:
            self.ops.append("expire")
            if self._live(key) is None:
                return False
            self._exp[key] = time.time() + float(ttl_s)
            return True

    # ── 测试用扩展（真实 KV 的可选能力） ─────────────────────
    def keys(self, prefix: str = "") -> list[str]:
        with self._lock:
            for k in list(self._data):
                self._live(k)
            return sorted(k for k in self._data if k.startswith(prefix))

    def raw(self, key: str) -> str | None:
        with self._lock:
            return self._live(key)

    def reset_ops(self) -> None:
        with self._lock:
            self.ops.clear()


class SharedFakeKV(FakeKV):
    """自述「跨副本共享」的后端（模拟 RESPKV 的能力声明）。

    用来证明 `info()` 是**读后端自述**而不是拍脑袋：同一个类族换个声明，
    `shared` 跟着变。
    """

    shared = True
    atomic = True
    note = "测试替身：声明 shared=True 以模拟 RESPKV"


class WindowlessKV(FakeKV):
    """不支持 key 枚举的后端（用 `_kv_keys` 的降级路径）。"""

    keys = None  # type: ignore[assignment]


# ══════════════════════════════════════════════════════════════
# 假时钟
# ══════════════════════════════════════════════════════════════
class FakeClock:
    def __init__(self, start: float = 1_700_000_000.0) -> None:
        self.t = start
        self._lock = threading.Lock()

    def __call__(self) -> float:
        with self._lock:
            return self.t

    def advance(self, seconds: float) -> None:
        with self._lock:
            self.t += float(seconds)


# ══════════════════════════════════════════════════════════════
# ① 黑板：9 条主用例 + 3 条加固
# ══════════════════════════════════════════════════════════════
class DistributedBlackboardTest(unittest.TestCase):
    def setUp(self) -> None:
        self.kv = FakeKV()
        self.bb = DistributedBlackboard(self.kv)

    # 1 ─ create/read 闭环，version=1
    def test_create_read_roundtrip_version_1(self) -> None:
        r = self.bb.create("plan", {"steps": ["A"]}, "leader")
        self.assertTrue(r.ok)
        self.assertEqual(1, r.version)
        entry = self.bb.read("plan")
        self.assertIsInstance(entry, BlackboardEntry)
        self.assertEqual({"steps": ["A"]}, entry.value)
        self.assertEqual(1, entry.version)
        self.assertEqual("leader", entry.owner)
        self.assertEqual(("leader",), entry.writers)
        self.assertIs(Partition.WORKING, entry.partition)
        self.assertGreater(entry.updated_at, 0.0)

    # 2 ─ 重复 create → conflict，且不静默覆盖
    def test_duplicate_create_conflicts_without_overwrite(self) -> None:
        self.assertTrue(self.bb.create("plan", {"v": 1}, "alice").ok)
        r = self.bb.create("plan", {"v": 2}, "bob")
        self.assertFalse(r.ok)
        self.assertEqual(1, r.current_version)
        self.assertIn("already exists", r.reason)
        self.assertEqual({"v": 1}, self.bb.read("plan").value)   # 没有被抹掉
        self.assertEqual(1, self.bb.conflicts)

    # 3 ─ CAS 成功 → version+1
    def test_cas_success_bumps_version(self) -> None:
        self.bb.create("plan", {"v": 1}, "alice")
        r = self.bb.compare_and_set("plan", 1, {"v": 2}, "bob")
        self.assertTrue(r.ok)
        self.assertEqual(2, r.version)
        entry = self.bb.read("plan")
        self.assertEqual({"v": 2}, entry.value)
        self.assertEqual(2, entry.version)
        self.assertEqual("bob", entry.owner)
        self.assertEqual(("alice", "bob"), entry.writers)        # 写者历史不被丢

    # 4 ─ CAS expected 落后 → conflict，值未变，current_version 正确
    def test_cas_stale_expected_conflicts_and_keeps_value(self) -> None:
        self.bb.create("plan", {"v": 1}, "alice")
        self.bb.compare_and_set("plan", 1, {"v": 2}, "bob")      # → v2
        r = self.bb.compare_and_set("plan", 1, {"v": 99}, "carol")
        self.assertFalse(r.ok)
        self.assertEqual(2, r.current_version)
        self.assertIn("expected v1, current v2", r.reason)
        entry = self.bb.read("plan")
        self.assertEqual({"v": 2}, entry.value)                  # 落后的一方没写进去
        self.assertEqual(2, entry.version)

    # 5 ─ 跨实例：A 写 B 能读到（同一 KV，两个 DistributedBlackboard）
    def test_cross_instance_visibility(self) -> None:
        a = DistributedBlackboard(self.kv)
        b = DistributedBlackboard(self.kv)          # 模拟第二个副本
        self.assertTrue(a.create("plan", {"by": "A"}, "A").ok)
        seen = b.read("plan")
        self.assertIsNotNone(seen)
        self.assertEqual({"by": "A"}, seen.value)
        self.assertTrue(b.compare_and_set("plan", 1, {"by": "B"}, "B").ok)
        self.assertEqual(2, a.read("plan").version)   # A 也看到 B 的升版
        self.assertEqual({"by": "B"}, a.read("plan").value)

    # 6 ─ 跨实例 CAS 竞争：A、B 同时 CAS 同一 expected → 恰好 1 个成功
    def test_cross_instance_cas_race_has_exactly_one_winner(self) -> None:
        a = DistributedBlackboard(self.kv)
        b = DistributedBlackboard(self.kv)
        a.create("plan", {"n": 0}, "seed")

        results: list[CASResult] = []
        lock = threading.Lock()
        barrier = threading.Barrier(2)

        def contend(bb: DistributedBlackboard, name: str) -> None:
            base = bb.read("plan")
            self.assertEqual(1, base.version)          # 双方都基于 v1
            barrier.wait(timeout=5)                    # 同时进门（真并发）
            r = bb.compare_and_set("plan", 1, {"n": name}, name)
            with lock:
                results.append(r)

        ta = threading.Thread(target=contend, args=(a, "A"))
        tb = threading.Thread(target=contend, args=(b, "B"))
        ta.start(); tb.start()
        ta.join(timeout=5); tb.join(timeout=5)
        self.assertFalse(ta.is_alive() or tb.is_alive())

        winners = [r for r in results if r.ok]
        losers = [r for r in results if not r.ok]
        self.assertEqual(1, len(winners), f"必须恰好一个成功，实际 {results}")
        self.assertEqual(1, len(losers))
        self.assertEqual(2, losers[0].current_version)   # 失败方拿到真版本，不是猜的
        self.assertEqual(2, self.bb.read("plan").version)
        # 只多了一版：两个都写进去的话会是 v3 或者值互相覆盖
        self.assertEqual(1, len([r for r in results if r.ok]))

    # 7 ─ 私有分区隔离（含跨实例：别人的私有 key 连 key 都解析不到）
    def test_private_partition_isolation(self) -> None:
        self.assertTrue(self.bb.create("note", "alice-secret", "alice",
                                       partition=Partition.PRIVATE).ok)
        self.assertTrue(self.bb.create("note", "team-note", "bob").ok)   # WORKING

        # ① 私有区对他人不可见（同一实例）
        self.assertIsNone(self.bb.read("note", agent="bob", partition=Partition.PRIVATE))
        # ② 跨实例仍不可见（隔离不能只在内存里）
        other = DistributedBlackboard(self.kv)
        self.assertIsNone(other.read("note", agent="bob", partition=Partition.PRIVATE))
        # ③ 自己看得到，且遮蔽顺序里私有优先
        self.assertEqual("alice-secret", self.bb.read("note", agent="alice",
                                                      partition=Partition.PRIVATE).value)
        self.assertEqual("alice-secret",
                         self.bb.read("note", agent="alice").value)
        self.assertEqual("team-note", self.bb.read("note", agent="bob").value)
        # ④ 物理 key 就分开了（不是靠代码里加 if）
        keys = self.kv.keys("bb:")
        self.assertIn("bb:private:@alice:note", keys)
        self.assertIn("bb:working:note", keys)

    # 8 ─ delete 带版本（含墓碑、重建立、跨实例）
    def test_delete_requires_current_version(self) -> None:
        self.bb.create("plan", {"v": 1}, "alice")
        self.bb.compare_and_set("plan", 1, {"v": 2}, "bob")             # → v2
        stale = self.bb.delete("plan", 1, "alice")
        self.assertFalse(stale.ok)
        self.assertEqual(2, stale.current_version)
        self.assertEqual({"v": 2}, self.bb.read("plan").value)          # 别人刚改过，没删

        ok = self.bb.delete("plan", 2, "alice")
        self.assertTrue(ok.ok)
        self.assertEqual(2, ok.version)
        self.assertIsNone(self.bb.read("plan"))
        self.assertIsNone(DistributedBlackboard(self.kv).read("plan"))  # 跨实例也真删了
        self.assertEqual(1, self.bb.deletes)

        gone = self.bb.delete("plan", 2, "alice")
        self.assertFalse(gone.ok)
        self.assertEqual(0, gone.current_version)                        # 已不存在
        self.assertIn("not found", gone.reason)

        self.assertTrue(self.bb.create("plan", {"v": "new"}, "carol").ok)
        self.assertEqual(1, self.bb.read("plan").version)                # 重建从 v1 起

    # 9 ─ info().shared 正确（诚实：不替后端吹牛，也不否认真共享）
    def test_info_reports_backend_capability_honestly(self) -> None:
        local = DistributedBlackboard(FakeKV()).info()
        self.assertFalse(local.shared, "进程内 dict 不许自称跨副本共享")
        self.assertTrue(local.atomic)
        self.assertIn("shared=False", local.note)

        shared = DistributedBlackboard(SharedFakeKV()).info()
        self.assertTrue(shared.shared)
        self.assertTrue(shared.atomic)

        # 配额的自述也要带上这条警告（否则运维只看 quota.info() 会以为已经精确）
        self.assertIn("× 副本数", DistributedQuota(FakeKV()).info().note)

    # 10 ─ 结构性证明：写路径上没有「读-判断-写」的裸 set
    def test_write_path_uses_only_atomic_primitives(self) -> None:
        self.kv.reset_ops()
        self.bb.create("k", 1, "a")
        self.bb.compare_and_set("k", 1, 2, "a")
        self.bb.delete("k", 2, "a")
        ops = list(self.kv.ops)
        self.assertIn("set_nx", ops)              # create = 天然的「期望不存在」
        self.assertIn("compare_and_set", ops)     # CAS 由存储端判定
        self.assertNotIn("set", ops, "写路径出现裸 set 就退化成读-判断-写了")
        # 每一次 CAS 尝试都必须是 KV 的原子动作，不是本地 if
        self.assertEqual(2, ops.count("compare_and_set"))  # 一次更新 + 一次墓碑
        self.assertEqual(1, ops.count("delete"))           # 删完不留 key（墓碑也抹掉）

        # 墓碑语义：读到墓碑等同「不存在」，不会把旧值复活成一条 version=0 的条目
        self.bb.create("ghost", {"v": 1}, "a")
        self.kv.set("bb:working:ghost", TOMBSTONE)
        self.assertIsNone(self.bb.read("ghost"))
        self.assertFalse(self.bb.compare_and_set("ghost", 1, {"v": 2}, "a").ok)

    # 11 ─ GLOBAL 语义与单进程版一致（create 拒绝；publish 一次成型）
    def test_global_partition_requires_publish(self) -> None:
        with self.assertRaises(PartitionViolation):
            self.bb.create("policy", {"p": 1}, "alice", partition=Partition.GLOBAL)
        self.assertTrue(self.bb.publish_global("policy", {"p": 1}, "alice").ok)
        again = self.bb.publish_global("policy", {"p": 2}, "bob")
        self.assertFalse(again.ok)
        self.assertEqual(1, again.current_version)
        self.assertEqual({"p": 1}, self.bb.read("policy", partition=Partition.GLOBAL).value)
        self.assertTrue(self.bb.compare_and_set("policy", 1, {"p": 3}, "bob",
                                                partition=Partition.GLOBAL).ok)

    # 12 ─ 快照：json 可序列化 + 复合键；后端不支持枚举时明说 unsupported
    def test_snapshot_is_json_and_honest_about_enumeration(self) -> None:
        self.bb.create("plan", {"v": 1}, "alice")
        self.bb.create("note", "x", "alice", partition=Partition.PRIVATE)
        snap = self.bb.snapshot()
        json.dumps(snap)                                       # 必须可序列化
        self.assertEqual({"working:plan", "private:@alice:note"},
                         set(snap["entries"]))
        self.assertEqual(1, snap["versions"]["working:plan"])
        self.assertEqual("kv.keys", snap["enumeration"])
        self.assertFalse(snap["info"]["shared"])

        blind = DistributedBlackboard(WindowlessKV()).snapshot()
        self.assertEqual("unsupported", blind["enumeration"])
        self.assertEqual({}, blind["entries"])                 # 空 ≠ 支持枚举的空

    # 13 ─ 写不可序列化的值 → 指名 key 的 TypeError（跨副本排查的唯一线索）
    def test_non_serializable_value_names_the_key(self) -> None:
        with self.assertRaises(TypeError) as ctx:
            self.bb.create("weird", {1, 2, 3}, "alice")
        self.assertIn("weird", str(ctx.exception))


# ══════════════════════════════════════════════════════════════
# ② 配额：7 条主用例 + 负向对照
# ══════════════════════════════════════════════════════════════
class DistributedQuotaTest(unittest.TestCase):
    def setUp(self) -> None:
        self.kv = FakeKV()
        self.clock = FakeClock()
        self.q = DistributedQuota(self.kv, per_minute={"tool_call": 3, "llm_call": 5},
                                  clock=self.clock)

    # 10 ─ 计费累加
    def test_charge_accumulates(self) -> None:
        for expect in (1, 2, 3):
            v = self.q.charge("acme")
            self.assertTrue(v.allowed, v.reason)
            self.assertEqual(expect, v.payload["used"])
            self.assertEqual(3, v.payload["limit"])
            self.assertEqual(3 - expect, v.payload["remaining"])
        self.assertEqual(3, self.q.used("acme"))
        self.assertEqual(0, self.q.denied_total)

    # 11 ─ 超限 block（fail-closed，且 payload 里给出 retry_after）
    def test_over_limit_blocks(self) -> None:
        for _ in range(3):
            self.assertTrue(self.q.charge("acme").allowed)
        v = self.q.charge("acme")
        self.assertFalse(v.allowed)
        self.assertIn("quota exceeded", v.reason)
        self.assertIn("quota_exceeded", v.hits)
        self.assertEqual(3, v.payload["used"])
        self.assertEqual(1, v.payload["requested"])
        self.assertGreater(v.payload["retry_after_s"], 0.0)
        self.assertEqual(1, self.q.denied_total)

    # 12 ─ 超限回滚：被拒后再 charge，used 不继续涨（也不偷偷漏一次）
    def test_rejected_charge_rolls_back(self) -> None:
        for _ in range(3):
            self.assertTrue(self.q.charge("acme").allowed)
        key = self.q._key("acme", "tool_call")

        self.assertFalse(self.q.charge("acme").allowed)
        self.assertEqual(3, self.q.used("acme"))
        self.assertEqual("3", self.kv.raw(key), "被拒的请求必须 DECRBY 回滚")
        self.assertFalse(self.q.charge("acme").allowed)
        self.assertFalse(self.q.charge("acme").allowed)
        self.assertEqual(3, self.q.used("acme"))
        self.assertEqual("3", self.kv.raw(key))
        self.assertEqual(3, self.q.rollbacks)

        # 回滚不能把额度「让」出来：再放行必须靠窗口切换，不是靠失败次数
        again = self.q.charge("acme")
        self.assertFalse(again.allowed)
        self.assertEqual(3, again.payload["used"])      # 拒绝回包里 used 也是真值
        self.assertEqual(3, self.q.used("acme"))

    # 13 ─ 窗口切换后重置（假时钟推进 61 秒）
    def test_window_rollover_resets(self) -> None:
        wid = self.q._key("acme", "tool_call")
        for _ in range(3):
            self.assertTrue(self.q.charge("acme").allowed)
        self.assertFalse(self.q.charge("acme").allowed)

        self.clock.advance(61)
        v = self.q.charge("acme")
        self.assertTrue(v.allowed, "新窗口第一笔必须放行")
        self.assertEqual(1, v.payload["used"])
        self.assertNotEqual(wid, self.q._key("acme", "tool_call"))   # 换 key 了，不是清零
        self.assertIn("window_id", v.payload)
        self.assertEqual(int(self.clock.t // 60), v.payload["window_id"])   # 绝对窗口号
        self.assertEqual(1, self.q.snapshot()["windows"]["acme:tool_call"]["used"])

    # 14 ─ 跨实例精确：两个 quota 共享 KV，限额 10，合计第 11 次被拒
    def test_cross_instance_capacity_is_exact(self) -> None:
        kv = FakeKV()
        clock = FakeClock()
        a = DistributedQuota(kv, per_minute={"tool_call": 10}, clock=clock)
        b = DistributedQuota(kv, per_minute={"tool_call": 10}, clock=clock)

        allowed = 0
        for i in range(10):                       # 交替计费，模拟两个副本轮流接请求
            inst = a if i % 2 == 0 else b
            v = inst.charge("acme")
            self.assertTrue(v.allowed, f"第 {i + 1} 次不该被拒：{v.reason}")
            allowed += 1
        self.assertEqual(10, allowed)

        for inst in (a, b):                       # 第 11 次从任一副本进都拒
            v = inst.charge("acme")
            self.assertFalse(v.allowed, "限额 10，合计第 11 次必须拒")
            self.assertIn("quota_exceeded", v.hits)
        # 两个副本看到的是同一个分母
        self.assertEqual(10, a.used("acme"))
        self.assertEqual(10, b.used("acme"))
        self.assertEqual("10", kv.raw(a._key("acme", "tool_call")))
        self.assertEqual(10, a.snapshot()["windows"]["acme:tool_call"]["used"])

    # 负向对照 ── 每副本独立窗口的错误账：限额 10 → 实际放行 20
    def test_negative_control_per_process_window_doubles_the_limit(self) -> None:
        clock = FakeClock()
        g1 = QuotaGuard(per_minute={"tool_call": 10}, now=clock)
        g2 = QuotaGuard(per_minute={"tool_call": 10}, now=clock)
        drifted = sum(1 for i in range(20) if (g1 if i % 2 == 0 else g2)
                      .charge("acme").allowed)
        self.assertEqual(20, drifted, "这就是要修的洞：2 副本 = 2 倍限额")

        q1 = DistributedQuota(FakeKV(), per_minute={"tool_call": 10}, clock=clock)
        kv = q1._kv
        q2 = DistributedQuota(kv, per_minute={"tool_call": 10}, clock=clock)
        exact = sum(1 for i in range(20) if (q1 if i % 2 == 0 else q2)
                    .charge("acme").allowed)
        self.assertEqual(10, exact, "集中式窗口必须让合计收敛回限额")

    # 15 ─ used() 查询
    def test_used_query(self) -> None:
        self.assertEqual(0, self.q.used("nobody"))
        self.q.charge("acme", n=2)
        self.assertEqual(2, self.q.used("acme"))
        self.assertEqual(0, self.q.used("acme", "llm_call"))      # kind 维度分开
        self.q.charge("acme", "llm_call", 1)
        self.assertEqual(1, self.q.used("acme", "llm_call"))
        # n=0 是纯查询：不能凭空创建 key（查询不等于使用）
        before = len(self.kv.keys("quota:"))
        v = self.q.charge("acme", n=0)
        self.assertTrue(v.allowed)
        self.assertEqual(2, v.payload["used"])
        self.assertEqual(before, len(self.kv.keys("quota:")), "n=0 不许 INCRBY 出新 key")

    # 16 ─ 不同 tenant 互不影响
    def test_tenants_are_isolated(self) -> None:
        for _ in range(3):
            self.assertTrue(self.q.charge("acme").allowed)
        self.assertFalse(self.q.charge("acme").allowed)
        other = self.q.charge("globex")                 # 另一个租户照常放行
        self.assertTrue(other.allowed)
        self.assertEqual(1, other.payload["used"])
        self.assertEqual(0, self.q.used("acme", "tool_call") - 3)

    # 加固 ─ 参数非法一律 fail-closed（绝不「算不出来就放行」）
    def test_invalid_charge_inputs_fail_closed(self) -> None:
        for kwargs in ({"tenant": ""}, {"tenant": "  "}):
            self.assertFalse(self.q.charge(**kwargs).allowed)
        self.assertFalse(self.q.charge("acme", kind="").allowed)
        self.assertFalse(self.q.charge("acme", n=-1).allowed)
        self.assertFalse(self.q.charge("acme", n=True).allowed)   # bool 不是数量
        self.assertEqual(0, self.q.used("acme"))

    # 加固 ─ 上限配置坏掉 → 全拦（配置坏了不能变成无限额）
    def test_broken_limit_config_blocks_everything(self) -> None:
        q = DistributedQuota(FakeKV(), per_minute={"tool_call": "abc"},
                             clock=FakeClock())
        v = q.charge("acme")
        self.assertFalse(v.allowed)                 # 配置坏了 = 允许 0 次，不是无限额
        self.assertEqual(0, v.payload["limit"])
        self.assertEqual(0, q.used("acme"))         # 且没真的计费进去（回滚过）


# ══════════════════════════════════════════════════════════════
# ③ singleflight：5 条主用例 + 1 条加固
# ══════════════════════════════════════════════════════════════
class SingleFlightTest(unittest.TestCase):
    # 17 ─ 并发 10 个同 key → fn 只执行 1 次
    def test_concurrent_same_key_executes_once(self) -> None:
        flight = SingleFlight()
        counter = {"n": 0}
        lock = threading.Lock()

        def slow() -> str:
            with lock:
                counter["n"] += 1
            time.sleep(0.15)          # 足够长：让 10 个线程都进来等
            return "payload"

        barrier = threading.Barrier(10)
        out: list[str] = []
        out_lock = threading.Lock()

        def worker() -> None:
            barrier.wait(timeout=5)
            got = flight.do("k", slow)
            with out_lock:
                out.append(got)

        threads = [threading.Thread(target=worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        self.assertFalse(any(t.is_alive() for t in threads))

        self.assertEqual(1, counter["n"], "同 key 并发只该真正执行一次")
        self.assertEqual(10, len(out))
        self.assertEqual(["payload"] * 10, out)
        stats = flight.stats()
        self.assertEqual(10, stats["calls"])
        self.assertEqual(9, stats["hits"])
        self.assertEqual(1, stats["shared"])
        self.assertEqual(1, stats["executions"])
        self.assertEqual(0, stats["errors"])

    # 18 ─ 10 个等待者拿到同一结果对象（不是各跑一遍、也不是深拷贝）
    def test_waiters_receive_the_same_result_object(self) -> None:
        flight = SingleFlight()
        started = threading.Event()

        def slow() -> dict:
            started.set()
            time.sleep(0.2)
            return {"big": list(range(100))}

        barrier = threading.Barrier(10)
        seen: list[int] = []
        lock = threading.Lock()

        def worker() -> None:
            barrier.wait(timeout=5)
            got = flight.do("k", slow)
            with lock:
                seen.append(id(got))

        threads = [threading.Thread(target=worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        self.assertTrue(started.is_set())
        self.assertEqual(10, len(seen))
        self.assertEqual(1, len(set(seen)), "所有等待者必须拿到同一个结果对象")
        self.assertEqual(1, flight.stats()["executions"])

    # 19 ─ fn 抛异常 → 所有等待者都收到同一个异常（不吞）
    def test_exception_is_shared_by_all_waiters(self) -> None:
        flight = SingleFlight()
        boom = ResourceError("downstream exploded")
        calls = {"n": 0}
        lock = threading.Lock()

        def failing() -> str:
            with lock:
                calls["n"] += 1
            time.sleep(0.15)
            raise boom

        barrier = threading.Barrier(10)
        errors: list[BaseException] = []

        def worker() -> None:
            barrier.wait(timeout=5)
            try:
                flight.do("k", failing)
            except BaseException as exc:   # noqa: BLE001 - 这里就是要抓异常
                with lock:
                    errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        self.assertEqual(1, calls["n"])
        self.assertEqual(10, len(errors), "异常不许被吞成 None")
        self.assertTrue(all(e is boom for e in errors), "等待者拿到同一个异常对象")
        self.assertEqual(1, flight.stats()["errors"])
        # 失败不会被记住：key 已摘牌，下一次 do 重新执行（不会永久返回同一个异常）
        self.assertEqual("ok", flight.do("k", lambda: "ok"))
        self.assertEqual(1, flight.stats()["executions"])

    # 20 ─ 不同 key 并行（不互相阻塞）
    def test_different_keys_do_not_block_each_other(self) -> None:
        flight = SingleFlight()
        done: dict[str, float] = {}
        lock = threading.Lock()

        def make(key: str, delay: float):
            def fn() -> str:
                time.sleep(delay)
                with lock:
                    done[key] = time.time()
                return key
            return fn

        start = time.time()
        threads = [threading.Thread(target=lambda k=k: flight.do(k, make(k, 0.25)))
                   for k in ("a", "b")]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        elapsed = time.time() - start
        self.assertEqual({"a", "b"}, set(done))
        self.assertLess(elapsed, 0.45, "两个不同 key 必须并行（串行会是 ~0.5s）")
        self.assertEqual(0, flight.stats()["hits"])

    # 21 ─ 超时 → SingleFlightTimeout（leader 不受影响）
    def test_waiter_times_out(self) -> None:
        flight = SingleFlight()
        started = threading.Event()

        def slow() -> str:
            started.set()
            time.sleep(0.4)
            return "late"

        leader_holder: dict[str, object] = {}

        def leader() -> None:
            leader_holder["v"] = flight.do("k", slow)

        t = threading.Thread(target=leader)
        t.start()
        self.assertTrue(started.wait(timeout=2), "leader 必须已在跑")
        with self.assertRaises(SingleFlightTimeout) as ctx:
            flight.do("k", lambda: "never", timeout_s=0.05)
        self.assertEqual("k", ctx.exception.key)
        self.assertAlmostEqual(0.05, ctx.exception.timeout_s)
        t.join(timeout=5)
        self.assertEqual("late", leader_holder.get("v"), "等待者超时不影响 leader")
        self.assertEqual(1, flight.stats()["timeouts"])
        self.assertEqual(1, flight.stats()["executions"])

    # 加固 ─ fn 必须可调用；完成后不缓存（singleflight ≠ 缓存）
    def test_not_a_cache_and_requires_callable(self) -> None:
        flight = SingleFlight()
        with self.assertRaises(TypeError):
            flight.do("k", "not callable")           # type: ignore[arg-type]
        self.assertEqual(1, flight.do("k", lambda: 1))
        self.assertEqual(2, flight.do("k", lambda: 2), "顺序调用必须重新执行")


# ══════════════════════════════════════════════════════════════
# ④ wrap_with_singleflight：4 条主用例 + 1 条越权链加固
# ══════════════════════════════════════════════════════════════
class _RedactingGovernance:
    """假护栏：把 `sk-` 掩码成 `***`（验证出口护栏仍然生效）。"""

    def before_output(self, text: str) -> GuardVerdict:
        return GuardVerdict.ok(payload=text.replace("sk-live", "***"))


class _BlockingGovernance:
    def before_output(self, text: str) -> GuardVerdict:
        return GuardVerdict.block("policy: forbidden payload")


def _principal(user: str = "u1", scopes: tuple[str, ...] = ("resource.read",)) -> Principal:
    return Principal(user_id=user, tenant="acme", scopes=frozenset(scopes))


class WrapWithSingleFlightTest(unittest.TestCase):
    def _build(self, *, governance=None, cache_size: int = 8, body: str = "hello"):
        registry = ResourceRegistry()
        calls = {"n": 0}
        lock = threading.Lock()

        def reader(uri: str) -> str:
            with lock:
                calls["n"] += 1
            time.sleep(0.15)                 # 真下游的延迟：stampede 的触发条件
            return body

        registry.register(ResourceRef(uri="file://doc.txt", mime="text/markdown"), reader)
        runtime = ResourceRuntime(registry, governance=governance, cache_size=cache_size)
        return runtime, calls, registry

    @staticmethod
    def _ctx(principal: Principal) -> ExecCtx:
        return ExecCtx(principal=principal)

    # 22 ─ 并发 10 次读同一 uri → reader 只被调 1 次
    def test_concurrent_reads_hit_reader_once(self) -> None:
        runtime, calls, _ = self._build()
        wrapped = wrap_with_singleflight(runtime)
        self.assertIsInstance(wrapped, SingleFlightResourceRuntime)

        ctx = self._ctx(_principal())
        barrier = threading.Barrier(10)
        results: list[dict] = []
        errors: list[BaseException] = []
        lock = threading.Lock()

        def worker() -> None:
            barrier.wait(timeout=5)
            try:
                got = wrapped.read("file://doc.txt", ctx)
            except BaseException as exc:      # noqa: BLE001
                with lock:
                    errors.append(exc)
                return
            with lock:
                results.append(got)

        threads = [threading.Thread(target=worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        self.assertEqual([], errors)
        self.assertEqual(10, len(results))
        self.assertEqual(1, calls["n"], "并发首次读同一个 uri，reader 只该挨一次")
        self.assertEqual(1, wrapped.flight.stats()["executions"])
        self.assertEqual(9, wrapped.flight.stats()["hits"])
        # 缓存仍然在工作：后续读不再触达 reader
        wrapped.read("file://doc.txt", ctx)
        self.assertEqual(1, calls["n"])

    # 23 ─ 结果正确转发（content/mime/bytes/cached/truncated 都不丢）
    def test_result_fields_are_forwarded(self) -> None:
        runtime, calls, _ = self._build(body="x" * 10)
        wrapped = wrap_with_singleflight(runtime)
        ctx = self._ctx(_principal())
        got = wrapped.read("file://doc.txt", ctx)
        self.assertEqual("file://doc.txt", got["uri"])
        self.assertEqual("text/markdown", got["mime"])
        self.assertEqual("x" * 10, got["content"])
        self.assertEqual(10, got["bytes"])
        self.assertFalse(got["cached"])
        self.assertFalse(got["truncated"])
        # 第二次走缓存：cached=True，字段口径不变
        again = wrapped.read("file://doc.txt", ctx)
        self.assertTrue(again["cached"])
        self.assertEqual(got["content"], again["content"])
        # 其余接口原样可用（__getattr__ 转发）
        self.assertEqual(1, wrapped.cache_len())
        self.assertEqual(2, runtime.stats["reads"])
        self.assertEqual(1, wrapped.stats["hits"])          # 转发的是 dict，不是方法
        # singleflight ≠ 缓存：两次顺序读是两次 leader，第二次由内部 LRU 挡掉 reader。
        # 所以这里 executions=2 是对的（说明本层没偷偷把结果留成缓存），
        # 而 reader 调用数仍然是 1（上一行证明）。
        flights = wrapped.flight_stats()
        self.assertEqual((2, 0), (flights["executions"], flights["hits"]))
        self.assertIs(runtime, wrapped.inner)
        self.assertTrue(wrapped.invalidate("file://doc.txt"))
        self.assertEqual(0, wrapped.cache_len())
        self.assertEqual(1, calls["n"])

    # 24 ─ 治理/授权仍然生效（加了 singleflight 不许绕过 scope 检查）
    def test_authorization_is_not_bypassed(self) -> None:
        runtime, calls, _ = self._build()
        wrapped = wrap_with_singleflight(runtime)

        # ① 无权限主体：直接拒，且 reader 一次都没被调（先授权后读）
        with self.assertRaises(ResourcePermissionError):
            wrapped.read("file://doc.txt", self._ctx(_principal("u_bad", scopes=())))
        self.assertEqual(0, calls["n"])

        # ② 有权限主体：正常读
        ok = wrapped.read("file://doc.txt", self._ctx(_principal()))
        self.assertEqual("hello", ok["content"])

        # ③ 已缓存之后，无权限主体仍然被拒（缓存不是权限的旁路）
        with self.assertRaises(ResourcePermissionError):
            wrapped.read("file://doc.txt", self._ctx(_principal("u_bad", scopes=())))
        self.assertEqual(1, calls["n"])

        # ④ 未注册 / 非法 uri 的错误语义原样保留
        with self.assertRaises(ResourceError):
            wrapped.read("file://missing.txt", self._ctx(_principal()))

    # 25 ─ 输出护栏仍然生效（脱敏 / 拦截都沿原路径）
    def test_output_governance_still_applies(self) -> None:
        runtime, _, _ = self._build(governance=_RedactingGovernance(), body="key=sk-live-1")
        wrapped = wrap_with_singleflight(runtime)
        got = wrapped.read("file://doc.txt", self._ctx(_principal()))
        self.assertNotIn("sk-live", got["content"])
        self.assertIn("***", got["content"])

        blocked_runtime, calls2, _ = self._build(governance=_BlockingGovernance())
        blocked = wrap_with_singleflight(blocked_runtime)
        with self.assertRaises(ResourceError) as ctx:
            blocked.read("file://doc.txt", self._ctx(_principal()))
        self.assertIn("governance", str(ctx.exception))
        self.assertEqual(1, calls2["n"], "护栏允许先读（顺序语义不变），只是不放行")

    # 加固 ─ 跨主体不许共享：否则 A 的读会替无权限的 B 免检
    def test_different_principals_never_share_a_flight(self) -> None:
        runtime, calls, _ = self._build()
        wrapped = wrap_with_singleflight(runtime)
        barrier = threading.Barrier(2)
        errors: list[BaseException] = []

        def worker(user: str) -> None:
            barrier.wait(timeout=5)
            try:
                wrapped.read("file://doc.txt", self._ctx(_principal(user)))
            except BaseException as exc:      # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=("u1",)),
                   threading.Thread(target=worker, args=("u2",))]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        self.assertEqual([], errors)
        self.assertEqual(2, calls["n"], "不同主体必须各走一次（权限检查不可复用）")
        self.assertEqual(0, wrapped.flight.stats()["hits"])
        key1 = wrapped.flight_key("file://doc.txt", self._ctx(_principal("u1")))
        key2 = wrapped.flight_key("file://doc.txt", self._ctx(_principal("u2")))
        self.assertNotEqual(key1, key2)


# ══════════════════════════════════════════════════════════════
# ⑤ 用真 InMemoryKV 再验一遍（core/kv.py 未落盘则 skip）
# ══════════════════════════════════════════════════════════════
def _load_in_memory_kv():
    try:
        from core.kv import InMemoryKV          # 另一个 worker 的文件
    except Exception:                           # noqa: BLE001 - 还没落盘/还在改
        return None
    return InMemoryKV


class RealInMemoryKVTest(unittest.TestCase):
    """`FakeKV` 是我自己写的「顺手」实现，必须再用真后端验一次同样的结论。"""

    def setUp(self) -> None:
        factory = _load_in_memory_kv()
        if factory is None:
            self.skipTest("core/kv.py 尚未提供 InMemoryKV（并行施工中）")
        self.kv = factory()

    def test_blackboard_cas_across_instances_on_real_kv(self) -> None:
        a = DistributedBlackboard(self.kv)
        b = DistributedBlackboard(self.kv)
        self.assertTrue(a.create("plan", {"v": 1}, "A").ok)
        self.assertEqual({"v": 1}, b.read("plan").value)
        self.assertTrue(b.compare_and_set("plan", 1, {"v": 2}, "B").ok)
        stale = a.compare_and_set("plan", 1, {"v": 9}, "A")
        self.assertFalse(stale.ok)
        self.assertEqual(2, stale.current_version)
        self.assertEqual({"v": 2}, a.read("plan").value)

    def test_quota_total_is_exact_on_real_kv(self) -> None:
        a = DistributedQuota(self.kv, per_minute={"tool_call": 5})
        b = DistributedQuota(self.kv, per_minute={"tool_call": 5})
        allowed = sum(1 for i in range(8) if (a if i % 2 == 0 else b).charge("t").allowed)
        self.assertEqual(5, allowed)
        self.assertEqual(5, b.used("t"))


# ══════════════════════════════════════════════════════════════
# ⑥ 真·跨进程（两个 OS 进程 = 两个副本）：没有 Redis 时最短的诚实证明路径
# ══════════════════════════════════════════════════════════════
#: 仓库根（子进程 cwd，保证 `import plane.distributed` 能解析）
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: 子进程脚本：两个副本各自计费，合计必须收敛到同一个限额
_QUOTA_CHILD = r"""
import os, sys, time
sys.path.insert(0, os.getcwd())
from tests.test_distributed import FileLockedKV
from plane.distributed import DistributedQuota

kv = FileLockedKV(os.environ["W29_KV"])
start = float(os.environ["W29_START"])
while time.time() < start:          # 同一个起跑线：不看运气也能撞上竞态
    time.sleep(0.004)
q = DistributedQuota(kv, per_minute={"tool_call": 10})
print(sum(1 for _ in range(15) if q.charge("acme").allowed))
"""

#: 子进程脚本：两个副本都**手里攥着 v1** 提交，必须恰好一个成功。
#:
#: 刻意不用 "读 → 用读到的版本 CAS"：那会引入启动抖动（先起的那个已经把
#: 版本推到 v2，后起的读到 v2 再 CAS 也会成功 → 两个 OK，测试变随机）。
#: 这里显式传 `expected=1`，精确复现「两个副本都以为自己是 v1」这个丢更新
#: 的经典现场，且**结果与时序无关**：对同一份 v1 原始值，只有一个 CAS 能赢。
_CAS_CHILD = r"""
import os, sys, time
sys.path.insert(0, os.getcwd())
from tests.test_distributed import FileLockedKV
from plane.distributed import DistributedBlackboard

kv = FileLockedKV(os.environ["W29_KV"])
bb = DistributedBlackboard(kv)
name = os.environ["W29_NAME"]
start = float(os.environ["W29_START"])
while time.time() < start:
    time.sleep(0.004)
base = bb.read("plan")             # 双方都看到 v1（现场还原）
r = bb.compare_and_set("plan", 1, {"by": name}, name)   # 都基于 v1 提交
tag = "OK" if r.ok else ("CONFLICT@v%d" % r.current_version)
print("%s|seen=v%d" % (tag, base.version))
"""


class FileLockedKV:
    """跨进程 KV 替身：**文件 + `fcntl.flock` 排他锁**。

    本机没有 redis-server，而「两个副本一致」这件事必须用**两个 OS 进程**
    才能诚实地证明（同一进程里的两个实例，哪怕共享一个 dict，也说明不了
    跨副本）。flock 是全系统排他锁，效果等同「服务端一次只执行一个事务」：
    每个方法的「读 → 判定 → 写」都在同一把排他锁内完成，所以
    `compare_and_set` / `incr` 的原子性是**真的**（不是本进程的 RLock 假装的）。

    `shared = True` 也是真的：任何进程打开同一个文件都看得见同一份数据。
    """

    shared = True
    atomic = True
    note = "文件 + flock：跨进程排他事务（无 Redis 时的替身）"

    def __init__(self, path: str) -> None:
        self.path = str(path)
        if not os.path.exists(self.path):
            with open(self.path, "w", encoding="utf-8") as fh:
                fh.write("{}")

    @contextlib.contextmanager
    def _txn(self):
        with open(self.path, "r+", encoding="utf-8") as fh:
            import fcntl
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            try:
                fh.seek(0)
                data = json.loads(fh.read() or "{}")
                yield data
                fh.seek(0)
                fh.truncate()
                fh.write(json.dumps(data))
                fh.flush()
                os.fsync(fh.fileno())
            finally:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)

    # ── KV 协议 ─────────────────────────────────────────────
    def get(self, key: str) -> str | None:
        with self._txn() as data:
            return data.get(key)

    def set(self, key: str, value: str, *, ttl_s: float | None = None) -> bool:
        with self._txn() as data:
            data[key] = value
            return True

    def set_nx(self, key: str, value: str, *, ttl_s: float | None = None) -> bool:
        with self._txn() as data:
            if key in data:
                return False
            data[key] = value
            return True

    def compare_and_set(self, key: str, expected: str, value: str,
                        *, ttl_s: float | None = None) -> bool:
        with self._txn() as data:
            if data.get(key) != expected:
                return False
            data[key] = value
            return True

    def incr(self, key: str, delta: int = 1, *, ttl_s: float | None = None) -> int:
        with self._txn() as data:
            new = int(data.get(key, "0")) + int(delta)
            data[key] = str(new)
            return new

    def delete(self, key: str) -> bool:
        with self._txn() as data:
            return data.pop(key, None) is not None

    def expire(self, key: str, ttl_s: float) -> bool:
        return True

    def keys(self, prefix: str = "") -> list[str]:
        with self._txn() as data:
            return sorted(k for k in data if k.startswith(prefix))


class CrossProcessConsistencyTest(unittest.TestCase):
    """两个 OS 进程 = 两个副本。**这是「跨副本精确」唯一不含假设的证明形态**。

    一个进程里的两个 DistributedQuota 共享 dict，只能说明「分母集中」；
    两个进程才是「两个副本各有一份客户端、各有一个时钟」的真实形态。
    """

    def setUp(self) -> None:
        try:
            import fcntl  # noqa: F401 - 非 POSIX 平台没有 flock
        except Exception:  # noqa: BLE001
            self.skipTest("flock 不可用（非 POSIX），跨进程用例跳过")
        self.dir = tempfile.mkdtemp(prefix="w29-dist-")
        self.kv_path = os.path.join(self.dir, "kv.json")
        with open(self.kv_path, "w", encoding="utf-8") as fh:
            fh.write("{}")

    def tearDown(self) -> None:
        shutil.rmtree(self.dir, ignore_errors=True)

    def _run_children(self, script: str, names: list[str]) -> list[str]:
        start = time.time() + 0.5          # 统一起跑线
        env = {**os.environ, "W29_KV": self.kv_path, "W29_START": repr(start)}
        procs = []
        for name in names:
            child_env = {**env, "W29_NAME": name}
            procs.append(subprocess.Popen(
                [sys.executable, "-c", script], cwd=_REPO_ROOT, env=child_env,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True))
        outs: list[str] = []
        for proc in procs:
            out, err = proc.communicate(timeout=60)
            outs.append(out.strip() if out.strip() else f"ERR:{err.strip()[-300:]}")
        return outs

    def test_two_processes_share_one_quota(self) -> None:
        outs = self._run_children(_QUOTA_CHILD, ["replica-1", "replica-2"])
        allowed = [int(o) for o in outs]          # 两个 15 次尝试的放行数
        self.assertEqual(10, sum(allowed), f"合计必须收敛到限额 10，实际每个副本 {allowed}")
        kv = FileLockedKV(self.kv_path)
        q = DistributedQuota(kv, per_minute={"tool_call": 10})
        self.assertEqual(10, q.used("acme"))      # 计数器就是 10，不是 10×2

    def test_two_processes_race_one_cas_winner(self) -> None:
        DistributedBlackboard(FileLockedKV(self.kv_path)).create("plan", {"v": 1}, "seed")
        outs = self._run_children(_CAS_CHILD, ["A", "B"])
        oks = [o for o in outs if o.startswith("OK")]
        conflicts = [o for o in outs if o.startswith("CONFLICT")]
        self.assertEqual(1, len(oks), f"必须恰好一个成功：{outs}")
        self.assertEqual(1, len(conflicts), f"{outs}")
        self.assertIn("CONFLICT@v2", conflicts[0])   # 失败方拿到真版本，不是猜的
        self.assertGreaterEqual(sum(1 for o in outs if o.endswith("seen=v1")), 1,
                                f"至少有一方确实是基于 v1 提交的：{outs}")
        entry = DistributedBlackboard(FileLockedKV(self.kv_path)).read("plan")
        self.assertEqual(2, entry.version)        # 只升了一版：没有丢失更新/双写


if __name__ == "__main__":  # pragma: no cover
    unittest.main(verbosity=2)
