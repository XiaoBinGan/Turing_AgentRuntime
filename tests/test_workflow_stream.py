"""Workflow 级 SSE：流水线进度实时可见。

为什么需要：`GET /v1/workflows/{id}` 是同步快照，"跑到第几层"只能靠反复轮询。
运维盯发布时最想要的是"它现在在做什么"，不是"你再去问一次"。

实现是**轮询式**（不是事件总线）：状态本来就在内存，读一次很便宜；
轮询天然幂等 —— 重连的客户端不会漏掉"已经是 done"这种既成事实。
代价是延迟上限 = 轮询间隔（默认 200ms），对"第几层"这个粒度够用。
"""
from __future__ import annotations

import json
import time
import unittest

from fastapi.testclient import TestClient

import serve
from core.observability import JsonLogger, Metrics, ShutdownCoordinator
from core.store import MemoryStore

P = {"user_id": "u", "tenant": "acme", "roles": ["r"],
     "scopes": ["runtime:run"]}
SILENT = lambda *_a, **_k: None                             # noqa: E731


class _Turn:
    def __init__(self, ok=True, text="ok"):
        self.ok = ok
        self.text = text
        self.steps = 1
        self.phase = "DONE" if ok else "FAILED"
        self.term_reason = "DONE" if ok else "UNRECOVERABLE"
        self.error = "" if ok else "boom"
        self.audit_id = "a"
        self.task_id = "t"
        self.latency_ms = 1.0
        self.vertex: dict = {}
        self.partial = False


def _client(*, delay=0.0, fail_goal=None):
    class G:
        def handle(self, request):
            if delay:
                time.sleep(delay)
            if fail_goal and request.get("goal") == fail_goal:
                return _Turn(False)
            return _Turn(True)

        def handle_stream(self, request):
            yield {"type": "accepted", "seq": 0, "data": {}}
            yield {"type": "completed", "seq": 1, "data": {"ok": True, "text": "x"}}
    return TestClient(serve.create_app(
        gateway=G(), metrics=Metrics(), logger=JsonLogger(sink=SILENT),
        shutdown=ShutdownCoordinator(), deps={"store": None}, authenticator=None,
        workflow_store=MemoryStore(), workflow_workers=1))


def _parse(lines):
    out, ev = [], None
    for line in lines:
        line = line.strip()
        if line.startswith("event:"):
            ev = line[6:].strip()
        elif line.startswith("data:"):
            out.append((ev, json.loads(line[5:])))
    return out


def _submit(c, nodes):
    return c.post("/v1/workflows",
                  json={"name": "rel", "nodes": nodes, "principal": P}).json()["workflow_id"]


class TestWorkflowStream(unittest.TestCase):
    def test_stream_emits_progress_then_completed(self):
        c = _client(delay=0.1)
        wid = _submit(c, [{"name": "a", "goal": "A"},
                          {"name": "b", "goal": "B", "depends_on": ["a"]}])
        with c.stream("GET", f"/v1/workflows/{wid}/stream") as r:
            self.assertEqual(r.status_code, 200)
            self.assertIn("text/event-stream", r.headers.get("content-type", ""))
            events = _parse(r.iter_lines())
        kinds = [e[0] for e in events]
        self.assertEqual(kinds[0], "accepted")
        self.assertEqual(kinds[-1], "completed")
        self.assertIn("progress", kinds, "没有中间进度 = 只是同步查询的复读")

    def test_progress_shows_node_transitions(self):
        c = _client(delay=0.12)
        wid = _submit(c, [{"name": "a", "goal": "A"},
                          {"name": "b", "goal": "B", "depends_on": ["a"]}])
        with c.stream("GET", f"/v1/workflows/{wid}/stream") as r:
            events = _parse(r.iter_lines())
        done_counts = [e[1]["progress"]["done"] for e in events if e[0] == "progress"]
        self.assertGreaterEqual(max(done_counts), 1, "节点完成数没有递增")

    def test_completed_carries_final_status(self):
        c = _client()
        wid = _submit(c, [{"name": "a", "goal": "A"}])
        with c.stream("GET", f"/v1/workflows/{wid}/stream") as r:
            events = _parse(r.iter_lines())
        final = [e for e in events if e[0] == "completed"][-1][1]
        self.assertTrue(final["ok"])
        self.assertEqual(final["status"], "done")
        self.assertEqual(final["progress"]["done"], 1)

    def test_failed_workflow_stream_marks_not_ok(self):
        c = _client(fail_goal="BAD")
        wid = _submit(c, [{"name": "a", "goal": "BAD"}])
        with c.stream("GET", f"/v1/workflows/{wid}/stream") as r:
            events = _parse(r.iter_lines())
        final = [e for e in events if e[0] == "completed"][-1][1]
        self.assertFalse(final["ok"])
        self.assertEqual(final["status"], "failed")

    def test_unknown_id_is_404(self):
        c = _client()
        r = c.get("/v1/workflows/wf_nope/stream")
        self.assertEqual(r.status_code, 404)

    def test_no_duplicate_frames_when_idle(self):
        """轮询期间状态没变时**不该重复发帧**（否则 5 分钟刷 1500 条一样的事件）。"""
        c = _client(delay=0.5)
        wid = _submit(c, [{"name": "a", "goal": "A"}])
        with c.stream("GET", f"/v1/workflows/{wid}/stream") as r:
            events = _parse(r.iter_lines())
        sigs = [(e[1].get("status"), tuple((n["name"], n["status"])
                                           for n in e[1].get("nodes", [])))
                for e in events if e[0] == "progress"]
        self.assertEqual(len(sigs), len(set(sigs)), f"有重复帧：{sigs}")

    def test_stream_works_after_finish(self):
        """已完成的也能看（客户端连上时它已经跑完了 → 立刻收到终态）。"""
        c = _client()
        wid = _submit(c, [{"name": "a", "goal": "A"}])
        for _ in range(60):
            if c.get(f"/v1/workflows/{wid}").json()["status"] in ("done", "failed"):
                break
            time.sleep(0.02)
        with c.stream("GET", f"/v1/workflows/{wid}/stream") as r:
            events = _parse(r.iter_lines())
        self.assertEqual([e[0] for e in events][-1], "completed")
        self.assertEqual([e for e in events if e[0] == "completed"][-1][1]["status"], "done")

    def test_interval_is_configurable(self):
        """轮询间隔可配（延迟 vs 事件量是权衡，部署方自己定）。"""
        import os
        old = os.environ.get("AGENT_RUNTIME_WORKFLOW_STREAM_MS")
        try:
            os.environ["AGENT_RUNTIME_WORKFLOW_STREAM_MS"] = "50"
            c = _client()
            wid = _submit(c, [{"name": "a", "goal": "A"}])
            with c.stream("GET", f"/v1/workflows/{wid}/stream") as r:
                events = _parse(r.iter_lines())
            accepted = [e for e in events if e[0] == "accepted"][0][1]
            self.assertEqual(accepted["interval_ms"], 50)
        finally:
            if old is None:
                os.environ.pop("AGENT_RUNTIME_WORKFLOW_STREAM_MS", None)
            else:
                os.environ["AGENT_RUNTIME_WORKFLOW_STREAM_MS"] = old

    def test_sse_headers_present(self):
        c = _client()
        wid = _submit(c, [{"name": "a", "goal": "A"}])
        with c.stream("GET", f"/v1/workflows/{wid}/stream") as r:
            self.assertEqual(r.headers.get("x-accel-buffering"), "no")
            self.assertEqual(r.headers.get("cache-control"), "no-cache")


if __name__ == "__main__":
    unittest.main(verbosity=2)
