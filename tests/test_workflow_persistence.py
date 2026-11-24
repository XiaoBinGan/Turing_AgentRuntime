"""Workflow 持久化验收。

**为什么必须持久化**：workflow 是"发布/回滚"这类长流程。进程重启丢状态 =
发布做了一半没人知道。内存状态只适合演示。

**为什么不自动续跑**：重启后原地重放 DAG 会**重复执行已经做完的节点**。
发布流程里"重复执行"可能是灾难（重复删库、重复发版）。
所以只做到"状态可查 + 明确告知在哪断的"，续跑交给人的决定。
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


class _Result:
    ok = True
    text = "ok"
    steps = 1
    phase = "DONE"
    term_reason = "DONE"
    error = ""
    audit_id = "a"
    task_id = "t"
    latency_ms = 1.0
    vertex: dict = {}
    partial = False


def _gateway(delay: float = 0.0):
    class G:
        def handle(self, request):
            if delay:
                time.sleep(delay)
            return _Result()

        def handle_stream(self, request):
            yield {"type": "accepted", "seq": 0, "data": {}}
            yield {"type": "completed", "seq": 1, "data": {"ok": True, "text": "x"}}
    return G()


def _client(store, *, delay=0.0, logs=None, **kw):
    return TestClient(serve.create_app(
        gateway=_gateway(delay), metrics=Metrics(),
        logger=JsonLogger(sink=logs.append if logs is not None else SILENT),
        shutdown=ShutdownCoordinator(), deps={"store": None},
        authenticator=None, workflow_store=store, workflow_workers=1, **kw))


def _spec(nodes=None):
    return {"name": "rel",
            "nodes": nodes or [{"name": "a", "goal": "x"}],
            "principal": P}


class TestPersistedOnSubmit(unittest.TestCase):
    def test_key_written_immediately(self):
        store = MemoryStore()
        c = _client(store, delay=0.5)
        c.post("/v1/workflows", json=_spec())
        # 立刻检查（不等它跑完）—— 提交即落盘
        keys = [k for k in (store.list_pending() or [])]
        self.assertTrue(any(str(k).startswith("workflow__") for k in keys),
                        f"提交后没落盘：{keys}")

    def test_key_format_is_store_compatible(self):
        """store 的 ctx_id 只允许 `[A-Za-z0-9_.-]` —— 冒号会让 save 直接抛。"""
        store = MemoryStore()
        c = _client(store, delay=0.3)
        c.post("/v1/workflows", json=_spec())
        for key in store.list_pending() or []:
            self.assertNotIn(":", str(key), "键里有冒号 → store 会拒绝")

    def test_payload_has_status_and_id(self):
        import json
        store = MemoryStore()
        c = _client(store, delay=0.5)
        wid = c.post("/v1/workflows", json=_spec()).json()["workflow_id"]
        raw = store.load(f"workflow__{wid}")
        self.assertIsNotNone(raw, "没找到落盘内容")
        payload = json.loads(raw)
        self.assertEqual(payload["id"], wid)
        self.assertIn(payload["status"], ("pending", "running"))


class TestRestoreAfterRestart(unittest.TestCase):
    def test_unfinished_becomes_interrupted(self):
        """重启后未完成 → `interrupted`（不是 failed：没跑错，是没跑完）。"""
        store = MemoryStore()
        c1 = _client(store, delay=3.0)
        wid = c1.post("/v1/workflows", json=_spec(
            [{"name": "a", "goal": "x"},
             {"name": "b", "goal": "y", "depends_on": ["a"]}])).json()["workflow_id"]
        time.sleep(0.25)                    # 让它停在 running

        c2 = _client(store)                 # 模拟重启（同一 store）
        r = c2.get(f"/v1/workflows/{wid}")
        self.assertEqual(r.status_code, 200, "重启后查不到 = 持久化没生效")
        d = r.json()
        self.assertEqual(d["status"], "interrupted")
        self.assertIn("重启", d.get("error", ""))
        self.assertEqual([n["name"] for n in d["nodes"]], ["a", "b"])

    def test_interrupted_says_no_auto_resume(self):
        """必须说清"不会自动续跑"及其理由 —— 否则运维以为它在自己恢复。"""
        store = MemoryStore()
        c1 = _client(store, delay=3.0)
        wid = c1.post("/v1/workflows", json=_spec()).json()["workflow_id"]
        time.sleep(0.25)
        d = _client(store).get(f"/v1/workflows/{wid}").json()
        self.assertIn("未自动续跑", d.get("error", ""))

    def test_finished_is_restored_as_finished(self):
        """**已完成的也要读回来** —— 那是审计轨迹。

        早先只恢复未完成的，结果"重启后查不到上周的发布记录"。
        持久化的意义不只是"接着跑"，还包括"事后能查"。
        """
        store = MemoryStore()
        c1 = _client(store, delay=0.0)
        wid = c1.post("/v1/workflows", json=_spec()).json()["workflow_id"]
        for _ in range(40):
            if c1.get(f"/v1/workflows/{wid}").json()["status"] in ("done", "failed"):
                break
            time.sleep(0.05)
        c2 = _client(store)
        d = c2.get(f"/v1/workflows/{wid}")
        self.assertEqual(d.status_code, 200, "已完成的 workflow 重启后必须还能查到")
        self.assertEqual(d.json()["status"], "done",
                         "已完成的不能被标成 interrupted（那是篡改历史）")

    def test_reported_in_info(self):
        store = MemoryStore()
        c1 = _client(store, delay=3.0)
        c1.post("/v1/workflows", json=_spec())
        time.sleep(0.25)
        logs = []
        c2 = _client(store, logs=logs)
        c2.get("/v1/info")
        self.assertTrue(any("restored" in str(e) for e in logs),
                        "恢复动作必须有日志（静默恢复 = 不知道它恢复过）")


class TestStoreFailureFailOpen(unittest.TestCase):
    def test_broken_store_does_not_break_workflow(self):
        """存储坏了不能让流水线跑不下去（观测面 fail-open），但要有痕迹。"""

        class Broken:
            def save(self, *a, **k):
                raise RuntimeError("disk full")

            def load(self, *a, **k):
                raise RuntimeError("disk full")

            def list_pending(self):
                raise RuntimeError("disk full")

        logs = []
        c = _client(Broken(), delay=0.0, logs=logs)
        r = c.post("/v1/workflows", json=_spec())
        self.assertEqual(r.status_code, 202, "存储坏了不该让提交失败")
        wid = r.json()["workflow_id"]
        for _ in range(40):
            if c.get(f"/v1/workflows/{wid}").json()["status"] in ("done", "failed"):
                break
            time.sleep(0.05)
        self.assertEqual(c.get(f"/v1/workflows/{wid}").json()["status"], "done")

    def test_store_without_persistence_methods_ok(self):
        """传一个不支持存取的 store 也不能崩（鸭子类型，缺方法就跳过）。"""
        class Minimal:
            pass
        c = _client(Minimal(), delay=0.0)
        self.assertEqual(c.post("/v1/workflows", json=_spec()).status_code, 202)


if __name__ == "__main__":
    unittest.main(verbosity=2)
