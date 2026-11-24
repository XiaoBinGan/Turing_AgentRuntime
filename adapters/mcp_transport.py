"""adapters/mcp_transport.py — MCP 的**真传输层**：起 stdio 子进程 server。

## 这个文件补的是什么

`adapters/mcp_client.py` 里的 `MCPToolBridge` 已验证「MCP 工具 → ToolManifest」的翻译，
但它对会话的要求只有两条协程方法（`list_tools` / `call_tool`），所以测试里用
`FakeSession` 就够了 —— **进程内、无传输、无子进程**。

而生态里能拿来即用的 server（filesystem / git / postgres / slack）**全是子进程**，
走 `mcp.client.stdio`。这条路径不真跑一遍，等于「接口对得上、生态接不上」。
本模块把这条路径做出来并锁住它的失败模式（起不来 / 中途崩 / 停了还调 / 超时 / 泄漏）。

## 生命周期的难点

`Dispatcher` 是同步的（`fn(args, ctx)`），MCP SDK 全是协程，`stdio_client` 还是
**async context manager**（它内部起 anyio task group 管着子进程的 stdout 读循环、
stdin 写循环、退出清理）。三种错误做法：

  * 每次调用 `asyncio.run(...)` —— 等于每次调用都起停一次子进程。MCP 的
    `initialize` 握手比一次工具调用贵得多，而且每次重握手会丢掉 server 侧状态
    （filesystem server 的 cwd、postgres server 已建立的会话）。
  * 在调用线程里 `loop.run_until_complete` —— 把 loop 绑在调用线程上，而
    Dispatcher 的调用线程来自它自己的 `ThreadPoolExecutor`，线程会随调用换。
  * 只进不出（`loop.run_forever` 但不收尾）—— 子进程变孤儿，CI 里越跑越多。

所以：**一个连接 = 一个专用后台线程 + 一个常驻 loop**，所有会话操作通过
`asyncio.run_coroutine_threadsafe` 投递进去（`MCPToolBridge(loop=...)` 走的正是
`run_coroutine_sync` 的第 ① 条路径）。`start()` 阻塞到 `initialize` 握手完成，
`stop()` 阻塞到 session 关掉、子进程退出、线程结束。

## SDK 事实（以现场 `inspect` 为准，2.3.0）

  * `stdio_client(server: StdioServerParameters, errlog: TextIO = sys.stderr)`
    —— `@asynccontextmanager`，yield `(read_stream, write_stream)`，**不暴露进程句柄**。
  * `StdioServerParameters(command, args, env, cwd, encoding, ...)`，其中
    `env` 的语义是「合并到 `get_default_environment()` 之上」（白名单继承，不是全量继承）。
  * 内部 `await _create_platform_compatible_process(...)` 才是真正 `anyio.open_process`
    的地方。为了能回答「子进程到底死没死」（`poll()`），本模块在 `start()` 的窗口内
    **临时包一层**这个模块级函数以捕获 `Process` 句柄，随后立刻还原；拿不到就退回
    `pid` 存活探测。见 `_ProcessCapture`。
  * `ClientSession(read, write)` 是低层会话，必须自己 `await session.initialize()`。

## 失败模式一览（每条都有对应用例）

| 失败模式 | 行为 |
|---|---|
| `start()` 两次 | 第二次直接返回，**不重复起进程** |
| `stop()` 两次 | 第二次直接返回，不抛 |
| 子进程起不来（命令不存在 / 立即退出） | `MCPTransportError`，附 stderr 末尾 |
| 子进程握手无响应 | `MCPTransportError`（`startup_timeout_s` 到点，**不是挂死**） |
| 运行中子进程崩掉 | `call()` 抛 `MCPTransportError`，带 `exit=` 与 stderr 末尾 |
| `stop()` 之后再调 | `MCPTransportError`（明确说「已停止」，不是 `AttributeError`/`None`） |
| server 往 stderr 打日志 | 落到临时文件，`tail_stderr()` 可取；**绝不进 stdout**（stdout 是协议流） |
"""
from __future__ import annotations

import asyncio
import io
import logging
import os
import signal
import tempfile
import threading
import time
from dataclasses import replace
from typing import Any, Callable, Mapping, Sequence

from adapters.mcp_client import (
    MCPToolBridge,
    register_mcp_tools,
    run_coroutine_sync,
)
from contracts import RiskTier, SideEffect, ToolManifest

# mcp 是可选依赖：没装时模块仍可 import（只读校验 / 纯诊断场景），
# 一旦真去 start() 才明确报错。这样 `import adapters.mcp_transport` 不会因环境不同而炸。
try:                                             # pragma: no cover - 取决于环境
    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client
    MCP_TRANSPORT_AVAILABLE = True
except ImportError:                              # pragma: no cover
    ClientSession = None                         # type: ignore[assignment]
    StdioServerParameters = None                 # type: ignore[assignment]
    stdio_client = None                          # type: ignore[assignment]
    MCP_TRANSPORT_AVAILABLE = False

logger = logging.getLogger(__name__)

__all__ = [
    "MCPTransportError",
    "StdioMCPConnection",
    "attach_stdio_server",
    "STDIO_THREAD_PREFIX",
    "EXIT_UNKNOWN",
]

#: 后台 loop 线程名（用例按它数线程，别改名）
STDIO_THREAD_PREFIX = "mcp-stdio"

#: 进程已退出但拿不到真实退出码时的取值（SDK 内部回收了 Process 句柄才可能发生）
EXIT_UNKNOWN = -1

#: 从当前进程环境里继承给子进程的键。
#:
#: 为什么是白名单而不是 `dict(os.environ)`：MCP server 是**第三方代码**，
#: 全量继承等于把 `OPENAI_API_KEY` / `AWS_SECRET_ACCESS_KEY` / `GITHUB_TOKEN`
#: 一并交给它。SDK 自己也只继承一份白名单（`get_default_environment`），
#: 这里保持同一立场；需要额外变量的调用方显式传 `env=`。
_INHERIT_ENV_KEYS = (
    "PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "LC_ALL", "LC_CTYPE",
    "TMPDIR", "TEMP", "TMP", "PWD", "TERM",
    "PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV", "PYTHONIOENCODING",
    "SYSTEMROOT", "COMSPEC", "PATHEXT", "USERPROFILE", "APPDATA", "LOCALAPPDATA",
)

#: 副作用 → 默认风险档。与 `MCPToolBridge` 的规则一致：只读直接放行、
#: 写要审批（T2）、不可逆只能拒（T3）。
_SIDE_EFFECT_TIER: dict[SideEffect, RiskTier] = {
    SideEffect.READ_ONLY: RiskTier.T0_SAFE,
    SideEffect.MUTATING: RiskTier.T2_HIGH,
    SideEffect.IRREVERSIBLE: RiskTier.T3_CRITICAL,
}


class MCPTransportError(RuntimeError):
    """传输层失败（起不来 / 崩了 / 超时 / 已停止）。

    继承 `RuntimeError` 而不是自定义 Exception 基类：`Dispatcher` 的 handler 里
    任何异常都会被包成 `ToolOutcome.failed`，而调用方按 `RuntimeError` 兜底捕获的
    代码（`MCPToolBridge.call_sync` 就是这么写的）不会漏掉这一类错误。
    """


# ══════════════════════════════════════════════════════════════
# ① 环境变量
# ══════════════════════════════════════════════════════════════
def _build_env(
    extra: Mapping[str, str] | None = None,
    *,
    base: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """构造传给 MCP server 的环境变量：白名单继承 + 显式覆盖。

    `base` 默认 `os.environ`（测试里好注入，不用污染真实环境）。
    `extra` 里的键**无论值是否为空**都原样生效（空串是「清掉这个变量」的合法语义），
    并且允许覆盖继承来的同名键。

    返回值会作为 `StdioServerParameters.env` 传下去，SDK 还会再并一次它自己的白名单，
    所以这里多带几个键不会丢，只是这里多带的优先级更高。
    """
    source = os.environ if base is None else base
    env: dict[str, str] = {}
    for key in _INHERIT_ENV_KEYS:
        value = source.get(key)
        if value is not None:
            env[key] = str(value)
    for key, value in (extra or {}).items():
        env[str(key)] = "" if value is None else str(value)
    return env


# ══════════════════════════════════════════════════════════════
# ② 进程句柄捕获（拿 `poll()`）
# ══════════════════════════════════════════════════════════════
#: 起进程的窗口是**全局的**：`mcp.client.stdio` 里的 spawn 函数是模块级名字，
#: 同时只有一次 spawn 能被正确归属。所以 `start()` 的「装钩子 → 等到握手完成」
#: 这一段串行执行。取舍写在用例 8（start 幂等）旁边：并行起 server 的收益远小于
#: 「进程句柄挂到错误的连接上」的代价。
_SPAWN_LOCK = threading.RLock()


class _ProcessCapture:
    """临时包装 `mcp.client.stdio._create_platform_compatible_process`，抓 `Process`。

    为什么要抓：`stdio_client` 只 yield 两条流，**不 yield 进程**。而没有进程句柄就
    回答不了「`stop()` 之后子进程真的没了吗」「server 是不是早就崩了」这两个问题
    —— 只能靠猜或者靠 `ps` 扫（扫到别的连接的子进程就错得更离谱）。

    这是对 SDK 私有函数的依赖，所以做成**可选增强**：属性不存在 / 签名不兼容时
    静默降级，`poll()` 退回 pid 存活探测，`start()`/`stop()` 语义不变。
    """

    def __init__(self) -> None:
        self.process: Any = None
        self._module: Any = None
        self._original: Any = None

    def __enter__(self) -> "_ProcessCapture":
        try:
            import mcp.client.stdio as _stdio
        except ImportError:                      # pragma: no cover - 没装 SDK
            return self
        original = getattr(_stdio, "_create_platform_compatible_process", None)
        if original is None:                     # pragma: no cover - SDK 改内部结构
            logger.warning("mcp.client.stdio 没有 _create_platform_compatible_process，"
                           "退化为 pid 存活探测")
            return self
        capture = self

        async def _wrapper(*args: Any, **kwargs: Any) -> Any:
            proc = await original(*args, **kwargs)
            capture.process = proc
            return proc

        _stdio._create_platform_compatible_process = _wrapper
        self._module = _stdio
        self._original = original
        return self

    def __exit__(self, *_exc: Any) -> bool:
        if self._module is not None:
            try:
                self._module._create_platform_compatible_process = self._original
            except Exception:                    # pragma: no cover - 还原失败不该连带炸
                logger.warning("还原 mcp.client.stdio spawn 钩子失败", exc_info=True)
        self._module = None
        self._original = None
        return False


# ══════════════════════════════════════════════════════════════
# ③ stdio 连接
# ══════════════════════════════════════════════════════════════
class StdioMCPConnection:
    """起一个 MCP server 子进程，维持 `ClientSession`，提供同步调用入口。

    用法（**必须 stop**，推荐 with）：

        with StdioMCPConnection(command=sys.executable,
                                args=["tests/mcp_fixture_server.py"]) as conn:
            bridge = MCPToolBridge(conn.session, server_name="fix", loop=conn.loop)
            manifests = bridge.discover_sync()
            raw = conn.call("get_note", {"id": "a"})

    线程模型：`start()` 起一个 daemon 线程，里面 `loop.run_forever()`；bootstrap 协程
    在**同一个任务里**进入 `stdio_client` + `ClientSession`（anyio 的 cancel scope 要求
    进入/退出同任务），握手完成后挂在一个 `asyncio.Event` 上等 `stop()`。
    """

    def __init__(
        self,
        *,
        command: str,
        args: Sequence[str] | None = None,
        env: Mapping[str, str] | None = None,
        cwd: str | None = None,
        startup_timeout_s: float = 20.0,
        call_timeout_s: float = 30.0,
        stop_timeout_s: float = 15.0,
        stderr_max_bytes: int = 65536,
        name: str = "mcp",
    ) -> None:
        if not command:
            raise ValueError("command 不能为空")
        self.command = str(command)
        self.args = [str(a) for a in (args or ())]
        self.env = _build_env(env)
        self.cwd = cwd
        self.startup_timeout_s = float(startup_timeout_s)
        self.call_timeout_s = float(call_timeout_s)
        self.stop_timeout_s = float(stop_timeout_s)
        self.stderr_max_bytes = int(stderr_max_bytes)
        #: 只用在线程名与日志里，便于同时起多个 server 时区分
        self.name = str(name or "mcp")

        self._lock = threading.RLock()
        self._started = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._boot_task: Any = None
        self._session: Any = None
        self._stop_evt: asyncio.Event | None = None
        self._ready: threading.Event | None = None
        self._done: threading.Event | None = None
        self._process: Any = None
        self._pid: int | None = None
        self._boot_error: BaseException | None = None
        self._run_error: BaseException | None = None
        #: 诊断用：server 的 stderr 落到临时文件，stdout 是协议流不能碰
        self._stderr_path: str | None = None
        self._stderr_fp: Any = None
        self._errlog: Any = None

    # ── 生命周期 ────────────────────────────────────────────────
    def start(self) -> None:
        """起子进程并完成 MCP 握手（阻塞到 `initialize` 返回）。

        幂等：已经启动时直接返回，**不重复起进程**、不重复建线程。
        失败时抛 `MCPTransportError`，并把半成品全部收掉（不留孤儿进程/线程）。
        """
        if not MCP_TRANSPORT_AVAILABLE:       # pragma: no cover - 取决于环境
            raise MCPTransportError(
                "当前环境没有安装 mcp SDK，无法使用 stdio 传输（pip install mcp）")
        with self._lock:
            if self._started:
                return
            if self._thread is not None:
                # 上一次 start 失败/被强行打断留下的半成品：先收干净再起
                self._shutdown_locked(force=True)

            self._rotate_stderr()
            self._ready = threading.Event()
            self._done = threading.Event()
            self._boot_error = None
            self._run_error = None
            self._session = None
            self._process = None
            self._pid = None
            loop = asyncio.new_event_loop()
            self._loop = loop
            self._thread = threading.Thread(
                target=self._run_loop, name=f"{STDIO_THREAD_PREFIX}-{self.name}",
                daemon=True)
            self._thread.start()

            capture = _ProcessCapture()
            with _SPAWN_LOCK:                   # 见 _SPAWN_LOCK 处注释：起进程串行
                with capture:
                    ready = self._ready.wait(self.startup_timeout_s)
            self._process = capture.process
            self._pid = int(getattr(capture.process, "pid", 0) or 0) or None

            if not ready:
                detail = self._diagnose(
                    f"MCP server 启动超时（{self.startup_timeout_s:.1f}s 内没有完成握手）"
                    f"：{self.command} {' '.join(self.args)}")
                self._shutdown_locked(force=True)
                raise MCPTransportError(detail)

            if self._boot_error is not None:
                err = self._boot_error
                detail = self._diagnose(
                    f"MCP server 启动失败：{type(err).__name__}: {err}")
                self._shutdown_locked(force=True)
                raise MCPTransportError(detail) from err

            self._started = True

    def stop(self) -> None:
        """优雅停止：关 session → 关子进程 → 停线程。幂等，重复调用不报错。"""
        with self._lock:
            if self._thread is None and not self._started:
                return
            self._shutdown_locked()

    def __enter__(self) -> "StdioMCPConnection":
        self.start()
        return self

    def __exit__(self, *_exc: Any) -> bool:
        self.stop()
        return False

    # ── 同步入口（Dispatcher 走这里）────────────────────────────
    def tools(self) -> list[Any]:
        """同步拿工具清单（`mcp.types.Tool` 列表）。"""
        page = self._run(lambda session: session.list_tools(), "list_tools")
        raw = getattr(page, "tools", page)
        return list(raw) if isinstance(raw, (list, tuple)) else []

    def call(self, name: str, args: Mapping[str, Any] | None = None) -> Any:
        """同步调用一个 MCP 工具，返回**原始** `CallToolResult`。

        为什么返回原始结果而不是收口后的 payload：本类是传输层，职责是「把
        请求送达并把回包拿回来」。收口成 payload / 抛业务错误是 `MCPToolBridge`
        的事（`_payload`），两层各管一段，谁都不替对方做决定。
        """
        return self._run(lambda session: session.call_tool(str(name), dict(args or {})),
                         f"call_tool({name})")

    def wait_exit(self, timeout_s: float = 10.0) -> int | None:
        """等子进程退出，返回退出码（等不到返回 `None`）。幂等、可重复调用。"""
        deadline = time.monotonic() + max(0.0, float(timeout_s))
        while True:
            status = self.poll()
            if status is not None:
                return status
            if time.monotonic() >= deadline:
                return self.poll()
            time.sleep(0.02)

    # ── 状态 ────────────────────────────────────────────────────
    @property
    def session(self) -> Any:
        """当前 `ClientSession`。未启动 / 已停止 / 子进程已死 → `MCPTransportError`。"""
        return self._require_session()

    @property
    def loop(self) -> asyncio.AbstractEventLoop:
        """后台 loop（给 `MCPToolBridge(loop=...)` 用）。

        会话是**绑在这个 loop 上**的：从这里 `run_coroutine_threadsafe` 投递是安全的，
        在别的 loop 上 `await` 这个会话则是未定义行为。
        """
        if self._loop is None:
            raise MCPTransportError("MCP 连接未启动：loop 只在 start() 之后存在")
        return self._loop

    @property
    def pid(self) -> int | None:
        return self._pid

    @property
    def process(self) -> Any:
        """底层 `anyio` Process 句柄（SDK 内部结构变化时为 `None`）。"""
        return self._process

    @property
    def started(self) -> bool:
        return self._started

    def poll(self) -> int | None:
        """仿 `subprocess.Popen.poll()`：存活返回 `None`，已退出返回退出码。

        拿不到真实退出码时返回 `EXIT_UNKNOWN`（已退出这件事本身仍然是确定的）。
        """
        proc = self._process
        if proc is not None:
            code = getattr(proc, "returncode", None)
            if code is not None:
                return int(code)
            # 有句柄时以句柄为准：returncode 为 None 即存活
            if not self._pid_alive():
                return EXIT_UNKNOWN
            return None
        pid = self._pid
        if pid:
            return None if self._pid_alive() else EXIT_UNKNOWN
        return None

    def is_running(self) -> bool:
        """连接可用吗：已启动 且 子进程还在。"""
        return self._started and self.poll() is None

    def is_pid_alive(self) -> bool:
        """子进程还活着吗（`kill(pid, 0)` 探测，pid 未捕获时返回 False）。"""
        return self._pid_alive()

    def _pid_alive(self) -> bool:
        pid = self._pid
        if not pid:
            return False
        try:
            os.kill(int(pid), 0)
        except ProcessLookupError:
            return False
        except (PermissionError, OSError):       # pragma: no cover - 权限/平台差异
            return True
        return True

    # ── stderr 诊断 ─────────────────────────────────────────────
    def tail_stderr(self, limit_lines: int = 20) -> list[str]:
        """取 server stderr 的末尾若干行（诊断用）。

        stderr 被重定向到临时文件而不是继承父进程的 stderr：继承的话 server 的日志
        会直接落到跑 demo/测试的终端，看起来像我们自己打的 —— 定位时最难分辨的就是
        「这行错到底是 client 的还是 server 的」。
        """
        return self.stderr_text().splitlines()[-max(0, int(limit_lines)):]

    def stderr_text(self) -> str:
        """当前捕获到的 stderr 全文（最多读末尾 `stderr_max_bytes` 字节）。"""
        path = self._stderr_path
        if not path or not os.path.exists(path):
            return ""
        if self._errlog is not None:
            try:
                self._errlog.flush()
            except Exception:                    # pragma: no cover - 已关闭
                pass
        try:
            with open(path, "rb") as fh:
                fh.seek(0, os.SEEK_END)
                size = fh.tell()
                fh.seek(max(0, size - self.stderr_max_bytes))
                data = fh.read()
        except OSError:                          # pragma: no cover - 文件被外部删了
            return ""
        return data.decode("utf-8", errors="replace")

    def close_stderr(self) -> None:
        """关掉并删除 stderr 临时文件（`stop()` 之后诊断读完了再调）。"""
        wrapper, self._errlog = self._errlog, None
        if wrapper is not None:
            try:
                wrapper.close()                  # 关 wrapper 才会 flush 并释放 fd
            except Exception:                    # pragma: no cover
                pass
        fp, self._stderr_fp = self._stderr_fp, None
        path, self._stderr_path = self._stderr_path, None
        if fp is not None:
            try:
                fp.close()
            except Exception:                    # pragma: no cover
                pass
        if path:
            try:
                os.unlink(path)
            except OSError:                      # pragma: no cover
                pass

    def __del__(self):                            # pragma: no cover - 兜底
        try:
            self.close_stderr()
        except Exception:
            pass

    # ── 内部：线程 / loop ───────────────────────────────────────
    def _run_loop(self) -> None:
        """后台线程主体：建 bootstrap 任务 → 常驻 loop → 收尾时清干净。"""
        loop = self._loop
        assert loop is not None
        asyncio.set_event_loop(loop)
        try:
            self._boot_task = loop.create_task(self._bootstrap())
            loop.run_forever()
        finally:
            # 收尾：把残留任务取消掉再关 loop，否则 "Task was destroyed but it is
            # pending!" 会刷到 stderr，且 anyio 的清理任务可能拖住进程退出。
            try:
                pending = [t for t in asyncio.all_tasks(loop) if not t.done()]
                for task in pending:
                    task.cancel()
                if pending:
                    loop.run_until_complete(
                        asyncio.gather(*pending, return_exceptions=True))
            except Exception:                    # pragma: no cover - 收尾期不再抛
                logger.debug("清理残留任务时出错", exc_info=True)
            try:
                loop.close()
            except Exception:                    # pragma: no cover
                pass
            asyncio.set_event_loop(None)

    async def _bootstrap(self) -> None:
        """在**同一个任务**里进入 stdio_client + ClientSession，然后挂到 stop 事件上。

        必须同任务：`stdio_client` 内部是 `anyio.create_task_group()`，cancel scope
        要求「进入和退出在同一个任务里」，跨任务退出会抛 RuntimeError（任何把
        `__aenter__`/`__aexit__` 拆到两个任务里的「优雅实现」都会踩这个）。
        """
        stop_evt = asyncio.Event()
        self._stop_evt = stop_evt
        try:
            params = StdioServerParameters(
                command=self.command, args=list(self.args),
                env=dict(self.env) or None, cwd=self.cwd)
            async with stdio_client(params, errlog=self._errlog) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    self._session = session
                    self._ready.set()
                    await stop_evt.wait()
        except BaseException as exc:             # noqa: BLE001 — 启动/运行期错误都要留痕
            if self._ready.is_set():
                # 已经握手成功过 → 这是运行期的失联（server 崩了 / 流断了）
                self._run_error = exc
            else:
                self._boot_error = exc
        finally:
            self._session = None
            if self._ready is not None:
                self._ready.set()                # 唤醒 start()（失败路径也要唤醒）
            if self._done is not None:
                self._done.set()

    def _shutdown_locked(self, *, force: bool = False) -> None:
        """收尾：信号 stop → 等 bootstrap 退出 → 停 loop → join 线程。

        调用方必须持有 `self._lock`。顺序不能换：
          * 先 `_started = False`：让并发的 `call()` 立刻拿到「已停止」，
            而不是把一个请求投递到正在关闭的 loop 上、然后等超时；
          * 再发 stop 信号：bootstrap 只有收到它才会退出 `async with` 链，
            SDK 的 `__aexit__` 才会去 terminate 子进程；
          * 最后才 `loop.stop()`：反过来的话 bootstrap 没有机会跑清理，
            子进程就成孤儿（`ps` 里一排 fixture-server）。
        """
        self._started = False
        loop = self._loop
        task = self._boot_task
        thread = self._thread
        done = self._done

        if loop is not None and not loop.is_closed():
            def _signal() -> None:
                if self._stop_evt is not None:
                    self._stop_evt.set()
                elif task is not None:           # 还没进到 wait 那一行 → 直接取消
                    task.cancel()
            try:
                loop.call_soon_threadsafe(_signal)
            except RuntimeError:                 # pragma: no cover - loop 已关
                pass

        if force:
            self._kill_process()
        if done is not None:
            done.wait(10.0 if force else max(1.0, self.stop_timeout_s))
            if not done.is_set():
                # 正常 stop 没等到：进程可能卡在「stdout 写满」或握手中间，
                # 这时 SDK 的 terminate 也推进不了，只能自己动手。
                logger.warning(
                    "mcp-stdio:%s stop 未在 %.1fs 内完成，强杀子进程",
                    self.name, self.stop_timeout_s)
                self._kill_process()
                done.wait(5.0)

        if loop is not None and not loop.is_closed():
            try:
                loop.call_soon_threadsafe(loop.stop)
            except RuntimeError:                 # pragma: no cover
                pass
        if thread is not None and thread is not threading.current_thread():
            thread.join(max(1.0, self.stop_timeout_s))
            if thread.is_alive():                # pragma: no cover - 极端：线程卡在 C 层
                logger.warning("mcp-stdio:%s 后台线程未退出（daemon，不阻塞进程退出）",
                               self.name)

        self._session = None
        self._stop_evt = None
        self._boot_task = None
        self._loop = None
        self._thread = None
        self._ready = None
        self._done = None

    # ── 内部：调用 / 诊断 ───────────────────────────────────────
    def _require_session(self, what: str = "") -> Any:
        """取会话；不可用时抛**带上下文**的 `MCPTransportError`。

        `what` 是调用方的动作名（如 `call_tool(get_note)`）。没有它，报错只剩
        「连接已停止」—— 一次 turn 里可能调了十个工具，人根本不知道是哪个触发的。
        """
        label = f"（{what}）" if what else ""
        if not self._started or self._session is None:
            raise MCPTransportError(
                f"MCP 连接未启动或已停止{label}：先 start()；stop() 之后不能再调用")
        status = self.poll()
        if status is not None:
            raise MCPTransportError(self._diagnose(
                f"MCP server 子进程已退出（exit={status}），无法继续调用{label}"))
        return self._session

    def _run(self, factory: Callable[[Any], Any], what: str) -> Any:
        """把「吃 session 的协程工厂」投递到后台 loop 跑完，错误统一收口。"""
        session = self._require_session(what)
        loop = self._loop
        try:
            return run_coroutine_sync(
                lambda: factory(session), timeout_s=self.call_timeout_s, loop=loop)
        except MCPTransportError:
            raise
        except TimeoutError as exc:
            raise MCPTransportError(
                self._diagnose(f"{what} 超时（{self.call_timeout_s:.1f}s 内没有回包）")
            ) from exc
        except Exception as exc:                 # noqa: BLE001 — 协议/传输错误统一收口
            raise MCPTransportError(
                self._diagnose(f"{what} 失败：{type(exc).__name__}: {exc}")
            ) from exc

    def _diagnose(self, message: str) -> str:
        """错误信息里必须自带「进程活着吗 + server 最后说了什么」。

        不带这两样，报错就只剩 `ClosedResourceError` 这种没人能行动的文本。
        """
        parts = [message]
        status = self.poll()
        if status is not None:
            parts.append(f"进程已退出 exit={status}")
        else:
            parts.append("进程仍在运行")
        tail = self.tail_stderr(8)
        if tail:
            parts.append("server stderr 末尾：\n    " + "\n    ".join(tail))
        return "\n".join(parts)

    # ── 内部：stderr 文件 ───────────────────────────────────────
    def _rotate_stderr(self) -> None:
        """每次 start 换一个新的 stderr 临时文件（上一次的只用于上一次的诊断）。"""
        if self._errlog is not None:
            try:
                self._errlog.close()
            except Exception:                    # pragma: no cover
                pass
        old_path = self._stderr_path
        old_fp = self._stderr_fp
        fd, path = tempfile.mkstemp(prefix="mcp-stderr-", suffix=".log")
        self._stderr_path = path
        self._stderr_fp = os.fdopen(fd, "w+b")
        # 必须给 SDK 一个**有 fileno 的文本流**：asyncio/anyio 起子进程时 `stderr=`
        # 只接受 fd 或带 fileno 的文件对象，`io.StringIO` 之类会在 open_process 里
        # 直接 TypeError（这是本项目踩过的坑）。
        self._errlog = io.TextIOWrapper(
            self._stderr_fp, encoding="utf-8", errors="replace",
            write_through=True, line_buffering=True)
        if old_fp is not None:
            try:
                old_fp.close()
            except Exception:                    # pragma: no cover
                pass
        if old_path:
            try:
                os.unlink(old_path)
            except OSError:                      # pragma: no cover
                pass

    # ── 内部：杀进程 ────────────────────────────────────────────
    def _kill_process(self) -> None:
        """兜底杀进程（正常路径靠 SDK 的 terminate，这里只在它没做到时用）。"""
        pid = self._pid
        if not pid:
            return
        for sig in (signal.SIGTERM, signal.SIGKILL):
            if not self._pid_alive():
                return
            try:
                os.kill(int(pid), sig)
            except (ProcessLookupError, PermissionError, OSError):
                return
            time.sleep(0.05)


# ══════════════════════════════════════════════════════════════
# ④ 端到端：起 server → discover → 注册进 ToolRegistry
# ══════════════════════════════════════════════════════════════
def _apply_policy_override(
    manifest: ToolManifest,
    overrides: Mapping[str, Any],
) -> ToolManifest:
    """按名字把某几个工具的副作用/风险档改成调用方声明的值。

    为什么需要这个口子：`classify_side_effect()` 是**启发式**，它对
    `delete_<普通名词>` 只能判到 MUTATING/T2（「删除」是写操作，但这个对象
    不在高价值名词表里，所以不升级为不可逆）。实测 fixture 的 `delete_note`
    就是 T2 —— 也就是说**光靠名字猜，删数据会被降级成「可审批」而不是「拒绝」**。
    生产接入必须对这类工具显式声明，这个参数就是声明入口。

    `overrides` 的键可以是注册后的全名（`mcp__delete_note`）或 MCP 原始名
    （`delete_note`）；值可以是 `SideEffect`，也可以是 `(SideEffect, RiskTier)`。
    """
    forced = overrides.get(manifest.name)
    if forced is None:
        forced = overrides.get(manifest.name.split(MCPToolBridge.PREFIX_SEP, 1)[-1])
    if forced is None:
        return manifest
    side_raw, tier_raw = forced if isinstance(forced, tuple) else (forced, None)
    side = SideEffect(side_raw)
    tier = RiskTier(tier_raw) if tier_raw is not None else _SIDE_EFFECT_TIER[side]
    return replace(
        manifest,
        side_effect=side,
        risk_tier=tier,
        # 只读才幂等（与 MCPToolBridge 同一条规则：写操作重复执行就是真的重复副作用）
        idempotent=(side is SideEffect.READ_ONLY),
        required_scope=("tool.read" if side is SideEffect.READ_ONLY else "tool.write"),
    )


def attach_stdio_server(
    registry: Any,
    *,
    command: str,
    args: Sequence[str] | None = None,
    env: Mapping[str, str] | None = None,
    server_name: str = "mcp",
    policy_overrides: Mapping[str, Any] | None = None,
    **kw: Any,
) -> tuple[StdioMCPConnection, list[ToolManifest]]:
    """起 server → discover → 注册进 `ToolRegistry`，返回 `(conn, manifests)`。

    返回的 `conn` 是**调用方的责任**：用完必须 `conn.stop()`（或者用
    `with` 包住整段）。注册进 registry 的 handler 持有 `MCPToolBridge`，而 bridge
    持有会话 —— 会话断了之后那些 handler 也就不可用了，所以 registry 与 conn 的
    生命周期必须一起管：**先 stop 连接，再丢弃 registry**。

    参数：
      * `server_name`：工具名前缀（注册后是 `mcp__get_note` 这种形状，`__` 是为了
        兼容 OpenAI function name 的字符集）；
      * `policy_overrides`：见 `_apply_policy_override`（启发式高估/低估时的声明入口）；
      * 其余 `**kw` 原样给 `StdioMCPConnection`（`startup_timeout_s` / `cwd` / ...）。

    启动或 discover 失败时**自己收掉子进程**再抛，调用方不用再管清理。
    """
    conn = StdioMCPConnection(command=command, args=args, env=env,
                              name=server_name, **kw)
    conn.start()
    try:
        bridge = MCPToolBridge(
            conn.session,
            server_name=server_name,
            # 会话绑在 conn 的后台 loop 上：不传 loop 的话 bridge 会在调用线程里
            # 另起 asyncio.run，那是在**另一个 loop** 上 await 别人的会话。
            loop=conn.loop,
            timeout_ms=int(conn.call_timeout_s * 1000),
        )
        manifests = bridge.discover_sync()
        if policy_overrides:
            manifests = [_apply_policy_override(m, policy_overrides) for m in manifests]
        registered = register_mcp_tools(registry, manifests, bridge)
    except BaseException:
        conn.stop()
        raise
    return conn, registered
