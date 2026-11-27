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
