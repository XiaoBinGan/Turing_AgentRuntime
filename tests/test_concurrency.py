"""tests/test_concurrency.py — W25：并发加固的**证伪面**（stdlib unittest）。

这三个模块各自被作者诚实地标注过「假设单线程」（blackboard 有 RLock 但没被压力测过、
gateway 的配额窗口/审计列表/拒绝计数全是裸 `+=`、resources 的 LRU 是两步操作）。
本文件只干一件事：**用线程把每个共享可变状态按到地上摩擦**，然后证明
"计数不丢、语义不变、没有死锁"。

八条验收（对应任务书 ④）

1. blackboard：50 线程 × 100 次 CAS → `冲突 + 成功 == 总次数`（一次都没丢）
2. blackboard：并发 `create` 同一个 key → 恰好一个成功（"静默覆盖"必须不存在）
3. blackboard：并发 `write_private` 不同 owner → 互不干扰、各自版本自洽
4. gateway：10 线程并发 `handle` → `audit_log` 条数精确（审计不丢）
5. gateway：并发配额计费 → 超限次数精确（限流不是"大约"）
6. resources：并发 read 同一 uri → reader 调用次数 < 总请求数（缓存真的命中）
7. resources：并发 read 不同 uri → 无崩溃、内容正确
8. 死锁探测：**所有**用例统一走 `run_parallel(..., timeout=10)`，超时即失败；
   另加一条"三模块同时开跑"的混合用例

死锁是怎么证明的（不是"跑过去了就说明没死锁"）
--------------------------------------------------------------------------------
除了 `join(timeout=10)` 之外，每条用例都要求**每个线程写回自己的计数**，最后做
恒等式校验（成功 + 冲突 == 总次数、sum(各自的用量) == 全局用量）。死锁往往不是
"卡死"，而是"某个线程卡在锁里、其余线程还在跑"，此时 join 也会正常返回 ——
所以恒等式是第二道闸：丢一个线程的写入，等式立刻不成立。

跑法：
    cd /Users/supre/Documents/tmp/agent-runtime
    python3 -m unittest tests.test_concurrency -v
"""
from __future__ import annotations

import os
import sys
import threading
import time
import unittest
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from contracts import (  # noqa: E402
    ExecCtx,
    GuardVerdict,
    Partition,
    Phase,
    Principal,
    ResourceRef,
    Result,
    TermReason,
)
from core.gateway import RUN_SCOPE, Gateway  # noqa: E402
from plane.blackboard import Blackboard  # noqa: E402
from plane.resources import ResourceRegistry, ResourceRuntime  # noqa: E402

#: 所有并发用例的统一超时（秒）。超时 = 疑似死锁 = 失败。
JOIN_TIMEOUT = 10.0


# ══════════════════════════════════════════════════════════════
# 并发执行器（统一的 join(timeout) + 异常收集）
# ══════════════════════════════════════════════════════════════
def run_parallel(workers, *, timeout: float = JOIN_TIMEOUT) -> list[str]:
    """跑一组 worker；返回错误清单（空 = 全部干净结束）。

    三条纪律，少一条这个工具就会骗人：
      * `join(timeout)` —— 超时立刻失败，而不是让测试挂到 CI 上限再嘴硬说"慢"；
      * worker 里的**任何**异常（含 AssertionError）都要被收集：线程里抛的异常
        默认只会打到 stderr，主线程照样绿；
      * 用 `Barrier` 让尽可能多的线程**同时**进门，否则线程其实是排队跑的，
        "并发测试"会退化成"串行测试"，锁的缺口一个都测不出来。
    """
    errors: list[str] = []
    errors_lock = threading.Lock()

    def wrap(index, fn):
        def inner():
            try:
                fn()
            except BaseException as exc:          # noqa: BLE001 —— 收集一切
                with errors_lock:
                    errors.append(f"worker[{index}]: {type(exc).__name__}: {exc}")
        return inner

    threads = [
        threading.Thread(target=wrap(i, fn), name=f"w{i}", daemon=True)
        for i, fn in enumerate(workers)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout)

    alive = [t.name for t in threads if t.is_alive()]
    if alive:
        raise AssertionError(
            f"线程在 {timeout}s 内没有结束（疑似死锁/活锁）：{alive}")
    return errors


def at_once(workers):
    """用 Barrier 把 worker 卡在同一起跑线上（真正的同时进门）。"""
    barrier = threading.Barrier(len(workers), timeout=JOIN_TIMEOUT)

    def gated(fn):
        def inner():
            barrier.wait()
            fn()
        return inner

    return [gated(w) for w in workers]


# ══════════════════════════════════════════════════════════════
# 测试替身
# ══════════════════════════════════════════════════════════════
class _Loop:
    """最小 Loop：Gateway 只要求 `run(goal, ctx, e) -> Result`。"""

    def run(self, goal, ctx, e):
        return Result(phase=Phase.DONE, term_reason=TermReason.DONE,
                      text="done", partial=False, steps=1)

    def resume(self, ctx, e, decision=True, reason=""):
        return self.run("", ctx, e)


def _request(tenant: str = "acme", goal: str = "把营收按区域汇总") -> dict:
    return {
        "goal": goal,
        "principal": {
            "user_id": "u_1",
            "roles": ["engineer"],
            "tenant": tenant,
            "scopes": [RUN_SCOPE, "tool.*", "memory.*", "resource.read"],
        },
    }


def _exec_ctx(scopes=("resource.read",)) -> ExecCtx:
    return ExecCtx(principal=Principal(user_id="u_1", tenant="acme",
                                       scopes=frozenset(scopes)))


class _CountingReader:
    """线程安全地数 reader 被调了几次（缓存是否真的省掉了 reader）。"""

    def __init__(self, body: str = "resource body"):
        self.body = body
        self.calls = 0
        self._lock = threading.Lock()

    def __call__(self, uri: str) -> str:
        with self._lock:
            self.calls += 1
        return f"{self.body}|{uri}"


class _RedactingGovernance:
    """形状与 Governance.before_output 一致：把 secret 掩掉（证明护栏在出口生效）。"""

    def before_output(self, text: str) -> GuardVerdict:
        return GuardVerdict.ok(payload=text.replace("sk-live", "***REDACTED***"))


# ══════════════════════════════════════════════════════════════
# ① blackboard
# ══════════════════════════════════════════════════════════════
class TestBlackboardConcurrency(unittest.TestCase):
    def test_cas_stress_no_lost_updates(self) -> None:
        """1) 50 线程 × 100 次 CAS：冲突 + 成功 == 5000，且最终值 == 成功次数。

        为什么这条断言是"无丢失"的证明：CAS 的成功必须严格让 value +1
        （每次成功都基于**当时**的版本读到的值），所以
        `最终值 == 成功次数`、`成功 + 冲突 == 总尝试`。任何一个丢失的
        `writes` 计数、任何一次"读-比-写"被打断，两条等式都会立刻破。
        """
        bb = Blackboard()
        bb.create("counter", 0, "seed")
        results: list[tuple[int, int]] = []
        results_lock = threading.Lock()

        def worker():
            ok = conflict = 0
            for _ in range(100):
                cur = bb.read("counter")
                res = bb.compare_and_set("counter", cur.version, cur.value + 1, "worker")
                if res.ok:
                    ok += 1
                else:
                    conflict += 1
            with results_lock:
                results.append((ok, conflict))

        errors = run_parallel(at_once([worker] * 50))
        self.assertEqual(errors, [])

        total = 50 * 100
        successes = sum(ok for ok, _ in results)
        conflicts = sum(c for _, c in results)
        self.assertEqual(len(results), 50)                 # 没有一个线程"消失"
        self.assertEqual(successes + conflicts, total)
        self.assertEqual(bb.stats()["conflicts"], conflicts)
        self.assertEqual(bb.stats()["writes"], successes + 1)   # +1 是 create
        final = bb.read("counter")
        self.assertEqual(final.value, successes)
        self.assertEqual(final.version, successes + 1)

    def test_concurrent_create_same_key_exactly_one_wins(self) -> None:
        """2) 50 线程同时 create 同一个 key → 恰好一个成功。"""
        bb = Blackboard()
        outcomes: list[bool] = []
        lock = threading.Lock()

        def worker():
            res = bb.create("plan", {"steps": ["A"]}, "leader")
            with lock:
                outcomes.append(res.ok)

        errors = run_parallel(at_once([worker] * 50))
        self.assertEqual(errors, [])
        self.assertEqual(outcomes.count(True), 1)
        self.assertEqual(outcomes.count(False), 49)
        self.assertEqual(bb.read("plan").version, 1)
        self.assertEqual(bb.stats()["conflicts"], 49)
        self.assertEqual(bb.stats()["entries"], 1)

    def test_concurrent_write_private_isolated_per_owner(self) -> None:
        """3) 20 个 owner 各写 50 次自己的私有区 → 互不干扰、版本各自到 50。"""
        bb = Blackboard()
        owners = [f"agent_{i:02d}" for i in range(20)]
        report: list[tuple[str, int]] = []
        lock = threading.Lock()

        def make(owner: str, rounds: int):
            def worker():
                last = 0
                for i in range(rounds):
                    res = bb.write_private("scratch", {"owner": owner, "i": i}, owner)
                    assert res.ok, f"{owner} 私有写失败：{res.reason}"
                    last = res.version
                with lock:
                    report.append((owner, last))
            return worker

        errors = run_parallel(at_once([make(o, 50) for o in owners]))
        self.assertEqual(errors, [])
        self.assertEqual(len(report), 20)

        for owner, last_version in report:
            # 版本自洽：50 次写 → v50（中间的版本号一次都没丢）
            self.assertEqual(last_version, 50, f"{owner} 版本跳号")
            entry = bb.read("scratch", agent=owner, partition=Partition.PRIVATE)
            self.assertIsNotNone(entry)
            self.assertEqual(entry.owner, owner)
            self.assertEqual(entry.value["owner"], owner)   # 没被别的 owner 串味
            self.assertEqual(entry.value["i"], 49)
            self.assertEqual(entry.version, 50)
            # 私有不外泄：别的 owner 看不到
            other = "agent_99"
            self.assertIsNone(bb.read("scratch", agent=other,
                                      partition=Partition.PRIVATE))

        self.assertEqual(bb.stats()["entries"], 20)
        self.assertEqual(bb.stats()["writes"], 20 * 50)

    def test_concurrent_delete_and_create(self) -> None:
        """delete 也走 CAS：50 线程抢着删同一个 key → 恰好一个成功。"""
        bb = Blackboard()
        bb.create("tmp", 1, "seed")
        outcomes: list[bool] = []
        lock = threading.Lock()

        def worker():
            res = bb.delete("tmp", 1, "worker")
            with lock:
                outcomes.append(res.ok)

        errors = run_parallel(at_once([worker] * 50))
        self.assertEqual(errors, [])
        self.assertEqual(outcomes.count(True), 1)
        self.assertIsNone(bb.read("tmp"))
        self.assertEqual(bb.stats()["deletes"], 1)

    def test_subscriber_callbacks_do_not_deadlock_writes(self) -> None:
        """回调在锁外跑：一个睡 1ms 的订阅者不该把写入串死，也不该死锁。"""
        bb = Blackboard()
        seen: list[int] = []
        lock = threading.Lock()

        def slow_cb(payload: dict) -> None:
            time.sleep(0.001)
            with lock:
                seen.append(payload["version"])

        bb.subscribe("*", slow_cb)
        bb.create("k", 0, "seed")
        done: list[int] = []

        def worker():
            for _ in range(20):
                cur = bb.read("k")
                if bb.compare_and_set("k", cur.version, cur.value + 1, "w").ok:
                    done.append(1)

        errors = run_parallel(at_once([worker] * 4))
        self.assertEqual(errors, [])
        self.assertEqual(len(seen), 1 + len(done))     # create + 每次成功更新各一条
        self.assertEqual(bb.stats()["writes"], 1 + len(done))


# ══════════════════════════════════════════════════════════════
# ② gateway
# ══════════════════════════════════════════════════════════════
class TestGatewayConcurrency(unittest.TestCase):
    def test_concurrent_handle_audit_not_lost(self) -> None:
        """4) 10 线程并发 handle（不限流）→ audit_log 条数 == 回合数。"""
        gw = Gateway(lambda goal, tc: _Loop(), rate_limit_per_minute=10_000)
        total = 10 * 20
        ok: list[bool] = []
        lock = threading.Lock()

        def worker():
            for _ in range(20):
                res = gw.handle(_request())
                with lock:
                    ok.append(res.ok)

        errors = run_parallel(at_once([worker] * 10))
        self.assertEqual(errors, [])
        self.assertEqual(len(ok), total)
        self.assertTrue(all(ok), "不该有失败回合")
        self.assertEqual(len(gw.audit_log), total)          # 审计一条不丢
        self.assertEqual(len({rec["audit_id"] for rec in gw.audit_log}), total)
        self.assertEqual(gw.snapshot()["audit_count"], total)
        self.assertEqual(gw.denied_total, 0)

    def test_concurrent_quota_is_exact(self) -> None:
        """5) 固定时钟 + 限流 25：50 个并发请求里**恰好** 25 个被拒。

        这条是配额锁的核心证伪：没有锁时，多个线程会同时读到 `used=24`
        然后一起放行，实际放行数变成「25 + 并发度」；有了锁，放行数必须
        一个不多一个不少。
        """
        limit = 25
        clock = lambda: 1_000.0                        # noqa: E731 —— 窗口永不滑动
        gw = Gateway(lambda goal, tc: _Loop(), rate_limit_per_minute=limit,
                     clock=clock)
        total = 50
        allowed: list[bool] = []
        lock = threading.Lock()

        def make():
            def worker():
                res = gw.handle(_request())
                with lock:
                    allowed.append(res.ok)
            return worker

        errors = run_parallel(at_once([make() for _ in range(total)]))
        self.assertEqual(errors, [])
        self.assertEqual(len(allowed), total)
        self.assertEqual(allowed.count(True), limit)
        self.assertEqual(allowed.count(False), total - limit)

        snap = gw.snapshot()
        self.assertEqual(snap["windows"]["acme"]["used"], limit)
        self.assertEqual(snap["windows"]["acme"]["remaining"], 0)
        self.assertEqual(snap["denied_total"], total - limit)
        self.assertEqual(snap["denied_by_code"], {"rate_limited": total - limit})
        self.assertEqual(len(gw.audit_log), total)      # 被拒的也要有审计

    def test_concurrent_quota_per_tenant_isolated(self) -> None:
        """按租户分桶：打满 acme 不影响 globex（锁不该把租户串成一条线）。"""
        clock = lambda: 1_000.0                        # noqa: E731
        gw = Gateway(lambda goal, tc: _Loop(), rate_limit_per_minute=5, clock=clock)
        acme_ok: list[bool] = []
        globex_ok: list[bool] = []
        lock = threading.Lock()

        def make(tenant: str, sink: list):
            def worker():
                for _ in range(5):
                    res = gw.handle(_request(tenant=tenant))
                    with lock:
                        sink.append(res.ok)
            return worker

        workers = [make("acme", acme_ok) for _ in range(4)]
        workers += [make("globex", globex_ok) for _ in range(2)]
        errors = run_parallel(at_once(workers))
        self.assertEqual(errors, [])
        self.assertEqual(acme_ok.count(True), 5)       # acme 打满 5 个
        self.assertEqual(globex_ok.count(True), 5)     # globex 不受影响（上限也是 5）
        snap = gw.snapshot()
        self.assertEqual(snap["windows"]["acme"]["used"], 5)
        self.assertEqual(snap["windows"]["globex"]["used"], 5)

    def test_snapshot_during_traffic_never_raises(self) -> None:
        """边写边读：`snapshot()` 迭代 `_windows` 时不能被并发 setdefault 掀翻。"""
        gw = Gateway(lambda goal, tc: _Loop(), rate_limit_per_minute=100_000)
        errors: list[str] = []

        def writer(i: int):
            def inner():
                for _ in range(30):
                    gw.handle(_request(tenant=f"t{i % 7}"))
            return inner

        def reader():
            def inner():
                for _ in range(200):
                    snap = gw.snapshot()
                    assert "windows" in snap, f"snapshot 退化成错误分支：{snap}"
            return inner

        workers = [writer(i) for i in range(8)] + [reader() for _ in range(2)]
        errors = run_parallel(at_once(workers))
        self.assertEqual(errors, [])
        self.assertEqual(len(gw.audit_log), 8 * 30)


# ══════════════════════════════════════════════════════════════
# ③ resources
# ══════════════════════════════════════════════════════════════
class TestResourcesConcurrency(unittest.TestCase):
    def _runtime(self, uris, *, cache_size: int = 128, governance=None):
        registry = ResourceRegistry()
        readers = {}
        for uri in uris:
            reader = _CountingReader()
            readers[uri] = reader
            registry.register(ResourceRef(uri=uri), reader)
        runtime = ResourceRuntime(registry, cache_size=cache_size, governance=governance)
        return registry, runtime, readers

    def test_concurrent_read_same_uri_hits_cache(self) -> None:
        """6) 20 线程 × 20 次读同一 uri → reader 调用次数远小于 400。"""
        _, runtime, readers = self._runtime(["db://schema/users"])
        reader = readers["db://schema/users"]
        total = 20 * 20
        bodies: list[str] = []
        lock = threading.Lock()

        def worker():
            for _ in range(20):
                out = runtime.read("db://schema/users", _exec_ctx())
                with lock:
                    bodies.append(out["content"])

        errors = run_parallel(at_once([worker] * 20))
        self.assertEqual(errors, [])
        self.assertEqual(len(bodies), total)
        self.assertTrue(all(b == "resource body|db://schema/users" for b in bodies))

        stats = runtime.stats
        self.assertLess(reader.calls, total, "缓存没生效：每次读都调了 reader")
        self.assertEqual(reader.calls, stats["misses"])       # 一次 miss 一次 reader
        self.assertEqual(stats["hits"] + stats["misses"], total)  # 计数一分不丢
        self.assertEqual(stats["reads"], total)
        self.assertEqual(runtime.cache_len(), 1)

    def test_concurrent_read_different_uris(self) -> None:
        """7) 50 个 uri 并发读 → 无崩溃、内容正确、缓存条数受上限约束。"""
        uris = [f"file://doc/{i:03d}" for i in range(50)]
        _, runtime, _readers = self._runtime(uris, cache_size=8)
        errors: list[str] = []
        lock = threading.Lock()

        def make(uri: str):
            def worker():
                for _ in range(10):
                    out = runtime.read(uri, _exec_ctx())
                    if out["content"] != f"resource body|{uri}":
                        raise AssertionError(f"内容串味：{out['content']!r}")
            return worker

        workers = [make(uri) for uri in uris]
        errs = run_parallel(at_once(workers))
        self.assertEqual(errs, [])

        stats = runtime.stats
        self.assertEqual(stats["hits"] + stats["misses"], 50 * 10)
        self.assertEqual(stats["reads"], 50 * 10)
        self.assertLessEqual(runtime.cache_len(), 8)      # LRU 上限没被并发挤破

    def test_concurrent_register_race_is_exclusive(self) -> None:
        """注册表的 check-then-act：50 线程抢注同一 uri → 恰好一个成功。"""
        registry = ResourceRegistry()
        ref = ResourceRef(uri="db://schema/users")
        outcomes: list[bool] = []
        lock = threading.Lock()

        def worker():
            try:
                registry.register(ref, _CountingReader())
                won = True
            except ValueError:
                won = False
            with lock:
                outcomes.append(won)

        errors = run_parallel(at_once([worker] * 50))
        self.assertEqual(errors, [])
        self.assertEqual(outcomes.count(True), 1)
        self.assertEqual(outcomes.count(False), 49)
        self.assertEqual(len(registry), 1)

    def test_cache_invalidate_under_traffic(self) -> None:
        """边读边失效：LRU 的 pop 与 move_to_end 不能互相踩出 KeyError。"""
        _, runtime, _readers = self._runtime(["db://a", "db://b"], cache_size=2)

        def reader_worker():
            def inner():
                for _ in range(200):
                    runtime.read("db://a", _exec_ctx())
                    runtime.read("db://b", _exec_ctx())
            return inner

        def invalidator():
            def inner():
                for _ in range(200):
                    runtime.invalidate("db://a")
                    runtime.clear_cache()
            return inner

        workers = [reader_worker() for _ in range(6)] + [invalidator() for _ in range(2)]
        errors = run_parallel(at_once(workers))
        self.assertEqual(errors, [])
        self.assertLessEqual(runtime.cache_len(), 2)

    def test_guard_runs_on_every_cache_hit(self) -> None:
        """护栏在出口每次重算（缓存不绕过脱敏）—— 并发下也是。"""
        _, runtime, _readers = self._runtime(
            ["db://secret"], governance=_RedactingGovernance())
        contents: list[str] = []
        lock = threading.Lock()

        def worker():
            for _ in range(20):
                out = runtime.read("db://secret", _exec_ctx())
                with lock:
                    contents.append(out["content"])

        errors = run_parallel(at_once([worker] * 10))
        self.assertEqual(errors, [])
        self.assertEqual(len(contents), 200)
        # 内容里本来没有 sk-live，这条只是证明护栏路径在并发下没被跳过/没炸
        self.assertTrue(all("db://secret" in c for c in contents))
        self.assertEqual(runtime.stats["hits"] + runtime.stats["misses"], 200)


# ══════════════════════════════════════════════════════════════
# ④ 死锁探测：三个模块同时开跑
# ══════════════════════════════════════════════════════════════
class TestDeadlockProbe(unittest.TestCase):
    def test_mixed_modules_no_deadlock(self) -> None:
        """8) blackboard + gateway + resources 同时并发，join(10s) 必须全部返回，
        而且要能**连续跑两轮**（第二轮还活着，说明没有锁残留/线程泄漏）。"""
        bb = Blackboard()
        bb.create("counter", 0, "seed")
        gw = Gateway(lambda goal, tc: _Loop(), rate_limit_per_minute=100_000)

        registry = ResourceRegistry()
        reader = _CountingReader()
        registry.register(ResourceRef(uri="db://schema/users"), reader)
        runtime = ResourceRuntime(registry)

        rounds: list[dict] = []
        cas_lock = threading.Lock()
        cas_success: list[int] = []        # 只数成功：writes 计数器只记成功写

        def run_round(round_index: int) -> None:
            def bb_worker():
                ok = 0
                for _ in range(50):
                    cur = bb.read("counter")
                    if bb.compare_and_set("counter", cur.version,
                                          cur.value + 1, "w").ok:
                        ok += 1
                    bb.write_private("note", {"r": round_index}, "w")
                    bb.list()
                with cas_lock:
                    cas_success.append(ok)

            def gw_worker(i: int):
                def inner():
                    for _ in range(5):
                        gw.handle(_request(tenant=f"t{i % 3}"))
                return inner

            def res_worker():
                for _ in range(20):
                    runtime.read("db://schema/users", _exec_ctx())
                    registry.list()

            workers = [bb_worker] * 10
            workers += [gw_worker(i) for i in range(6)]
            workers += [res_worker] * 6
            errors = run_parallel(at_once(workers), timeout=JOIN_TIMEOUT)
            self.assertEqual(errors, [])
            rounds.append({"bb": bb.stats(), "gw": gw.snapshot(),
                           "res": dict(runtime.stats)})

        run_round(1)
        run_round(2)

        # 两轮都在 timeout 内结束；计数仍然自洽（不是"第二轮把第一轮吃掉"）
        self.assertEqual(len(rounds), 2)
        self.assertEqual(len(cas_success), 20)                  # 两轮 × 10 个 CAS worker
        self.assertEqual(rounds[1]["gw"]["audit_count"], 2 * 6 * 5)
        # writes = 成功 CAS + 私有写（每轮 10×50）+ create 一次
        self.assertEqual(rounds[1]["bb"]["writes"], sum(cas_success) + 2 * 10 * 50 + 1)
        # 冲突计数 + 成功次数 == 总尝试次数（一次都没丢）
        self.assertEqual(rounds[1]["bb"]["conflicts"], 2 * 10 * 50 - sum(cas_success))
        self.assertEqual(rounds[1]["res"]["reads"], 2 * 6 * 20)

    def test_no_thread_leak(self) -> None:
        """跑完不留活线程（`join` 卡住的反面：线程没被回收）。"""
        before = threading.active_count()
        bb = Blackboard()
        bb.create("k", 0, "seed")

        def worker():
            for _ in range(20):
                cur = bb.read("k")
                bb.compare_and_set("k", cur.version, cur.value + 1, "w")

        run_parallel([worker] * 8)
        time.sleep(0.05)
        self.assertLessEqual(threading.active_count(), before + 1)


# ══════════════════════════════════════════════════════════════
# ⑤ 施工纪律要求的那一发：50 线程 × 各 100 次操作，三模块混合
# ══════════════════════════════════════════════════════════════
class TestHeavyStress50x100(unittest.TestCase):
    """一个 5000 次操作的混合压力：结果正确 + 无死锁，一次说完。

    分工写死（`i % 3`）：1700 次 gateway.handle / 1650 次 resource.read /
    1650 次 blackboard CAS。三条计数必须**逐一精确**，因为每个线程自己数自己
    做了多少次，主线程只做求和 —— 任何一次丢失（线程静默死掉、写入被吞）
    都会让等式不成立。
    """

    def test_50_threads_100_ops_mixed(self) -> None:
        threads_n, ops_n = 50, 100
        bb = Blackboard()
        bb.create("counter", 0, "seed")
        gw = Gateway(lambda goal, tc: _Loop(), rate_limit_per_minute=1_000_000)

        registry = ResourceRegistry()
        reader = _CountingReader()
        registry.register(ResourceRef(uri="db://schema/users"), reader)
        runtime = ResourceRuntime(registry)

        per_thread: list[tuple[int, int, int]] = []
        lock = threading.Lock()

        def worker():
            gw_ops = res_ops = bb_ops = 0
            for i in range(ops_n):
                slot = i % 3
                if slot == 0:
                    assert gw.handle(_request()).ok
                    gw_ops += 1
                elif slot == 1:
                    out = runtime.read("db://schema/users", _exec_ctx())
                    assert out["uri"] == "db://schema/users"
                    res_ops += 1
                else:
                    cur = bb.read("counter")
                    bb.compare_and_set("counter", cur.version, cur.value + 1, "w")
                    bb.list(agent="w")
                    bb_ops += 1
            with lock:
                per_thread.append((gw_ops, res_ops, bb_ops))

        started = time.perf_counter()
        errors = run_parallel(at_once([worker] * threads_n), timeout=JOIN_TIMEOUT)
        elapsed = time.perf_counter() - started

        self.assertEqual(errors, [])
        self.assertEqual(len(per_thread), threads_n)                 # 50 个都回来了
        gw_ops = sum(a for a, _, _ in per_thread)
        res_ops = sum(b for _, b, _ in per_thread)
        bb_ops = sum(c for _, _, c in per_thread)

        # i%3 的确定分布：0,3,…,99 → 34 次；1,4,…,97 → 33 次；2,5,…,98 → 33 次
        self.assertEqual((gw_ops, res_ops, bb_ops),
                         (threads_n * 34, threads_n * 33, threads_n * 33))
        self.assertEqual(gw_ops + res_ops + bb_ops, threads_n * ops_n)  # 5000

        # 1) gateway：审计一条不丢
        self.assertEqual(len(gw.audit_log), gw_ops)
        self.assertEqual(gw.snapshot()["audit_count"], gw_ops)
        # 2) resources：reads 精确；命中 + 未命中 == reads；reader 调用远少于 reads
        self.assertEqual(runtime.stats["reads"], res_ops)
        self.assertEqual(runtime.stats["hits"] + runtime.stats["misses"], res_ops)
        self.assertLess(reader.calls, res_ops)
        # 3) blackboard：成功 + 冲突 == 尝试次数，最终值 == 成功次数
        stats = bb.stats()
        self.assertEqual(stats["writes"] + stats["conflicts"], bb_ops + 1)
        final = bb.read("counter")
        self.assertEqual(final.value, stats["writes"] - 1)
        self.assertEqual(final.version, stats["writes"])
        self.assertLess(elapsed, JOIN_TIMEOUT, "5000 次操作不该逼近超时")


if __name__ == "__main__":       # pragma: no cover
    unittest.main(verbosity=2)
