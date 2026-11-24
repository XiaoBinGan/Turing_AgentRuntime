"""tests/mcp_fixture_server.py — 最小 stdio MCP server（测试夹具，**真子进程**）。

为什么要有这个文件：`tests/test_mcp_client.py` 里全部用 `FakeSession` 做替身，
验证的是「桥的翻译逻辑」；而生态里的 server（filesystem / git / postgres）**都是
子进程**，走 `mcp.client.stdio`。这条路径不真跑一遍，等于没接上生态。

启动方式（两种都行，测试里用第二种，路径确定）：

    .venv/bin/python -m tests.mcp_fixture_server
    .venv/bin/python tests/mcp_fixture_server.py

暴露 4 个工具（名字/参数/行为固定，测试用例按它写断言）：

| 工具          | 参数            | 行为                                  | 副作用 |
|---------------|-----------------|---------------------------------------|--------|
| `get_note`    | `{"id": str}`   | `{"id":…, "text": "note-<id>"}`       | 只读 |
| `list_notes`  | `{}`            | 列表                                  | 只读 |
| `create_note` | `{"text": str}` | 返回 `{"id":…, "text":…}`             | 写 |
| `delete_note` | `{"id": str}`   | `{"deleted": true}`                   | 写（不可逆） |

环境变量（全部可选，供测试构造故障场景）：

  * `MCP_FIXTURE_LABEL`     —— 新建笔记 id 的前缀（验证环境变量真的透传进子进程）
  * `MCP_FIXTURE_STDERR_MARKER` —— 启动时往 stderr 打这行（验证 stderr 被捕获而非泄漏）
  * `MCP_FIXTURE_DIE_AFTER_S`   —— N 秒后 `os._exit(9)`（模拟 server 运行中崩溃）
  * `MCP_FIXTURE_EXIT_NOW`   —— 启动瞬间 `os._exit(7)`（模拟 server 起不来）

注意：**stderr 只用于诊断**。MCP 的协议帧走 stdout，任何无意的 print 到 stdout 都会
把协议流打乱（客户端会当成坏的 JSON-RPC 帧），所以本文件所有日志都显式 `file=sys.stderr`。
"""
from __future__ import annotations

import itertools
import os
import sys
import threading
import time

from mcp.server.mcpserver import MCPServer

#: server 名（只影响日志可读性，注册后 Runtime 侧用的是 attach 时传的 server_name）
SERVER_NAME = "fixture"

#: 进程内状态：这个 server 是「有状态」的，正好验证 create→list→delete 的连贯性
NOTES: dict[str, dict] = {}

#: 新建笔记的序号与 id 前缀（前缀来自环境变量，用于验证 env 透传）
_SEQ = itertools.count(1)
_LABEL = os.environ.get("MCP_FIXTURE_LABEL", "")

server = MCPServer(SERVER_NAME)


def _log(message: str) -> None:
    """诊断日志只走 stderr。stdout 是协议流，写一个字节都会污染帧解析。"""
    print(message, file=sys.stderr, flush=True)


# ══════════════════════════════════════════════════════════════
# 4 个工具
# ══════════════════════════════════════════════════════════════
#: 工具调用计数（按次数触发崩溃用 —— **比按时间触发确定得多**）
_CALLS = {"n": 0}


def _maybe_die_on_call() -> None:
    """`MCP_FIXTURE_DIE_AFTER_CALLS=N`：第 N 次**工具调用之后**硬崩。

    为什么需要它（按秒触发不够用）：`MCP_FIXTURE_DIE_AFTER_S=0.4` 在
    **全套件 CPU 争抢**下会 flaky —— 首个调用可能还没返回，server 就死了，
    于是"崩之前是好的"这条断言随机失败（实测 6 次全套件跑中挂 1 次）。
    按次数触发没有时间窗口，完全确定。
    """
    raw = os.environ.get("MCP_FIXTURE_DIE_AFTER_CALLS", "") or ""
    try:
        limit = int(raw)
    except ValueError:
        return
    if limit <= 0:
        return
    _CALLS["n"] += 1
    if _CALLS["n"] >= limit:
        _log(f"fixture-server: 主动崩溃 os._exit(9)（第 {_CALLS['n']} 次调用）")
        os._exit(9)


@server.tool()
def get_note(id: str) -> dict:
    """按 id 读取一条笔记（只读，不修改任何状态）。"""
    _maybe_die_on_call()
    return {"id": id, "text": f"note-{id}"}


@server.tool()
def list_notes() -> list[dict]:
    """列出当前进程里所有笔记（只读）。"""
    return list(NOTES.values())


@server.tool()
def create_note(text: str) -> dict:
    """新建一条笔记并返回它（写操作）。"""
    note_id = f"{_LABEL}{next(_SEQ)}"
    NOTES[note_id] = {"id": note_id, "text": text}
    return NOTES[note_id]


@server.tool()
def delete_note(id: str) -> dict:
    """删除一条笔记（写操作，内容不可恢复）。"""
    NOTES.pop(id, None)
    return {"deleted": True}


# ══════════════════════════════════════════════════════════════
# 启动 / 故障注入
# ══════════════════════════════════════════════════════════════
def _schedule_death() -> None:
    """按环境变量安排「运行中崩溃」：`os._exit` 而不是 `sys.exit`，
    保证是硬崩（不留 finally 清理），更贴近真实 server 挂掉的样子。"""
    raw = os.environ.get("MCP_FIXTURE_DIE_AFTER_S", "") or ""
    try:
        delay = float(raw)
    except ValueError:
        return
    if delay <= 0:
        return

    def _die() -> None:
        time.sleep(delay)
        _log(f"fixture-server: 主动崩溃 os._exit(9)（DIE_AFTER_S={delay}）")
        os._exit(9)

    threading.Thread(target=_die, name="fixture-death", daemon=True).start()


def main() -> int:
    _log(f"fixture-server: starting pid={os.getpid()} label={_LABEL!r}")
    marker = os.environ.get("MCP_FIXTURE_STDERR_MARKER", "")
    if marker:
        _log(marker)

    if os.environ.get("MCP_FIXTURE_EXIT_NOW", ""):
        _log("fixture-server: EXIT_NOW，立即退出（模拟 server 起不来）")
        os._exit(7)

    _schedule_death()
    # 阻塞在这里，直到 stdin 关闭（客户端 stop 时）
    server.run("stdio")
    return 0


if __name__ == "__main__":
    sys.exit(main())
