"""adapters/mcp_client.py — MCP 接入层（官方 SDK → Runtime 的 ToolManifest）。

## 为什么是「接入层」而不是「换掉内部类型」

`contracts.ToolManifest` 是已经冻结的公开契约：`Dispatcher` 的权限 / 审批 / 幂等 /
限流 / 预算 / 脱敏全部挂在它的字段上（`side_effect` / `risk_tier` / `required_scope` /
`idempotent` / `timeout_ms` / `max_result_bytes` / `per_turn_limit` / `input_schema`）。
MCP 的 `Tool` 只有 `name / description / input_schema / output_schema / annotations`，
**没有任何一项能表达「这次调用要不要人工审批」**。

所以分工是：
  * MCP 负责「有哪些工具、参数 schema 是什么」（生态互通：filesystem / git /
    postgres / slack 的 server 拿来即用）；
  * Runtime 负责「这次调用有多危险、要不要审批」（治理语义只能自己声明）。

`ToolManifest` 保持原样，本模块只做**翻译**。挂错了方向就是「为了接生态把治理拆了」。

## MCP 2.3.0 的实际 API（以现场 `inspect.signature` 为准，与最初设想有差异）

  * `ClientSession(read_stream, write_stream, ...)`：`async initialize()` /
    `async list_tools(*, params=None) -> ListToolsResult` /
    `async call_tool(name, arguments=None, read_timeout_seconds=None, ...)`。
    这是**低层**会话，必须自己驱动 transport（`stdio_client` / `streamable_http_client`）。
  * `mcp.Client(...)`：高层的 dataclass 客户端，接受 URL 字符串 /
    `StdioServerParameters` / `Transport`，2.3 还接受 `MCPServer` 实例走进程内连接
    —— 真 server 的冒烟验证就是用它做的。
  * **两个客户端的翻页签名不一样**：`ClientSession.list_tools(*, params=None)`，
    而 `Client.list_tools(*, cursor=None)`。见 `_cursor_kwargs()`：两种都试，
    否则在另一种客户端上会「永远只拿到第一页」且毫无报错。
  * `call_tool` 两者同形：`call_tool(name, arguments=None, ...)`，所以本模块只按
    鸭子类型依赖 `list_tools()` / `call_tool(name, arguments)` 两个协程。
  * `types.Tool` 的字段是 **`input_schema`（snake_case）**，构造时两种写法都收
    （`model_config.alias_generator=to_camel` + `populate_by_name=True`），所以线上
    wire 格式的 `inputSchema` 也认。**这点和「直接读 tool.inputSchema」的设想不同**，
    见 `_attr_any()`：两种都要读，因为 off-wire 的 dict 形状（老版本回包、测试桩、
    缓存下来的 JSON）里只有 camelCase。
  * `types.CallToolResult`：`content: list[ContentBlock]`、`structured_content`、
    `is_error: bool`。注意 **工具自身的业务错误是 `is_error=True` 的「正常回包」**，
    不是协议级异常 —— 两种都要处理，否则模型会把「权限不足」当成空结果继续瞎猜。
  * 2.3 起 server 端默认开 structured output，真回包会**同时**带
    `structured_content` 和一段镜像文本。原样透传 = 同一事实进 Prompt 两次，
    所以 `_payload()` 会折叠镜像文本（`_is_mirror()` 只认强形态，不误吞 prose）。
  * `FastMCP` 在 2.x 已更名为 `MCPServer`（`mcp.server.mcpserver`）；本模块不用
    server 端 API，只在测试/冒烟里用到它，不影响接入层。

## 同步 / 异步桥（Dispatcher 是同步的，MCP SDK 是异步的）

`Dispatcher` 在 `ThreadPoolExecutor` 的 worker 里同步调 `fn(args, ctx)`；MCP 全是协程。
直接在已有事件循环里 `asyncio.run` 会直接炸（`asyncio.run() cannot be called from a
running event loop`），所以三条路径：

  1. 显式给了 `loop=`（真实 `ClientSession` 的正确用法：会话是绑在某个 loop 上的）
     → `asyncio.run_coroutine_threadsafe`，**不新建 loop、不新建线程**；
  2. 当前线程没有运行中的 loop（Dispatcher 的主路径）
     → `asyncio.run`，同样不新建常驻线程；
  3. 当前线程有运行中的 loop（有人在 async 函数里同步调 handler）
     → 交给**进程级共享**的线程池，在子线程里 `asyncio.run`。
        为什么不每次 `threading.Thread`：每次新建会漏线程，长跑进程里线程数只增不减。

路径 3 会阻塞当前 loop（同步语义的必然代价），注释里写清楚，别当特性用。
"""
from __future__ import annotations

import asyncio
import concurrent.futures as _futures
import inspect
import json
import logging
import re
import threading
import warnings
from typing import Any, Callable, Iterable, Sequence

from contracts import ExecCtx, RiskTier, SideEffect, ToolManifest

# mcp 是可选依赖：本模块的翻译逻辑不依赖 SDK 类型（全走鸭子类型），
# 所以没装 mcp 也要能 import（测试桩、离线校验场景）。装了只是多一个能力探测。
try:                                              # pragma: no cover - 取决于环境
    import mcp as _mcp                            # noqa: F401  —— 仅用于能力探测
    MCP_AVAILABLE = True
except ImportError:                               # pragma: no cover
    _mcp = None                                   # type: ignore[assignment]
    MCP_AVAILABLE = False

logger = logging.getLogger(__name__)

__all__ = [
    "MCPToolBridge",
    "classify_side_effect",
    "register_mcp_tools",
    "run_coroutine_sync",
    "MCP_BRIDGE_THREAD_PREFIX",
]

#: 共享线程池的线程名前缀（线程泄漏测试按它数线程）
MCP_BRIDGE_THREAD_PREFIX = "mcp-bridge"


# ══════════════════════════════════════════════════════════════
# ① 启发式副作用判定
# ══════════════════════════════════════════════════════════════
#: 危险度从高到低：判定的优先级也按这个顺序（**危险信号优先**）。
#: 理由：`exec_query` 里的 exec 比 query 更能决定后果；`read_and_write` 是写。
#: 把 READ_ONLY 优先会直接把写操作放成只读，那是把审批闸门关掉的级别的事故。
#: 不可逆动作（**单看动词就足够**的那些：执行任意代码、销毁容器/文件系统）。
_IRREVERSIBLE_VERBS = frozenset({
    "exec", "execute", "run", "shell", "bash", "sh", "zsh", "deploy", "drop",
    "destroy", "purge", "truncate", "format", "rm", "remove_all", "kill",
    "reboot", "shutdown", "migrate", "rollback", "revoke_all", "wipe", "reset",
})

#: 破坏性动词：**自己不足以判不可逆** —— `remove_label` 能再加回来，
#: `delete_repo` 不能。必须和下面的高价值对象组合才升级。
_DESTRUCTIVE_VERBS = frozenset({
    "delete", "del", "remove", "erase", "clear", "unlink", "terminate",
    "expunge", "detach", "uninstall", "deregister", "revoke",
})

#: 高价值对象：数据/身份/基础设施。删掉它们不是"改个状态"，是"没了"。
_HIGH_VALUE_NOUNS = frozenset({
    "repo", "repository", "database", "db", "table", "collection", "bucket",
    "volume", "backup", "snapshot", "user", "account", "org", "organization",
    "project", "workspace", "cluster", "namespace", "node", "instance",
    "production", "prod", "secret", "key", "credential", "certificate",
    "pipeline", "environment", "tenant", "workspace",
})
_MUTATING_VERBS = frozenset({
    "create", "update", "write", "set", "put", "post",
    "patch", "insert", "upsert", "add", "append", "save", "upload", "send",
    "push", "publish", "commit", "apply", "rename", "move", "copy", "mkdir",
    "touch", "chmod", "chown", "enable", "disable", "grant", "revoke", "sync",
    "import", "edit", "modify", "merge", "register",
})
_READ_VERBS = frozenset({
    "get", "list", "read", "search", "query", "fetch", "describe", "find",
    "show", "lookup", "inspect", "view", "head", "tail", "count", "stat",
    "status", "exists", "has", "ls", "cat", "grep", "select", "scan",
    "browse", "diff", "export_dry", "preview", "check",
})

#: 副作用 → 默认风险档。T0/T2/T3 与 Dispatcher 的闸门一一对应
#: （T3 不可确认只能拒绝、T2 需审批、T0 直接放行）。
_SIDE_EFFECT_TIER: dict[SideEffect, RiskTier] = {
    SideEffect.READ_ONLY: RiskTier.T0_SAFE,
    SideEffect.MUTATING: RiskTier.T2_HIGH,
    SideEffect.IRREVERSIBLE: RiskTier.T3_CRITICAL,
}

_CAMEL_BOUNDARY = re.compile(r"([a-z0-9])([A-Z])")
_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def _tokens(text: str) -> list[str]:
    """`getUserPosts` / `get_user_posts` / `get-user.posts` → ['get','user','posts']。

    必须按 token 比对而不是子串：`settings` 里含 `set`、`dataset` 里含 `set`、
    `format` 里含 `format`……子串匹配会把只读工具判成写，或者把写工具放过。
    """
    if not text:
        return []
    spaced = _CAMEL_BOUNDARY.sub(r"\1_\2", str(text))
    return [t for t in _NON_ALNUM.split(spaced.lower()) if t]


def _verbs_in(tokens: Iterable[str]) -> tuple[bool, bool, bool]:
    """返回 (命中不可逆, 命中写, 命中读)。"""
    toks = set(tokens)
    irreversible = write = read = False
    for token in toks:
        # 复合判定：破坏性动词 + 高价值对象 → 不可逆。
        # `delete_repo` / `drop_table` 是数据毁灭；`remove_label` 只是改状态 ——
        # 单看动词判不出可逆性，必须看动作对象。
        if token in _DESTRUCTIVE_VERBS and (toks & _HIGH_VALUE_NOUNS):
            irreversible = True
        if token in _IRREVERSIBLE_VERBS:
            irreversible = True
        elif token in _MUTATING_VERBS or token in _DESTRUCTIVE_VERBS:
            # 破坏性动词即便没升级到「不可逆」，也**一定是写操作**。
            # （把 delete 从 _MUTATING_VERBS 摘出来后，裸 delete 词元一度什么
            #  都不命中，`get_records(delete_id=…)` 被误判成只读 —— 那等于拆掉审批闸门。）
            write = True
        elif token in _READ_VERBS:
            read = True
    return irreversible, write, read


def classify_side_effect(
    tool_name: str,
    schema: dict | None = None,
) -> tuple[SideEffect, RiskTier]:
    """从名字（+ schema 弱信号）启发式猜副作用。**协议层不告诉你，只能猜，所以要保守。**

    规则（危险优先，先命中先返回）：
      * 名字含 `exec/run/shell/bash/deploy/drop…` → IRREVERSIBLE / T3
      * 名字含 `create/update/delete/write/set/remove/put/post…` → MUTATING / T2
      * 名字含 `get/list/read/search/query/fetch/describe…` → READ_ONLY / T0
      * 判不出来 → **MUTATING / T2**（宁可多一道审批，不可放过写操作）

    `schema` 只用来**升级**危险度，永远不能降级：
    参数名里出现 `delete`/`exec` 这类写信号时，即便名字看起来像只读，也升级。
    反向不成立 —— 参数里有 `query` 不代表这个工具只读（写工具常带查询参数），
    拿它把写操作降成只读就是把审批闸门拆了。

    Returns:
        `(SideEffect, RiskTier)`，两者一一对应，不会出现「只读 + T3」这种自相矛盾。
    """
    name_irr, name_write, name_read = _verbs_in(_tokens(tool_name))

    # ① 名字直接定级
    if name_irr:
        return SideEffect.IRREVERSIBLE, RiskTier.T3_CRITICAL
    if name_write:
        return SideEffect.MUTATING, RiskTier.T2_HIGH
    if name_read:
        # 名字像只读：再看 schema 有没有矛盾信号（只能升级）
        schema_irr, schema_write, _ = _verbs_in(_schema_tokens(schema))
        if schema_irr:
            return SideEffect.IRREVERSIBLE, RiskTier.T3_CRITICAL
        if schema_write:
            return SideEffect.MUTATING, RiskTier.T2_HIGH
        return SideEffect.READ_ONLY, RiskTier.T0_SAFE

    # ② 名字判不出来：schema 只能往上推，推不动就保守
    schema_irr, schema_write, _ = _verbs_in(_schema_tokens(schema))
    if schema_irr:
        return SideEffect.IRREVERSIBLE, RiskTier.T3_CRITICAL
    return SideEffect.MUTATING, RiskTier.T2_HIGH


def _schema_tokens(schema: Any) -> list[str]:
    """从 schema 里抽「动词信号」：顶层 properties 的键名 + required 里的字段名。"""
    if not isinstance(schema, dict):
        return []
    out: list[str] = []
    props = schema.get("properties")
    if isinstance(props, dict):
        for key in props:
            out.extend(_tokens(key))
    required = schema.get("required")
    if isinstance(required, (list, tuple)):
        for key in required:
            out.extend(_tokens(key))
    return out


# ══════════════════════════════════════════════════════════════
# ② 同步 / 异步桥
# ══════════════════════════════════════════════════════════════
_POOL_LOCK = threading.Lock()
_POOL: _futures.ThreadPoolExecutor | None = None


def _shared_pool() -> _futures.ThreadPoolExecutor:
    """进程级共享线程池（懒建）。

    为什么共享：用例 20 要求「多次调用后线程数稳定」。每次 `threading.Thread(...)`
    起一个一次性线程，长跑进程里线程数会随着调用次数漂移（哪怕都退出了，
    `active_count()` 的瞬态也不可控），而且线程创建成本比协程调度高两个数量级。
    """
    global _POOL
    with _POOL_LOCK:
        if _POOL is None:
            _POOL = _futures.ThreadPoolExecutor(
                max_workers=4, thread_name_prefix=MCP_BRIDGE_THREAD_PREFIX)
        return _POOL


def _running_loop() -> asyncio.AbstractEventLoop | None:
    """当前线程是否有运行中的 loop。

    `asyncio.get_event_loop()` 在 3.14 已经被弃用且会「凭空造 loop」，
    只有 `get_running_loop()` 能准确回答「我现在是不是在协程里」这个问题。
    """
    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        return None


def run_coroutine_sync(
    factory: Callable[[], Any],
    *,
    timeout_s: float | None = None,
    loop: asyncio.AbstractEventLoop | None = None,
) -> Any:
    """把一个「协程工厂」同步跑完。**接受工厂而不是协程对象**：

    协程对象在被拒绝的路径上会留下「coroutine was never awaited」警告，
    而路径选择（有没有 loop、用不用外部 loop）必须在建协程**之前**决定。
    """
    async def _guarded() -> Any:
        coro = factory()
        if timeout_s is None:
            return await coro
        return await asyncio.wait_for(coro, timeout_s)

    # ① 会话自带 loop：线程安全地投递到那个 loop（真实 ClientSession 的唯一正确用法）
    if loop is not None:
        future = asyncio.run_coroutine_threadsafe(_guarded(), loop)
        try:
            return future.result(timeout_s)
        except _futures.TimeoutError as exc:
            future.cancel()
            raise TimeoutError(
                f"mcp 调用超时（{timeout_s:.3f}s，外部 loop）") from exc

    # ② 没有运行中的 loop：当前线程直接开一个（Dispatcher worker 的常规路径）
    if _running_loop() is None:
        return asyncio.run(_guarded())

    # ③ 有运行中的 loop：不能在它里面再 asyncio.run，丢给共享池的子线程
    def _in_thread() -> Any:
        return asyncio.run(_guarded())

    future = _shared_pool().submit(_in_thread)
    try:
        # 超时由 inner wait_for 负责；这里不再设 second timeout，
        # 否则 cancelled 的等待会在池里留下一个还在跑的协程。
        return future.result()
    except _futures.CancelledError as exc:            # pragma: no cover - 极端路径
        raise TimeoutError("mcp 调用被取消") from exc


# ══════════════════════════════════════════════════════════════
# ③ 属性读取：兼容 snake_case / camelCase / dict
# ══════════════════════════════════════════════════════════════
def _attr_any(obj: Any, *names: str, default: Any = None) -> Any:
    """按顺序读第一个存在的属性/键。

    为什么需要：MCP 2.3 的 `types.Tool` 字段叫 `input_schema`，而 wire 格式是
    `inputSchema`（`alias_generator=to_camel`）。测试桩、缓存下来的 JSON、
    老版本回包只有 camelCase；直接写 `tool.inputSchema` 在真 SDK 上会 AttributeError。
    """
    for name in names:
        if isinstance(obj, dict):
            if name in obj:
                return obj[name]
            continue
        if hasattr(obj, name):
            return getattr(obj, name)
    return default


def _tool_remote_name(raw: Any) -> str:
    name = _attr_any(raw, "name")
    return name.strip() if isinstance(name, str) else ""


def _tool_input_schema(raw: Any) -> dict | None:
    schema = _attr_any(raw, "input_schema", "inputSchema")
    return schema if isinstance(schema, dict) and schema else None


def _extract_tools(page: Any) -> list[Any]:
    """`ListToolsResult` / list / dict 三种形状都收。"""
    if page is None:
        return []
    tools = _attr_any(page, "tools")
    if isinstance(tools, (list, tuple)):
        return list(tools)
    if isinstance(page, (list, tuple)):
        return list(page)
    return []


def _accepts_kw(fn: Any, keyword: str) -> bool:
    """会话的 `list_tools` 是否接受某个关键字参数（决定能不能翻页）。

    不能直接 try/except TypeError：那是把「参数用错」和「服务端报错」混为一谈。
    """
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):                   # 内建/装饰过的可调用：保守认为不支持
        return False
    if keyword in params:
        return True
    return any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())


#: 空 schema 兜底：MCP 要求根上是 `type: object`，而 `ToolManifest.input_schema`
#: 传 `{}` 会让 `openai_compat.tool_schema()` 退回保守形状（空 dict 不是合法 schema）。
_EMPTY_SCHEMA: dict = {"type": "object", "properties": {}}


_SAFE_NUM_PAGES = 50   # 翻页上限：防服务端 next_cursor 不收敛把 discover 挂死


# ══════════════════════════════════════════════════════════════
# ④ 桥本体
# ══════════════════════════════════════════════════════════════
class MCPToolBridge:
    """把一个 MCP Server 暴露的工具，转成 Runtime 能用的 ToolManifest + handler。

    只依赖会话的两个协程方法（鸭子类型）：

        async def list_tools() -> ListToolsResult | list[Tool]
        async def call_tool(name: str, arguments: dict) -> CallToolResult

    `ClientSession` 与 `mcp.Client` 都满足，测试里的 FakeSession 也满足。
    """

    #: 前缀分隔符。用 `__` 而不是 `.`/`:`：前者在 OpenAI function name 的
    #: `^[a-zA-Z0-9_-]+$` 里合法，后两者会被某些兼容服务直接 400。
    PREFIX_SEP = "__"

    def __init__(
        self,
        session: Any,
        *,
        server_name: str = "mcp",
        side_effect: SideEffect | None = None,
        risk_tier: RiskTier | None = None,
        timeout_ms: int = 30000,
        auto_classify: bool = True,
        loop: asyncio.AbstractEventLoop | None = None,
    ) -> None:
        """
        参数语义（三个分支，注释写清楚免得被误当 bug）：

          * `side_effect` **和** `risk_tier` 都给 → 完全强制，不跑启发式；
          * 只给 `side_effect` → 该副作用生效，`risk_tier` 取它的默认档
            （READ_ONLY→T0 / MUTATING→T2 / IRREVERSIBLE→T3）；
          * 都不给 → `auto_classify=True` 时走 `classify_side_effect()` 逐工具判，
            否则退回保守默认 **MUTATING / T2**。

        为什么默认是 `None` 而不是直接写死 `MUTATING`：契约里没有副作用语义的只有
        MCP，而「每个工具都 T2」等于所有工具都要人工审批 —— 一个 get 类工具也要
        审批，等于没人会真的去审批。让启发式先分档，判不出来的仍然落到 MUTATING/T2，
        和保守默认完全一致，安全性没有让步，可用性回来了。
        """
        if session is None:
            raise ValueError("session 不能为空")
        for method in ("list_tools", "call_tool"):
            if not callable(getattr(session, method, None)):
                raise TypeError(
                    f"session 缺少 {method}()（MCP 会话或同形桩都行）")
        self.session = session
        self.server_name = str(server_name or "mcp")
        self.timeout_ms = int(timeout_ms)
        self.auto_classify = bool(auto_classify)
        self.loop = loop
        # 归一化显式声明（也接受字符串，避免调用方 import 枚举）
        self._forced_side: SideEffect | None = (
            SideEffect(side_effect) if side_effect is not None else None)
        self._forced_tier: RiskTier | None = (
            RiskTier(risk_tier) if risk_tier is not None else None)
        if self._forced_tier is None and self._forced_side is not None:
            self._forced_tier = _SIDE_EFFECT_TIER[self._forced_side]

    # ── 发现 ────────────────────────────────────────────────────
    async def discover(self) -> list[ToolManifest]:
        """调 `list_tools()` 拿到工具清单，逐个翻译成 `ToolManifest`。

        MCP 会分页（`next_cursor`），这里跟随到底 —— 只拉第一页在工具多的 server
        （filesystem 之类）上会静默少一半工具，而模型看到的是「工具不存在」。
        """
        manifests: list[ToolManifest] = []
        seen: set[str] = set()
        page = await self.session.list_tools()
        pages = 0
        while True:
            manifests.extend(self._to_manifests(page, seen))
            pages += 1
            cursor = _attr_any(page, "next_cursor", "nextCursor")
            if not cursor or pages >= _SAFE_NUM_PAGES:
                break
            kwargs = self._cursor_kwargs(cursor)
            if kwargs is None:
                # 会话两种翻页参数都不接受：停在第一页，别硬塞 kwargs 把会话打崩
                logger.warning(
                    "mcp:%s 返回 next_cursor 但 list_tools 不接受 params/cursor，停止翻页",
                    self.server_name)
                break
            page = await self.session.list_tools(**kwargs)
        return manifests

    def _cursor_kwargs(self, cursor: Any) -> dict | None:
        """翻页参数有两种实际形状，必须都认：

          * `ClientSession.list_tools(*, params: PaginatedRequestParams | None)`
            → `params={"cursor": c}`
          * `mcp.Client.list_tools(*, cursor: str | None)`（2.3 的高层客户端）
            → `cursor=c`

        只写死一种，在另一种客户端上就会「永远只拿到第一页」而没有任何报错 ——
        模型看到的是「工具不存在」，人去查会查很久。
        """
        if _accepts_kw(self.session.list_tools, "params"):
            return {"params": {"cursor": cursor}}
        if _accepts_kw(self.session.list_tools, "cursor"):
            return {"cursor": cursor}
        return None

    def discover_sync(self) -> list[ToolManifest]:
        """`discover()` 的同步版（同步调用方 / 测试脚本用）。"""
        return run_coroutine_sync(self.discover, timeout_s=self.timeout_ms / 1000.0,
                                  loop=self.loop)

    def _to_manifests(self, page: Any, seen: set[str]) -> list[ToolManifest]:
        out: list[ToolManifest] = []
        for raw in _extract_tools(page):
            remote = _tool_remote_name(raw)
            if not remote:
                # 畸形 tool 跳过而不是整批失败：一个 server 里有个坏工具
                # 不该让其余 20 个可用工具全部消失
                self._warn(f"跳过畸形 tool（缺 name）: {raw!r}")
                continue
            full = f"{self.server_name}{self.PREFIX_SEP}{remote}"
            if full in seen:
                self._warn(f"跳过重名 tool: {full}")
                continue
            seen.add(full)
            schema = _tool_input_schema(raw)
            side, tier = self._resolve_policy(remote, schema)
            out.append(
                ToolManifest(
                    name=full,
                    side_effect=side,
                    risk_tier=tier,
                    # 只读才幂等：写操作重复执行就是真的重复副作用，
                    # Dispatcher 的幂等缓存只对幂等工具生效，标错等于「发两次消息」
                    idempotent=(side is SideEffect.READ_ONLY),
                    timeout_ms=self.timeout_ms,
                    # MCP server 普遍回包大（目录列表 / 文件正文），
                    # 5s 默认会在慢 server 上假超时，这里跟 timeout_ms 一起由调用方定
                    max_result_bytes=262144,
                    required_scope=("tool.read" if side is SideEffect.READ_ONLY
                                    else "tool.write"),
                    description=self._describe(raw, remote),
                    input_schema=schema if schema is not None else dict(_EMPTY_SCHEMA),
                )
            )
        return out

    def _resolve_policy(self, remote: str,
                        schema: dict | None) -> tuple[SideEffect, RiskTier]:
        """显式声明优先，其次启发式，最后保守默认。"""
        side = self._forced_side
        tier = self._forced_tier
        if side is not None:
            return side, tier if tier is not None else _SIDE_EFFECT_TIER[side]
        if self.auto_classify:
            return classify_side_effect(remote, schema)
        return SideEffect.MUTATING, RiskTier.T2_HIGH

    def _describe(self, raw: Any, remote: str) -> str:
        """描述兜底：`ToolManifest.description` 会原样进 Prompt，空字符串等于
        「模型只看到一个名字」，它就会凭名字瞎猜用途。兜底文案至少给出
        「这是哪个 server 的什么工具」。"""
        desc = _attr_any(raw, "description")
        text = desc.strip() if isinstance(desc, str) else ""
        return text or f"MCP 工具 {remote}（server={self.server_name}）"

    def _warn(self, message: str) -> None:
        logger.warning("mcp:%s %s", self.server_name, message)
        warnings.warn(message, RuntimeWarning, stacklevel=3)

    # ── handler ─────────────────────────────────────────────────
    def make_handler(self, tool_name: str) -> Callable[[dict, ExecCtx], Any]:
        """返回**同步** handler（`Dispatcher` 要求 `fn(args, ctx)` 同步返回）。

        `tool_name` 可以是注册后的带前缀名（Dispatcher 只会用它注册时看到的名字），
        也可以是 MCP 原始名 —— 内部统一剥前缀，调用方不用记两套。
        """
        remote = self.strip_prefix(tool_name)

        def handler(args: dict, _ctx: ExecCtx | None = None) -> Any:
            return self.call_sync(remote, args)

        handler.__name__ = f"mcp_handler_{self.server_name}_{remote}".replace("-", "_")
        handler.__doc__ = (f"MCP bridge handler: {self.server_name}"
                           f"{self.PREFIX_SEP}{remote}")
        # 供 dispatcher 之外的调用方/调试工具识别（不参与 ToolManifest）
        handler.mcp_remote_name = remote          # type: ignore[attr-defined]
        handler.mcp_server_name = self.server_name  # type: ignore[attr-defined]
        return handler

    def strip_prefix(self, tool_name: str) -> str:
        prefix = f"{self.server_name}{self.PREFIX_SEP}"
        name = str(tool_name or "")
        return name[len(prefix):] if name.startswith(prefix) else name

    # ── 调用 ────────────────────────────────────────────────────
    def call_sync(self, tool_name: str, arguments: dict | None = None) -> Any:
        """同步调一次 MCP 工具，返回已收口的 payload。"""
        remote = self.strip_prefix(tool_name)
        payload_args = dict(arguments or {})

        def factory() -> Any:
            return self.session.call_tool(remote, payload_args)

        try:
            result = run_coroutine_sync(
                factory, timeout_s=self.timeout_ms / 1000.0, loop=self.loop)
        except TimeoutError:
            # 超时单独放行：Dispatcher 的 timeout 语义能用得上，别混成普通失败
            raise
        except RuntimeError:
            raise
        except Exception as exc:              # noqa: BLE001 — 协议级错误统一收口
            raise RuntimeError(
                f"mcp call_tool failed: {remote}: {type(exc).__name__}: {exc}") from exc
        return self._payload(result, remote)

    async def call(self, tool_name: str, arguments: dict | None = None) -> Any:
        """原生异步调用（Async 调用方直接用，不经过线程桥）。"""
        remote = self.strip_prefix(tool_name)
        try:
            result = await self.session.call_tool(remote, dict(arguments or {}))
        except Exception as exc:              # noqa: BLE001
            raise RuntimeError(
                f"mcp call_tool failed: {remote}: {type(exc).__name__}: {exc}") from exc
        return self._payload(result, remote)

    def _payload(self, result: Any, remote: str) -> Any:
        """`CallToolResult` → Dispatcher/Normalizer 能吃的东西。

        三种情况都要落到具体形状：
          * `is_error=True`（工具业务错误，协议级是正常回包）→ RuntimeError，
            这样 Dispatcher 会包成 `ToolOutcome.failed`，模型看到的是「失败了，
            原因是什么」，而不是「返回空，随便编」；
          * 纯文本 content → 直接给字符串（模型读起来最省 token）；
          * `structured_content` / 非文本块 → 结构化 dict，保留原始块。
        """
        if result is None:
            raise RuntimeError(f"mcp tool {remote} 返回空结果")

        is_error = bool(_attr_any(result, "is_error", "isError", default=False))
        blocks = _attr_any(result, "content", default=None)
        texts: list[str] = []
        others: list[Any] = []
        for block in (blocks or ()):
            if isinstance(block, str):
                texts.append(block)
                continue
            text = _attr_any(block, "text")
            if isinstance(text, str):
                texts.append(text)
                continue
            others.append(_block_to_json(block))

        structured = _attr_any(result, "structured_content", "structuredContent",
                               default=None)

        if is_error:
            detail = "\n".join(texts) or (repr(others) if others else "(无错误详情)")
            raise RuntimeError(f"mcp tool {remote} 执行失败: {detail}")

        joined = "\n".join(texts)

        if structured is not None:
            # MCP 2.3 起 `@server.tool()` 默认开 structured_output，回包会**同时**带
            # `structured_content` 和一段镜像文本（同一份数据两条）。原样透传等于
            # 让同一事实进 Prompt 两次：白烧 token，模型还可能当成两条独立证据。
            # 所以「文本只是 structured 的镜像」时折叠掉文本，只留结构化结果。
            if len(texts) == 1 and not others and _is_mirror(texts[0], structured):
                return structured
            payload: dict[str, Any] = {"structured": structured}
            if joined:
                payload["text"] = joined
            if others:
                payload["content"] = others
            return payload

        if others:
            payload = {"content": others}
            if texts:
                payload["text"] = "\n".join(texts)
            return payload

        return joined


def _is_mirror(text: str, structured: Any) -> bool:
    """判断文本是不是 structured 的「人读镜像」。

    只认两种强形态，宁可不折叠也不误折叠（误折叠 = 丢掉 prose 说明）：
      * `{"result": "..."}` 这种单键包裹，值等于文本；
      * 文本恰好等于 structured 的 JSON 序列化。
    """
    if isinstance(structured, dict) and len(structured) == 1:
        only = next(iter(structured.values()))
        if isinstance(only, str) and only == text:
            return True
        if not isinstance(only, (dict, list)) and str(only) == text:
            return True
    try:
        return text == json.dumps(structured, ensure_ascii=False, default=str)
    except (TypeError, ValueError):       # pragma: no cover - 已收口的 JSON 值
        return False


def _block_to_json(block: Any) -> Any:
    """非文本 ContentBlock（图片 / 资源 / 音频）→ 可 JSON 化的 dict。

    用 `model_dump()` 而不是手抄字段：SDK 每次加字段（2.3 就有 icons/meta），
    手抄的实现会静默丢字段，而丢掉的可能是模型判断该不该用的依据。
    """
    if isinstance(block, dict):
        return block
    dump = getattr(block, "model_dump", None)
    if callable(dump):
        try:
            return dump(exclude_none=True)
        except Exception:                 # noqa: BLE001 — 序列化自身崩了也不能炸调用链
            pass
    return {"type": type(block).__name__, "repr": repr(block)}


# ══════════════════════════════════════════════════════════════
# ⑤ 批量注册
# ══════════════════════════════════════════════════════════════
def register_mcp_tools(registry: Any, manifests: Sequence[ToolManifest],
                       bridge: MCPToolBridge) -> list[ToolManifest]:
    """把发现到的 MCP 工具批量注册进 `ToolRegistry`。

    返回**实际注册成功**的那些（已存在的同名工具跳过并发出警告）。

    为什么跳过而不是抛：一次 `discover()` 的结果是「这个 server 的全集」，
    其中某个名字可能已经被本地工具占用。为这一个失败把整批 20 个工具都丢掉，
    等于让所有 MCP 工具不可用；跳过 + 警告则让人看得见冲突、又保住其余能力。
    （`ToolRegistry.register` 本身对同名是硬拒绝 —— 静默覆盖会换掉权限声明，
    这条不变，这里只是在批量场景里先挡一次。）
    """
    registered: list[ToolManifest] = []
    for manifest in manifests or ():
        if not isinstance(manifest, ToolManifest):
            bridge._warn(f"跳过非 ToolManifest: {manifest!r}")
            continue
        try:
            exists = manifest.name in registry
        except TypeError:                 # 鸭子型 registry 不支持 in
            exists = False
        if exists:
            bridge._warn(f"跳过已注册的工具名: {manifest.name}")
            continue
        registry.register(manifest, bridge.make_handler(manifest.name))
        registered.append(manifest)
    return registered
