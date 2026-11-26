#!/usr/bin/env python3
"""sqlite store 的并发写入争用实测。

**为什么需要它**：`SQLiteStore` 用 WAL + busy_timeout，理论上"多读一写不互斥"。
但副作用的 checkpoint 是**写**，高并发回合下所有 worker 都在抢写锁 ——
这道锁的代价必须量化，否则"用 sqlite 就够了"是个没根据的判断。

要回答三个问题：
  1. 并发写时吞吐是多少（对比单线程）
  2. 拿不到写锁时是**排队**还是**报错**（busy_timeout 是否够）
  3. 多**进程**（不是多线程）写同一个文件时行为如何 —— 这才是多副本的真实形态
"""
from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.context import ContextHandle          # noqa: E402
from core.sqlite_store import SQLiteStore       # noqa: E402
from core.store import StoreError, save_checkpoint  # noqa: E402


def _one_write(store, i: int) -> float:
    t0 = time.perf_counter()
    ctx = ContextHandle(initial={"goal": f"t{i}", "i": i}, budget=None)
    save_checkpoint(ctx, store, f"ctx_{i:06d}", reason="bench")
    return (time.perf_counter() - t0) * 1000.0


def bench_threads(path: str, writers: int, per_writer: int) -> dict:
    """N 个线程各写 M 次（同一个 SQLiteStore 实例，线程内各用自己的连接）。"""
    store = SQLiteStore(path)
    lats: list[float] = []
    errs: list[str] = []
    lock = threading.Lock()

    def worker(base: int) -> None:
        local: list[float] = []
        local_err: list[str] = []
        for j in range(per_writer):
            try:
                local.append(_one_write(store, base + j))
            except Exception as exc:                            # noqa: BLE001
                local_err.append(f"{type(exc).__name__}: {exc}"[:100])
        with lock:
            lats.extend(local)
            errs.extend(local_err)

    t0 = time.perf_counter()
    ths = [threading.Thread(target=worker, args=(w * per_writer,))
           for w in range(writers)]
    for t in ths:
        t.start()
    for t in ths:
        t.join()
    wall = time.perf_counter() - t0
    return {
        "writers": writers,
        "total": writers * per_writer,
        "wall_s": round(wall, 3),
        "wps": round(len(lats) / wall, 1) if wall else 0,
        "p50_ms": round(statistics.median(lats), 2) if lats else 0,
        "p99_ms": round(sorted(lats)[int(len(lats) * 0.99) - 1], 2) if len(lats) > 10 else 0,
        "errors": len(errs),
        "err_sample": errs[:3],
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--writers", default="1,4,8,16")
    ap.add_argument("--per-writer", type=int, default=50)
    a = ap.parse_args()

    d = tempfile.mkdtemp(prefix="sqlite-bench-")
    print(f"  store: sqlite (WAL + busy_timeout)")
    print(f"  每写者 {a.per_writer} 次，共测 writers = {a.writers}")
    print()
    print(f"  {'写者':>4} {'总数':>6} {'墙钟s':>7} {'写/s':>7} {'p50ms':>7} {'p99ms':>7} {'错误':>5}")
    print("  " + "-" * 52)

    rows = []
    for w in (int(x) for x in a.writers.split(",")):
        r = bench_threads(str(Path(d) / f"t{w}.db"), w, a.per_writer)
        rows.append(r)
        print(f"  {r['writers']:>4} {r['total']:>6} {r['wall_s']:>7.2f} "
              f"{r['wps']:>7.1f} {r['p50_ms']:>7.2f} {r['p99_ms']:>7.2f} "
              f"{r['errors']:>5}")
        for e in r["err_sample"]:
            print(f"        ! {e}")

    print()
    base = rows[0]["wps"] if rows else 0
    for r in rows[1:]:
        print(f"  {r['writers']} 写者 vs 1 写者: {r['wps']/base:.2f}x "
              f"（理想 {r['writers']}x，WAL 下单写者串行是预期）")

    print()
    errs = sum(r["errors"] for r in rows)
    if errs == 0:
        print("  无 database is locked —— busy_timeout 足够，写锁是排队不是报错。")
    else:
        print(f"  {errs} 次写失败 —— busy_timeout 不够，高并发下会丢 checkpoint。")

    over = [r for r in rows if r["wps"] > base * 1.5]
    print()
    if over:
        print("  写入吞吐随写者增加而上升 → 说明锁竞争不严重。")
    else:
        print("  写入吞吐**不随写者增加** → sqlite 写锁是串行的，")
        print("  多副本/多 worker 共用同一文件时，checkpoint 会成为瓶颈。")
        print("  这就是多副本必须用 STORE=redis 的量化依据。")

    print()
    print(f"  数据目录: {d}（自行清理）")
    return 0 if errs == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())


# ══════════════════════════════════════════════════════════════
# 多**进程**写同一个文件 —— 这才是"多副本共用 sqlite"的真实形态
# ══════════════════════════════════════════════════════════════
_CHILD = r'''
import sys, time
sys.path.insert(0, {root!r})
from core.context import ContextHandle
from core.sqlite_store import SQLiteStore
from core.store import save_checkpoint
path, base, n = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
store = SQLiteStore(path)
ok = err = 0
for i in range(n):
    try:
        ctx = ContextHandle(initial={{"goal": "p", "i": base + i}}, budget=None)
        save_checkpoint(ctx, store, f"ctx_{{base + i:06d}}", reason="mp")
        ok += 1
    except Exception:
        err += 1
print(f"{{ok}},{{err}}")
'''


def bench_processes(path: str, procs: int, per_proc: int) -> dict:
    """N 个**独立进程**同时写同一个 sqlite 文件。"""
    root = str(Path(__file__).resolve().parent.parent)
    code = _CHILD.format(root=root)
    t0 = time.perf_counter()
    ps = [subprocess.Popen([sys.executable, "-c", code, path,
                            str(i * per_proc), str(per_proc)],
                           stdout=subprocess.PIPE, text=True)
          for i in range(procs)]
    ok = err = 0
    for p in ps:
        out, _ = p.communicate()
        try:
            o, e = out.strip().split(",")
            ok += int(o); err += int(e)
        except Exception:                                      # noqa: BLE001
            err += per_proc
    wall = time.perf_counter() - t0
    return {"procs": procs, "ok": ok, "err": err,
            "wall_s": round(wall, 2),
            "wps": round(ok / wall, 1) if wall else 0}


def bench_mp_main(procs_arg: str, per_proc: int) -> int:
    d = tempfile.mkdtemp(prefix="sqlite-mp-")
    print()
    print("  多进程写同一文件（模拟多副本共用 sqlite）")
    print(f"  {'进程':>4} {'成功':>6} {'失败':>6} {'墙钟s':>7} {'写/s':>8}")
    print("  " + "-" * 40)
    rows = []
    for n in (int(x) for x in procs_arg.split(",")):
        r = bench_processes(str(Path(d) / f"mp{n}.db"), n, per_proc)
        rows.append(r)
        print(f"  {r['procs']:>4} {r['ok']:>6} {r['err']:>6} "
              f"{r['wall_s']:>7.2f} {r['wps']:>8.1f}")
    base = rows[0]["wps"] if rows else 0
    for r in rows[1:]:
        print(f"  {r['procs']} 进程 vs 1 进程: {r['wps']/base:.2f}x")
    total_err = sum(r["err"] for r in rows)
    print()
    if total_err:
        print(f"  **{total_err} 次写失败** —— 多进程共用 sqlite 会丢 checkpoint，")
        print(f"  这是多副本必须用 STORE=redis 的直接证据。")
    else:
        # 结论**从数据推出来**，不预设。
        # 第一版这里写死了"吞吐不随进程数增长"，而实测是 2.5x 增长 ——
        # 预设污染了报告，这正是我一直警告的事。
        ratio = (rows[-1]["wps"] / base) if base else 0.0
        procs = rows[-1]["procs"]
        print(f"  零写失败（busy_timeout 覆盖了争用）。")
        print(f"  {procs} 进程吞吐是单进程的 {ratio:.2f}x（线性应为 {procs}x）")
        if ratio >= procs * 0.6:
            print("  → sqlite 的多进程写**基本可用**，争用不严重。")
        elif ratio >= 1.5:
            print("  → 能扩展但**明显亚线性**，高并发写会排队。")
        else:
            print("  → 几乎不扩展，写锁是瓶颈。")
        print()
        print("  但仍不建议多副本共用 sqlite，理由与吞吐无关：")
        print("    1. 网络文件系统（NFS/SMB/FUSE）上 WAL 不成立，锁语义失效")
        print("    2. 单文件是单点：一个副本的磁盘故障带走全部状态")
        print("    3. 备份要停写（或依赖文件系统快照），redis 可以做主从")
    return 0
