#!/usr/bin/env python3
"""压测基线：在固定并发下测吞吐与延迟分位数。

**为什么需要它**：没有 p50/p95/p99 就无法定 SLO，也无法回答"几个副本够"。
功能测试能证明"能跑"，只有压测能证明"能扛多少"。

用法：
    .venv/bin/python scripts/loadtest.py --url http://127.0.0.1:18140 \\
        --concurrency 20 --duration 30

设计取舍：
  * **闭循环**（固定并发、每 worker 串行发下一个）而不是开循环 ——
    闭循环测的是"这个并发下系统能到多少 QPS"，正是容量规划要的数字。
  * 只统计**成功受理**（2xx）的延迟；错误单独计数，不混进分位数
    （否则一次超时会把 p99 拉得好看或难看，掩盖真实分布）。
  * 客户端用 httpx.AsyncClient 复用连接 —— 否则测的是 TCP 握手。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
from collections import Counter

try:
    import httpx
except ImportError:
    sys.exit("需要 httpx：pip install httpx")


BODY = {
    "goal": "查一下 api 服务的 p99 指标",
    "principal": {"user_id": "load", "tenant": "loadtest",
                  "roles": ["sre"], "scopes": ["runtime:run", "tool.read"]},
}


def pct(values: list[float], q: float) -> float:
    """分位数（线性插值）。values 必须已排序。"""
    if not values:
        return 0.0
    if len(values) == 1:
        return values[0]
    pos = (len(values) - 1) * q
    lo = int(pos)
    hi = min(lo + 1, len(values) - 1)
    frac = pos - lo
    return values[lo] * (1 - frac) + values[hi] * frac


async def worker(client: httpx.AsyncClient, url: str, deadline: float,
                 lat: list[float], codes: Counter, errors: Counter) -> None:
    """闭循环：发一个 → 等回来 → 再发下一个，直到到点。"""
    while time.monotonic() < deadline:
        t0 = time.perf_counter()
        try:
            r = await client.post(url, json=BODY, timeout=30.0)
            elapsed = (time.perf_counter() - t0) * 1000.0
            codes[r.status_code] += 1
            if 200 <= r.status_code < 300:
                lat.append(elapsed)
        except Exception as exc:                              # noqa: BLE001
            errors[type(exc).__name__] += 1
            codes["exception"] += 1


async def run(url: str, concurrency: int, duration: float,
              warmup: float) -> dict:
    limits = httpx.Limits(max_connections=concurrency,
                          max_keepalive_connections=concurrency)
    async with httpx.AsyncClient(limits=limits) as client:
        # 预热：建立连接池 + 让服务端 JIT/缓存热起来，不计入统计
        if warmup > 0:
            w_end = time.monotonic() + warmup
            await asyncio.gather(*[
                worker(client, url, w_end, [], Counter(), Counter())
                for _ in range(concurrency)])

        lat: list[float] = []
        codes: Counter = Counter()
        errors: Counter = Counter()
        start = time.monotonic()
        deadline = start + duration
        await asyncio.gather(*[
            worker(client, url, deadline, lat, codes, errors)
            for _ in range(concurrency)])
        wall = time.monotonic() - start

    lat.sort()
    ok = sum(1 for c, n in codes.items() if isinstance(c, int) and 200 <= c < 300
             for _ in range(n))
    return {
        "url": url,
        "concurrency": concurrency,
        "duration_s": round(wall, 2),
        "requests": sum(codes.values()),
        "ok": ok,
        "rps": round(sum(codes.values()) / wall, 2) if wall else 0.0,
        "codes": {str(k): v for k, v in sorted(codes.items(), key=lambda kv: str(kv[0]))},
        "errors": dict(errors),
        "latency_ms": {
            "count": len(lat),
            "min": round(lat[0], 2) if lat else 0.0,
            "p50": round(pct(lat, 0.50), 2),
            "p90": round(pct(lat, 0.90), 2),
            "p95": round(pct(lat, 0.95), 2),
            "p99": round(pct(lat, 0.99), 2),
            "max": round(lat[-1], 2) if lat else 0.0,
            "mean": round(statistics.fmean(lat), 2) if lat else 0.0,
            "stdev": round(statistics.pstdev(lat), 2) if len(lat) > 1 else 0.0,
        },
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Agent Runtime 压测")
    ap.add_argument("--url", required=True, help="例如 http://127.0.0.1:18140/v1/turns")
    ap.add_argument("--concurrency", type=int, default=10)
    ap.add_argument("--duration", type=float, default=20.0, help="测多久（秒）")
    ap.add_argument("--warmup", type=float, default=3.0, help="预热多久（秒）")
    ap.add_argument("--json", action="store_true", help="只输出 JSON")
    args = ap.parse_args()

    result = asyncio.run(run(args.url, args.concurrency, args.duration, args.warmup))
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    lat = result["latency_ms"]
    print(f"  并发 {result['concurrency']} × {result['duration_s']}s")
    print(f"  请求 {result['requests']} | 成功 {result['ok']} | RPS {result['rps']}")
    print(f"  状态码 {result['codes']}")
    if result["errors"]:
        print(f"  异常 {result['errors']}")
    print(f"  延迟 ms: p50={lat['p50']} p90={lat['p90']} "
          f"p95={lat['p95']} p99={lat['p99']} max={lat['max']}")
    print(f"           mean={lat['mean']} stdev={lat['stdev']} n={lat['count']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
