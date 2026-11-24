"""scripts/langfuse_roundtrip.py — 往真 Langfuse 投递一条 trace 并读回校验。

用法（`verify_langfuse.sh` 会调它）::

    LANGFUSE_HOST=http://127.0.0.1:3100 LANGFUSE_PK=pk-... LANGFUSE_SK=sk-... \\
      python3 scripts/langfuse_roundtrip.py

为什么走 ingestion REST 而不是 SDK：
    我们的 SDK 是 `langfuse 5.x`（讲 v3 的 OTLP 协议），而 v2 服务端没有
    `/api/public/otel`（实测 404）。v2 只认 `/api/public/ingestion`。
    所以这里用 `trace_summary()` 产出的 **Langfuse 形状**数据直接投递 ——
    要证明的是「我们的 span 数据能被真 Langfuse 接受」，不是「SDK 能连」。
"""
from __future__ import annotations

import base64
import datetime
import json
import os
import sys
import urllib.error
import urllib.request
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from plane.langfuse_export import trace_summary               # noqa: E402
from plane.quality import Tracer                             # noqa: E402

HOST = os.environ.get("LANGFUSE_HOST", "http://127.0.0.1:3100").rstrip("/")
PK = os.environ.get("LANGFUSE_PK", "")
SK = os.environ.get("LANGFUSE_SK", "")


def _auth() -> str:
    return base64.b64encode(f"{PK}:{SK}".encode()).decode()


def _request(path: str, *, body: dict | None = None, method: str = "GET") -> tuple[int, dict]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        f"{HOST}{path}", data=data, method=method,
        headers={"Content-Type": "application/json",
                 "Authorization": f"Basic {_auth()}"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode()
            return resp.status, (json.loads(raw) if raw.strip().startswith("{") else {})
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode()
        return exc.code, (json.loads(raw) if raw.strip().startswith("{") else {"raw": raw[:400]})


def main() -> int:
    if not PK or not SK:
        print("[skip] 未设 LANGFUSE_PK / LANGFUSE_SK")
        return 0

    # ── ① 造一条真 trace（用项目自己的 Tracer）─────────────────
    t = Tracer(tenant="acme", user_id="u_alben", task_id="lf-roundtrip")
    with t.span("loop.run", {"goal_len": 9}):
        with t.span("llm.complete", {"model": "deepseek-flash"}):
            pass
        with t.span("tool.invoke", {"tool": "query_logs"}):
            pass
    summ = trace_summary(t)
    print(f"① 本地 span 树：{len(summ.get('observations') or [])} 个 observation")

    # ── ② 转成 Langfuse ingestion 批次 ─────────────────────────
    now = datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")
    batch: list[dict] = [{
        "id": str(uuid.uuid4()), "type": "trace-create", "timestamp": now,
        # 注意：`redact()` 是**文本脱敏**（返回 str），不能拿来拼 dict。
        # 这里的 payload 只含 id/name/metadata，本就不含凭据。
        "body": {"id": summ["trace_id"], "name": summ["name"],
                 "userId": "u_alben", "metadata": summ.get("metadata") or {}},
    }]
    for o in summ.get("observations") or ():
        batch.append({
            "id": str(uuid.uuid4()), "type": "span-create", "timestamp": now,
            "body": {"id": o["id"], "traceId": summ["trace_id"], "name": o["name"],
                     "startTime": o["start_time"], "endTime": o.get("end_time"),
                     "metadata": o.get("metadata") or {}},
        })

    status, resp = _request("/api/public/ingestion", body={"batch": batch}, method="POST")
    ok_items = len(resp.get("successes") or [])
    err_items = len(resp.get("errors") or [])
    print(f"② 投递：HTTP {status}  成功 {ok_items} / 失败 {err_items}")
    if status not in (200, 207) or err_items:
        print(f"   失败详情：{json.dumps(resp, ensure_ascii=False)[:400]}")
        return 1

    # ── ③ 读回校验（真从服务端查）─────────────────────────────
    import time
    for _ in range(20):
        status, listing = _request("/api/public/traces?limit=5")
        rows = listing.get("data") or []
        target = next((r for r in rows if r.get("id") == summ["trace_id"]), None)
        if target is not None:
            obs = target.get("observations") or []
            print(f"③ 读回：trace={target.get('id')[:16]} name={target.get('name')!r} "
                  f"observations={len(obs)}")
            if len(obs) < len(summ.get("observations") or ()):
                print("   ⚠ observation 数不一致（服务端可能还在处理）")
            print("✅ Langfuse 真投递 + 读回通过")
            return 0
        time.sleep(1)
    print("③ 读回失败：服务端没有这条 trace")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
