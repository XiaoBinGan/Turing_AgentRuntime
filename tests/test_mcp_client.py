"""tests/test_mcp_client.py — MCP 接入层的用例。

**不连真 MCP server**：`FakeSession` 是 duck typing 替身，只实现 MCP 客户端那两个
协程方法（`list_tools()` / `call_tool(name, arguments)`）。这样既能锁住桥的行为，
又不需要起 transport / 子进程。

但**工具与回包对象用真 SDK 类型**（`mcp.types.Tool` / `CallToolResult` / `TextContent`）：
桥最容易踩的坑恰恰是 SDK 字段命名（2.3 是 `input_schema` 而非 `inputSchema`），
全用 SimpleNamespace 桩就测不出这类漂移。另有一个用例专测「纯 dict camelCase 形状」，
保证 wire 格式也认。
"""
from __future__ import annotations

import asyncio
import threading
import unittest
import warnings
from types import SimpleNamespace

from mcp.types import CallToolResult, ImageContent, TextContent
from mcp.types import Tool as McpTool

from adapters.mcp_client import (
    MCP_BRIDGE_THREAD_PREFIX,
    MCPToolBridge,
    classify_side_effect,
    register_mcp_tools,
    run_coroutine_sync,
)
from contracts import ExecCtx, Approval, RiskTier, SideEffect, ToolManifest, args_hash
from plane.tool import Dispatcher, ToolRegistry


# ══════════════════════════════════════════════════════════════
# 测试替身
# ══════════════════════════════════════════════════════════════
class FakeProtocolError(Exception):
    """模拟协议级错误（找不到工具 / 传输断了）。"""


def mcp_tool(name: str, description: str | None = "一个测试工具",
             schema: dict | None = None) -> McpTool:
    """构造真 SDK 的 `Tool`（注意字段名是 input_schema）。"""
    return McpTool(
        name=name,
        description=description,
        input_schema=schema if schema is not None
        else {"type": "object", "properties": {"path": {"type": "string"}},
              "required": ["path"]},
    )


def text_result(*chunks: str) -> CallToolResult:
    return CallToolResult(content=[TextContent(text=c) for c in chunks])


class FakeSession:
    """MCP 会话的同形替身：两个 async 方法，签名对齐 `ClientSession`。"""

    def __init__(self, tools=(), *, results: dict | None = None,
                 errors=(), delay: float = 0.0, pages=None) -> None:
        self._tools = list(tools)
        self.results: dict[str, CallToolResult] = dict(results or {})
        self.errors = set(errors)
        self.delay = float(delay)
        #: [(tools, next_cursor), ...]；非 None 时走分页路径
        self.pages = pages
        self.calls: list[tuple[str, dict]] = []

    async def list_tools(self, *, params=None):
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.pages is not None:
            index = 0 if params is None else int(params["cursor"])
            tools, nxt = self.pages[index]
            return SimpleNamespace(tools=list(tools), next_cursor=nxt)
        return SimpleNamespace(tools=list(self._tools), next_cursor=None)

    async def call_tool(self, name, arguments=None):
        if self.delay:
            await asyncio.sleep(self.delay)
        self.calls.append((name, dict(arguments or {})))
        if name in self.errors:
            raise FakeProtocolError(f"transport down for {name}")
        if name in self.results:
            return self.results[name]
        return text_result(f"ok:{name}")


def bridge_threads() -> list[threading.Thread]:
    return [t for t in threading.enumerate()
            if t.name.startswith(MCP_BRIDGE_THREAD_PREFIX)]


# ══════════════════════════════════════════════════════════════
# ① discover：MCP Tool → ToolManifest
# ══════════════════════════════════════════════════════════════
class TestDiscover(unittest.TestCase):
    def test_01_translate_to_manifest_with_prefix(self):
        """① 转成 ToolManifest，name 带 `server__` 前缀。"""
        session = FakeSession([mcp_tool("read_file")])
        bridge = MCPToolBridge(session, server_name="fs")
        manifests = asyncio.run(bridge.discover())

        self.assertEqual(len(manifests), 1)
        manifest = manifests[0]
        self.assertIsInstance(manifest, ToolManifest)
        self.assertEqual(manifest.name, "fs__read_file")
        # 只读工具：T0 放行 + 幂等 + 只要读权限
        self.assertIs(manifest.side_effect, SideEffect.READ_ONLY)
        self.assertIs(manifest.risk_tier, RiskTier.T0_SAFE)
        self.assertTrue(manifest.idempotent)
        self.assertEqual(manifest.required_scope, "tool.read")
        self.assertEqual(manifest.timeout_ms, 30000)

    def test_02_input_schema_passthrough(self):
        """② inputSchema 原样透传（MCP 本身就是 JSON Schema，翻译只会引入偏差）。"""
        schema = {"type": "object",
                  "properties": {"path": {"type": "string"},
                                 "lines": {"type": "integer", "minimum": 1}},
                  "required": ["path"],
                  "additionalProperties": False}
        bridge = MCPToolBridge(FakeSession([mcp_tool("read_file", schema=schema)]))
        manifest = asyncio.run(bridge.discover())[0]

        self.assertEqual(manifest.input_schema, schema)
        # 嵌套结构也要一模一样（不是浅拷贝后丢字段）
        self.assertEqual(manifest.input_schema["properties"]["lines"], {"type": "integer",
                                                                       "minimum": 1})

    def test_03_description_passthrough(self):
        """③ description 透传。"""
        bridge = MCPToolBridge(FakeSession([mcp_tool("read_file", "读取文件的若干行")]))
        self.assertEqual(asyncio.run(bridge.discover())[0].description, "读取文件的若干行")

    def test_04_description_fallback(self):
        """④ 描述为空时有兜底文案（空描述 = 模型只看到一个名字，只能瞎猜）。"""
        session = FakeSession([mcp_tool("read_file", description=None),
                               mcp_tool("list_dir", description="   ")])
        manifests = asyncio.run(MCPToolBridge(session, server_name="fs").discover())
        for manifest in manifests:
            self.assertTrue(manifest.description.strip())
        self.assertIn("read_file", manifests[0].description)
        self.assertIn("fs", manifests[0].description)

    def test_19_missing_input_schema_falls_back(self):
        """⑲ input_schema 缺失 → 合法兜底 `{"type":"object","properties":{}}`。

        不能给 `{}`：`openai_compat.tool_schema()` 会把空 dict 当「没声明」退回保守
        形状，而严格的 OpenAI 兼容服务对空 parameters 直接 400。
        """
        raw = McpTool(name="weird", description="d", input_schema={"type": "object"})
        raw.input_schema = {}                     # 模拟服务端给了空 schema
        manifests = asyncio.run(MCPToolBridge(FakeSession([raw])).discover())

        self.assertEqual(manifests[0].input_schema, {"type": "object", "properties": {}})
        self.assertEqual(manifests[0].input_schema.get("type"), "object")

    def test_18_malformed_tool_skipped_with_warning(self):
        """⑱ 缺 name 的畸形 tool → 跳过 + warning，不炸掉整批。"""
        good = mcp_tool("read_file")
        no_name = McpTool(name="", description="坏工具",
                          input_schema={"type": "object"})
        session = FakeSession([no_name, good])
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            manifests = asyncio.run(MCPToolBridge(session).discover())

        self.assertEqual([m.name for m in manifests], ["mcp__read_file"])
        self.assertTrue(any("畸形" in str(w.message) for w in caught))
        self.assertTrue(any(issubclass(w.category, RuntimeWarning) for w in caught))

    def test_26_camel_case_dict_shape_accepted(self):
        """附加：纯 dict（wire 格式 camelCase）也认 —— 老回包/桩/缓存 JSON 的形状。"""
        raw = {"name": "read_file", "description": "d",
               "inputSchema": {"type": "object", "properties": {}}}
        manifests = asyncio.run(MCPToolBridge(FakeSession([raw])).discover())
        self.assertEqual(manifests[0].name, "mcp__read_file")
        self.assertEqual(manifests[0].input_schema, {"type": "object", "properties": {}})

    def test_22_discover_follows_pagination(self):
        """附加：跟随 next_cursor 翻页（只拉第一页会静默少一半工具）。"""
        session = FakeSession(pages=[([mcp_tool("get_a")], "1"),
                                     ([mcp_tool("get_b")], "2"),
                                     ([mcp_tool("get_c")], None)])
        manifests = asyncio.run(MCPToolBridge(session).discover())
        self.assertEqual([m.name for m in manifests],
                         ["mcp__get_a", "mcp__get_b", "mcp__get_c"])

    def test_28_pagination_cursor_style_client(self):
        """附加：高层 `mcp.Client.list_tools(*, cursor=...)` 的翻页形状也要认。

        `ClientSession` 收 `params={"cursor": c}`，`mcp.Client` 收 `cursor=c`。
        只写死一种 → 在另一种客户端上「永远只拿到第一页」且没有任何报错。
        """
        class CursorSession(FakeSession):
            async def list_tools(self, *, cursor=None):
                await asyncio.sleep(0)
                index = 0 if cursor is None else int(cursor)
                tools, nxt = self.pages[index]
                return SimpleNamespace(tools=list(tools), next_cursor=nxt)

        session = CursorSession(pages=[([mcp_tool("get_a")], "1"),
                                       ([mcp_tool("get_b")], None)])
        manifests = asyncio.run(MCPToolBridge(session).discover())
        self.assertEqual([m.name for m in manifests], ["mcp__get_a", "mcp__get_b"])

    def test_29_pagination_unsupported_stops_at_first_page(self):
        """附加：会话不接受任何翻页参数 → 停在第一页（不硬塞 kwargs 打崩会话）。"""
        class NoPagingSession:
            async def list_tools(self):
                return SimpleNamespace(tools=[mcp_tool("get_a")], next_cursor="1")

            async def call_tool(self, name, arguments=None):
                return text_result("ok")

        with self.assertLogs("adapters.mcp_client", level="WARNING") as logs:
            manifests = asyncio.run(MCPToolBridge(NoPagingSession()).discover())
        self.assertEqual([m.name for m in manifests], ["mcp__get_a"])
        self.assertTrue(any("停止翻页" in line for line in logs.output))

    def test_discover_sync_wrapper(self):
        """`discover_sync()` = 同步版 discover。"""
        bridge = MCPToolBridge(FakeSession([mcp_tool("get_x")]), server_name="s")
        self.assertEqual([m.name for m in bridge.discover_sync()], ["s__get_x"])


# ══════════════════════════════════════════════════════════════
# ② 启发式副作用判定
# ══════════════════════════════════════════════════════════════
class TestClassifySideEffect(unittest.TestCase):
    def test_05_read_verbs_are_read_only_t0(self):
        """⑤ get_* → READ_ONLY / T0。"""
        for name in ("get_weather", "list_dir", "read_file", "search_docs",
                     "query_rows", "fetch_url", "describe_table", "getUserPosts",
                     "list-issues", "read.me"):
            with self.subTest(name=name):
                side, tier = classify_side_effect(name)
                self.assertIs(side, SideEffect.READ_ONLY)
                self.assertIs(tier, RiskTier.T0_SAFE)

    def test_06_write_verbs_are_mutating_t2(self):
        """⑥ create_* → MUTATING / T2。"""
        for name in ("create_file", "write_file", "update_row", "delete_branch",
                     "set_env", "remove_label", "put_object", "post_message",
                     "insert_record", "createIssue"):
            with self.subTest(name=name):
                side, tier = classify_side_effect(name)
                self.assertIs(side, SideEffect.MUTATING)
                self.assertIs(tier, RiskTier.T2_HIGH)

    def test_07_exec_verbs_are_irreversible_t3(self):
        """⑦ exec_* → IRREVERSIBLE / T3（Dispatcher 对 T3 只能拒绝）。"""
        for name in ("exec_command", "run_shell", "shell", "bash", "deploy_app",
                     "drop_table", "execSQL"):
            with self.subTest(name=name):
                side, tier = classify_side_effect(name)
                self.assertIs(side, SideEffect.IRREVERSIBLE)
                self.assertIs(tier, RiskTier.T3_CRITICAL)

    def test_08_weird_name_is_conservative(self):
        """⑧ 判不出来的名字 → 保守 MUTATING / T2（宁可多一道审批）。"""
        for name in ("frobnicate", "hocus", "zap_the_thing", ""):
            with self.subTest(name=name):
                self.assertEqual(classify_side_effect(name),
                                 (SideEffect.MUTATING, RiskTier.T2_HIGH))

    def test_09_explicit_params_override_heuristic(self):
        """⑨ 显式构造参数覆盖启发式。"""
        session = FakeSession([mcp_tool("get_weather"), mcp_tool("create_file")])

        forced = MCPToolBridge(session, side_effect=SideEffect.IRREVERSIBLE,
                               risk_tier=RiskTier.T3_CRITICAL)
        for manifest in asyncio.run(forced.discover()):
            self.assertIs(manifest.side_effect, SideEffect.IRREVERSIBLE)
            self.assertIs(manifest.risk_tier, RiskTier.T3_CRITICAL)

        # 只给 side_effect：risk_tier 跟着它走，不会出现「只读 + T3」这种自相矛盾
        only_side = MCPToolBridge(session, side_effect=SideEffect.READ_ONLY)
        for manifest in asyncio.run(only_side.discover()):
            self.assertIs(manifest.side_effect, SideEffect.READ_ONLY)
            self.assertIs(manifest.risk_tier, RiskTier.T0_SAFE)

        # auto_classify=False：不跑启发式，全部落到保守默认
        fixed = MCPToolBridge(session, auto_classify=False)
        for manifest in asyncio.run(fixed.discover()):
            self.assertIs(manifest.side_effect, SideEffect.MUTATING)
            self.assertIs(manifest.risk_tier, RiskTier.T2_HIGH)

    def test_24_schema_signal_only_escalates(self):
        """附加：schema 只能升级危险度，不能降级。

        参数里有 `query` 的写工具很常见 → 拿参数名把写操作降成只读 = 拆掉审批闸门。
        """
        read_name_write_param = {"type": "object",
                                 "properties": {"delete_id": {"type": "string"}}}
        side, tier = classify_side_effect("get_records", read_name_write_param)
        self.assertIs(side, SideEffect.MUTATING)
        self.assertIs(tier, RiskTier.T2_HIGH)

        # 名字判不出来 + 参数里有 exec → 升到 T3
        self.assertEqual(classify_side_effect("frobnicate",
                                              {"type": "object",
                                               "properties": {"exec_cmd": {}}}),
                         (SideEffect.IRREVERSIBLE, RiskTier.T3_CRITICAL))

        # 名字判不出来 + 参数里有 query（读信号）→ 仍然保守，不被降级
        self.assertEqual(classify_side_effect("frobnicate",
                                              {"type": "object",
                                               "properties": {"query": {}}}),
                         (SideEffect.MUTATING, RiskTier.T2_HIGH))

    def test_25_substring_is_not_token(self):
        """附加：按 token 比对而不是子串 —— `settings` 里含 `set` 不许判成写。"""
        self.assertEqual(classify_side_effect("read_settings"),
                         (SideEffect.READ_ONLY, RiskTier.T0_SAFE))
        self.assertEqual(classify_side_effect("get_dataset"),
                         (SideEffect.READ_ONLY, RiskTier.T0_SAFE))


# ══════════════════════════════════════════════════════════════
# ③ handler：调用与结果收口
# ══════════════════════════════════════════════════════════════
class TestHandler(unittest.TestCase):
    def test_10_handler_passes_name_and_args(self):
        """⑩ make_handler 调 call_tool 并传对参数（前缀要剥掉）。"""
        session = FakeSession([mcp_tool("read_file")])
        bridge = MCPToolBridge(session, server_name="fs")
        handler = bridge.make_handler("fs__read_file")

        out = handler({"path": "/tmp/a.txt"}, ExecCtx())

        self.assertEqual(session.calls, [("read_file", {"path": "/tmp/a.txt"})])
        self.assertEqual(out, "ok:read_file")
        self.assertEqual(handler.mcp_remote_name, "read_file")

    def test_11_content_list_to_payload(self):
        """⑪ MCP content 列表 → 可返回结构。"""
        bridge = MCPToolBridge(FakeSession(results={
            "single": text_result("hello"),
            "multi": text_result("line1", "line2"),
            "rich": CallToolResult(content=[
                TextContent(text="见图"),
                ImageContent(data="aGk=", mime_type="image/png"),
            ]),
        }))
        self.assertEqual(bridge.call_sync("single"), "hello")
        self.assertEqual(bridge.call_sync("multi"), "line1\nline2")

        rich = bridge.call_sync("rich")
        self.assertIsInstance(rich, dict)
        self.assertEqual(rich["text"], "见图")
        self.assertEqual(rich["content"][0]["type"], "image")
        self.assertEqual(rich["content"][0]["mime_type"], "image/png")

    def test_21_structured_content_preserved(self):
        """附加：structured_content 保留（别把结构化结果压成字符串丢掉字段）。"""
        bridge = MCPToolBridge(FakeSession(results={
            "rows": CallToolResult(content=[TextContent(text="2 rows")],
                                   structured_content={"rows": [{"id": 1}, {"id": 2}]}),
        }))
        payload = bridge.call_sync("rows")
        self.assertEqual(payload["structured"], {"rows": [{"id": 1}, {"id": 2}]})
        self.assertEqual(payload["text"], "2 rows")

    def test_12_error_raises_runtime_error(self):
        """⑫ MCP 返回 error → handler 抛 RuntimeError（两种错误都要管）。"""
        bridge = MCPToolBridge(FakeSession(
            results={"is_error": CallToolResult(content=[TextContent(text="权限不足")],
                                                is_error=True)},
            errors={"transport"},
        ))
        with self.assertRaises(RuntimeError) as raised:
            bridge.call_sync("is_error")
        self.assertIn("权限不足", str(raised.exception))

        with self.assertRaises(RuntimeError) as raised:
            bridge.call_sync("transport")
        self.assertIn("transport", str(raised.exception))

    def test_13_handler_without_running_loop(self):
        """⑬ 无事件循环下调用 handler 正常（Dispatcher worker 的主路径）。"""
        with self.assertRaises(RuntimeError):
            asyncio.get_running_loop()        # 前提校验：当前线程确实没有 loop

        session = FakeSession([mcp_tool("get_x")])
        bridge = MCPToolBridge(session)
        handler = bridge.make_handler("mcp__get_x")

        for _ in range(3):
            self.assertEqual(handler({"a": 1}, ExecCtx()), "ok:get_x")
        self.assertEqual(len(session.calls), 3)

    def test_14_handler_inside_running_loop(self):
        """⑭ 有事件循环下调用 handler 正常（不能在 loop 里 asyncio.run）。

        这条用例直接覆盖 `RuntimeError: asyncio.run() cannot be called from a
        running event loop` 那个坑：桥必须把协程丢到别的线程去跑。
        """
        session = FakeSession([mcp_tool("get_x")])
        bridge = MCPToolBridge(session)
        handler = bridge.make_handler("mcp__get_x")

        async def scenario():
            # 这里一定有 running loop；同步 handler 仍要能返回
            return handler({"a": 2}, ExecCtx())

        self.assertEqual(asyncio.run(scenario()), "ok:get_x")
        self.assertEqual(session.calls, [("get_x", {"a": 2})])

    def test_27_run_coroutine_sync_both_paths(self):
        """附加：`run_coroutine_sync` 两条路径都能跑同一个协程工厂。"""
        async def echo(value):
            await asyncio.sleep(0)
            return value

        # 无 loop 路径
        self.assertEqual(run_coroutine_sync(lambda: echo(1)), 1)

        # 有 loop 路径（子线程里 asyncio.run）
        async def inside_loop():
            return run_coroutine_sync(lambda: echo(2))

        self.assertEqual(asyncio.run(inside_loop()), 2)

    def test_30_mirrored_text_collapsed(self):
        """附加：`structured_content` + 镜像文本 → 只留结构化结果。

        MCP 2.3 的 `@server.tool()` 默认开 structured_output，真回包长这样：
        `content=[TextContent(text='contents of /a.txt')]`,
        `structured_content={'result': 'contents of /a.txt'}`。
        两条是同一份数据，原样透传等于同一事实进 Prompt 两次。
        """
        bridge = MCPToolBridge(FakeSession(results={
            "mirror": CallToolResult(content=[TextContent(text="contents of /a.txt")],
                                     structured_content={"result": "contents of /a.txt"}),
            "prose": CallToolResult(content=[TextContent(text="另外注意：编码是 UTF-8")],
                                    structured_content={"result": "contents of /a.txt"}),
        }))
        self.assertEqual(bridge.call_sync("mirror"), {"result": "contents of /a.txt"})

        # 文本不是镜像（带额外说明）→ 两个都保留，别把 prose 吞掉
        kept = bridge.call_sync("prose")
        self.assertEqual(kept["structured"], {"result": "contents of /a.txt"})
        self.assertIn("UTF-8", kept["text"])

    def test_23_timeout_raises_timeout_error(self):
        """附加：超时抛 TimeoutError，而不是无限等（慢 server 会把 turn 挂死）。"""
        bridge = MCPToolBridge(FakeSession(delay=1.0), timeout_ms=50)
        with self.assertRaises(TimeoutError):
            bridge.call_sync("slow_tool")


# ══════════════════════════════════════════════════════════════
# ④ 注册与端到端
# ══════════════════════════════════════════════════════════════
class TestRegister(unittest.TestCase):
    def test_15_register_batch(self):
        """⑮ register_mcp_tools 批量注册进 ToolRegistry。"""
        session = FakeSession([mcp_tool("read_file"), mcp_tool("list_dir")])
        bridge = MCPToolBridge(session, server_name="fs")
        registry = ToolRegistry()

        manifests = register_mcp_tools(registry, asyncio.run(bridge.discover()), bridge)

        self.assertEqual([m.name for m in manifests], ["fs__read_file", "fs__list_dir"])
        self.assertEqual(sorted(registry.names()), ["fs__list_dir", "fs__read_file"])
        self.assertTrue(callable(registry.get("fs__read_file")[1]))

        # 再注册一次：同名跳过并告警，不炸整批
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            again = register_mcp_tools(registry, manifests, bridge)
        self.assertEqual(again, [])
        self.assertTrue(any("已注册" in str(w.message) for w in caught))
        self.assertEqual(len(registry), 2)

    def test_16_dispatcher_end_to_end(self):
        """⑯ 注册后的工具能被真 Dispatcher 调用（端到端）。"""
        session = FakeSession([
            mcp_tool("get_status", schema={
                "type": "object", "properties": {"service": {"type": "string"}},
                "required": ["service"]}),
            mcp_tool("create_item", schema={
                "type": "object", "properties": {"name": {"type": "string"}},
                "required": ["name"]}),
        ])
        bridge = MCPToolBridge(session, server_name="srv")
        registry = ToolRegistry()
        register_mcp_tools(registry, asyncio.run(bridge.discover()), bridge)
        dispatcher = Dispatcher(registry)

        # 只读工具：T0，直接放行；透传的 input_schema 真的进了 Dispatcher 的校验链
        bad = dispatcher.invoke("srv__get_status", {}, ExecCtx())
        self.assertEqual(bad.kind, "failed")
        self.assertIn("args.service: 缺少必填字段", bad.reason)

        ok = dispatcher.invoke("srv__get_status", {"service": "api"}, ExecCtx())
        self.assertEqual(ok.kind, "ok")
        self.assertEqual(ok.result.payload, "ok:get_status")
        self.assertEqual(ok.result.tool, "srv__get_status")

        # 写工具：T2，先要求审批
        args = {"name": "x"}
        pending = dispatcher.invoke("srv__create_item", args, ExecCtx())
        self.assertEqual(pending.kind, "needs_confirmation")
        self.assertIn("args_hash=", pending.reason)

        approved_ctx = ExecCtx(approvals=(Approval(
            tool="srv__create_item", args_hash=args_hash(args), approved=True),))
        done = dispatcher.invoke("srv__create_item", args, approved_ctx)
        self.assertEqual(done.kind, "ok")
        self.assertEqual(session.calls[-1], ("create_item", {"name": "x"}))

        # 工具自身报错 → ToolOutcome.failed（不是抛给调用方）
        session.errors.add("create_item")
        failed = dispatcher.invoke("srv__create_item", {"name": "y"},
                                   ExecCtx(approvals=(Approval(
                                       tool="srv__create_item",
                                       args_hash=args_hash({"name": "y"}),
                                       approved=True),)))
        self.assertEqual(failed.kind, "failed")
        self.assertIn("RuntimeError", failed.reason)

    def test_17_prefix_prevents_collision(self):
        """⑰ 两个 server 有同名 tool → 注册后不冲突。"""
        fs_session = FakeSession([mcp_tool("read_file")],
                                 results={"read_file": text_result("from-fs")})
        git_session = FakeSession([mcp_tool("read_file")],
                                  results={"read_file": text_result("from-git")})
        fs = MCPToolBridge(fs_session, server_name="fs")
        git = MCPToolBridge(git_session, server_name="git")

        registry = ToolRegistry()
        register_mcp_tools(registry, asyncio.run(fs.discover()), fs)
        register_mcp_tools(registry, asyncio.run(git.discover()), git)

        self.assertEqual(sorted(registry.names()), ["fs__read_file", "git__read_file"])
        self.assertEqual(registry.get("fs__read_file")[1]({}, ExecCtx()), "from-fs")
        self.assertEqual(registry.get("git__read_file")[1]({}, ExecCtx()), "from-git")


# ══════════════════════════════════════════════════════════════
# ⑤ 线程卫生
# ══════════════════════════════════════════════════════════════
class TestThreadHygiene(unittest.TestCase):
    def test_20_thread_count_stable(self):
        """⑳ 同步/异步桥不泄漏线程：多次调用后线程数稳定。"""
        session = FakeSession([mcp_tool("get_x")])
        bridge = MCPToolBridge(session)
        handler = bridge.make_handler("mcp__get_x")

        async def from_loop():
            return handler({}, ExecCtx())

        # 预热：让共享池把该建的线程建出来（懒建，第一次走 loop 路径才建）
        asyncio.run(from_loop())
        warm = len(bridge_threads())

        for _ in range(5):
            self.assertEqual(asyncio.run(from_loop()), "ok:get_x")
        after_loop_path = len(bridge_threads())

        # 无 loop 路径（asyncio.run）不应该再新增常驻线程
        for _ in range(5):
            handler({}, ExecCtx())
        after_plain_path = len(bridge_threads())

        self.assertEqual(after_loop_path, warm)
        self.assertEqual(after_plain_path, warm)
        self.assertLessEqual(after_plain_path, 4)
        self.assertEqual(len(session.calls), 11)


# ══════════════════════════════════════════════════════════════
# ⑥ 生态链路：MCP schema → ToolManifest → OpenAI function schema
# ══════════════════════════════════════════════════════════════
class TestEcosystemChain(unittest.TestCase):
    def test_31_manifest_feeds_openai_tool_schema(self):
        """附加：真 MCP schema 经桥之后，能被既有适配器原样发成 function schema。

        这一条是「接不上生态」这个问题的验收点：MCP server 的 `inputSchema`
        （含 SDK 自动加的 `title` 键）一路透传到模型看到的 parameters，不丢字段。
        """
        from adapters.openai_compat import OpenAICompatAdapter

        schema = {"type": "object",
                  "properties": {"path": {"type": "string", "title": "Path"}},
                  "required": ["path"], "title": "read_fileArguments"}
        bridge = MCPToolBridge(FakeSession([mcp_tool("read_file", "读文件", schema)]),
                              server_name="fs")
        manifest = asyncio.run(bridge.discover())[0]

        payload = OpenAICompatAdapter.tool_schema(manifest)
        self.assertEqual(payload["type"], "function")
        self.assertEqual(payload["function"]["name"], "fs__read_file")
        self.assertEqual(payload["function"]["parameters"], schema)


if __name__ == "__main__":
    unittest.main()
