#!/usr/bin/env python3
"""真实 LLM 容量：测**上游决定的那部分**延迟。

**为什么必须单独测**：内置 `scripted` 适配器的 145 RPS 说明不了任何事 ——
真实回合的延迟几乎全在上游。一个 root-cause 任务实测 4-6 步、每步一次 LLM 调用，
如果单次调用 3 秒，那整个回合就是 12-18 秒，本层的排队开销（< 1 秒）可以忽略。

所以容量规划要分两层：
  * **本层**：单副本 145 QPS（已测，见 CAPACITY.md）
  * **上游**：每次调用的往返延迟分布（本脚本）

两者的关系不是加和，是**串行**：回合延迟 ≈ 步数 × 上游单次延迟 + 本层开销。
并发能力受上游 rate limit 约束，不受本层约束。

用法：
    BABEL_API_KEY=... BABEL_BASE_URL=... .venv/bin/python scripts/real_llm_capacity.py \\
        --model deepseek-flash --calls 20 --concurrency 4
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor

try:
    import httpx
except ImportError:
    sys.exit("需要 httpx：pip install httpx")

PROMPT = ("你是 SRE。线上 api 服务 p99 从 800ms 涨到 3s，"
          "请用不超过 60 字给出最可能的一个根因和下一步要查的指标。")


def one_call(client: httpx.Client, base: str, key: str, model: str) -> dict:
    """发一次 chat/completions，返回延迟与 usage。"""
    t0 = time.perf_counter()
    r = client.post(f"{base.rstrip('/')}/chat/completions",
                    headers={"Authorization": f"Bearer {key}"},
                    json={"model": model, "temperature": 0,
                          "messages": [{"role": "user", "content": PROMPT}],
                          "max_tokens": 120},
                    timeout=120.0)
    ms = (time.perf_counter() - t0) * 1000.0
    out = {"ms": ms, "status": r.status_code, "usage": {}, "err": ""}
    if r.status_code == 200:
        try:
            u = r.json().get("usage") or {}
            out["usage"] = {"in": u.get("prompt_tokens", 0),
                            "out": u.get("completion_tokens", 0),
                            "cached": u.get("prompt_cache_hit_tokens", 0)}
        except Exception:                                      # noqa: BLE001
            pass
    else:
        out["err"] = (r.text or "")[:120]
    return out


def pct(v: list[float], q: float) -> float:
    v = sorted(v)
    if not v:
        return 0.0
    if len(v) == 1:
        return v[0]
    pos = (len(v) - 1) * q
    lo = int(pos)
    hi = min(lo + 1, len(v) - 1)
    return v[lo] * (1 - (pos - lo)) + v[hi] * (pos - lo)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default=os.environ.get("BABEL_MODEL", "deepseek-flash"))
    ap.add_argument("--calls", type=int, default=20, help="总调用次数")
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--warmup", type=int, default=2,
                    help="前 N 次调用不计入统计（冷启动：模型/kv cache 未热）")
    ap.add_argument("--json-out", default="")
    a = ap.parse_args()

    base = os.environ.get("BABEL_BASE_URL") or os.environ.get("OPENAI_BASE_URL") or ""
    key = os.environ.get("BABEL_API_KEY") or os.environ.get("OPENAI_API_KEY") or ""
    if not base or not key:
        print("缺 BABEL_BASE_URL / BABEL_API_KEY（或 OPENAI_*）", file=sys.stderr)
        return 2

    print(f"  上游 {base}  模型 {a.model}")
    print(f"  {a.calls} 次调用，并发 {a.concurrency}")
    print()

    results: list[dict] = []
    t_all = time.perf_counter()
    with httpx.Client() as client, ThreadPoolExecutor(max_workers=a.concurrency) as pool:
        futs = [pool.submit(one_call, client, base, key, a.model)
                for _ in range(a.calls)]
        for i, f in enumerate(futs, 1):
            r = f.result()
            results.append(r)
            tag = "ok" if r["status"] == 200 else f"HTTP {r['status']}"
            print(f"  #{i:<3} {r['ms']:8.0f}ms  {tag}  "
                  f"in={r['usage'].get('in',0)} out={r['usage'].get('out',0)} "
                  f"cached={r['usage'].get('cached',0)}")
    wall = time.perf_counter() - t_all

    ok_all = [r for r in results if r["status"] == 200]
    # **剔除冷启动**：实测 qwen3.8-flash-next 前 2 次 25-40 秒，稳定后 1.4-2.1 秒。
    # 不剔除的话 p50 会被拖到 16 秒，结论完全错 —— 而"首次请求慢"是模型加载
    # 权重与构建 KV cache 的一次性成本，不是服务能力。
    warm = max(0, int(a.warmup))
    ok = ok_all[warm:] if len(ok_all) > warm else ok_all
    if warm and len(ok_all) > warm:
        cold = [r["ms"] for r in ok_all[:warm]]
        print(f"  （已剔除前 {warm} 次冷启动：{[round(x) for x in cold]} ms）")
    lat = [r["ms"] for r in ok]
    if not lat:
        print("\n  全部失败，无法给出容量结论。")
        for r in results[:3]:
            print(f"    {r['err']}")
        return 1

    tin = sum(r["usage"].get("in", 0) for r in ok)
    tout = sum(r["usage"].get("out", 0) for r in ok)
    tcached = sum(r["usage"].get("cached", 0) for r in ok)

    res = {
        "base": base, "model": a.model,
        "calls": len(results), "ok": len(ok),
        "warmup_dropped": min(warm, len(ok_all)),
        "wall_s": round(wall, 2),
        "rps": round(len(ok) / wall, 2) if wall else 0,
        "latency_ms": {
            "min": round(min(lat), 0), "p50": round(pct(lat, .5), 0),
            "p90": round(pct(lat, .9), 0), "p95": round(pct(lat, .95), 0),
            "p99": round(pct(lat, .99), 0), "max": round(max(lat), 0),
            "mean": round(statistics.fmean(lat), 0),
            "stdev": round(statistics.pstdev(lat), 0) if len(lat) > 1 else 0,
        },
        "tokens": {"in_total": tin, "out_total": tout, "cached_total": tcached,
                   "cache_ratio": round(tcached / tin, 3) if tin else 0},
    }
    if a.json_out:
        from pathlib import Path
        Path(a.json_out).write_text(json.dumps(res, ensure_ascii=False, indent=2))

    L = res["latency_ms"]
    print()
    print(f"  成功 {len(ok)}/{len(results)} | 墙钟 {wall:.1f}s | 有效 RPS {res['rps']}")
    print(f"  单次延迟 p50={L['p50']:.0f} p90={L['p90']:.0f} "
          f"p95={L['p95']:.0f} p99={L['p99']:.0f} max={L['max']:.0f} ms")
    print(f"  抖动 stdev={L['stdev']:.0f}ms（占 p50 的 {L['stdev']/max(L['p50'],1)*100:.0f}%）")
    print(f"  token in={tin} out={tout} cached={tcached}"
          f"（缓存命中率 {res['tokens']['cache_ratio']*100:.1f}%）")
    print()
    steps_range = range(4, 7)     # 实测 root-cause 任务 4-6 步
    print(f"  推算回合延迟（4-6 步 × 单次 p50）："
          f"{4*L['p50']/1000:.1f} ~ {6*L['p50']/1000:.1f} 秒")
    print(f"  推算回合延迟（4-6 步 × 单次 p99）："
          f"{4*L['p99']/1000:.1f} ~ {6*L['p99']/1000:.1f} 秒")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
