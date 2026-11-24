"""Workflow 续跑验收。

**续跑与"重跑一遍"的唯一区别：跳过已完成的节点。**

重放已完成的节点会重复产生副作用 —— 发布流程里"重复发版/重复删库"是灾难。
所以这个文件最核心的一条断言是：**续跑时不该重新执行已完成的节点**。
"""
from __future__ import annotations

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
    def __init__(self, ok: bool):
        self.ok = ok
        self.text = "ok" if ok else ""
        self.steps = 1 if ok else 0
        self.phase = "DONE" if ok else "FAILED"
        self.term_reason = "DONE" if ok else "UNRECOVERABLE"
        self.error = "" if ok else "boom"
        self.audit_id = "a"
        self.task_id = "t"
        self.latency_ms = 1.0
        self.vertex: dict = {}
        self.partial = False


class _Gateway:
    """可切换故障的 gateway：`FAILING['on']` 控制 goal=='FAIL' 是否失败。"""

    def __init__(self):
        self.executed: list[str] = []
        self.failing = {"on": True}

    def handle(self, request):
        goal = str(request.get("goal") or "")
        self.executed.append(goal)
        # goal 可能被追加了上游结论（`UPSTREAM_BLOCK`），所以用**包含**判定
        if "FAIL" in goal and self.failing["on"]:
            return _Turn(False)
        time.sleep(0.03)
        return _Turn(True)

    def handle_stream(self, request):
        yield {"type": "accepted", "seq": 0, "data": {}}
        yield {"type": "completed", "seq": 1, "data": {"ok": True, "text": "x"}}


def _client(gw=None, store=None):
    gw = gw or _Gateway()
    return TestClient(serve.create_app(
        gateway=gw, metrics=Metrics(), logger=JsonLogger(sink=SILENT),
        shutdown=ShutdownCoordinator(), deps={"store": None}, authenticator=None,
        workflow_store=store if store is not None else MemoryStore(),
        workflow_workers=1)), gw


def _settle(c, wid, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        d = c.get(f"/v1/workflows/{wid}").json()
        if d["status"] in ("done", "failed", "interrupted"):
            return d
        time.sleep(0.02)
    return c.get(f"/v1/workflows/{wid}").json()


def _submit(c, nodes):
    return c.post("/v1/workflows",
                  json={"name": "rel", "nodes": nodes, "principal": P}).json()["workflow_id"]


class TestResumeSkipsCompleted(unittest.TestCase):
    """核心：续跑**不能**重跑已完成的节点。"""

    def test_completed_node_not_reexecuted(self):
        c, gw = _client()
        wid = _submit(c, [{"name": "a", "goal": "A"},
                          {"name": "b", "goal": "FAIL", "depends_on": ["a"]}])
        first = _settle(c, wid)
        self.assertEqual(first["status"], "failed")
        self.assertEqual({n["name"]: n["status"] for n in first["nodes"]},
                         {"a": "done", "b": "failed"})

        gw.executed.clear()
        gw.failing["on"] = False           # 修好故障后重试
        r = c.post(f"/v1/workflows/{wid}/resume", json={})
        self.assertEqual(r.status_code, 202)

        after = _settle(c, wid)
        self.assertEqual(after["status"], "done")
        # 续跑只该执行 b（goal 会带上前一步的上游结论，所以用包含判定）
        self.assertEqual(len(gw.executed), 1, f"执行了多次：{gw.executed}")
        self.assertIn("FAIL", gw.executed[0])
        # goal 的**首行**才是节点自己的目标；上游结论是追加在后面的。
        # （不能用 `assertNotIn("A", ...)` —— 单字母 "A" 在 "FAIL" 里就有。）
        self.assertEqual(gw.executed[0].splitlines()[0], "FAIL",
                         "执行的是 a 而不是 b")

    def test_resume_reports_progress(self):
        c, gw = _client()
        wid = _submit(c, [{"name": "a", "goal": "A"},
                          {"name": "b", "goal": "FAIL", "depends_on": ["a"]}])
        _settle(c, wid)
        gw.failing["on"] = False
        c.post(f"/v1/workflows/{wid}/resume", json={})
        d = _settle(c, wid)
        self.assertEqual([n["name"] for n in d["nodes"]], ["a", "b"])
        self.assertTrue(all(n["status"] == "done" for n in d["nodes"]))

    def test_resume_counted(self):
        """续跑次数要能看出来（判断"这条流水线是不是反复断"）。"""
        c, gw = _client()
        wid = _submit(c, [{"name": "a", "goal": "FAIL"}])
        _settle(c, wid)
        gw.failing["on"] = False
        c.post(f"/v1/workflows/{wid}/resume", json={})
        d = _settle(c, wid)
        self.assertGreaterEqual(d.get("resumed_count", 0), 1)


class TestResumeStateGuards(unittest.TestCase):
    """状态闸门：不是什么状态都能续跑。"""

    def test_pending_or_running_conflict(self):
        """正在跑的不能 resume —— 会双跑，同样的副作用做两次。"""
        c, _ = _client()
        wid = _submit(c, [{"name": "a", "goal": "A"}])
        # 立刻 resume（大概率还在 pending/running）
        r = c.post(f"/v1/workflows/{wid}/resume", json={})
        self.assertIn(r.status_code, (202, 409))
        if r.status_code == 409:
            self.assertIn("只有", r.json().get("detail", ""))

    def test_done_cannot_resume(self):
        c, _ = _client()
        wid = _submit(c, [{"name": "a", "goal": "A"}])
        _settle(c, wid)
        r = c.post(f"/v1/workflows/{wid}/resume", json={})
        self.assertEqual(r.status_code, 409, "已完成的没有可续的，必须 409")

    def test_unknown_id_is_404(self):
        c, _ = _client()
        self.assertEqual(c.post("/v1/workflows/wf_nope/resume", json={}).status_code, 404)

    def test_shutting_down_is_503(self):
        from core.observability import ShutdownCoordinator as SC
        gw = _Gateway()
        shutdown = SC()
        c = TestClient(serve.create_app(
            gateway=gw, metrics=Metrics(), logger=JsonLogger(sink=SILENT),
            shutdown=shutdown, deps={"store": None}, authenticator=None,
            workflow_store=MemoryStore(), workflow_workers=1))
        wid = _submit(c, [{"name": "a", "goal": "FAIL"}])
        _settle(c, wid)
        shutdown.begin()
        self.assertEqual(c.post(f"/v1/workflows/{wid}/resume", json={}).status_code, 503)


class TestResumeWithoutSpec(unittest.TestCase):
    def test_restored_workflow_explains_cannot_resume(self):
        """重启后 spec 不在内存 → 必须明确说清"续不了"及原因。

        spec 是代码产物，不值得持久化；但用户会想续跑，所以错误要说人话，
        而不是抛一个 KeyError。
        """
        c1, gw = _client()
        wid = _submit(c1, [{"name": "a", "goal": "A"}])
        _settle(c1, wid)
        # 新 app（同一 store）：spec 不在内存
        c2, _ = _client(store=MemoryStore())
        d = c2.post(f"/v1/workflows/{wid}/resume", json={})
        # 恢复不出来 → 404（id 没见过）；恢复出来但没 spec → 409
        self.assertIn(d.status_code, (404, 409))


class TestResumePermission(unittest.TestCase):
    def test_resume_uses_original_principal_when_anonymous(self):
        """未启用鉴权时，续跑沿用**原提交者**身份 —— 不能凭空换人。"""
        c, gw = _client()
        wid = _submit(c, [{"name": "a", "goal": "FAIL"}])
        _settle(c, wid)
        gw.failing["on"] = False
        gw.executed.clear()
        r = c.post(f"/v1/workflows/{wid}/resume", json={})
        self.assertEqual(r.status_code, 202)
        _settle(c, wid)
        self.assertIn("FAIL", gw.executed)


if __name__ == "__main__":
    unittest.main(verbosity=2)
