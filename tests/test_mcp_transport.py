"""tests/test_mcp_transport.py — MCP **真传输层**（stdio 子进程）的用例。

这些用例**真起子进程**（`tests/mcp_fixture_server.py`），所以比
`tests/test_mcp_client.py` 慢一个数量级（每个用例 ~0.3s，全部 ~6s）。
值得慢：`FakeSession` 能验证「翻译对不对」，但验证不了

  * 子进程能不能被 `mcp.client.stdio` 拉起、握手能不能完成；
  * 子进程崩了 / 起不来 / 停了还调，报的是**明确错误**还是 `KeyError`/`None`/挂死；
  * `stop()` 之后进程是不是真的没了、线程是不是真的退了（CI 里最容易漏的一类）；
  * server 的 stderr 有没有污染协议流 / 我们的终端。

每个真起进程的用例都带超时（`startup_timeout_s` / `call_timeout_s` 都调小），
并且 `tearDown` 里无条件 `stop()` —— CI 挂死的代价远大于多用几秒。

命名：用例方法序号与任务书条目对齐（01~15 真子进程，16~18 不依赖子进程，
19+ 是补的）。
"""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import threading
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from adapters.mcp_client import MCPToolBridge, classify_side_effect
from adapters.mcp_transport import (
    EXIT_UNKNOWN,
    MCPTransportError,
    STDIO_THREAD_PREFIX,
    StdioMCPConnection,
    _build_env,
    attach_stdio_server,
)
from contracts import Approval, ExecCtx, RiskTier, SideEffect, args_hash
from plane.tool import Dispatcher, ToolRegistry

#: 夹具 server 的绝对路径（用绝对路径而不是 `-m tests.xxx`：
#: 后者依赖 cwd 与包结构，CI 里换个目录跑就找不到模块）
FIXTURE = str(Path(__file__).resolve().with_name("mcp_fixture_server.py"))
PY = sys.executable


# ══════════════════════════════════════════════════════════════
# 小工具
# ══════════════════════════════════════════════════════════════
def fixture_args(*extra: str) -> list[str]:
    return [FIXTURE, *extra]


def result_text(result) -> str:
    """把 `CallToolResult` 的文本块拼起来（本夹具只回文本 + 可能的 structured）。"""
    blocks = getattr(result, "content", None) or []
    return "".join(getattr(b, "text", "") or "" for b in blocks)


def result_json(result) -> dict:
    return json.loads(result_text(result))


def stdio_threads() -> list[str]:
    """当前存活的 stdio 后台线程名（线程泄漏用例按它数）。"""
    return [t.name for t in threading.enumerate()
            if t.name.startswith(STDIO_THREAD_PREFIX)]


def child_pids() -> set[int]:
    """当前进程的直接子进程 pid 集合（子进程泄漏用例按它数）。

    注意必须排除 `ps` **自己**：`ps` 是我们起的子进程，ppid 就是本进程，
    把它算进来会让「每次调用后子进程集合都变」——第一版就在这儿误报过一次。
    """
    try:
        out = subprocess.run(["ps", "-eo", "pid=,ppid=,comm="], capture_output=True,
                             text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):        # pragma: no cover - 平台差异
        return set()
    mine = os.getpid()
    pids: set[int] = set()
    for line in out.splitlines():
        parts = line.split(None, 2)
        if len(parts) < 2 or not parts[1].isdigit():
            continue
        comm = os.path.basename(parts[2].strip()) if len(parts) > 2 else ""
        if comm == "ps":
            continue
        if int(parts[1]) == mine:
            pids.add(int(parts[0]))
    return pids


class TransportCase(unittest.TestCase):
    """公共夹具：每例的连接都在 tearDown 里收掉，且**永不让异常穿透**。"""

    def setUp(self) -> None:
        self._conns: list[StdioMCPConnection] = []

    def tearDown(self) -> None:
        for conn in self._conns:
            try:
                conn.stop()
            except Exception:                            # pragma: no cover
                pass
            try:
                conn.close_stderr()
            except Exception:                            # pragma: no cover
                pass

    def make(self, **kw) -> StdioMCPConnection:
        """登记 + 返回连接（不 start，由用例决定何时 start）。"""
        kw.setdefault("startup_timeout_s", 15.0)
        kw.setdefault("call_timeout_s", 20.0)
        conn = StdioMCPConnection(command=PY, args=fixture_args(), **kw)
        self._conns.append(conn)
        return conn

    def started(self, **kw) -> StdioMCPConnection:
        conn = self.make(**kw)
        conn.start()
        return conn


# ══════════════════════════════════════════════════════════════
# ①~③ 真子进程：基本能力
# ══════════════════════════════════════════════════════════════
class TestRealStdioServer(TransportCase):
    def test_01_tools_from_real_server(self):
        """① 真 server 起的来：`tools()` 拿到 4 个工具，且 schema 是 server 声明的。"""
        conn = self.started()
        names = sorted(t.name for t in conn.tools())
        self.assertEqual(names, ["create_note", "delete_note", "get_note", "list_notes"])
        by_name = {t.name: t for t in conn.tools()}
        self.assertEqual(sorted(by_name["get_note"].input_schema["properties"]), ["id"])
        self.assertEqual(by_name["get_note"].input_schema.get("required"), ["id"])
        self.assertEqual(sorted(by_name["create_note"].input_schema["properties"]), ["text"])
        # 空参数工具：properties 为空对象（不是 None，否则 Dispatcher 会跳过校验）
        self.assertEqual(by_name["list_notes"].input_schema.get("properties"), {})
        self.assertIsNone(conn.poll(), "server 应该还活着")

    def test_02_call_get_note(self):
        """② `call()` 真走一次 stdio 往返，内容对得上。"""
        conn = self.started()
        payload = result_json(conn.call("get_note", {"id": "a"}))
        self.assertEqual(payload, {"id": "a", "text": "note-a"})

    def test_03_call_create_note(self):
        """③ 写工具：子进程**有状态** —— create 之后 list 能看到（不是无状态假回包）。"""
        conn = self.started()
        created = result_json(conn.call("create_note", {"text": "hello"}))
        self.assertEqual(created["text"], "hello")
        self.assertTrue(created["id"])

        listed = conn.call("list_notes", {})
        items = (getattr(listed, "structured_content", None) or {}).get("result")
        if items is None:                        # 旧版回包只有文本 → 自己解析
            items = json.loads(result_text(listed))
        self.assertEqual([i["id"] for i in items], [created["id"]])
        self.assertEqual(items[0]["text"], "hello")


# ══════════════════════════════════════════════════════════════
# ④~⑥ 端到端：真 server + 真 Dispatcher
# ══════════════════════════════════════════════════════════════
class TestDispatcherEndToEnd(TransportCase):
    def attach(self, **kw):
        registry = ToolRegistry()
        kw.setdefault("startup_timeout_s", 15.0)
        conn, manifests = attach_stdio_server(
            registry, command=PY, args=fixture_args(), server_name="mcp", **kw)
        self._conns.append(conn)
        return conn, registry, manifests

    def test_04_dispatcher_read_only_ok(self):
        """④ 只读工具端到端：`Dispatcher.invoke("mcp__get_note")` → ok，payload 是 server 的返回。"""
        conn, registry, manifests = self.attach()
        dispatcher = Dispatcher(registry)

        get_note = next(m for m in manifests if m.name == "mcp__get_note")
        self.assertIs(get_note.side_effect, SideEffect.READ_ONLY)
        self.assertIs(get_note.risk_tier, RiskTier.T0_SAFE)
        self.assertTrue(get_note.idempotent)

        outcome = dispatcher.invoke("mcp__get_note", {"id": "z"}, ExecCtx())
        self.assertEqual(outcome.kind, "ok", outcome.reason)
        self.assertEqual(outcome.result.tool, "mcp__get_note")
        self.assertEqual(json.loads(outcome.result.payload), {"id": "z", "text": "note-z"})

        # 入参契约真的生效（透明传递 server 的 inputSchema，不是放过任意参数）
        bad = dispatcher.invoke("mcp__get_note", {}, ExecCtx())
        self.assertEqual(bad.kind, "failed")
        self.assertIn("args.id", bad.reason)

    def test_05_dispatcher_write_needs_confirmation_then_approved(self):
        """⑤ 写工具端到端：`create_note` 判 MUTATING/T2 → 无审批 `needs_confirmation`，
        带审批 → 真写进子进程。"""
        conn, registry, manifests = self.attach()
        dispatcher = Dispatcher(registry)

        create = next(m for m in manifests if m.name == "mcp__create_note")
        self.assertIs(create.side_effect, SideEffect.MUTATING)
        self.assertIs(create.risk_tier, RiskTier.T2_HIGH)
        self.assertFalse(create.idempotent, "写操作不能标幂等（重复执行=重复副作用）")
        self.assertEqual(create.required_scope, "tool.write")

        args = {"text": "from-dispatcher"}
        pending = dispatcher.invoke("mcp__create_note", args, ExecCtx())
        self.assertEqual(pending.kind, "needs_confirmation", pending.reason)
        self.assertIn("args_hash=", pending.reason)

        approved = ExecCtx(approvals=(Approval(
            tool="mcp__create_note", args_hash=args_hash(args), approved=True),))
        done = dispatcher.invoke("mcp__create_note", args, approved)
        self.assertEqual(done.kind, "ok", done.reason)
        self.assertEqual(json.loads(done.result.payload)["text"], "from-dispatcher")

        # 审批是「一次性」的：换个 turn 但同参数仍要再批（args_hash 相同也算，
        # 因为 approvals 是 ctx 上的，不是全局的）
        self.assertEqual(dispatcher.invoke("mcp__create_note", args, ExecCtx()).kind,
                         "needs_confirmation")

    def test_06a_delete_note_heuristic_is_only_t2(self):
        """⑥a **真行为记录**：`delete_note` 被启发式判成 MUTATING/T2（不是 T3）。

        `classify_side_effect` 的规则是「破坏性动词 + 高价值名词 → 不可逆」，
        而 `note` 不在高价值名词表里，所以「删掉一篇笔记」只落到「要审批」。
        这不是 bug（删一篇笔记确实不像删库），但它说明**接入生产时必须显式声明**：
        猜出来的档位不能当成治理结论。见 ⑥b。
        """
        side, tier = classify_side_effect("delete_note",
                                          {"type": "object", "properties": {"id": {"type": "string"}}})
        self.assertIs(side, SideEffect.MUTATING)
        self.assertIs(tier, RiskTier.T2_HIGH)

    def test_06b_delete_note_declared_irreversible_denied(self):
        """⑥b 显式声明不可逆 → T3 → `denied`（T3 连审批都不接受，只能拒）。"""
        conn, registry, manifests = self.attach(
            policy_overrides={"delete_note": (SideEffect.IRREVERSIBLE, RiskTier.T3_CRITICAL)})
        dispatcher = Dispatcher(registry)

        delete = next(m for m in manifests if m.name == "mcp__delete_note")
        self.assertIs(delete.side_effect, SideEffect.IRREVERSIBLE)
        self.assertIs(delete.risk_tier, RiskTier.T3_CRITICAL)
        self.assertFalse(delete.idempotent)

        args = {"id": "1"}
        denied = dispatcher.invoke("mcp__delete_note", args, ExecCtx())
        self.assertEqual(denied.kind, "denied", denied.reason)
        self.assertIn("critical", denied.reason.lower())

        # 就算硬塞一条「已批准」也进不去：T3 的闸门在审批之前
        forced = ExecCtx(approvals=(Approval(
            tool="mcp__delete_note", args_hash=args_hash(args), approved=True),))
        self.assertEqual(dispatcher.invoke("mcp__delete_note", args, forced).kind, "denied")


# ══════════════════════════════════════════════════════════════
# ⑦~⑮ 生命周期 / 故障模式
# ══════════════════════════════════════════════════════════════
class TestLifecycle(TransportCase):
    def test_07_process_gone_after_stop(self):
        """⑦ `stop()` 之后：子进程真的没了、线程真的退了、会话不可再用。"""
        conn = self.started()
        pid = conn.pid
        self.assertIsNotNone(pid)
        self.assertTrue(conn.is_pid_alive())
        self.assertEqual(conn.call("get_note", {"id": "x"}).is_error, False)

        conn.stop()

        self.assertIsNotNone(conn.poll(), "stop() 之后 poll() 必须是「已退出」")
        self.assertFalse(conn.is_pid_alive(), f"子进程 {pid} 还活着（资源泄漏）")
        self.assertEqual(conn.wait_exit(5.0) is not None, True)
        self.assertEqual(stdio_threads(), [], "stop() 之后不该有 mcp-stdio 线程")
        self.assertFalse(conn.started)

    def test_08_start_idempotent(self):
        """⑧ `start()` 两次：不重复起进程、不重复建线程。"""
        conn = self.started()
        pid = conn.pid
        threads_before = stdio_threads()
        self.assertEqual(len(threads_before), 1)

        conn.start()                    # 第二次
        conn.start()                    # 第三次

        self.assertEqual(conn.pid, pid, "重复 start() 起了新进程（旧进程会变孤儿）")
        self.assertEqual(stdio_threads(), threads_before)
        self.assertEqual(result_json(conn.call("get_note", {"id": "a"}))["text"], "note-a")

    def test_09_stop_idempotent(self):
        """⑨ `stop()` 两次：第二次不抛。未 start 的 `stop()` 也不抛。"""
        conn = self.started()
        conn.stop()
        conn.stop()                     # 不该抛
        conn.stop()

        never_started = self.make()
        never_started.stop()            # 没 start 就 stop：幂等
        never_started.stop()

    def test_10_startup_timeout(self):
        """⑩ 子进程起得来但永不握手 → `MCPTransportError`（**不是挂死**），且进程被杀。"""
        conn = StdioMCPConnection(
            command=PY, args=["-c", "import time; time.sleep(600)"],
            startup_timeout_s=1.0, call_timeout_s=2.0, name="hang")
        self._conns.append(conn)

        started_at = time.monotonic()
        with self.assertRaises(MCPTransportError) as caught:
            conn.start()
        elapsed = time.monotonic() - started_at

        self.assertLess(elapsed, 15.0, "超时保护没生效（快挂死了）")
        self.assertIn("启动超时", str(caught.exception))
        # 起不来的进程必须被收掉，否则每次失败都留一个 sleep 600 的进程
        self.assertFalse(conn.is_pid_alive(), "超时后子进程还在")
        self.assertEqual(stdio_threads(), [])

    def test_11_command_not_found(self):
        """⑪ 命令不存在 → 明确错误（带异常类型），不是静默 None。"""
        conn = StdioMCPConnection(command="/nonexistent/mcp-server-xyz",
                                  startup_timeout_s=5.0, name="noent")
        self._conns.append(conn)

        with self.assertRaises(MCPTransportError) as caught:
            conn.start()
        message = str(caught.exception)
        self.assertIn("启动失败", message)
        self.assertIn("FileNotFoundError", message)
        self.assertFalse(conn.started)

    def test_12_server_crash_then_call_errors(self):
        """⑫ 运行中 server 崩掉 → `call()` 明确报「进程已退出 exit=9」+ stderr 末尾。"""
        # **按次数触发，不按时间**：`DIE_AFTER_S=0.4` 在整套件跑时（CPU 争抢）
        # 会让"崩之前的那次调用"随机赶不上 —— 实测 6 次全套件跑挂 1 次。
        # 第 2 次调用后崩：第 1 次一定成功、第 2 次一定失败，无时间窗口。
        conn = self.started(env={"MCP_FIXTURE_DIE_AFTER_CALLS": "2"},
                            call_timeout_s=3.0)
        self.assertIsNone(conn.poll())
        conn.call("get_note", {"id": "before"})     # 崩之前是好的（第 1 次调用）

        # 第 2 次调用**触发**崩溃（`DIE_AFTER_CALLS=2`）——所以"调用 → 崩"是同一步。
        # 时间触发版本是"等它自己崩"，按次数版本是"这次调用把它弄崩"：
        # 后者没有时间窗口，全套件跑时不会因为 CPU 争抢而随机失败。
        with self.assertRaises(MCPTransportError) as caught:
            conn.call("get_note", {"id": "after"})
        self.assertIsNotNone(conn.wait_exit(10.0), "server 应该已经崩了")
        message = str(caught.exception)
        self.assertIn("exit=", message)
        self.assertIn("get_note", message)
        # 崩溃现场要能诊断：stderr 末尾必须带出来
        self.assertIn("主动崩溃", message)

    def test_13_call_after_stop_raises(self):
        """⑬ `stop()` 之后再调：`call()` / `tools()` / `session` 都报「已停止」。"""
        conn = self.started()
        conn.stop()

        for action in (lambda: conn.call("get_note", {"id": "a"}),
                       lambda: conn.tools(),
                       lambda: conn.session):
            with self.assertRaises(MCPTransportError) as caught:
                action()
            self.assertIn("停止", str(caught.exception))

    def test_14_fifty_calls_no_leak(self):
        """⑭ 连续 50 次调用：线程数 / 子进程数稳定，不随调用次数漂移。"""
        conn = self.started()
        threads_baseline = sorted(stdio_threads())
        children_baseline = child_pids()

        for index in range(50):
            payload = result_json(conn.call("get_note", {"id": str(index)}))
            self.assertEqual(payload["text"], f"note-{index}")

        self.assertEqual(sorted(stdio_threads()), threads_baseline,
                         f"线程数漂移：{threads_baseline} → {sorted(stdio_threads())}")
        self.assertEqual(child_pids(), children_baseline,
                         "子进程数漂移（每次调用起了一个 server？）")
        self.assertIsNone(conn.poll())

    def test_15_stderr_captured_not_leaked_to_stdout(self):
        """⑮ server 的 stderr 被捕获（不落到我们的 stdout），且能取出来诊断。"""
        marker = "FIXTURE-STDERR-MARKER-0x9f"
        conn = self.make(env={"MCP_FIXTURE_STDERR_MARKER": marker,
                              "MCP_FIXTURE_LABEL": "L-"})
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            conn.start()
            conn.call("get_note", {"id": "a"})
        captured = buffer.getvalue()

        self.assertNotIn(marker, captured,
                         "server 的 stderr 落到我们的 stdout 了（会污染协议/日志）")
        self.assertIn(marker, conn.stderr_text(), "stderr 没被捕获，诊断就没料可用")
        self.assertIn("fixture-server: starting", "\n".join(conn.tail_stderr(5)))
        # tail 只回末尾 N 行（长日志不会把错误信息挤出报错窗口）
        self.assertEqual(len(conn.tail_stderr(1)), 1)


# ══════════════════════════════════════════════════════════════
# ⑯~⑱ 不依赖子进程（快）
# ══════════════════════════════════════════════════════════════
class TestTransportUnits(unittest.TestCase):
    def test_16_error_type(self):
        """⑯ `MCPTransportError` 是 `RuntimeError`：调用方按 RuntimeError 兜底也能接住。"""
        self.assertTrue(issubclass(MCPTransportError, RuntimeError))
        self.assertEqual(MCPTransportError.__mro__[1], RuntimeError)
        with self.assertRaises(RuntimeError):
            raise MCPTransportError("x")

    def test_17_env_passthrough(self):
        """⑰ `_build_env`：白名单继承 + 显式覆盖 + **不**全量继承（不把密钥递给第三方 server）。"""
        base = {"PATH": "/usr/bin", "HOME": "/home/u",
                "AWS_SECRET_ACCESS_KEY": "should-not-leak",
                "OPENAI_API_KEY": "should-not-leak-either"}
        env = _build_env({"MCP_FIXTURE_LABEL": "L-", "PATH": "/custom/bin"}, base=base)

        self.assertEqual(env["MCP_FIXTURE_LABEL"], "L-")
        self.assertEqual(env["HOME"], "/home/u")
        self.assertEqual(env["PATH"], "/custom/bin", "显式传入必须覆盖继承值")
        self.assertNotIn("AWS_SECRET_ACCESS_KEY", env)
        self.assertNotIn("OPENAI_API_KEY", env)
        # 空串是「显式清掉」的合法语义，不能被吞成「未设置」
        self.assertEqual(_build_env({"X": ""}, base=base)["X"], "")
        self.assertEqual(_build_env(None, base={}), {})

    def test_18_manifests_are_prefixed(self):
        """⑱ 端到端注册的名字带 server 前缀（`mcp__xxx`），前缀可自定义。"""
        registry = ToolRegistry()
        conn, manifests = attach_stdio_server(
            registry, command=PY, args=fixture_args(), server_name="notes",
            startup_timeout_s=15.0)
        try:
            names = sorted(m.name for m in manifests)
            self.assertEqual(names, ["notes__create_note", "notes__delete_note",
                                     "notes__get_note", "notes__list_notes"])
            self.assertEqual(sorted(registry.names()), names)
            # 未注册的工具名不能凭空可用
            self.assertFalse("get_note" in registry)
        finally:
            conn.stop()
            conn.close_stderr()


# ══════════════════════════════════════════════════════════════
# ⑲+ 补充：真子进程下更容易暴露的边角
# ══════════════════════════════════════════════════════════════
class TestTransportExtras(TransportCase):
    def test_19_env_reaches_subprocess(self):
        """⑲ 环境变量真透传到子进程（不是只到 `params.env` 就完事）。"""
        conn = self.started(env={"MCP_FIXTURE_LABEL": "ENV-"})
        created = result_json(conn.call("create_note", {"text": "t"}))
        self.assertTrue(created["id"].startswith("ENV-"), created)
        self.assertIn("label='ENV-'", conn.stderr_text())

    def test_20_context_manager_stops_on_exit(self):
        """⑳ `with` 退出即停：出块之后进程已死（调用方忘了 stop 也不会留孤儿）。"""
        with self.make() as conn:
            self.assertIsNone(conn.poll())
            self.assertEqual(result_json(conn.call("get_note", {"id": "c"}))["text"], "note-c")
        self.assertIsNotNone(conn.poll())
        self.assertFalse(conn.is_pid_alive())

    def test_21_concurrent_calls_share_one_loop(self):
        """㉑ 多线程并发调用同一个连接：全靠一个后台 loop 投递，结果不串。"""
        conn = self.started()
        errors: list[str] = []
        results: dict[str, str] = {}

        def worker(index: int) -> None:
            try:
                payload = result_json(conn.call("get_note", {"id": f"t{index}"}))
                results[f"t{index}"] = payload["text"]
            except Exception as exc:                     # noqa: BLE001
                errors.append(f"{type(exc).__name__}: {exc}")

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
            self.assertFalse(thread.is_alive(), "并发调用把线程挂住了")

        self.assertEqual(errors, [])
        self.assertEqual(results, {f"t{i}": f"note-t{i}" for i in range(8)})
        self.assertEqual(len(stdio_threads()), 1, "并发调用不该多起 loop 线程")

    def test_22_bridge_uses_connection_loop(self):
        """㉒ `MCPToolBridge(loop=conn.loop)` 能直接复用这个连接（不必每次 asyncio.run）。"""
        conn = self.started()
        bridge = MCPToolBridge(conn.session, server_name="fix", loop=conn.loop)
        manifests = bridge.discover_sync()
        self.assertEqual(len(manifests), 4)
        payload = bridge.call_sync("fix__get_note", {"id": "q"})
        self.assertEqual(json.loads(payload), {"id": "q", "text": "note-q"})
        # 同一个连接被两个消费方（bridge + conn.call）共用，互不打断
        self.assertEqual(result_json(conn.call("get_note", {"id": "r"}))["text"], "note-r")

    def test_24_two_connections_keep_distinct_processes(self):
        """㉔ 同时起两个 server：进程句柄各归各，停一个不影响另一个。

        这条盯的是实现里最脆的一处：`poll()` 依赖临时包装 SDK 的 spawn 函数来拿
        `Process`。包装是**模块级**的，两次启动要是抢同一个钩子，句柄就会挂到
        错误的连接上 —— 表现为「停了 A，B 显示已退出」或者「A 的 poll 永远是 None」。
        """
        a = self.started(env={"MCP_FIXTURE_LABEL": "A-"}, name="conn-a")
        b = self.started(env={"MCP_FIXTURE_LABEL": "B-"}, name="conn-b")

        self.assertIsNotNone(a.pid)
        self.assertIsNotNone(b.pid)
        self.assertNotEqual(a.pid, b.pid, "两个连接拿到了同一个进程句柄")
        self.assertIsNone(a.poll())
        self.assertIsNone(b.poll())

        self.assertEqual(result_json(a.call("get_note", {"id": "x"}))["text"], "note-x")
        self.assertEqual(result_json(b.call("get_note", {"id": "y"}))["text"], "note-y")

        a.stop()
        self.assertIsNotNone(a.poll())
        self.assertFalse(a.is_pid_alive())
        self.assertIsNone(b.poll(), "停 A 把 B 的进程也算成退出了")
        self.assertTrue(b.is_pid_alive())
        self.assertEqual(result_json(b.call("get_note", {"id": "z"}))["text"], "note-z")

    def test_23_exit_unknown_constant(self):
        """㉓ `poll()` 的三态语义：存活 None / 退出有码 / 退出无码 `EXIT_UNKNOWN`。"""
        conn = self.started()
        self.assertIsNone(conn.poll())
        conn.stop()
        status = conn.poll()
        self.assertIsNotNone(status)
        self.assertIsInstance(status, int)
        self.assertNotEqual(status, EXIT_UNKNOWN, "正常停止应该拿到真实退出码 0")


if __name__ == "__main__":
    unittest.main()
