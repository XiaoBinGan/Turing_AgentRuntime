"""tests/test_workflow_api.py — Workflow 显式入口（B）的验收用例。

设计原则（与 `tests/test_serve.py` 一致，理由相同）：

1. **不启真实端口**：全部走 `fastapi.testclient.TestClient`（进程内 ASGI）。
2. **不连外部服务**：gateway / metrics / logger / shutdown / deps 全部注入假件。
3. **断言 HTTP 语义与可观测事实**，不断言实现细节：状态码、body 形状、状态迁移、
   容量淘汰的结果。实现重构（换 runner、换存储）不该让这些用例变红。

为什么这些用例值得单独一个文件：`POST /v1/workflows` 与 `/v1/turns` 的**生命周期
完全不同** —— turns 是请求级（客户端等结果），workflows 是进程级（提交即返回，
执行在后台线程池里）。两者的正确性判据没有交集，混在一个文件里会互相稀释。

覆盖清单（对应任务书的 22 条）：

    ① 提交返回 202 + workflow_id           ② 提交后能查到
    ③ pending/running → done（轮询）       ④ 成环 → 400
    ⑤ 未知依赖 → 400                       ⑥ 空节点 → 400（见下方理由）
    ⑦ 不存在的 id → 404                    ⑧ 列表
    ⑨ 分页                                 ⑩ 倒序
    ⑪ 未启用鉴权 + 缺 principal → 422      ⑫ 启用鉴权 + 凭据身份 → 202
    ⑬ 容量上限淘汰最老已结束                ⑭ 不淘汰 running
    ⑮ /v1/info 含 workflow 段              ⑯ 节点失败 → workflow failed
    ⑰ 节点失败不牵连同层其他节点            ⑱ 关停中 → 503
    ⑲ 多层 DAG 的层信息                    ⑳ 并发提交各自独立
    ㉑ id 格式合法且唯一                    ㉒ 淘汰后旧 id → 404

关于 ⑥ 的选择（任务书允许 400 或 202 二选一）：**选 400**。`plane/dag.py` 的
`DAG.validate()` 认为空图合法（没有节点就没有错误），但接受一个什么都不做的
workflow 会白占一个追踪位（容量是有界的），并在审计里留下一条"成功"的假记录。
拒绝比"接受一个 no-op"更诚实。
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
import unittest
from unittest import mock

from fastapi.testclient import TestClient

import serve
from auth import Authenticator
from contracts import Principal
from core.gateway import TurnResult
from core.observability import JsonLogger, Metrics, ShutdownCoordinator

#: 一份合法的 principal（scope 必须含 runtime:run，否则 Gateway 判 forbidden）
PRINCIPAL = {"user_id": "u_alben", "tenant": "acme", "roles": ["sre"],
             "scopes": ["runtime:run"]}

#: 绝对静音的日志 sink：测试失败时不该被一堆 JSON 行淹没
SILENT = lambda _line: None  # noqa: E731

#: workflow_id 的合法形状：`wf_` + 12 位十六进制
ID_PATTERN = re.compile(r"^wf_[0-9a-f]{12}$")


# ══════════════════════════════════════════════════════════════
# 一、假件
# ══════════════════════════════════════════════════════════════
class GoodDep:
    """健康依赖：info() 自报 available=True。"""

    def info(self):
        return {"kind": "fake", "available": True, "reason": "ok"}


class FakeGateway:
    """最小 Gateway 替身：按 goal 决定成功/失败，可选"卡住"来造 running 现场。

    **必须线程安全**：workflow 的节点在后台线程池里跑（层的并行会同时进多个
    worker），所以记录与计数都要上锁 —— 否则并发用例自己就在测试一个竞态。

    三个开关都是"goal 里出现某段子串"就生效：
        `fail_goals`   → 返回 `ok=False`（模拟节点失败，永不抛异常 —— 那是 Gateway 契约）
        `block_goals`  → 阻塞到 `release()`（造"正在跑"的确定性现场）
        `delay`        → 每个请求睡的秒数（造慢节点）
    """

    def __init__(self, *, delay: float = 0.0, fail_goals=(),
                 block_goals=()) -> None:
        self.delay = float(delay)
        self.fail_goals = tuple(fail_goals)
        self.block_goals = tuple(block_goals)
        self._lock = threading.Lock()
        self.requests: list[dict] = []
        self._release = threading.Event()
        self.blocked = 0

    # ── 控制面 ──────────────────────────────────────────────
    def release(self) -> None:
        self._release.set()

    @property
    def goals(self) -> list[str]:
        with self._lock:
            return [str(req.get("goal") or "") for req in self.requests]

    def matched(self, token: str) -> int:
        with self._lock:
            return sum(1 for req in self.requests if token in str(req.get("goal") or ""))

    # ── Gateway 契约 ────────────────────────────────────────
    def handle(self, request):
        goal = str(request.get("goal") or "")
        with self._lock:
            self.requests.append(request)
        if any(token in goal for token in self.block_goals):
            with self._lock:
                self.blocked += 1
            self._release.wait(timeout=10.0)     # 超时兜底：不让一个失控用例挂住整套
        if self.delay:
            time.sleep(self.delay)
        if any(token in goal for token in self.fail_goals):
            return TurnResult(ok=False, text="", phase="FAILED",
                              term_reason="UNRECOVERABLE",
                              error="boom: 下游 500", steps=1,
                              task_id="task_fake")
        return TurnResult(ok=True, text="ok:" + goal, phase="DONE",
                          term_reason="DONE", steps=2, task_id="task_fake")


# ══════════════════════════════════════════════════════════════
# 二、测试脚手架（自己写一份：`tests/test_serve.py` 的 make_app 不许改）
# ══════════════════════════════════════════════════════════════
def make_app(gateway=None, *, workflow_max=None, authenticator=None,
             shutdown=None) -> object:
    """造 app：默认注入静音日志、真实 Metrics、真实 ShutdownCoordinator、假依赖。"""
    return serve.create_app(
        gateway=gateway if gateway is not None else FakeGateway(),
        metrics=Metrics(),
        logger=JsonLogger(sink=SILENT),
        shutdown=shutdown if shutdown is not None else ShutdownCoordinator(),
        deps={"store": GoodDep()},
        workflow_max=workflow_max,
        authenticator=authenticator,
    )


def node(name: str, *, depends_on=(), goal: str | None = None) -> dict:
    out: dict = {"name": name}
    if depends_on:
        out["depends_on"] = list(depends_on)
    if goal is not None:
        out["goal"] = goal
    return out


def dag(*nodes: dict, name: str = "release") -> dict:
    return {"name": name, "nodes": list(nodes)}


def submit(client: TestClient, spec: dict, principal=None, **extra):
    body = dict(spec)
    body["principal"] = dict(principal) if principal is not None else dict(PRINCIPAL)
    body.update(extra)
    return client.post("/v1/workflows", json=body)


def wait_settled(client: TestClient, workflow_id: str, *, timeout: float = 10.0,
                 headers: dict | None = None) -> dict:
    """轮询到 workflow 落定（done/failed）。**带超时上限**：测试不许无限等。"""
    deadline = time.time() + timeout
    snapshot: dict | None = None
    while time.time() < deadline:
        response = client.get(f"/v1/workflows/{workflow_id}", headers=headers)
        assert response.status_code == 200, response.text
        snapshot = response.json()
        if snapshot["status"] in ("done", "failed"):
            return snapshot
        time.sleep(0.02)
    raise AssertionError(
        f"workflow {workflow_id} 在 {timeout}s 内没有落定，最后状态: {snapshot}")


def wait_node_running(client: TestClient, workflow_id: str, *,
                      timeout: float = 5.0, headers: dict | None = None) -> dict:
    """等到至少一个节点真的在跑（证明执行发生在**后台**，不是同步跑完才返回）。"""
    deadline = time.time() + timeout
    snapshot: dict | None = None
    while time.time() < deadline:
        snapshot = client.get(f"/v1/workflows/{workflow_id}", headers=headers).json()
        if any(item["status"] == "running" for item in snapshot["nodes"]):
            return snapshot
        time.sleep(0.01)
    raise AssertionError(f"workflow {workflow_id} 没有进入 running，最后: {snapshot}")


def nodes_by_name(snapshot: dict) -> dict[str, dict]:
    return {item["name"]: item for item in snapshot["nodes"]}


# ══════════════════════════════════════════════════════════════
# 三、提交与查询（① ② ㉑）
# ══════════════════════════════════════════════════════════════
class TestSubmitAndQuery(unittest.TestCase):
    def setUp(self) -> None:
        self.gateway = FakeGateway()
        self.app = make_app(self.gateway)
        self.client = TestClient(self.app)

    # ①
    def test_submit_returns_202_with_workflow_id(self):
        response = submit(self.client, dag(node("build")))
        self.assertEqual(response.status_code, 202, response.text)
        body = response.json()
        self.assertEqual(body["status"], "accepted")
        self.assertRegex(body["workflow_id"], ID_PATTERN)
        self.assertEqual(body["spec_name"], "release")
        self.assertEqual(body["layers"], [["build"]])
        self.assertEqual(body["node_count"], 1)
        # Location 是 202 的标准配套：客户端不必自己拼 URL
        self.assertEqual(body["links"]["self"], f"/v1/workflows/{body['workflow_id']}")
        self.assertEqual(response.headers.get("location"),
                         f"/v1/workflows/{body['workflow_id']}")

    # ②
    def test_submitted_workflow_is_queryable(self):
        workflow_id = submit(self.client, dag(node("a"), node("b", depends_on=("a",)))).json()["workflow_id"]
        snapshot = wait_settled(self.client, workflow_id)
        self.assertEqual(snapshot["id"], workflow_id)
        self.assertEqual(snapshot["spec_name"], "release")
        self.assertEqual(snapshot["layers"], [["a"], ["b"]])
        self.assertEqual([item["name"] for item in snapshot["nodes"]], ["a", "b"])
        self.assertEqual(snapshot["progress"]["total"], 2)
        self.assertGreater(snapshot["finished_at"], 0.0)

    # ㉑
    def test_workflow_id_format_and_uniqueness(self):
        ids = []
        for index in range(5):
            response = submit(self.client, dag(node(f"n{index}"), name=f"wf{index}"))
            self.assertEqual(response.status_code, 202, response.text)
            ids.append(response.json()["workflow_id"])
        self.assertEqual(len(set(ids)), len(ids), f"id 重复了：{ids}")
        for workflow_id in ids:
            self.assertRegex(workflow_id, ID_PATTERN)
            self.assertEqual(self.client.get(f"/v1/workflows/{workflow_id}").status_code, 200)

    def test_node_goal_falls_back_to_workflow_and_node_name(self):
        """没写 goal 的节点用 `{workflow}:{node}`（`DAGRunner` 的既有语义，确认透传）。"""
        workflow_id = submit(self.client, dag(node("build"), name="deploy")).json()["workflow_id"]
        wait_settled(self.client, workflow_id)
        self.assertEqual(self.gateway.goals, ["deploy:build"])

    def test_identity_is_propagated_to_each_node(self):
        """节点跑的是**提交者的**回合：principal 必须一路带到 gateway.handle。"""
        workflow_id = submit(self.client, dag(node("a"))).json()["workflow_id"]
        wait_settled(self.client, workflow_id)
        request = self.gateway.requests[0]
        self.assertEqual(request["principal"]["user_id"], "u_alben")
        self.assertEqual(request["principal"]["tenant"], "acme")

    def test_submit_does_not_judge_rbac(self):
        """提交层**不**判 scope/RBAC（那是 Gateway 的事，硬纪律 1）。

        没有 `runtime:run` 的 principal 依然能提交：本层的失败面是"登记/执行"，
        不是"准入"。真要拒绝，位置在 Gateway（`forbidden`），那时节点会 `failed`
        并**如实记账** —— 而不是在 HTTP 层另开一套口径。
        """
        response = submit(self.client, dag(node("a")),
                          principal={"user_id": "u1", "tenant": "acme"})
        self.assertEqual(response.status_code, 202, response.text)

    def test_unknown_body_field_is_422(self):
        """拼错字段必须 422（`extra="forbid"`）：静默忽略会造出一个"顺序不对"的 DAG。"""
        response = submit(self.client, {"name": "x", "nodes": [node("a")],
                                        "nodez": []})
        self.assertEqual(response.status_code, 422, response.text)


# ══════════════════════════════════════════════════════════════
# 四、拓扑校验（④ ⑤ ⑥）
# ══════════════════════════════════════════════════════════════
class TestValidation(unittest.TestCase):
    def setUp(self) -> None:
        self.app = make_app(FakeGateway())
        self.client = TestClient(self.app)

    # ④
    def test_cycle_is_400_and_names_the_nodes(self):
        response = submit(self.client, dag(node("a", depends_on=("c",)),
                                           node("b", depends_on=("a",)),
                                           node("c", depends_on=("b",))))
        self.assertEqual(response.status_code, 400, response.text)
        body = response.json()
        self.assertEqual(body["error"], "invalid_dag")
        self.assertIn("成环", body["detail"])
        # 必须点名**具体是哪几个节点**成环（不只是"有环"）
        for name in ("a", "b", "c"):
            self.assertIn(name, body["detail"])
        self.assertTrue(any("成环" in item for item in body["errors"]))

    def test_cycle_never_reaches_the_gateway(self):
        """静态失败**一个节点都不派发**（`DAGRunner` 的既有序语义，确认透传）。"""
        gateway = FakeGateway()
        client = TestClient(make_app(gateway))
        submit(client, dag(node("a", depends_on=("b",)), node("b", depends_on=("a",))))
        self.assertEqual(gateway.requests, [])

    # ⑤
    def test_unknown_dependency_is_400(self):
        response = submit(self.client, dag(node("a"), node("b", depends_on=("ghost",))))
        self.assertEqual(response.status_code, 400, response.text)
        body = response.json()
        self.assertIn("依赖不存在的节点", body["detail"])
        self.assertIn("ghost", body["detail"])

    def test_self_dependency_is_400(self):
        response = submit(self.client, dag(node("a", depends_on=("a",))))
        self.assertEqual(response.status_code, 400, response.text)
        self.assertIn("自依赖", response.json()["detail"])

    def test_duplicate_node_names_are_400(self):
        response = submit(self.client, dag(node("a"), node("a")))
        self.assertEqual(response.status_code, 400, response.text)
        self.assertIn("重名", response.json()["detail"])

    # ⑥
    def test_empty_nodes_is_400(self):
        """空 DAG → 400（选 400 的理由见模块 docstring：no-op workflow 只会污染容量与审计）。"""
        response = submit(self.client, {"name": "empty", "nodes": [],
                                        "principal": dict(PRINCIPAL)})
        self.assertEqual(response.status_code, 400, response.text)
        body = response.json()
        self.assertEqual(body["error"], "invalid_dag")
        self.assertIn("nodes 不能为空", body["detail"])

    def test_missing_name_is_422(self):
        response = self.client.post("/v1/workflows",
                                    json={"nodes": [node("a")],
                                          "principal": dict(PRINCIPAL)})
        self.assertEqual(response.status_code, 422, response.text)
        self.assertIn("name", json.dumps(response.json(), ensure_ascii=False))


# ══════════════════════════════════════════════════════════════
# 五、异步与状态迁移（③ ⑲ ⑳）
# ══════════════════════════════════════════════════════════════
class TestAsyncExecution(unittest.TestCase):
    def test_status_goes_pending_running_done(self):
        """③ 提交**立即**返回（此时 DAG 还没跑完），随后才迁移到 done。"""
        gateway = FakeGateway(block_goals=("slow",))
        client = TestClient(make_app(gateway))
        try:
            response = submit(client, dag(node("a", goal="slow node")))
            self.assertEqual(response.status_code, 202)
            workflow_id = response.json()["workflow_id"]
            # 节点卡在 gateway 里 → 提交后的状态**不可能**是 done：证明它是异步的
            immediate = client.get(f"/v1/workflows/{workflow_id}").json()
            self.assertIn(immediate["status"], ("pending", "running"), immediate)
            # 真的进到 running（而不是"提交后一直 pending 直到跑完"）
            running = wait_node_running(client, workflow_id)
            self.assertEqual(running["nodes"][0]["status"], "running")
            self.assertGreater(running["nodes"][0]["started_at"], 0.0)
            self.assertEqual(running["nodes"][0]["ended_at"], 0.0)
        finally:
            gateway.release()
        settled = wait_settled(client, workflow_id)
        self.assertEqual(settled["status"], "done")
        self.assertEqual(settled["nodes"][0]["status"], "done")
        self.assertEqual(settled["nodes"][0]["steps"], 2)
        self.assertEqual(settled["nodes"][0]["error"], "")
        self.assertGreater(settled["nodes"][0]["ended_at"],
                           settled["nodes"][0]["started_at"])
        self.assertGreater(settled["finished_at"], 0.0)

    # ⑲
    def test_multi_layer_dag_exposes_layers(self):
        gateway = FakeGateway()
        client = TestClient(make_app(gateway))
        workflow_id = submit(
            client,
            dag(node("build"), node("lint"), node("test", depends_on=("build",)),
                node("ship", depends_on=("test", "lint")))).json()["workflow_id"]
        snapshot = wait_settled(client, workflow_id)
        self.assertEqual(snapshot["status"], "done")
        # 层号 = 到最长依赖链的长度；同层按名字排序（`plane/dag.py` 的确定性基准）
        self.assertEqual(snapshot["layers"],
                         [["build", "lint"], ["test"], ["ship"]])
        self.assertEqual(snapshot["progress"],
                         {"pending": 0, "running": 0, "done": 4, "failed": 0,
                          "skipped": 0, "total": 4, "finished": 4})

    def test_layers_run_in_order(self):
        """层是屏障：第 k 层全部落定，第 k+1 层才开始（顺序即语义）。"""
        gateway = FakeGateway()
        client = TestClient(make_app(gateway))
        workflow_id = submit(client, dag(node("a", goal="first"),
                                         node("b", goal="second", depends_on=("a",)))).json()["workflow_id"]
        wait_settled(client, workflow_id)
        # goal 现在会**追加**上游结论（`UPSTREAM_BLOCK`），所以不可能是精确相等。
        # 断言"以什么开头 + 顺序正确"才是这条用例真正要守的东西。
        self.assertEqual(len(gateway.goals), 2)
        self.assertTrue(gateway.goals[0].startswith("first"))
        self.assertTrue(gateway.goals[1].startswith("second"))
        self.assertIn("上游节点结论", gateway.goals[1],
                      "下游节点应当拿到上游结论")

    # ⑳
    def test_two_concurrent_workflows_are_independent(self):
        gateway = FakeGateway(block_goals=("slow-a", "slow-b"))
        client = TestClient(make_app(gateway))
        try:
            first = submit(client, dag(node("a", goal="slow-a"), name="wa"))
            second = submit(client, dag(node("b", goal="slow-b"), name="wb"))
            self.assertEqual((first.status_code, second.status_code), (202, 202))
            id_a = first.json()["workflow_id"]
            id_b = second.json()["workflow_id"]
            self.assertNotEqual(id_a, id_b)
            # 两条都在跑（互不阻塞：线程池里各占一个 worker）
            deadline = time.time() + 5.0
            while time.time() < deadline:
                if gateway.blocked >= 2:
                    break
                time.sleep(0.01)
            self.assertEqual(gateway.blocked, 2, "两条 workflow 没有同时在跑")
            state_a = client.get(f"/v1/workflows/{id_a}").json()
            state_b = client.get(f"/v1/workflows/{id_b}").json()
            self.assertEqual(state_a["spec_name"], "wa")
            self.assertEqual(state_b["spec_name"], "wb")
            # 各自只含自己的节点（状态不串门）
            self.assertEqual([item["name"] for item in state_a["nodes"]], ["a"])
            self.assertEqual([item["name"] for item in state_b["nodes"]], ["b"])
        finally:
            gateway.release()
        self.assertEqual(wait_settled(client, id_a)["status"], "done")
        self.assertEqual(wait_settled(client, id_b)["status"], "done")


# ══════════════════════════════════════════════════════════════
# 六、失败语义（⑯ ⑰）
# ══════════════════════════════════════════════════════════════
class TestFailureSemantics(unittest.TestCase):
    # ⑯
    def test_node_failure_marks_workflow_failed_with_error(self):
        gateway = FakeGateway(fail_goals=("boom",))
        client = TestClient(make_app(gateway))
        workflow_id = submit(client, dag(node("a", goal="boom now"))).json()["workflow_id"]
        snapshot = wait_settled(client, workflow_id)
        self.assertEqual(snapshot["status"], "failed")
        self.assertIn("节点失败", snapshot["error"])
        self.assertIn("a", snapshot["error"])
        item = nodes_by_name(snapshot)["a"]
        self.assertEqual(item["status"], "failed")
        self.assertIn("boom", item["error"])
        self.assertGreater(item["ended_at"], 0.0)

    def test_downstream_node_is_skipped_not_failed(self):
        """短路（skipped）与失败（failed）必须分开记：前者没花钱，后者花了。"""
        gateway = FakeGateway(fail_goals=("boom",))
        client = TestClient(make_app(gateway))
        workflow_id = submit(client, dag(node("a", goal="boom"),
                                         node("b", depends_on=("a",)))).json()["workflow_id"]
        snapshot = wait_settled(client, workflow_id)
        self.assertEqual(snapshot["status"], "failed")
        by_name = nodes_by_name(snapshot)
        self.assertEqual(by_name["a"]["status"], "failed")
        self.assertEqual(by_name["b"]["status"], "skipped")
        self.assertIn("短路", by_name["b"]["error"])
        self.assertIn("被短路跳过", snapshot["error"])
        self.assertEqual(snapshot["progress"]["skipped"], 1)
        # 被短路的节点**一次都没派发**（不是"派发了再判定"）
        self.assertEqual(gateway.matched("release:b"), 0)

    # ⑰
    def test_node_failure_does_not_affect_same_layer(self):
        """同层无依赖关系的节点互不牵连（`DAGRunner` 的语义，确认透传）。

        用 `max_parallel=2` 让它们真的并行：串行下"不影响"很容易被顺序掩盖，
        并行下才是真正的隔离考验。
        """
        gateway = FakeGateway(fail_goals=("boom",))
        client = TestClient(make_app(gateway))
        response = submit(client, dag(node("bad", goal="boom"), node("good", goal="fine")),
                          max_parallel=2)
        self.assertEqual(response.status_code, 202, response.text)
        snapshot = wait_settled(client, response.json()["workflow_id"])
        by_name = nodes_by_name(snapshot)
        self.assertEqual(by_name["bad"]["status"], "failed")
        self.assertEqual(by_name["good"]["status"], "done")
        self.assertEqual(by_name["good"]["steps"], 2)
        self.assertEqual(snapshot["status"], "failed")
        self.assertIn("bad", snapshot["error"])
        self.assertNotIn("good", snapshot["error"])
        # 两个节点都真的被派发过（失败的那个没有把同层的兄弟挤掉）
        self.assertEqual(sorted(gateway.goals), ["boom", "fine"])

    def test_gateway_exception_is_isolated_to_one_node(self):
        """`gateway.handle` 抛异常（基础设施故障）→ 该节点 failed，workflow 不炸在 500。"""

        class ExplodingGateway(FakeGateway):
            def handle(self, request):
                goal = str(request.get("goal") or "")
                if "explode" in goal:
                    raise RuntimeError("连接模型服务超时")
                return super().handle(request)

        client = TestClient(make_app(ExplodingGateway()))
        workflow_id = submit(client, dag(node("a", goal="explode"),
                                         node("b", goal="fine", depends_on=("a",)))).json()["workflow_id"]
        snapshot = wait_settled(client, workflow_id)
        self.assertEqual(snapshot["status"], "failed")
        by_name = nodes_by_name(snapshot)
        self.assertEqual(by_name["a"]["status"], "failed")
        # `instrument_gateway` 会把 gateway 的异常收敛成 `internal_error: ...` 的结果
        # （观测层兜底），所以这里断言"错误被如实记下"而不是某一句固定文案。
        self.assertIn("连接模型服务超时", by_name["a"]["error"])
        self.assertEqual(by_name["b"]["status"], "skipped")


# ══════════════════════════════════════════════════════════════
# 七、404 与列表（⑦ ⑧ ⑨ ⑩）
# ══════════════════════════════════════════════════════════════
class TestListAndNotFound(unittest.TestCase):
    def setUp(self) -> None:
        self.app = make_app(FakeGateway())
        self.client = TestClient(self.app)

    # ⑦
    def test_unknown_id_is_404(self):
        response = self.client.get("/v1/workflows/wf_000000000000")
        self.assertEqual(response.status_code, 404, response.text)
        body = response.json()
        self.assertEqual(body["error"], "workflow_not_found")
        self.assertEqual(body["status"], 404)

    # ⑧
    def test_list_returns_workflows(self):
        for index in range(3):
            submit(self.client, dag(node("a"), name=f"w{index}"))
        body = self.client.get("/v1/workflows").json()
        self.assertEqual(body["total"], 3)
        self.assertEqual(len(body["workflows"]), 3)
        self.assertEqual(body["limit"], 20)
        self.assertEqual(body["offset"], 0)
        self.assertEqual(body["max_tracked"], 200)
        for item in body["workflows"]:
            self.assertIn("layers", item)
            self.assertIn("nodes", item)
            self.assertIn("progress", item)

    # ⑨
    def test_list_pagination(self):
        ids = [submit(self.client, dag(node("a"), name=f"w{index}")).json()["workflow_id"]
               for index in range(5)]
        first_page = self.client.get("/v1/workflows?limit=2&offset=0").json()
        second_page = self.client.get("/v1/workflows?limit=2&offset=2").json()
        self.assertEqual(first_page["total"], 5)
        self.assertEqual(len(first_page["workflows"]), 2)
        self.assertEqual(len(second_page["workflows"]), 2)
        self.assertEqual([item["id"] for item in first_page["workflows"]], ids[::-1][:2])
        self.assertEqual([item["id"] for item in second_page["workflows"]], ids[::-1][2:4])
        # 越界窗口是空列表，不是 500
        tail = self.client.get("/v1/workflows?limit=2&offset=4").json()
        self.assertEqual(len(tail["workflows"]), 1)
        beyond = self.client.get("/v1/workflows?limit=2&offset=99").json()
        self.assertEqual(beyond["workflows"], [])

    def test_list_rejects_bad_pagination(self):
        self.assertEqual(self.client.get("/v1/workflows?limit=0").status_code, 422)
        self.assertEqual(self.client.get("/v1/workflows?offset=-1").status_code, 422)
        self.assertEqual(self.client.get("/v1/workflows?limit=1000").status_code, 422)

    # ⑩
    def test_list_is_newest_first(self):
        for index in range(4):
            submit(self.client, dag(node("a"), name=f"w{index}"))
            wait_settled(self.client, self.client.get("/v1/workflows").json()["workflows"][0]["id"])
        items = self.client.get("/v1/workflows").json()["workflows"]
        stamps = [item["created_at"] for item in items]
        self.assertEqual(stamps, sorted(stamps, reverse=True),
                         f"列表没有按 created_at 倒序：{stamps}")
        self.assertEqual([item["spec_name"] for item in items],
                         ["w3", "w2", "w1", "w0"])


# ══════════════════════════════════════════════════════════════
# 八、容量有界（⑬ ⑭ ㉒）
# ══════════════════════════════════════════════════════════════
class TestCapacity(unittest.TestCase):
    # ⑬ ㉒
    def test_oldest_finished_is_evicted(self):
        client = TestClient(make_app(FakeGateway(), workflow_max=2))
        ids = []
        for index in range(3):
            ids.append(submit(client, dag(node("a"), name=f"w{index}")).json()["workflow_id"])
            wait_settled(client, ids[-1])
        # 最老的那条被淘汰：查询 → 404（㉒）
        self.assertEqual(client.get(f"/v1/workflows/{ids[0]}").status_code, 404)
        self.assertEqual(client.get(f"/v1/workflows/{ids[1]}").status_code, 200)
        self.assertEqual(client.get(f"/v1/workflows/{ids[2]}").status_code, 200)
        listing = client.get("/v1/workflows").json()
        self.assertEqual(listing["total"], 2, "内存里的条数没有被界住")
        self.assertLessEqual(listing["total"], listing["max_tracked"])

    def test_capacity_counter_is_visible(self):
        client = TestClient(make_app(FakeGateway(), workflow_max=1))
        first = submit(client, dag(node("a"))).json()["workflow_id"]
        wait_settled(client, first)
        submit(client, dag(node("a")))
        wait_settled(client, client.get("/v1/workflows").json()["workflows"][0]["id"])
        info = client.get("/v1/info").json()["workflow"]
        self.assertEqual(info["max_tracked"], 1)
        self.assertEqual(info["tracked"], 1)
        self.assertEqual(info["evicted"], 1)
        self.assertEqual(info["rejected"], 0)

    # ⑭
    def test_env_var_sets_capacity(self):
        """`AGENT_RUNTIME_WORKFLOW_MAX` 生效，并在 `/v1/info` 自述（启动快照口径）。"""
        with mock.patch.dict(os.environ, {"AGENT_RUNTIME_WORKFLOW_MAX": "3"},
                             clear=False):
            client = TestClient(make_app())
        self.assertEqual(client.get("/v1/info").json()["workflow"]["max_tracked"], 3)

    def test_running_workflow_is_never_evicted(self):
        """容量满了但全在跑 → **拒绝新提交（429）**，绝不 kill 在跑的。

        "运行中"用阻塞的 gateway 造：完成时间由测试控制，结论才确定。
        """
        gateway = FakeGateway(block_goals=("slow",))
        client = TestClient(make_app(gateway, workflow_max=1))
        try:
            response = submit(client, dag(node("a", goal="slow")))
            self.assertEqual(response.status_code, 202, response.text)
            running_id = response.json()["workflow_id"]
            wait_node_running(client, running_id)
            # 再来一条：容量满且唯一的一条在跑 → 429 + Retry-After
            rejected = submit(client, dag(node("a"), name="second"))
            self.assertEqual(rejected.status_code, 429, rejected.text)
            body = rejected.json()
            self.assertEqual(body["error"], "workflow_capacity")
            self.assertIn("429", str(body["status"]))
            self.assertIn("Retry-After", rejected.headers)
            # 在跑的那条**还在**（既没被淘汰，也没被打断）
            still = client.get(f"/v1/workflows/{running_id}")
            self.assertEqual(still.status_code, 200, still.text)
            self.assertIn(still.json()["status"], ("running", "pending"))
        finally:
            gateway.release()
        # 跑完之后腾出位置：新提交被接受（并淘汰这条已结束的）
        self.assertEqual(wait_settled(client, running_id)["status"], "done")
        after = submit(client, dag(node("a"), name="third"))
        self.assertEqual(after.status_code, 202, after.text)
        self.assertEqual(client.get(f"/v1/workflows/{running_id}").status_code, 404)


# ══════════════════════════════════════════════════════════════
# 九、身份（⑪ ⑫）
# ══════════════════════════════════════════════════════════════
class TestIdentity(unittest.TestCase):
    # ⑪
    def test_missing_principal_without_auth_is_422(self):
        client = TestClient(make_app())
        response = client.post("/v1/workflows", json={"name": "x",
                                                      "nodes": [node("a")]})
        self.assertEqual(response.status_code, 422, response.text)
        body = response.json()
        self.assertEqual(body["error"], "validation_error")
        self.assertIn("principal.user_id", [item["field"] for item in body["fields"]])

    def test_missing_tenant_without_auth_is_422(self):
        client = TestClient(make_app())
        response = client.post("/v1/workflows",
                               json={"name": "x", "nodes": [node("a")],
                                     "principal": {"user_id": "u1",
                                                   "scopes": ["runtime:run"]}})
        self.assertEqual(response.status_code, 422, response.text)
        self.assertIn("principal.tenant",
                      [item["field"] for item in response.json()["fields"]])

    # ⑫
    @staticmethod
    def _auth() -> Authenticator:
        return Authenticator(api_keys={"sk-wf": Principal(
            user_id="u1", tenant="acme", roles=("sre",),
            scopes=("runtime:run", "tool.read"))})

    def test_credential_identity_is_enough(self):
        """启用鉴权：body **不写身份**也能提交，执行时用凭据身份。"""
        gateway = FakeGateway()
        client = TestClient(make_app(gateway, authenticator=self._auth()))
        response = client.post("/v1/workflows", headers={"X-API-Key": "sk-wf"},
                               json={"name": "x", "nodes": [node("a")]})
        self.assertEqual(response.status_code, 202, response.text)
        workflow_id = response.json()["workflow_id"]
        snapshot = wait_settled(client, workflow_id,
                               headers={"X-API-Key": "sk-wf"})
        self.assertEqual(snapshot["status"], "done")
        self.assertEqual(snapshot["principal"]["user_id"], "u1")
        self.assertEqual(snapshot["principal"]["tenant"], "acme")
        self.assertEqual(gateway.requests[0]["principal"]["user_id"], "u1")

    def test_credential_mismatch_is_403(self):
        client = TestClient(make_app(authenticator=self._auth()))
        response = client.post(
            "/v1/workflows", headers={"X-API-Key": "sk-wf"},
            json={"name": "x", "nodes": [node("a")],
                  "principal": {"user_id": "u1", "tenant": "evilcorp"}})
        self.assertEqual(response.status_code, 403, response.text)
        self.assertIn("principal_conflict", response.json()["detail"])

    def test_missing_credential_is_401(self):
        client = TestClient(make_app(authenticator=self._auth()))
        response = client.post("/v1/workflows", json={"name": "x",
                                                      "nodes": [node("a")]})
        self.assertEqual(response.status_code, 401, response.text)

    def test_read_endpoints_need_credentials_too(self):
        client = TestClient(make_app(authenticator=self._auth()))
        self.assertEqual(client.get("/v1/workflows").status_code, 401)


# ══════════════════════════════════════════════════════════════
# 十、runner 注入口（② 的实现选择：内置最小 runner 是**默认**，不是唯一）
# ══════════════════════════════════════════════════════════════
class FakeSubagentResult:
    """鸭子类型的 `SubagentResult`：`DAGRunner._payload` 只读这几个字段。

    这里刻意**不用**真的 `contracts.SubagentResult`：要证明的是"runner 只要长得像"
    就能接进来（`DAGRunner` 的鸭子类型契约），不是"必须是我们那个类"。
    """

    def __init__(self, name, ok=True, steps=1, error="", summary="") -> None:
        self.name = name
        self.ok = ok
        self.steps = steps
        self.error = error
        self.summary = summary
        self.artifacts = ()
        self.term_reason = "DONE" if ok else "UNRECOVERABLE"

    def to_dict(self) -> dict:
        return {"name": self.name, "ok": self.ok, "summary": self.summary,
                "artifacts": [], "steps": self.steps,
                "term_reason": self.term_reason, "error": self.error}


class RecordingRunner:
    def __init__(self, *, ok=True, boom=False) -> None:
        self.ok = ok
        self.boom = boom
        self.specs = []
        self.contexts = []

    def dispatch(self, spec, parent_ctx, parent_e):
        self.specs.append(spec)
        self.contexts.append((parent_ctx, parent_e))
        if self.boom:
            raise RuntimeError("runner 炸了")
        return FakeSubagentResult(spec.name, ok=self.ok, steps=3,
                                  error="" if self.ok else "runner 判失败")


class TestRunnerInjection(unittest.TestCase):
    def test_injected_runner_factory_is_used(self):
        """注入了 runner_factory → 节点**不**再走 gateway.handle（内置 runner 被替换）。"""
        gateway = FakeGateway()
        runner = RecordingRunner()
        app = serve.create_app(
            gateway=gateway, metrics=Metrics(), logger=JsonLogger(sink=SILENT),
            shutdown=ShutdownCoordinator(), deps={"store": GoodDep()},
            workflow_runner_factory=lambda principal: runner)
        client = TestClient(app)
        workflow_id = submit(client, dag(node("a", goal="按 SOP 发布"))).json()["workflow_id"]
        snapshot = wait_settled(client, workflow_id)
        self.assertEqual(snapshot["status"], "done")
        self.assertEqual([spec.name for spec in runner.specs], ["a"])
        self.assertEqual(runner.specs[0].goal, "按 SOP 发布")
        # allowed_tools=() ：DAGRunner 给每个节点的 spec 都是只读语义
        self.assertEqual(tuple(runner.specs[0].allowed_tools), ())
        self.assertEqual(gateway.requests, [])
        # 节点步骤数取自 runner 的返回值（不是内置 runner 的假数据）
        self.assertEqual(nodes_by_name(snapshot)["a"]["steps"], 3)

    def test_injected_runner_receives_parent_exec_ctx(self):
        """`parent_e` 是这条 workflow 自己的 ExecCtx（带提交者身份与 trace_id）。"""
        runner = RecordingRunner()
        app = serve.create_app(
            gateway=FakeGateway(), metrics=Metrics(), logger=JsonLogger(sink=SILENT),
            shutdown=ShutdownCoordinator(), deps={"store": GoodDep()},
            workflow_runner_factory=lambda principal: runner)
        client = TestClient(app)
        workflow_id = submit(client, dag(node("a"))).json()["workflow_id"]
        wait_settled(client, workflow_id)
        parent_ctx, parent_e = runner.contexts[0]
        # 每条 workflow 有**自己的** Context：`DAGRunner` 靠它落 `workflow_state`
        # （层 / 节点 / 跳过原因）。没有它就无法复盘。
        # 注意"独立"不等于"节点间共享" —— 节点仍是各自独立的回合。
        self.assertIsNotNone(parent_ctx, "workflow 必须有 Context 才能落 workflow_state")
        self.assertEqual(parent_ctx.engine, "dag", "workflow 的 Context 必须绑定 dag 引擎")
        self.assertEqual(parent_e.principal.user_id, "u_alben")
        self.assertEqual(parent_e.principal.tenant, "acme")
        self.assertTrue(parent_e.trace_id)

    def test_runner_failure_marks_node_failed(self):
        runner = RecordingRunner(ok=False)
        app = serve.create_app(
            gateway=FakeGateway(), metrics=Metrics(), logger=JsonLogger(sink=SILENT),
            shutdown=ShutdownCoordinator(), deps={"store": GoodDep()},
            workflow_runner_factory=lambda principal: runner)
        client = TestClient(app)
        workflow_id = submit(client, dag(node("a"))).json()["workflow_id"]
        snapshot = wait_settled(client, workflow_id)
        self.assertEqual(snapshot["status"], "failed")
        self.assertIn("runner 判失败", nodes_by_name(snapshot)["a"]["error"])

    def test_runner_exception_is_isolated_to_node(self):
        runner = RecordingRunner(boom=True)
        app = serve.create_app(
            gateway=FakeGateway(), metrics=Metrics(), logger=JsonLogger(sink=SILENT),
            shutdown=ShutdownCoordinator(), deps={"store": GoodDep()},
            workflow_runner_factory=lambda principal: runner)
        client = TestClient(app)
        workflow_id = submit(client, dag(node("a"), node("b", depends_on=("a",)))).json()["workflow_id"]
        snapshot = wait_settled(client, workflow_id)
        self.assertEqual(snapshot["status"], "failed")
        by_name = nodes_by_name(snapshot)
        self.assertEqual(by_name["a"]["status"], "failed")
        self.assertIn("dispatch 异常", by_name["a"]["error"])
        self.assertEqual(by_name["b"]["status"], "skipped")
        # 短路之后**不再派发**：runner 只收到一个 spec
        self.assertEqual(len(runner.specs), 1)

    def test_broken_runner_factory_falls_back_to_builtin(self):
        """坏插件不该让所有 workflow 一起挂：fail-open 回内置 runner，但要留日志。"""
        logs = []
        gateway = FakeGateway()

        def broken(_principal):
            raise RuntimeError("装配插件失败")

        app = serve.create_app(
            gateway=gateway, metrics=Metrics(),
            logger=JsonLogger(sink=lambda line: logs.append(str(line))),
            shutdown=ShutdownCoordinator(), deps={"store": GoodDep()},
            workflow_runner_factory=broken)
        client = TestClient(app)
        workflow_id = submit(client, dag(node("a"))).json()["workflow_id"]
        snapshot = wait_settled(client, workflow_id)
        self.assertEqual(snapshot["status"], "done")
        self.assertEqual(len(gateway.requests), 1)     # 回落到内置 runner
        self.assertTrue(any("runner_factory_failed" in line for line in logs), logs)


# ══════════════════════════════════════════════════════════════
# 十、自述与关停（⑮ ⑱）
# ══════════════════════════════════════════════════════════════
class TestInfoAndShutdown(unittest.TestCase):
    # ⑮
    def test_info_exposes_workflow_section(self):
        app = make_app(FakeGateway(), workflow_max=7)
        client = TestClient(app)
        section = client.get("/v1/info").json()["workflow"]
        self.assertTrue(section["enabled"])
        self.assertEqual(section["max_tracked"], 7)
        self.assertEqual(section["running"], 0)
        self.assertEqual(section["tracked"], 0)
        self.assertIn("workers", section)

        gateway = FakeGateway(block_goals=("slow",))
        client = TestClient(make_app(gateway))
        try:
            workflow_id = submit(client, dag(node("a", goal="slow"))).json()["workflow_id"]
            wait_node_running(client, workflow_id)
            moving = client.get("/v1/info").json()["workflow"]
            self.assertEqual(moving["running"], 1)
            self.assertEqual(moving["tracked"], 1)
        finally:
            gateway.release()
        wait_settled(client, workflow_id)
        self.assertEqual(client.get("/v1/info").json()["workflow"]["running"], 0)

    def test_info_reports_feature_flag(self):
        client = TestClient(make_app())
        self.assertTrue(client.get("/v1/info").json()["features"]["explicit_workflows"])

    # ⑱
    def test_shutting_down_rejects_submission_with_503(self):
        shutdown = ShutdownCoordinator()
        client = TestClient(make_app(FakeGateway(), shutdown=shutdown))
        shutdown.begin()
        response = submit(client, dag(node("a")))
        self.assertEqual(response.status_code, 503, response.text)
        body = response.json()
        self.assertEqual(body["error"], "shutting_down")
        # 关停期间不登记任何状态（先在门口拒，不进内存）
        self.assertEqual(client.get("/v1/info").json()["workflow"]["tracked"], 0)


if __name__ == "__main__":              # pragma: no cover
    unittest.main()
