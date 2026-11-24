"""tests/test_toolconfig.py — 声明式工具配置（`toolconfig.py`）的用例。

分层原则：**默认不依赖子进程**。
YAML 解析 / 插值 / import / schema / 策略这些错法占了配置事故的九成以上，
它们全在 `parse_tools_config` 这一层就能锁定，跑一次是毫秒级的。
真起 MCP 子进程的用例集中放在 `MCPRealServerTest` 一个类里（起 server 很贵，
且它验证的是「配置能驱动真进程」而不是「解析器逻辑」）。

跑法：
    .venv/bin/python -m unittest tests.test_toolconfig -v
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

# 允许从任意工作目录跑（统一用 `-t .` 时其实已经在根目录，这里只是保险）
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import toolconfig  # noqa: E402
from contracts import ExecCtx, Principal, RiskTier, SideEffect  # noqa: E402

#: 真 MCP 用例的前置条件
try:
    from adapters.mcp_transport import MCP_TRANSPORT_AVAILABLE
except Exception:                                # pragma: no cover - 环境缺 mcp
    MCP_TRANSPORT_AVAILABLE = False

_FIXTURE_SERVER = _ROOT / "tests" / "mcp_fixture_server.py"

#: jsonschema 是否可用于 schema 形状校验（缺了对应用例要 skip，而不是假装通过）
_JSONSCHEMA = toolconfig.validator_for is not None


# ══════════════════════════════════════════════════════════════
# 夹具：临时工具模块 + 写 YAML
# ══════════════════════════════════════════════════════════════
#: 临时模块的源码。每个用例用自己的模块名（sys.modules 会按名字缓存，
#: 同名复用会让「模块不存在」这类用例拿到上一次的旧模块）。
_TOOL_MODULE_SRC = '''"""测试用工具模块（一次性，跑完即弃）。"""

def query_metrics(args):
    """只读：返回一个固定形状的指标。"""
    return {"service": args.get("service"), "p99": 12.5}


def list_alerts(args):
    """只读：无必填参数。"""
    return [{"id": "a1", "state": "firing"}]


def with_ctx(args, ctx):
    """两参数版本：用来验证 ExecCtx 会被递进去。"""
    principal = getattr(ctx, "principal", None)
    return {"user": getattr(principal, "user_id", None), "args": args}


def no_args():
    """零参数版本：`fn()` 也要能调。"""
    return {"ok": True}


def write_note(args):
    """写操作（T2）。"""
    return {"written": args.get("text", "")}


NOT_CALLABLE = 42
'''


def _make_module(dirpath: str | Path) -> str:
    """在 `dirpath` 里写一个工具模块，返回模块名（不含 .py）。"""
    name = f"tcfg_tools_{uuid.uuid4().hex[:8]}"
    (Path(dirpath) / f"{name}.py").write_text(_TOOL_MODULE_SRC, encoding="utf-8")
    return name


def _write_yaml(path: str | Path, text: str) -> Path:
    p = Path(path)
    p.write_text(text, encoding="utf-8")
    return p


def _as_obj(payload: object) -> dict:
    """把 handler 的返回值收口成 dict。

    MCP 的 `_payload` 对「纯文本回包」返回字符串（JSON 文本），对带有
    `structured_content` 的回包返回 `{"structured": ...}`。这里两种都认，
    免得用例和一个**传输层实现细节**绑死。
    """
    if isinstance(payload, str):
        return json.loads(payload)
    if isinstance(payload, dict):
        structured = payload.get("structured")
        if isinstance(structured, dict):
            result = structured.get("result")
            return result if isinstance(result, dict) else structured
        return payload
    raise AssertionError(f"无法收口成 dict 的 payload: {payload!r}")


def _read_only_tool(module: str, name: str = "query_metrics",
                    **extra: object) -> str:
    """拼一段最小的只读工具 YAML（`extra` 是额外字段的原始 YAML 片段）。"""
    lines = [
        "tools:",
        f"  - name: {name}",
        f"    import: {module}:query_metrics",
        "    side_effect: read_only",
    ]
    lines += [f"    {key}: {value}" for key, value in extra.items()]
    return "\n".join(lines) + "\n"


# ══════════════════════════════════════════════════════════════
# ① 基本解析
# ══════════════════════════════════════════════════════════════
class ParseBasicsTest(unittest.TestCase):
    """`parse_tools_config()` 这一层：dict 进，`LoadedTools` 出，不起任何进程。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="tcfg-parse-")
        self.tmp = Path(self._tmp.name)
        self.module = _make_module(self.tmp)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _parse(self, data: dict) -> toolconfig.LoadedTools:
        return toolconfig.parse_tools_config(data, base_dir=self.tmp)

    def test_minimal_config_single_import_tool(self) -> None:
        loaded = self._parse({"version": 1, "tools": [{
            "name": "query_metrics",
            "import": f"{self.module}:query_metrics",
            "side_effect": "read_only",
        }]})
        try:
            self.assertEqual(len(loaded.manifests), 1)
            self.assertEqual(len(loaded.handlers), 1)
            manifest = loaded.manifests[0]
            self.assertEqual(manifest.name, "query_metrics")
            self.assertEqual(manifest.side_effect, SideEffect.READ_ONLY)
            self.assertEqual(loaded.connections, ())
        finally:
            loaded.close()

    def test_multiple_tools(self) -> None:
        loaded = self._parse({"version": 1, "tools": [
            {"name": "query_metrics", "import": f"{self.module}:query_metrics",
             "side_effect": "read_only"},
            {"name": "list_alerts", "import": f"{self.module}:list_alerts",
             "side_effect": "read_only"},
        ]})
        try:
            self.assertEqual(loaded.names(), ["query_metrics", "list_alerts"])
            self.assertEqual(set(loaded.handlers), {"query_metrics", "list_alerts"})
        finally:
            loaded.close()

    def test_side_effect_and_risk_tier_mapping(self) -> None:
        loaded = self._parse({"version": 1, "defaults": {"allow_irreversible": True},
                              "tools": [
            {"name": "r", "import": f"{self.module}:query_metrics",
             "side_effect": "read_only"},
            {"name": "w", "import": f"{self.module}:write_note",
             "side_effect": "mutating", "risk_tier": "T1"},
            {"name": "x", "import": f"{self.module}:write_note",
             "side_effect": "irreversible", "risk_tier": 3},
        ]})
        try:
            self.assertEqual(loaded.manifest("r").side_effect, SideEffect.READ_ONLY)
            self.assertEqual(loaded.manifest("r").risk_tier, RiskTier.T0_SAFE)
            self.assertEqual(loaded.manifest("w").side_effect, SideEffect.MUTATING)
            self.assertEqual(loaded.manifest("w").risk_tier, RiskTier.T1_LOW)
            self.assertEqual(loaded.manifest("x").side_effect, SideEffect.IRREVERSIBLE)
            self.assertEqual(loaded.manifest("x").risk_tier, RiskTier.T3_CRITICAL)
        finally:
            loaded.close()

    def test_scope_timeout_idempotent_passthrough(self) -> None:
        loaded = self._parse({"version": 1, "tools": [{
            "name": "write_note",
            "import": f"{self.module}:write_note",
            "side_effect": "mutating",
            "risk_tier": "T2",
            "idempotent": False,
            "required_scope": "tool.write",
            "timeout_ms": 2500,
            "max_result_bytes": 1024,
            "per_turn_limit": 3,
            "secrets": ["db_password"],
            "description": "写一条笔记",
        }]})
        try:
            m = loaded.manifest("write_note")
            self.assertEqual(m.required_scope, "tool.write")
            self.assertEqual(m.timeout_ms, 2500)
            self.assertIs(m.idempotent, False)
            self.assertEqual(m.max_result_bytes, 1024)
            self.assertEqual(m.per_turn_limit, 3)
            self.assertEqual(m.secrets, ("db_password",))
            self.assertEqual(m.description, "写一条笔记")
        finally:
            loaded.close()

    def test_defaults_are_conservative(self) -> None:
        """省掉字段时的默认值：副作用按 mutating、scope 按 tool.write、非幂等。"""
        loaded = self._parse({"version": 1, "tools": [{
            "name": "write_note",
            "import": f"{self.module}:write_note",
        }]})
        try:
            m = loaded.manifest("write_note")
            self.assertEqual(m.side_effect, SideEffect.MUTATING)
            self.assertEqual(m.risk_tier, RiskTier.T2_HIGH)
            self.assertEqual(m.required_scope, "tool.write")
            self.assertIs(m.idempotent, False)
            self.assertEqual(m.timeout_ms, 5000)
            # 没写 schema 要留下 warning（不是静默兜底）
            self.assertTrue(any("input_schema" in w for w in loaded.warnings))
        finally:
            loaded.close()

    def test_empty_config_yields_empty_toolset(self) -> None:
        loaded = self._parse({"version": 1})
        try:
            self.assertEqual(loaded.manifests, ())
            self.assertEqual(loaded.handlers, {})
            self.assertEqual(loaded.connections, ())
            self.assertEqual(len(loaded), 0)
        finally:
            loaded.close()

    def test_unknown_top_level_key_errors(self) -> None:
        with self.assertRaises(toolconfig.ToolConfigError) as ctx:
            self._parse({"version": 1, "tool": []})     # 拼错：tools → tool
        self.assertIn("tool", str(ctx.exception))

    def test_unknown_tool_key_errors(self) -> None:
        with self.assertRaises(toolconfig.ToolConfigError) as ctx:
            self._parse({"version": 1, "tools": [{
                "name": "x", "import": f"{self.module}:query_metrics",
                "side_effects": "read_only",           # 拼错：side_effect
            }]})
        self.assertIn("side_effects", str(ctx.exception))

    def test_illegal_tool_name_errors(self) -> None:
        with self.assertRaises(toolconfig.ToolConfigError) as ctx:
            self._parse({"version": 1, "tools": [{
                "name": "查指标", "import": f"{self.module}:query_metrics",
            }]})
        self.assertIn("非法字符", str(ctx.exception))

    def test_logged_warnings_are_reported_not_logged(self) -> None:
        """warnings 是返回值，而不是只打到 log —— 启动期日志没人看。"""
        loaded = self._parse({"version": 1, "tools": [{
            "name": "query_metrics", "import": f"{self.module}:query_metrics",
            "side_effect": "read_only",
        }]})
        try:
            self.assertIsInstance(loaded.warnings, tuple)
            self.assertTrue(all(isinstance(w, str) for w in loaded.warnings))
        finally:
            loaded.close()


# ══════════════════════════════════════════════════════════════
# ② 文件入口
# ══════════════════════════════════════════════════════════════
class FileLoadTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="tcfg-file-")
        self.tmp = Path(self._tmp.name)
        self.module = _make_module(self.tmp)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_relative_path_resolved_against_cwd_argument(self) -> None:
        _write_yaml(self.tmp / "tools.yaml",
                    f"version: 1\n{_read_only_tool(self.module)}")
        loaded = toolconfig.load_tools("tools.yaml", cwd=self.tmp)
        try:
            self.assertEqual(loaded.names(), ["query_metrics"])
        finally:
            loaded.close()

    def test_missing_file_error_contains_resolved_path(self) -> None:
        missing = self.tmp / "nope.yaml"
        with self.assertRaises(toolconfig.ToolConfigError) as ctx:
            toolconfig.load_tools("nope.yaml", cwd=self.tmp)
        message = str(ctx.exception)
        self.assertIn(str(missing), message)
        self.assertIn("不存在", message)

    def test_yaml_syntax_error_reports_line(self) -> None:
        # 第 3 行的缩进错位 → PyYAML 会给出 problem_mark
        _write_yaml(self.tmp / "bad.yaml",
                    "version: 1\ntools:\n   - name: x\n  import: y:z\n")
        with self.assertRaises(toolconfig.ToolConfigError) as ctx:
            toolconfig.load_tools("bad.yaml", cwd=self.tmp)
        message = str(ctx.exception)
        self.assertIn("YAML 语法错误", message)
        self.assertIn("第 ", message)          # 必须给行号，否则缩进错几乎无法人工定位

    def test_missing_version_errors(self) -> None:
        """缺 version = 报错（不取默认 1）。理由见 `_parse` 里的注释：

        没有版本号，未来 v2 改了字段语义时旧 loader 会静默按 v1 解析 ——
        「语义被解释错的配置」比「拒绝启动的配置」危险得多。
        """
        _write_yaml(self.tmp / "nover.yaml",
                    f"{_read_only_tool(self.module)}")
        with self.assertRaises(toolconfig.ToolConfigError) as ctx:
            toolconfig.load_tools("nover.yaml", cwd=self.tmp)
        self.assertIn("version", str(ctx.exception))

    def test_unknown_version_errors(self) -> None:
        _write_yaml(self.tmp / "v2.yaml",
                    f"version: 2\n{_read_only_tool(self.module)}")
        with self.assertRaises(toolconfig.ToolConfigError) as ctx:
            toolconfig.load_tools("v2.yaml", cwd=self.tmp)
        self.assertIn("version 1", str(ctx.exception))

    def test_empty_file_errors(self) -> None:
        _write_yaml(self.tmp / "empty.yaml", "")
        with self.assertRaises(toolconfig.ToolConfigError):
            toolconfig.load_tools("empty.yaml", cwd=self.tmp)

    def test_non_mapping_root_errors(self) -> None:
        _write_yaml(self.tmp / "list.yaml", "- a\n- b\n")
        with self.assertRaises(toolconfig.ToolConfigError) as ctx:
            toolconfig.load_tools("list.yaml", cwd=self.tmp)
        self.assertIn("<root>", str(ctx.exception))


# ══════════════════════════════════════════════════════════════
# ③ 环境变量插值
# ══════════════════════════════════════════════════════════════
class EnvInterpolationTest(unittest.TestCase):
    """用 MCP server 的 `cwd` 做观测点：它在**起进程之前**就解析完并做存在性检查，
    所以这几条用例是毫秒级的，不需要真起 server。

    （env 值本身的端到端验证在 `MCPRealServerTest.test_env_reaches_subprocess`。）
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="tcfg-env-")
        self.tmp = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _mcp_config(self, cwd: str) -> dict:
        return {"version": 1, "mcp_servers": [{
            "name": "srv", "command": sys.executable, "cwd": cwd,
        }]}

    def test_env_var_expansion(self) -> None:
        target = self.tmp / "expanded"
        with mock.patch.dict(os.environ, {"TCFG_CWD": str(target)}):
            with self.assertRaises(toolconfig.ToolConfigError) as ctx:
                toolconfig.parse_tools_config(self._mcp_config("${TCFG_CWD}"),
                                              base_dir=self.tmp)
        # 变量被展开成绝对路径（且路径存在性检查用的是展开后的值）
        self.assertIn(str(target), str(ctx.exception))

    def test_env_var_default_value_used(self) -> None:
        os.environ.pop("TCFG_MISSING_CWD", None)
        with self.assertRaises(toolconfig.ToolConfigError) as ctx:
            toolconfig.parse_tools_config(
                self._mcp_config("${TCFG_MISSING_CWD:-/nonexistent/tcfg-dir}"),
                base_dir=self.tmp)
        self.assertIn("/nonexistent/tcfg-dir", str(ctx.exception))

    def test_undefined_env_var_errors_with_variable_name(self) -> None:
        os.environ.pop("TCFG_TOTALLY_UNSET", None)
        with self.assertRaises(toolconfig.ToolConfigError) as ctx:
            toolconfig.parse_tools_config(self._mcp_config("${TCFG_TOTALLY_UNSET}"),
                                          base_dir=self.tmp)
        self.assertIn("TCFG_TOTALLY_UNSET", str(ctx.exception))
        self.assertIn("未定义", str(ctx.exception))


# ══════════════════════════════════════════════════════════════
# ④ import 加载与 handler 形状
# ══════════════════════════════════════════════════════════════
class ImportLoadingTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="tcfg-import-")
        self.tmp = Path(self._tmp.name)
        self.module = _make_module(self.tmp)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _load_one(self, import_spec: str, name: str = "t",
                  **extra: object) -> toolconfig.LoadedTools:
        tool: dict[str, object] = {"name": name, "import": import_spec,
                                   "side_effect": "read_only"}
        tool.update(extra)
        return toolconfig.parse_tools_config(
            {"version": 1, "tools": [tool]}, base_dir=self.tmp)

    def test_module_not_found_errors_with_module_name(self) -> None:
        with self.assertRaises(toolconfig.ToolConfigError) as ctx:
            self._load_one("tcfg_definitely_missing_module:fn")
        self.assertIn("tcfg_definitely_missing_module", str(ctx.exception))

    def test_function_not_found_errors_with_function_name(self) -> None:
        with self.assertRaises(toolconfig.ToolConfigError) as ctx:
            self._load_one(f"{self.module}:no_such_fn")
        message = str(ctx.exception)
        self.assertIn("no_such_fn", message)
        self.assertIn(self.module, message)

    def test_non_callable_import_errors(self) -> None:
        with self.assertRaises(toolconfig.ToolConfigError) as ctx:
            self._load_one(f"{self.module}:NOT_CALLABLE")
        self.assertIn("callable", str(ctx.exception))

    def test_import_spec_without_colon_errors(self) -> None:
        with self.assertRaises(toolconfig.ToolConfigError) as ctx:
            self._load_one("just_a_module")
        self.assertIn("module:function", str(ctx.exception))

    def test_handler_receives_args_dict(self) -> None:
        loaded = self._load_one(f"{self.module}:query_metrics")
        try:
            out = loaded.handlers["t"]({"service": "api"}, None)
            self.assertEqual(out, {"service": "api", "p99": 12.5})
        finally:
            loaded.close()

    def test_two_arg_handler_receives_exec_ctx(self) -> None:
        loaded = self._load_one(f"{self.module}:with_ctx")
        try:
            out = loaded.handlers["t"]({"a": 1}, ExecCtx(principal=Principal(user_id="u_1")))
            self.assertEqual(out, {"user": "u_1", "args": {"a": 1}})
        finally:
            loaded.close()

    def test_zero_arg_handler_is_callable(self) -> None:
        loaded = self._load_one(f"{self.module}:no_args")
        try:
            self.assertEqual(loaded.handlers["t"]({}, None), {"ok": True})
        finally:
            loaded.close()

    def test_none_args_are_coerced_to_dict(self) -> None:
        """模型可能传 None；handler 不能因此 TypeError（Dispatcher 也会兜，这里是第二道）。"""
        loaded = self._load_one(f"{self.module}:query_metrics")
        try:
            self.assertEqual(loaded.handlers["t"](None, None)["p99"], 12.5)
        finally:
            loaded.close()


# ══════════════════════════════════════════════════════════════
# ⑤ input_schema
# ══════════════════════════════════════════════════════════════
class InputSchemaTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="tcfg-schema-")
        self.tmp = Path(self._tmp.name)
        self.module = _make_module(self.tmp)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _parse(self, schema: object) -> toolconfig.LoadedTools:
        return toolconfig.parse_tools_config({"version": 1, "tools": [{
            "name": "query_metrics",
            "import": f"{self.module}:query_metrics",
            "side_effect": "read_only",
            "input_schema": schema,
        }]}, base_dir=self.tmp)

    @unittest.skipUnless(_JSONSCHEMA, "没装 jsonschema，形状校验按设计跳过")
    def test_invalid_schema_shape_errors(self) -> None:
        with self.assertRaises(toolconfig.ToolConfigError) as ctx:
            self._parse({"type": "notatype"})
        self.assertIn("input_schema", str(ctx.exception))

    def test_valid_schema_passes_through(self) -> None:
        schema = {"type": "object",
                  "properties": {"service": {"type": "string"}},
                  "required": ["service"]}
        loaded = self._parse(schema)
        try:
            self.assertEqual(loaded.manifest("query_metrics").input_schema, schema)
        finally:
            loaded.close()

    def test_missing_schema_gets_legal_fallback(self) -> None:
        loaded = toolconfig.parse_tools_config({"version": 1, "tools": [{
            "name": "query_metrics",
            "import": f"{self.module}:query_metrics",
            "side_effect": "read_only",
        }]}, base_dir=self.tmp)
        try:
            self.assertEqual(loaded.manifest("query_metrics").input_schema,
                             {"type": "object", "properties": {}})
        finally:
            loaded.close()

    def test_non_mapping_schema_errors(self) -> None:
        with self.assertRaises(toolconfig.ToolConfigError) as ctx:
            self._parse("not-a-schema")
        self.assertIn("input_schema", str(ctx.exception))


# ══════════════════════════════════════════════════════════════
# ⑥ 策略：不可逆闸门 / 重名 / 上限
# ══════════════════════════════════════════════════════════════
class PolicyTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="tcfg-policy-")
        self.tmp = Path(self._tmp.name)
        self.module = _make_module(self.tmp)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _tool(self, name: str = "drop_db", **extra: object) -> dict:
        tool: dict[str, object] = {
            "name": name,
            "import": f"{self.module}:write_note",
            "side_effect": "irreversible",
        }
        tool.update(extra)
        return tool

    def test_irreversible_is_fail_closed_by_default(self) -> None:
        """默认 allow_irreversible=false 时出现 irreversible → 报错（不是忽略）。"""
        with self.assertRaises(toolconfig.ToolConfigError) as ctx:
            toolconfig.parse_tools_config({"version": 1, "tools": [self._tool()]},
                                          base_dir=self.tmp)
        message = str(ctx.exception)
        self.assertIn("drop_db", message)
        self.assertIn("allow_irreversible", message)

    def test_irreversible_allowed_when_explicitly_enabled(self) -> None:
        loaded = toolconfig.parse_tools_config(
            {"version": 1, "defaults": {"allow_irreversible": True},
             "tools": [self._tool()]}, base_dir=self.tmp)
        try:
            self.assertEqual(loaded.manifest("drop_db").side_effect,
                             SideEffect.IRREVERSIBLE)
            self.assertEqual(loaded.manifest("drop_db").risk_tier, RiskTier.T3_CRITICAL)
        finally:
            loaded.close()

    def test_mcp_override_irreversible_blocked_before_spawning(self) -> None:
        """MCP 的 overrides 也算「配置里出现 irreversible」——而且要在起进程**之前**拦住。"""
        with self.assertRaises(toolconfig.ToolConfigError) as ctx:
            toolconfig.parse_tools_config({"version": 1, "mcp_servers": [{
                "name": "srv",
                "command": sys.executable,
                "cwd": str(self.tmp),
                "overrides": {"delete_note": {"side_effect": "irreversible"}},
            }]}, base_dir=self.tmp)
        self.assertIn("delete_note", str(ctx.exception))

    def test_duplicate_tool_name_errors(self) -> None:
        with self.assertRaises(toolconfig.ToolConfigError) as ctx:
            toolconfig.parse_tools_config({"version": 1, "tools": [
                {"name": "same", "import": f"{self.module}:query_metrics",
                 "side_effect": "read_only"},
                {"name": "same", "import": f"{self.module}:list_alerts",
                 "side_effect": "read_only"},
            ]}, base_dir=self.tmp)
        self.assertIn("same", str(ctx.exception))
        self.assertIn("重复", str(ctx.exception))

    def test_max_tools_exceeded_errors(self) -> None:
        with self.assertRaises(toolconfig.ToolConfigError) as ctx:
            toolconfig.parse_tools_config(
                {"version": 1, "defaults": {"max_tools": 1}, "tools": [
                    {"name": "a", "import": f"{self.module}:query_metrics",
                     "side_effect": "read_only"},
                    {"name": "b", "import": f"{self.module}:list_alerts",
                     "side_effect": "read_only"},
                ]}, base_dir=self.tmp)
        self.assertIn("max_tools", str(ctx.exception))

    def test_illegal_side_effect_errors(self) -> None:
        with self.assertRaises(toolconfig.ToolConfigError) as ctx:
            toolconfig.parse_tools_config({"version": 1, "tools": [{
                "name": "t", "import": f"{self.module}:query_metrics",
                "side_effect": "readonly",
            }]}, base_dir=self.tmp)
        self.assertIn("read_only", str(ctx.exception))

    def test_illegal_risk_tier_errors(self) -> None:
        with self.assertRaises(toolconfig.ToolConfigError) as ctx:
            toolconfig.parse_tools_config({"version": 1, "tools": [{
                "name": "t", "import": f"{self.module}:query_metrics",
                "side_effect": "read_only", "risk_tier": "T9",
            }]}, base_dir=self.tmp)
        self.assertIn("risk_tier", str(ctx.exception))

    def test_overrides_require_side_effect(self) -> None:
        """只写 risk_tier 会被拒绝：决定「要不要审批」的是副作用，不是档位。"""
        with self.assertRaises(toolconfig.ToolConfigError) as ctx:
            toolconfig.parse_tools_config({"version": 1, "mcp_servers": [{
                "name": "srv", "command": sys.executable, "cwd": str(self.tmp),
                "overrides": {"delete_note": {"risk_tier": "T3"}},
            }]}, base_dir=self.tmp)
        self.assertIn("side_effect", str(ctx.exception))


# ══════════════════════════════════════════════════════════════
# ⑦ 环境变量入口
# ══════════════════════════════════════════════════════════════
class EnvVarEntryTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="tcfg-entry-")
        self.tmp = Path(self._tmp.name)
        self.module = _make_module(self.tmp)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_load_from_env_reads_configured_path(self) -> None:
        path = _write_yaml(self.tmp / "tools.yaml",
                           f"version: 1\n{_read_only_tool(self.module)}")
        with mock.patch.dict(os.environ, {toolconfig.ENV_VAR: str(path)}):
            loaded = toolconfig.load_tools_from_env()
        try:
            self.assertEqual(loaded.names(), ["query_metrics"])
        finally:
            loaded.close()

    def test_load_from_env_unset_yields_empty_toolset(self) -> None:
        loaded = toolconfig.load_tools_from_env({})
        self.assertEqual(loaded.manifests, ())
        self.assertEqual(loaded.handlers, {})
        loaded.close()      # 空集合上 close 也必须安全（接线方无条件调用）

    def test_load_from_env_blank_is_treated_as_unset(self) -> None:
        self.assertEqual(len(toolconfig.load_tools_from_env({toolconfig.ENV_VAR: "  "})), 0)

    def test_load_from_env_broken_file_still_errors(self) -> None:
        """「未设置」= 空工具集；「设置但坏了」= 报错。两者不能混为一谈。"""
        with mock.patch.dict(os.environ, {toolconfig.ENV_VAR: str(self.tmp / "gone.yaml")}):
            with self.assertRaises(toolconfig.ToolConfigError):
                toolconfig.load_tools_from_env()


# ══════════════════════════════════════════════════════════════
# ⑧ 端到端：加载出来的东西真能被 Dispatcher 用
# ══════════════════════════════════════════════════════════════
class DispatcherEndToEndTest(unittest.TestCase):
    """这条用例回答的是：「配置加载出来的工具，和手写注册的工具有区别吗？」

    没有区别 —— 它走的是完整的 `Dispatcher.invoke` 链路（schema 校验 / scope /
    风险分级 / 审批 / 幂等 / 限流 / 预算 / Normalizer），一步都没绕。
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="tcfg-e2e-")
        self.tmp = Path(self._tmp.name)
        self.module = _make_module(self.tmp)
        _write_yaml(self.tmp / "tools.yaml", f"""version: 1
tools:
  - name: query_metrics
    description: 查指标
    import: {self.module}:query_metrics
    side_effect: read_only
    risk_tier: T0
    input_schema:
      type: object
      properties:
        service: {{type: string}}
      required: [service]
  - name: write_note
    description: 写一条笔记
    import: {self.module}:write_note
    side_effect: mutating
    risk_tier: T2
    idempotent: false
    input_schema:
      type: object
      properties:
        text: {{type: string}}
""")
        self.loaded = toolconfig.load_tools("tools.yaml", cwd=self.tmp)
        from plane.tool import Dispatcher, Normalizer, ToolRegistry
        self.registry = ToolRegistry()
        self.loaded.register_into(self.registry)
        self.dispatcher = Dispatcher(
            self.registry,
            normalizer=Normalizer(artifact_dir=str(self.tmp / "artifacts")),
        )

    def tearDown(self) -> None:
        self.loaded.close()
        self._tmp.cleanup()

    def test_registry_sees_config_tools(self) -> None:
        self.assertEqual(sorted(self.registry.names()), ["query_metrics", "write_note"])
        self.assertEqual(self.registry.get("query_metrics")[0].timeout_ms, 5000)

    def test_ok_invocation(self) -> None:
        outcome = self.dispatcher.invoke("query_metrics", {"service": "api"}, ExecCtx())
        self.assertEqual(outcome.kind, "ok", outcome.reason)
        self.assertEqual(outcome.result.payload["p99"], 12.5)
        self.assertEqual(outcome.result.tool, "query_metrics")

    def test_invalid_args_rejected_by_config_schema(self) -> None:
        outcome = self.dispatcher.invoke("query_metrics", {"svc": "api"}, ExecCtx())
        self.assertEqual(outcome.kind, "failed")
        self.assertIn("invalid args", outcome.reason)
        self.assertTrue(outcome.retryable)

    def test_t2_needs_confirmation(self) -> None:
        """配置里写的 T2 真的变成了审批要求（不是「配了就算」）。"""
        outcome = self.dispatcher.invoke("write_note", {"text": "hi"}, ExecCtx())
        self.assertEqual(outcome.kind, "needs_confirmation")
        self.assertIn("T2", outcome.reason)

    def test_scope_is_enforced_from_config(self) -> None:
        ctx = ExecCtx(principal=Principal(scopes=frozenset({"tool.read"})))
        outcome = self.dispatcher.invoke("write_note", {"text": "hi"}, ctx)
        self.assertEqual(outcome.kind, "denied")
        self.assertIn("tool.write", outcome.reason)


# ══════════════════════════════════════════════════════════════
# ⑨ MCP 默认值（纯逻辑，不起进程）
# ══════════════════════════════════════════════════════════════
class MCPDefaultSideEffectTest(unittest.TestCase):
    """`default_side_effect` 只对「启发式判不出来」的工具生效。

    这条边界很关键：对 `create_note` 也套 `read_only` 默认值 = 把写操作配成只读，
    等于拆掉审批闸门。所以这里直接锁住「判得出来就不覆盖」这条语义。
    """

    def _manifest(self, name: str, schema: dict | None = None) -> object:
        from contracts import ToolManifest
        return ToolManifest(
            name=name, side_effect=SideEffect.MUTATING,
            risk_tier=RiskTier.T2_HIGH, idempotent=False,
            required_scope="tool.write",
            input_schema=schema or {"type": "object", "properties": {}},
        )

    def test_undecided_name_takes_default(self) -> None:
        manifest = toolconfig._apply_mcp_defaults(
            self._manifest("srv__frobnicate"),
            default_side_effect=SideEffect.MUTATING,
            default_risk_tier=RiskTier.T1_LOW,
            warn=lambda _m: None,
        )
        self.assertEqual(manifest.risk_tier, RiskTier.T1_LOW)

    def test_decided_write_is_not_overridden(self) -> None:
        manifest = toolconfig._apply_mcp_defaults(
            self._manifest("srv__create_note"),
            default_side_effect=SideEffect.READ_ONLY,
            default_risk_tier=RiskTier.T0_SAFE,
            warn=lambda _m: None,
        )
        self.assertEqual(manifest.side_effect, SideEffect.MUTATING)
        self.assertEqual(manifest.risk_tier, RiskTier.T2_HIGH)

    def test_decided_read_is_not_overridden(self) -> None:
        manifest = toolconfig._apply_mcp_defaults(
            self._manifest("srv__get_note"),
            default_side_effect=SideEffect.IRREVERSIBLE,
            default_risk_tier=RiskTier.T3_CRITICAL,
            warn=lambda _m: None,
        )
        self.assertEqual(manifest.side_effect, SideEffect.MUTATING)


# ══════════════════════════════════════════════════════════════
# ⑩ 真 MCP server（子进程）
# ══════════════════════════════════════════════════════════════
@unittest.skipUnless(MCP_TRANSPORT_AVAILABLE, "没装 mcp SDK，跳过真子进程用例")
@unittest.skipUnless(_FIXTURE_SERVER.exists(), "缺少 tests/mcp_fixture_server.py")
class MCPRealServerTest(unittest.TestCase):
    """真起 `tests/mcp_fixture_server.py`（4 个工具），验证配置能驱动真进程。

    起 server 很贵，所以共享一份加载结果；`close()` 的用例自己再起一份
    （它要观察的是「收拾干净」这件事本身，不能和别人共用连接）。
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory(prefix="tcfg-mcp-")
        cls.tmp = Path(cls._tmp.name)
        cls._env = mock.patch.dict(os.environ, {"TCFG_LABEL": "prod_"})
        cls._env.start()
        cls.config_path = _write_yaml(cls.tmp / "tools.yaml", f"""version: 1
mcp_servers:
  - name: notes
    command: {sys.executable}
    args: ["-m", "tests.mcp_fixture_server"]
    cwd: {_ROOT}
    env:
      MCP_FIXTURE_LABEL: "${{TCFG_LABEL}}"
    startup_timeout_s: 30
    call_timeout_s: 40
    default_side_effect: mutating
    default_risk_tier: T2
    overrides:
      delete_note: {{side_effect: irreversible, risk_tier: T3}}
      no_such_tool: {{side_effect: read_only}}
defaults:
  allow_irreversible: true
""")
        cls.loaded = toolconfig.load_tools(cls.config_path)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.loaded.close()
        cls._env.stop()
        cls._tmp.cleanup()

    def test_discovers_four_tools_with_prefix(self) -> None:
        self.assertEqual(sorted(self.loaded.names()), [
            "notes__create_note", "notes__delete_note",
            "notes__get_note", "notes__list_notes",
        ])
        self.assertEqual(len(self.loaded.connections), 1)
        # 启发式把 get_note 判成只读（不需要审批），create_note 判成写
        self.assertEqual(self.loaded.manifest("notes__get_note").side_effect,
                         SideEffect.READ_ONLY)
        self.assertEqual(self.loaded.manifest("notes__get_note").risk_tier,
                         RiskTier.T0_SAFE)
        self.assertEqual(self.loaded.manifest("notes__create_note").side_effect,
                         SideEffect.MUTATING)

    def test_overrides_force_irreversible(self) -> None:
        manifest = self.loaded.manifest("notes__delete_note")
        # 启发式只能判到 MUTATING/T2（「note」不在高价值名词表里），
        # 配置里显式声明把它升成不可逆 —— 这就是 overrides 的意义
        self.assertEqual(manifest.side_effect, SideEffect.IRREVERSIBLE)
        self.assertEqual(manifest.risk_tier, RiskTier.T3_CRITICAL)
        self.assertEqual(manifest.required_scope, "tool.write")
        self.assertIs(manifest.idempotent, False)

    def test_unknown_override_produces_warning(self) -> None:
        self.assertTrue(any("no_such_tool" in w for w in self.loaded.warnings),
                        f"未生效的覆盖项必须报警告，实际 warnings={self.loaded.warnings}")

    def test_env_reaches_subprocess(self) -> None:
        """`env` 里的 `${TCFG_LABEL}` 真的进了子进程（新建笔记的 id 前缀可以证明）。"""
        handler = self.loaded.handlers["notes__create_note"]
        note = _as_obj(handler({"text": "hi"}, None))
        self.assertEqual(note["text"], "hi")
        self.assertTrue(str(note["id"]).startswith("prod_"), note)

    def test_mcp_tool_through_dispatcher(self) -> None:
        """配置里的 MCP 工具和内置工具走**同一条** Dispatcher 链路（不是特例路径）。"""
        from plane.tool import Dispatcher, Normalizer, ToolRegistry

        registry = ToolRegistry()
        self.loaded.register_into(registry)
        dispatcher = Dispatcher(
            registry,
            normalizer=Normalizer(artifact_dir=str(self.tmp / "artifacts")),
        )
        outcome = dispatcher.invoke("notes__get_note", {"id": "a1"}, ExecCtx())
        self.assertEqual(outcome.kind, "ok", outcome.reason)
        self.assertEqual(_as_obj(outcome.result.payload)["id"], "a1")

        # T2 的写工具（create_note）没有 approval → 审批，而不是直接执行
        blocked = dispatcher.invoke("notes__create_note", {"text": "x"}, ExecCtx())
        self.assertEqual(blocked.kind, "needs_confirmation")

    def test_connection_is_alive_while_open(self) -> None:
        conn = self.loaded.connections[0]
        self.assertTrue(conn.started)
        self.assertIsNone(conn.poll())          # 存活 → poll() 返回 None
        self.assertTrue(conn.is_running())

    def test_close_terminates_subprocess(self) -> None:
        """`close()` 之后子进程必须真退出（`poll() is not None`），不是只丢句柄。"""
        loaded = toolconfig.load_tools(self.config_path)
        conn = loaded.connections[0]
        self.assertIsNone(conn.poll())
        loaded.close()
        self.assertIsNotNone(conn.poll(), "close() 之后子进程仍然活着：孤儿进程")
        loaded.close()                          # 幂等：接线方可能调两次


if __name__ == "__main__":                      # pragma: no cover
    unittest.main(verbosity=2)
