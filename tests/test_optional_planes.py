"""五个「实现了但没接线」的可选能力。

这些模块在 `serve.py` 里曾经引用 **0 次**，只在 demo/tests 里跑过：

| 模块 | 行数 | 专项用例 | serve 引用（改前） |
|---|---|---|---|
| `plane/resources.py` | 494 | 间接 | **0** |
| `plane/knowledge.py` | 551 | 23 | **0** |
| `plane/sandbox_tool.py` | 372 | 33 | **0** |
| `plane/blackboard.py` | 660 | 35 | **0** |
| `plane/semantic.py` | 290 | 间接 | **0** |

共 2,367 行实现、91 个用例，**生产入口一个都没装**。架构图画着「四资源面」，
实际只装了 Tool / Memory / Workflow 三个。

这类缺陷的共同特征：**代码、测试、demo 都完整，只有装配路径漏了。**
不接线时表现是"配了 env 但毫无效果"——最难查的一种。

本文件验证：**配了就真的生效，不配就真的不生效**（两个方向都测）。
"""
from __future__ import annotations

import os
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

import serve


class _Log:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def info(self, ev: str, **kw) -> None:
        self.events.append((ev, kw))

    def warn(self, ev: str, **kw) -> None:
        self.events.append((ev, kw))

    def error(self, ev: str, **kw) -> None:
        self.events.append((ev, kw))

    def has(self, prefix: str) -> dict | None:
        for ev, kw in self.events:
            if ev.startswith(prefix):
                return kw
        return None


def _build(extra_env: dict, tmp_extra: dict | None = None):
    """在干净 env 下构造 gateway，返回 (gateway, log)。"""
    log = _Log()
    with mock.patch.dict(os.environ, extra_env, clear=False):
        return serve.build_gateway_from_env(logger=log), log


def _ctx(scopes: tuple[str, ...] = ("tool.read", "tool.write", "resource.read",
                                  "knowledge.read", "sandbox.run",
                                  "blackboard.read")):
    """最小 ExecCtx：直接调用注册函数时用，跳过 Dispatcher 的策略层。"""
    from contracts import ExecCtx, Principal
    return ExecCtx(principal=Principal(user_id="u_t", tenant="t",
                                       scopes=frozenset(scopes)))


def _call(gw, name: str, args: dict):
    """按名字取注册的实现并**真的调用**。

    只断言日志/`names()` 会漏掉一类缺陷：工具在表里、却一调用就抛。
    这里刻意不走 Dispatcher，只验证「实现与注册时的名字对得上」。
    """
    registry = getattr(gw, "_tool_registry")
    entry = registry.get(name)
    assert entry is not None, f"工具未注册: {name}"
    _manifest, fn = entry
    return fn(args, _ctx())


class _ModuleFixture(unittest.TestCase):
    """写一个临时模块，供 `module:attr` 加载。"""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()
        if self.tmp not in sys.path:
            sys.path.insert(0, self.tmp)

    def write(self, name: str, code: str) -> None:
        pathlib.Path(self.tmp, f"{name}.py").write_text(code)


class TestSemanticMemory(_ModuleFixture):
    """`AGENT_RUNTIME_SEMANTIC=1` 把 Memory 的 bigram 换成语义向量。"""

    def test_off_by_default(self) -> None:
        gw, log = _build({"AGENT_RUNTIME_EXTRACTOR":
                          "agents.extractor:OpsExtractor"})
        info = log.has("memory.enabled")
        self.assertIsNotNone(info)
        self.assertFalse(info["semantic"], "默认必须关 —— 语义召回依赖外部网关")

    def test_on_requires_embedding_config(self) -> None:
        """显式开了却没配 embedding → **报错**，不静默退回 bigram。

        静默退回的表现是"配了语义召回，效果和没配一样"，最难查。
        """
        with self.assertRaises(RuntimeError) as caught:
            _build({"AGENT_RUNTIME_EXTRACTOR": "agents.extractor:OpsExtractor",
                    "AGENT_RUNTIME_SEMANTIC": "1"})
        self.assertIn("embedding", str(caught.exception).lower())

    def test_on_with_embedding_uses_semantic_class(self) -> None:
        _gw, log = _build({
            "AGENT_RUNTIME_EXTRACTOR": "agents.extractor:OpsExtractor",
            "AGENT_RUNTIME_SEMANTIC": "1",
            "AGENT_RUNTIME_EMBEDDING_BASE_URL": "http://127.0.0.1:1/v1",
            "AGENT_RUNTIME_EMBEDDING_MODEL": "m",
        })
        sem = log.has("memory.semantic")
        self.assertIsNotNone(sem)
        self.assertTrue(sem["enabled"])
        self.assertEqual(sem["cls"], "SemanticMemory")


class TestResourcesPlane(_ModuleFixture):
    """`AGENT_RUNTIME_RESOURCES` 把外部资源暴露成只读工具。"""

    def test_off_by_default(self) -> None:
        _gw, log = _build({})
        self.assertIsNone(log.has("resources.enabled"))

    def test_wires_and_registers_tool(self) -> None:
        self.write("_res_fx", '''
from contracts import ResourceRef
def make():
    ref = ResourceRef(uri="git://repo/diff", mime="text/plain", name="diff")
    return [(ref, lambda uri: "+1 -1")]
''')
        _gw, log = _build({"AGENT_RUNTIME_RESOURCES": "_res_fx:make"})
        self.assertEqual(log.has("resources.enabled")["count"], 1)
        tools = log.has("resources.tools")
        self.assertEqual(tools["count"], 1)
        self.assertTrue(tools["names"][0].startswith("read_"))

    def test_tool_actually_reads(self) -> None:
        """注册的名字要能真的读回内容，不是只在表里挂个名字。"""
        self.write("_res_call", '''
from contracts import ResourceRef
def make():
    ref = ResourceRef(uri="git://repo/deploy.md", name="deploy",
                      mime="text/markdown")
    return [(ref, lambda uri: "# 部署\\n回滚：rollout undo")]
''')
        gw, _log = _build({"AGENT_RUNTIME_RESOURCES": "_res_call:make"})
        out = _call(gw, "read_git_repo_deploy.md", {})
        self.assertEqual(out["uri"], "git://repo/deploy.md")
        self.assertIn("rollout undo", out["content"])

    def test_bad_factory_shape_raises(self) -> None:
        """工厂返回非列表 → 报错，不静默跳过。"""
        self.write("_res_bad", "def make():\n    return 42\n")
        with self.assertRaises(RuntimeError):
            _build({"AGENT_RUNTIME_RESOURCES": "_res_bad:make"})

    def test_bad_entry_shape_raises(self) -> None:
        self.write("_res_bad2", '''
def make():
    return ["not-a-tuple"]
''')
        with self.assertRaises(RuntimeError):
            _build({"AGENT_RUNTIME_RESOURCES": "_res_bad2:make"})


class TestKnowledgePlane(_ModuleFixture):
    """`AGENT_RUNTIME_KNOWLEDGE` 补上第四个资源面。"""

    def test_off_by_default(self) -> None:
        _gw, log = _build({})
        self.assertIsNone(log.has("knowledge.enabled"))

    def test_wires_and_builds_index(self) -> None:
        self.write("_kn_fx", '''
from plane.knowledge import Document
def make():
    return [
        Document(doc_id="d1", text="连接池耗尽导致 p99 阶梯上升。", title="排障"),
        Document(doc_id="d2", text="发布流程先回归再灰度。", title="发布"),
    ]
''')
        _gw, log = _build({"AGENT_RUNTIME_KNOWLEDGE": "_kn_fx:make"})
        info = log.has("knowledge.enabled")
        self.assertEqual(info["docs"], 2)
        # `rebuild()` 不调的话 chunks 是 0，检索永远空 —— demo 里踩过
        self.assertGreater(info["chunks"], 0, "必须真的建了索引")
        self.assertEqual(log.has("knowledge.tools")["names"],
                         ["search_knowledge"])

    def test_bad_factory_shape_raises(self) -> None:
        self.write("_kn_bad", "def make():\n    return 'nope'\n")
        with self.assertRaises(RuntimeError):
            _build({"AGENT_RUNTIME_KNOWLEDGE": "_kn_bad:make"})

    def test_tool_actually_retrieves(self) -> None:
        """`search_knowledge` 必须能真的召回，不是注册完就完事。

        这条用例是在补一个真实缺陷：装配时存的是 `KnowledgeBase`，而
        `retrieve` 只长在只读门面 `KnowledgeReader`（`kb.reader()`）上。
        结果是 `/v1/tools` 看得见 `search_knowledge`，模型一调用就
        `AttributeError` —— 只断言 `knowledge.enabled` / `names()` 的用例
        全绿也发现不了，因为启动阶段一切正常。
        """
        self.write("_kn_call", '''
from plane.knowledge import Document
def make():
    return [
        Document(doc_id="oom", title="OOM 处置",
                 text="容器 OOMKilled 先看 Last State，再核对 memory limit 与内存上限。"),
        Document(doc_id="rel", title="发布", text="发布流程先回归再灰度。"),
    ]
''')
        gw, _log = _build({"AGENT_RUNTIME_KNOWLEDGE": "_kn_call:make"})
        out = _call(gw, "search_knowledge", {"query": "OOMKilled 内存上限"})
        self.assertEqual(out["count"], 1, "必须命中且只命中相关的一条")
        self.assertEqual(out["hits"][0]["doc_id"], "oom")
        self.assertGreater(out["hits"][0]["score"], 0)
        # 空 query 是调用方错误，不是"检索不到"
        with self.assertRaises(ValueError):
            _call(gw, "search_knowledge", {"query": "  "})


class TestSandboxTool(unittest.TestCase):
    """`AGENT_RUNTIME_SANDBOX_TOOL=1` 把沙箱注册成 T2 工具。"""

    def test_off_by_default(self) -> None:
        _gw, log = _build({})
        self.assertIsNone(log.has("sandbox.tool"))

    def test_default_is_t2(self) -> None:
        _gw, log = _build({"AGENT_RUNTIME_SANDBOX_TOOL": "1"})
        info = log.has("sandbox.tool")
        self.assertEqual(info["name"], "sandbox_run")
        self.assertEqual(info["risk_tier"], "T2_HIGH",
                         "沙箱执行命令必须有副作用档位，默认要审批")

    def test_tier_can_be_lowered(self) -> None:
        _gw, log = _build({"AGENT_RUNTIME_SANDBOX_TOOL": "1",
                           "AGENT_RUNTIME_SANDBOX_TIER": "1"})
        self.assertEqual(log.has("sandbox.tool")["risk_tier"], "T1_LOW")

    def test_invalid_tier_raises(self) -> None:
        with self.assertRaises(RuntimeError):
            _build({"AGENT_RUNTIME_SANDBOX_TOOL": "1",
                    "AGENT_RUNTIME_SANDBOX_TIER": "9"})

    def test_tool_actually_runs(self) -> None:
        """注册的 `sandbox_run` 要能真的执行一条命令并回传 stdout。

        T1 只为测试降档；默认 T2 的行为由 `test_default_is_t2` 守住。
        """
        gw, _log = _build({"AGENT_RUNTIME_SANDBOX_TOOL": "1",
                           "AGENT_RUNTIME_SANDBOX_TIER": "1"})
        out = _call(gw, "sandbox_run", {"argv": ["echo", "hi"]})
        self.assertEqual(out["exit_code"], 0)
        self.assertEqual(out["stdout"].strip(), "hi")


class TestBlackboard(_ModuleFixture):
    """`AGENT_RUNTIME_BLACKBOARD=1` 提供多 agent 共享状态。"""

    def test_off_by_default(self) -> None:
        _gw, log = _build({})
        self.assertIsNone(log.has("blackboard.enabled"))

    def test_in_process_when_no_kv(self) -> None:
        _gw, log = _build({"AGENT_RUNTIME_BLACKBOARD": "1"})
        self.assertEqual(log.has("blackboard.enabled")["mode"], "in-process")

    def test_registers_read_tool(self) -> None:
        _gw, log = _build({"AGENT_RUNTIME_BLACKBOARD": "1"})
        self.assertEqual(log.has("blackboard.tools")["names"],
                         ["blackboard_read"])

    def test_explicit_redis_unreachable_raises(self) -> None:
        """点名 `AGENT_RUNTIME_KV=redis` 却连不上 → 报错，不静默退回进程内。

        早先这里恒为 in-process：`build_gateway_from_env` 里根本没有 `kv`
        这个局部变量（它只在 `_dep_specs()` / `main()` 里存在），
        `"kv" in locals()` 恒假。症状是"配了 redis，两个副本各写各的"——
        单进程测不出来，跨副本才发现，而那时已经在生产上了。
        """
        with self.assertRaises(RuntimeError) as caught:
            _build({"AGENT_RUNTIME_BLACKBOARD": "1",
                    "AGENT_RUNTIME_KV": "redis",
                    "AGENT_RUNTIME_REDIS_PORT": "6399"})
        self.assertIn("不可达", str(caught.exception))

    def test_distributed_mode_when_kv_is_shared(self) -> None:
        """KV 可用时走 `DistributedBlackboard`（CAS 由服务端保证）。"""
        from core.kv import InMemoryKV

        class _Fake:
            @staticmethod
            def info() -> dict:
                return {"backend": "kv", "shared": True}

        with mock.patch.object(serve, "_make_kv", lambda _e: _Fake()):
            _gw, log = _build({"AGENT_RUNTIME_BLACKBOARD": "1",
                               "AGENT_RUNTIME_KV": "redis"})
        self.assertEqual(log.has("blackboard.enabled")["mode"], "distributed")

    def test_read_tool_answers_for_missing_key(self) -> None:
        """缺键是 `found=False`，不是抛异常 —— 模型要能继续往下走。"""
        gw, _log = _build({"AGENT_RUNTIME_BLACKBOARD": "1"})
        out = _call(gw, "blackboard_read", {"key": "plan.step1"})
        self.assertEqual(out["key"], "plan.step1")
        self.assertFalse(out["found"])


class TestNoRegression(unittest.TestCase):
    """五项全关时，行为和接线前**完全一致**。"""

    def test_all_off_is_clean(self) -> None:
        _gw, log = _build({})
        for prefix in ("memory.semantic", "resources.enabled",
                       "knowledge.enabled", "sandbox.tool",
                       "blackboard.enabled"):
            self.assertIsNone(log.has(prefix),
                              f"{prefix} 默认必须关闭")

    def test_tool_registry_stays_empty_by_default(self) -> None:
        """没配任何工具时表是**显式空**，不是"工具丢了"。"""
        gw, _log = _build({})
        registry = getattr(gw, "_tool_registry", None)
        self.assertIsNotNone(registry)
        self.assertEqual(registry.names(), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
