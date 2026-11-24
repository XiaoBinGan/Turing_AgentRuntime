"""tests/test_kv.py — W28：分布式原子 KV 与「换库之后」的**证伪面**。

==============================================================================
这个文件里最重要的东西不是 40 个用例，而是那个假 Redis
==============================================================================
"我的 CAS 是原子的"这句话只有在**真的有人并发改同一个 key** 时才有意义。
所以 `tests/` 里常驻一个迷你 RESP 服务端：

* 它跑在 `127.0.0.1:0`（内核分配端口，不撞端口、不依赖本机装没装 Redis）；
* 每个连接一个线程（`ThreadingTCPServer`），所以"两个客户端同时改一个 key"
  在服务端是**真并发**，不是模拟；
* `WATCH/MULTI/EXEC` 实现了**真实的乐观锁语义**（watch 集合 + 全局单调版本号，
  EXEC 那一刻发现版本变了就返回 nil）。如果这里偷懒做成"总是成功"，
  `test_13` / `test_23` 会立刻暴露 —— 假服务器的语义必须能被证伪，
  否则它只是在给客户端背书。

==============================================================================
换库之后，这个假服务器变成了什么
==============================================================================
`core/kv.py` 的 `RESPKV` 从**手写 RESP2 客户端**换成了官方 `redis` 库（redis-py）。
假服务器**没有退休，反而更重要了**：现在它是「真客户端 × 真协议 × 我们的服务端」
的三方集成测试 —— redis-py 会握手（`CLIENT SETINFO`）、会走连接池、会拼
`MULTI+SET+EXEC` 的批量帧，这些都在这里被真实地跑一遍。

为此补了 redis-py 握手需要的命令（都写了注释说明为什么存在）：
`CLIENT`（`SETINFO` 等 → `+OK`）、`HELLO`（回一个**真 RESP3 map**）、
`COMMAND`（空数组）、`SELECT`、`AUTH`。

顺便钉住一条被想当然的假设不成立的结论（test_00 验证）：**服务端对 `HELLO`
回 `-ERR` 并不会让 redis-py 8.x 降级到 RESP2** —— 它抛 `ResponseError`，连接
直接失败。所以 `RESPKV` 显式 `protocol=2`，而不是指望"降级"。

==============================================================================
被单独钉住的结论
==============================================================================
1. **CAS 并发下恰好一个赢家**（test_13）：10 个连接、同一 expected、同时开火。
   多一个赢家 = 丢更新；少一个 = 假冲突。两边都算失败。
2. **WATCH 真的看见别人的写**（test_23）：手工把 `SET` 插到 `WATCH` 和 `EXEC` 之间，
   断言事务被否。这是"服务端在写入那一刻做的判定"的直接证据。
3. **GET+SET 冒充 CAS 会丢更新**（test_24，负向对照）：同样的并发度，只是把 CAS
   换成"读-判定-写"，就能看到 6 个客户端都"成功"、而只有最后一次写入生效。
4. **库的默认重试会把非幂等命令重发一次**（test_32，负向对照）：这正是
   `RESPKV` 把 `Retry` pin 成 0 次的理由 —— 一次 `INCRBY` 会变成加 2。
5. **WATCH 期间断线 ≠ 冲突**（test_33）：结果未知必须报 `KVUnavailable`，
   不许说成"没抢到"。
6. **四个 store 实现跑同一组断言**（test_27）：MemoryStore / FileStore /
   SQLiteStore / RedisStore，行为不对齐就在这一条上炸。

==============================================================================
跑法
==============================================================================
    cd /Users/supre/Documents/tmp/agent-runtime
    .venv/bin/python -m unittest tests.test_kv -v

**不连真 Redis**（本机也没有）：全部用下面这个假服务器。
"""
from __future__ import annotations

import fnmatch
import json
import os
import shutil
import socket
import socketserver
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

import redis

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from core import kv as kv_module  # noqa: E402
from core.kv import (  # noqa: E402
    KVError,
    KVUnavailable,
    InMemoryKV,
    RESPKV,
)
from core.redis_store import RedisStore  # noqa: E402
from core.sqlite_store import SQLiteStore  # noqa: E402
from core.store import MemoryStore, FileStore, StoreError  # noqa: E402


# ══════════════════════════════════════════════════════════════
# 假 Redis — 服务端侧
# ══════════════════════════════════════════════════════════════
class _Simple:
    """RESP simple string（`+OK`）。"""
    __slots__ = ("text",)

    def __init__(self, text: str) -> None:
        self.text = text


class _Err:
    """RESP error（`-ERR ...`）。"""
    __slots__ = ("text",)

    def __init__(self, text: str) -> None:
        self.text = text


class _Map:
    """RESP3 map（`%N` 帧）：`HELLO` 的回复形状（RESP3 客户端要求它是 map）。"""

    __slots__ = ("items",)

    def __init__(self, items) -> None:
        self.items = list(items)


class _BadFrame(Exception):
    """客户端发来的帧不成形（假服务器的"协议错误"出口）。"""


class _DropConnection(Exception):
    """命令**已经执行了**，但回复被丢掉、连接被掐断。

    这是最难测也最该测的故障：客户端不知道服务端到底写没写进去。
    `test_31` / `test_32` 用它证明非幂等命令的重发策略是对的。
    """


def _enc(reply) -> bytes:
    """把假服务器内部表示的回复编码成 RESP2 字节。"""
    if isinstance(reply, _Simple):
        return b"+" + reply.text.encode("utf-8") + b"\r\n"
    if isinstance(reply, _Err):
        return b"-" + reply.text.encode("utf-8") + b"\r\n"
    if reply is None:
        return b"$-1\r\n"                      # nil bulk（EXEC 被否也用它）
    if isinstance(reply, bool):                # bool 是 int 子类：显式拒绝，避免静默变成 :1
        raise TypeError("假服务器不用 bool 表示回复，请用 int / _Simple")
    if isinstance(reply, int):
        return b":%d\r\n" % reply
    if isinstance(reply, _Map):
        return b"%%%d\r\n" % len(reply.items) + b"".join(
            _enc(k) + _enc(v) for k, v in reply.items
        )
    if isinstance(reply, list):
        return b"*%d\r\n" % len(reply) + b"".join(_enc(item) for item in reply)
    raw = str(reply).encode("utf-8")
    return b"$%d\r\n" % len(raw) + raw + b"\r\n"


class _FakeData:
    """假服务器的全部状态。**一把锁保护一切**（服务端本来就是单点串行语义的来源）。"""

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.values: dict[str, str] = {}
        self.sets: dict[str, set[str]] = {}
        self.expires: dict[str, float] = {}     # key → monotonic 截止时刻
        self.versions: dict[str, int] = {}      # key → 全局单调版本号（WATCH 的基础）
        self._seq = 0
        #: 收到的命令（只记名字，用于断言"握手真的发生了"）
        self.commands: list[str] = []
        #: 故障注入①：这些 key 的**下一次** INCRBY 会执行成功但丢掉回包
        self.drop_incr_reply: set[str] = set()
        #: 故障注入②：下一次 WATCH 回复完就掐断连接（模拟"WATCH 之后端口没了"）
        self.drop_after_watch = False
        #: 故障注入③：假装是 Redis 5（没有 HELLO）—— 验证 RESP3 客户端会连不上
        self.reject_hello = False

    def record(self, name: str) -> None:
        with self.lock:
            self.commands.append(name)

    def bump(self, key: str) -> None:
        """任何一次"这个 key 变了"都必须走这里 —— 否则 WATCH 看不见。"""
        self._seq += 1
        self.versions[key] = self._seq

    def version(self, key: str) -> int:
        return self.versions.get(key, 0)

    # ── 过期 ───────────────────────────────────────────────
    def _drop_if_expired(self, key: str) -> bool:
        deadline = self.expires.get(key)
        if deadline is not None and time.monotonic() >= deadline:
            self.values.pop(key, None)
            self.expires.pop(key, None)
            # 过期在 Redis 里也算一次改动：被 WATCH 的 key 过期后 EXEC 必须失败
            self.bump(key)
            return True
        return False

    def alive(self, key: str) -> bool:
        self._drop_if_expired(key)
        return key in self.values

    def get(self, key: str) -> str | None:
        self._drop_if_expired(key)
        return self.values.get(key)

    def put(self, key: str, value: str) -> None:
        self._drop_if_expired(key)
        self.values[key] = value
        self.sets.pop(key, None)               # 一个 key 只能是字符串或集合
        self.expires.pop(key, None)            # SET 覆盖会清掉旧 TTL（与真 Redis 一致）
        self.bump(key)

    def put_set(self, key: str, members: set[str]) -> None:
        self.sets[key] = members
        self.values.pop(key, None)
        self.bump(key)

    def set_ttl(self, key: str, seconds: float) -> None:
        self.expires[key] = time.monotonic() + seconds


class _FakeRedisHandler(socketserver.StreamRequestHandler):
    """一条连接一个 handler 线程：WATCH 状态与 MULTI 队列都是**连接私有**的。"""

    # ── 连接生命周期 ───────────────────────────────────────
    def setup(self) -> None:
        super().setup()
        self._watch: dict[str, int] = {}
        self._queue: list[list[str]] | None = None
        self.server.track(self.connection)

    def finish(self) -> None:
        self.server.untrack(self.connection)
        try:
            super().finish()
        except OSError:
            pass

    def handle(self) -> None:
        while True:
            try:
                argv = self._read_command()
            except _BadFrame as exc:
                self._send(_Err(f"ERR {exc}"))
                return
            except (OSError, ValueError):
                return                          # 连接被关掉（stop() 会主动断所有连接）
            if argv is None:
                return
            self.d.record(argv[0])
            try:
                reply = self._dispatch(argv)
            except _DropConnection:
                try:
                    self.connection.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                return
            except _BadFrame as exc:
                reply = _Err(f"ERR {exc}")
            self._send(reply)
            if argv[0].upper() == "WATCH" and self.d.drop_after_watch:
                # 故障注入②：WATCH 已经生效（服务端记下了版本），但这条连接没了
                self.d.drop_after_watch = False
                try:
                    self.connection.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                return

    # ── 编解码 ─────────────────────────────────────────────
    def _read_command(self) -> list[str] | None:
        line = self.rfile.readline()
        if not line:
            return None
        if not line.startswith(b"*"):
            raise _BadFrame(f"期望数组帧，收到 {line[:16]!r}")
        try:
            count = int(line[1:].strip())
        except ValueError as exc:
            raise _BadFrame(f"数组长度不可解析：{line[:16]!r}") from exc
        argv: list[str] = []
        for _ in range(count):
            header = self.rfile.readline()
            if not header.startswith(b"$"):
                raise _BadFrame(f"期望 bulk 帧，收到 {header[:16]!r}")
            try:
                length = int(header[1:].strip())
            except ValueError as exc:
                raise _BadFrame(f"bulk 长度不可解析：{header[:16]!r}") from exc
            body = self.rfile.read(length + 2)
            if body is None or len(body) < length + 2:
                raise _BadFrame("bulk 被截断")
            argv.append(body[:-2].decode("utf-8", "replace"))
        return argv

    def _send(self, reply) -> None:
        try:
            self.wfile.write(_enc(reply))
        except OSError:
            pass

    # ── 分发 ───────────────────────────────────────────────
    @property
    def d(self) -> _FakeData:
        return self.server.data

    def _dispatch(self, argv: list[str]):
        name = argv[0].upper()
        # MULTI 期间：除事务控制命令外一律排队
        if self._queue is not None and name not in (
            "EXEC", "DISCARD", "MULTI", "WATCH", "UNWATCH",
        ):
            self._queue.append(argv)
            return _Simple("QUEUED")
        return self._run(argv)

    def _run(self, argv: list[str]):
        name = argv[0].upper()
        handler = getattr(self, f"_cmd_{name.lower()}", None)
        if handler is None:
            return _Err(f"ERR unknown command '{argv[0]}'")
        try:
            return handler(*argv[1:])
        except _BadFrame as exc:
            return _Err(f"ERR {exc}")

    # ── 命令：握手（redis-py 会发这些）─────────────────────
    def _cmd_hello(self, *args):
        """`HELLO [proto] [AUTH user pass] [SETNAME name]` → 服务端元数据。

        必须回一个**真 map**：RESP3 客户端会用 `.get("proto")` 校验协商结果，
        回数组会让它直接 `AttributeError`。`reject_hello` 用来模拟 Redis 5。
        """
        if self.d.reject_hello:
            return _Err("ERR unknown command 'HELLO'")
        proto = 2
        for arg in args:
            try:
                proto = int(arg)
            except (TypeError, ValueError):
                continue
            break
        return _Map([
            ("server", "redis"),
            ("version", "7.2.0-fake"),
            ("proto", proto),
            ("id", 1),
            ("mode", "standalone"),
            ("role", "master"),
            ("modules", []),
        ])

    def _cmd_client(self, sub="", *rest):
        """`CLIENT SETINFO / SETNAME / GETNAME / ID`：握手时 redis-py 会发 SETINFO。"""
        name = str(sub).upper()
        if name == "GETNAME":
            return ""
        if name == "ID":
            return 1
        return _Simple("OK")

    def _cmd_command(self, *rest):
        """`COMMAND` 的元数据查询：本假服务器没有元数据，回空数组。"""
        return []

    def _cmd_select(self, db="0"):
        return _Simple("OK")

    def _cmd_auth(self, *args):
        return _Simple("OK")

    # ── 命令：字符串 ───────────────────────────────────────
    def _cmd_ping(self, *rest):
        if rest:
            return str(rest[0])
        return _Simple("PONG")

    def _cmd_echo(self, message=""):
        return message

    def _cmd_get(self, key):
        with self.d.lock:
            return self.d.get(key)

    def _cmd_set(self, key, value, *opts):
        nx = False
        ttl: float | None = None
        i = 0
        while i < len(opts):
            opt = opts[i].upper()
            if opt == "NX":
                nx = True
                i += 1
            elif opt in ("EX", "PX"):
                if i + 1 >= len(opts):
                    raise _BadFrame("EX/PX 缺少数值")
                try:
                    amount = float(opts[i + 1])
                except ValueError as exc:
                    raise _BadFrame(f"EX/PX 数值非法：{opts[i + 1]!r}") from exc
                ttl = amount if opt == "EX" else amount / 1000.0
                i += 2
            else:
                raise _BadFrame(f"syntax error near {opts[i]!r}")
        with self.d.lock:
            if nx and self.d.alive(key):
                return None                      # SET NX 竞争失败 → nil
            self.d.put(key, value)
            if ttl is not None:
                self.d.set_ttl(key, ttl)
            return _Simple("OK")

    def _cmd_setnx(self, key, value):
        with self.d.lock:
            if self.d.alive(key):
                return 0
            self.d.put(key, value)
            return 1

    def _cmd_del(self, *keys):
        removed = 0
        with self.d.lock:
            for key in keys:
                if self.d.alive(key):
                    self.d.values.pop(key, None)
                    self.d.expires.pop(key, None)
                    self.d.bump(key)
                    removed += 1
                elif key in self.d.sets:
                    self.d.sets.pop(key, None)
                    self.d.bump(key)
                    removed += 1
        return removed

    def _cmd_exists(self, *keys):
        with self.d.lock:
            return sum(1 for key in keys if self.d.alive(key) or key in self.d.sets)

    def _cmd_incrby(self, key, delta):
        try:
            step = int(delta)
        except ValueError:
            raise _BadFrame(f"INCRBY 增量非法：{delta!r}")
        with self.d.lock:
            raw = self.d.get(key)
            try:
                base = 0 if raw is None else int(raw)
            except ValueError:
                return _Err("ERR value is not an integer or out of range")
            new_value = base + step
            # 直接写 values：INCR 不重置 TTL（与真 Redis 一致）
            self.d.values[key] = str(new_value)
            self.d.bump(key)
            if key in self.d.drop_incr_reply:      # 故障注入：生效了，但回包丢掉
                self.d.drop_incr_reply.discard(key)
                raise _DropConnection()
            return new_value

    def _cmd_expire(self, key, seconds):
        return self._ttl(key, float(seconds))

    def _cmd_pexpire(self, key, millis):
        return self._ttl(key, float(millis) / 1000.0)

    def _ttl(self, key, seconds: float):
        with self.d.lock:
            if not self.d.alive(key):
                return 0
            self.d.set_ttl(key, float(seconds))
            return 1

    # ── 命令：集合 ─────────────────────────────────────────
    def _cmd_sadd(self, key, *members):
        with self.d.lock:
            bucket = set(self.d.sets.get(key, ()))
            before = len(bucket)
            bucket.update(members)
            self.d.put_set(key, bucket)
            return len(bucket) - before

    def _cmd_srem(self, key, *members):
        with self.d.lock:
            bucket = set(self.d.sets.get(key, ()))
            before = len(bucket)
            bucket.difference_update(members)
            if bucket:
                self.d.put_set(key, bucket)
            elif key in self.d.sets:
                self.d.sets.pop(key, None)
                self.d.bump(key)
            return before - len(bucket)

    def _cmd_smembers(self, key):
        with self.d.lock:
            return sorted(self.d.sets.get(key, ()))

    def _cmd_scard(self, key):
        with self.d.lock:
            return len(self.d.sets.get(key, ()))

    def _cmd_scan(self, cursor="0", *opts):
        match: str | None = None
        count = 10
        i = 0
        while i < len(opts):
            opt = opts[i].upper()
            if opt == "MATCH" and i + 1 < len(opts):
                match = opts[i + 1]
                i += 2
            elif opt == "COUNT" and i + 1 < len(opts):
                count = max(1, int(opts[i + 1]))
                i += 2
            else:
                raise _BadFrame(f"SCAN 选项非法：{opts[i]!r}")
        with self.d.lock:
            keys = sorted(set(self.d.values) | set(self.d.sets))
            keys = [k for k in keys if self.d.alive(k) or k in self.d.sets]
            start = max(0, int(cursor))
            end = min(len(keys), start + count)
            page = [k for k in keys[start:end]
                    if match is None or fnmatch.fnmatchcase(k, match)]
            next_cursor = "0" if end >= len(keys) else str(end)
            return [next_cursor, page]

    def _cmd_dbsize(self):
        with self.d.lock:
            return len(set(self.d.values) | set(self.d.sets))

    def _cmd_flushall(self, *rest):
        with self.d.lock:
            self.d.values.clear()
            self.d.sets.clear()
            self.d.expires.clear()
            for key in list(self.d.versions):
                self.d.bump(key)                 # 清库也是改动：WATCH 必须能看见
        return _Simple("OK")

    # ── 命令：事务（乐观锁的物理实现）───────────────────────
    def _cmd_watch(self, *keys):
        with self.d.lock:
            for key in keys:
                self._watch[key] = self.d.version(key)
        return _Simple("OK")

    def _cmd_unwatch(self):
        self._watch.clear()
        return _Simple("OK")

    def _cmd_multi(self):
        if self._queue is not None:
            return _Err("ERR MULTI calls can not be nested")
        self._queue = []
        return _Simple("OK")

    def _cmd_discard(self):
        if self._queue is None:
            return _Err("ERR DISCARD without MULTI")
        self._queue = None
        self._watch.clear()
        return _Simple("OK")

    def _cmd_exec(self):
        if self._queue is None:
            return _Err("ERR EXEC without MULTI")
        queued, self._queue = self._queue, None
        with self.d.lock:
            # 这是整个乐观锁的核心：**执行的那一刻**再比一次版本。
            # 客户端读值发生在更早的时刻，只有这里能代表"现在"。
            dirty = any(self.d.version(key) != seen
                        for key, seen in self._watch.items())
            self._watch.clear()
            if dirty:
                return None                      # nil：整个事务丢弃，一条没写
            return [self._run(list(argv)) for argv in queued]


class FakeRedisServer(socketserver.ThreadingTCPServer):
    """迷你 RESP 服务端。端口由内核分配（`127.0.0.1:0`），不撞端口。"""

    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, data: _FakeData, port: int = 0) -> None:
        self.data = data
        self._conns: set[socket.socket] = set()
        self._conn_lock = threading.Lock()
        self._thread: threading.Thread | None = None
        super().__init__(("127.0.0.1", port), _FakeRedisHandler)

    @property
    def port(self) -> int:
        return int(self.server_address[1])

    def start(self) -> "FakeRedisServer":
        self._thread = threading.Thread(
            target=self.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True,
        )
        self._thread.start()
        return self

    def track(self, conn: socket.socket) -> None:
        with self._conn_lock:
            self._conns.add(conn)

    def untrack(self, conn: socket.socket) -> None:
        with self._conn_lock:
            self._conns.discard(conn)

    def drop_all_connections(self) -> None:
        """主动掐断所有活连接 —— 模拟"服务器没了"，而不是"客户端以为它还在"。"""
        with self._conn_lock:
            conns, self._conns = list(self._conns), set()
        for conn in conns:
            try:
                conn.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                conn.close()
            except OSError:
                pass

    def stop(self) -> None:
        if self._thread is None:
            return
        self.shutdown()
        self.drop_all_connections()
        self.server_close()
        self._thread.join(3.0)
        self._thread = None

    def handle_error(self, request, client_address) -> None:
        """静默：断连/半包是测试**故意**制造的，不是被测对象的错，别刷屏。"""


class FakeRedis:
    """把假服务器包装成"可启停、可原地重启"的测试夹具。

    重启刻意保留同一份 `_FakeData`：`test_18` 要验证的是**客户端/连接池的自动
    重连**，不是"数据还能不能活下来"（那是 Redis 自己的事）。
    """

    def __init__(self) -> None:
        self.data = _FakeData()
        self.server: FakeRedisServer | None = None
        self.port = 0
        self.starts = 0

    def start(self, port: int = 0) -> "FakeRedis":
        last: Exception | None = None
        for _ in range(60):
            try:
                self.server = FakeRedisServer(self.data, port)
                break
            except OSError as exc:               # 端口还没从 TIME_WAIT 里出来
                last = exc
                time.sleep(0.02)
        else:                                    # pragma: no cover - 极端环境
            raise AssertionError(f"假 Redis 绑定失败：{last}")
        self.port = self.server.port
        self.server.start()
        self.starts += 1
        return self

    def stop(self) -> "FakeRedis":
        if self.server is not None:
            self.server.stop()
            self.server = None
        return self

    def restart(self) -> "FakeRedis":
        port = self.port
        self.stop()
        return self.start(port)

    def __enter__(self) -> "FakeRedis":
        return self.start()

    def __exit__(self, *exc_info: object) -> None:
        self.stop()


def free_port() -> int:
    """拿一个"刚放掉、肯定没人监听"的端口（用于验证连接失败路径）。"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])
    finally:
        sock.close()


# ══════════════════════════════════════════════════════════════
# 测试替身：注入用的假客户端（不连任何网络）
# ══════════════════════════════════════════════════════════════
class _StubClient:
    """只会记账的客户端：证明 `RESPKV` 用注入的那个、且不额外建连接。"""

    def __init__(self, values: dict[str, str] | None = None) -> None:
        self.values = dict(values or {})
        self.calls: dict[str, int] = {}
        self.last_keys: list[str] = []

    def _note(self, name: str, *keys: str) -> None:
        self.calls[name] = self.calls.get(name, 0) + 1
        self.last_keys = list(keys)

    # ── 被 RESPKV 用到的子集 ───────────────────────────────
    def get(self, key):
        self._note("get", key)
        return self.values.get(key)

    def set(self, key, value, **kw):
        self._note("set", key)
        self.values[key] = value
        return True

    def pipeline(self, transaction=True):       # pragma: no cover - 本类不测 CAS
        raise NotImplementedError("这个替身不实现事务：CAS 有独立用例")

    def ping(self):
        self._note("ping")
        return True

    def close(self):
        self._note("close")


# ══════════════════════════════════════════════════════════════
# ① InMemoryKV（8）
# ══════════════════════════════════════════════════════════════
class FakeClock:
    """可推进的假时钟（TTL 用例不用 sleep）。"""

    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class TestInMemoryKV(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FakeClock()
        self.kv = InMemoryKV(clock=self.clock)

    def test_01_get_set_roundtrip(self) -> None:
        self.assertIsNone(self.kv.get("k"))
        self.assertTrue(self.kv.set("k", "v1"))
        self.assertEqual(self.kv.get("k"), "v1")
        self.assertTrue(self.kv.set("k", "v2"))
        self.assertEqual(self.kv.get("k"), "v2")
        # 值必须是 str：契约是字符串语义，塞 bytes 应该被明确拒绝而不是静默 str() 掉
        with self.assertRaises(KVError):
            self.kv.set("k", b"bytes")          # type: ignore[arg-type]

    def test_02_set_nx_first_wins(self) -> None:
        self.assertTrue(self.kv.set_nx("lock", "a"))
        self.assertFalse(self.kv.set_nx("lock", "b"))
        self.assertEqual(self.kv.get("lock"), "a")
        # 删掉之后又能抢到（NX 的语义是"看当前状态"，不是"一辈子只准写一次"）
        self.assertTrue(self.kv.delete("lock"))
        self.assertTrue(self.kv.set_nx("lock", "c"))
        self.assertEqual(self.kv.get("lock"), "c")

    def test_03_incr_from_zero(self) -> None:
        self.assertEqual(self.kv.incr("n"), 1)          # 不存在 → 从 0 起
        self.assertEqual(self.kv.incr("n"), 2)
        self.assertEqual(self.kv.incr("n", 10), 12)
        self.assertEqual(self.kv.incr("n", -2), 10)
        self.assertEqual(self.kv.get("n"), "10")
        # 非整数值 → 明确的 KVError（不是 ValueError 漏出去）
        self.kv.set("s", "abc")
        with self.assertRaises(KVError):
            self.kv.incr("s")

    def test_04_ttl_expiry_with_fake_clock(self) -> None:
        self.kv.set("t", "v", ttl_s=5)
        self.assertEqual(self.kv.get("t"), "v")
        self.clock.advance(4.9)
        self.assertEqual(self.kv.get("t"), "v")         # 还没到
        self.clock.advance(0.2)
        self.assertIsNone(self.kv.get("t"))             # 到期即不见了
        # 到期是"逻辑上不存在"，不是"读的时候还给你旧值"
        self.assertFalse(self.kv.delete("t"))
        # incr 的 TTL 只在创建时设置
        self.assertEqual(self.kv.incr("w", 1, ttl_s=2), 1)
        self.clock.advance(1)
        self.assertEqual(self.kv.incr("w", 1, ttl_s=2), 2)   # 不续期
        self.clock.advance(1.1)
        self.assertIsNone(self.kv.get("w"))
        self.assertEqual(self.kv.incr("w", 1), 1)            # 归零重来

    def test_05_compare_and_set(self) -> None:
        self.kv.set("c", "v1")
        self.assertTrue(self.kv.compare_and_set("c", "v1", "v2"))
        self.assertEqual(self.kv.get("c"), "v2")
        # expected 不匹配 → False，且**值一点没变**
        self.assertFalse(self.kv.compare_and_set("c", "v1", "v3"))
        self.assertEqual(self.kv.get("c"), "v2")
        # expected=None 的用法：仅当不存在时创建
        self.assertTrue(self.kv.compare_and_set("fresh", None, "v1"))
        self.assertFalse(self.kv.compare_and_set("fresh", None, "v2"))
        self.assertEqual(self.kv.get("fresh"), "v1")
        # 与真 Redis 对齐：不带 ttl 的写入会清掉旧 TTL（不是"保留原 TTL"）
        self.kv.set("e", "v1", ttl_s=10)
        self.assertTrue(self.kv.compare_and_set("e", "v1", "v2"))
        self.clock.advance(60)
        self.assertEqual(self.kv.get("e"), "v2")

    def test_06_delete(self) -> None:
        self.kv.set("d", "v")
        self.assertTrue(self.kv.delete("d"))
        self.assertIsNone(self.kv.get("d"))
        self.assertFalse(self.kv.delete("d"))           # 幂等：再删返回 False
        self.assertEqual(len(self.kv), 0)

    def test_07_expire(self) -> None:
        self.kv.set("e", "v")
        self.assertFalse(self.kv.expire("nope", 5))     # 不凭空造 key
        self.assertTrue(self.kv.expire("e", 3))
        self.clock.advance(2.9)
        self.assertEqual(self.kv.get("e"), "v")
        self.clock.advance(0.2)
        self.assertIsNone(self.kv.get("e"))

    def test_08_info_not_shared(self) -> None:
        info = self.kv.info()
        self.assertEqual(info.backend, "in-memory")
        self.assertTrue(info.atomic)                    # 进程内是原子的
        self.assertFalse(info.shared)                   # 但跨副本看不见 —— 必须自曝
        self.assertIn("RESPKV", info.note)


# ══════════════════════════════════════════════════════════════
# ② RESPKV（redis-py × 假服务器）
# ══════════════════════════════════════════════════════════════
class TestRESPKV(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.redis = FakeRedis().start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.redis.stop()

    def setUp(self) -> None:
        #: 每个用例一个独立命名空间，用例之间互不污染（也是前缀隔离的日常形态）
        self.ns = f"t{id(self) % 100000}:"
        self.kv = self.client(self.ns)

    def client(self, prefix: str | None = None, **kw) -> RESPKV:
        # 单独一条连接池：并发用例之外的"另一个客户端"都靠它
        client = RESPKV(port=self.redis.port, key_prefix=prefix or self.ns, **kw)
        self.addCleanup(client.close)
        return client

    def raw(self) -> "redis.Redis":
        """裸 redis-py 客户端：用来验证"库 × 假服务端"的协议行为本身。"""
        client = redis.Redis(host="127.0.0.1", port=self.redis.port,
                             decode_responses=True,
                             socket_timeout=2.0, socket_connect_timeout=2.0)
        self.addCleanup(client.close)
        return client

    # ── 握手 / 协议 ────────────────────────────────────────
    def test_00_handshake_and_resp_versions(self) -> None:
        """换库之后的新事实：握手由库完成，协议版本是我们**显式钉住**的。

        这里顺手纠正一个想当然的假设：**「服务端回 -ERR 给 HELLO，客户端就降级到
        RESP2」不成立**（redis-py 8.x 会抛 `ResponseError`，连接直接失败）。
        所以 `RESPKV` 显式 `protocol=2`，而假服务器也实现了真 HELLO，
        让"默认 RESP3 客户端"同样能跑 —— 两种都验，才不会靠猜。
        """
        before = len(self.redis.data.commands)
        self.kv.get("warmup")                       # 触发一次真实握手
        sent = self.redis.data.commands[before:]
        self.assertIn("CLIENT", sent, "redis-py 握手会发 CLIENT SETINFO")
        self.assertNotIn("HELLO", sent, "RESPKV 钉的是 RESP2：不该发 HELLO")

        # 默认协议（RESP3）的库里客户端连得上 = 假服务器的 HELLO 是真的
        resp3 = self.raw()
        self.assertTrue(resp3.ping())
        self.assertIn("HELLO", self.redis.data.commands)

        # 反过来：服务端不支持 HELLO（Redis 5）时，RESP3 客户端直接连不上，
        # 而钉了 RESP2 的 RESPKV 毫发无伤 —— 这就是钉版本的现实理由。
        self.redis.data.reject_hello = True
        try:
            with self.assertRaises(redis.exceptions.RedisError):
                self.raw().ping()
            self.assertTrue(self.kv.ping())
        finally:
            self.redis.data.reject_hello = False

    def test_09_ping(self) -> None:
        self.assertTrue(self.kv.ping())
        # 库的 PING 回调把 "PONG" 翻成了 True（legacy_responses=True）——
        # 这层翻译由库做，我们只保证"活着 / 不活着"
        self.assertIs(self.kv.command("PING"), True)
        dead = RESPKV(port=free_port(), timeout_s=0.5)
        self.addCleanup(dead.close)
        self.assertFalse(dead.ping())                   # 探测不抛异常，返回 False

    def test_10_get_set_roundtrip(self) -> None:
        self.assertIsNone(self.kv.get("k"))
        self.assertTrue(self.kv.set("k", "v1"))
        self.assertEqual(self.kv.get("k"), "v1")
        self.assertTrue(self.kv.set("k", "v2"))
        self.assertEqual(self.kv.get("k"), "v2")
        self.assertTrue(self.kv.command("PING"))        # 连接复用后仍然同步

    def test_11_set_nx(self) -> None:
        self.assertTrue(self.kv.set_nx("lock", "a"))
        self.assertFalse(self.kv.set_nx("lock", "b"))
        self.assertEqual(self.kv.get("lock"), "a")
        self.kv.delete("lock")
        self.assertTrue(self.kv.set_nx("lock", "c"))
        # 带 TTL 的 NX 也要在竞争失败时**不影响**原有的 TTL
        self.assertTrue(self.kv.set_nx("ttl_lock", "x", ttl_s=0.2))
        self.assertFalse(self.kv.set_nx("ttl_lock", "y", ttl_s=10))
        time.sleep(0.28)
        self.assertIsNone(self.kv.get("ttl_lock"))

    def test_12_incr_and_first_ttl(self) -> None:
        self.assertEqual(self.kv.incr("n"), 1)
        self.assertEqual(self.kv.incr("n", 4), 5)
        self.assertEqual(self.kv.get("n"), "5")
        # 第一次 incr 建 key 时设 TTL；第二次**不续期**
        self.assertEqual(self.kv.incr("w", 1, ttl_s=0.15), 1)
        time.sleep(0.09)
        self.assertEqual(self.kv.incr("w", 1, ttl_s=0.15), 2)
        time.sleep(0.09)                                # 总计 0.18 > 0.15 → 必须已过期
        self.assertIsNone(self.kv.get("w"))
        # 非整数 → KVError（服务端 ERR，不是崩溃）
        self.kv.set("s", "abc")
        with self.assertRaises(KVError):
            self.kv.incr("s")

    # ── CAS：核心 ──────────────────────────────────────────
    def test_13_cas_concurrent_exactly_one_winner(self) -> None:
        key = "cas_race"
        self.kv.set(key, "v0")
        workers = 10
        barrier = threading.Barrier(workers)
        results: list[tuple[int, bool, str]] = []
        lock = threading.Lock()

        def worker(idx: int) -> None:
            # 每个线程**一个独立客户端 + 独立连接池**：并发必须是真的
            client = RESPKV(port=self.redis.port, key_prefix=self.ns)
            try:
                barrier.wait(5)         # 十个客户端同时开火
                ok = client.compare_and_set(key, "v0", f"w{idx}")
                with lock:
                    results.append((idx, ok, f"w{idx}"))
            finally:
                client.close()

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(workers)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
            self.assertFalse(t.is_alive(), "CAS 并发用例超时：疑似死锁")

        self.assertEqual(len(results), workers, "有线程没写回结果（死锁或丢线程）")
        winners = [idx for idx, ok, _ in results if ok]
        self.assertEqual(len(winners), 1,
                         f"CAS 必须恰好一个赢家，实际 {len(winners)} 个：{winners}")
        self.assertEqual(self.kv.get(key), f"w{winners[0]}")

    def test_14_cas_expected_mismatch(self) -> None:
        self.kv.set("c", "actual")
        self.assertFalse(self.kv.compare_and_set("c", "expected", "new"))
        self.assertEqual(self.kv.get("c"), "actual")    # 值未被改动
        # key 不存在时 expected=None 才成立
        self.assertFalse(self.kv.compare_and_set("absent", "whatever", "new"))
        self.assertIsNone(self.kv.get("absent"))
        self.assertTrue(self.kv.compare_and_set("absent", None, "created"))
        self.assertEqual(self.kv.get("absent"), "created")

    def test_15_cas_value_after_write(self) -> None:
        self.kv.set("c", "v1")
        self.assertTrue(self.kv.compare_and_set("c", "v1", "v2", ttl_s=0.25))
        self.assertEqual(self.kv.get("c"), "v2")        # 写入的值正确
        time.sleep(0.3)
        self.assertIsNone(self.kv.get("c"))             # 带 ttl 的 CAS 真的设了 TTL

        # 链式 CAS 能接上（每步的 expected 都是上一步写进去的值）
        self.kv.set("d", "v1")
        self.assertTrue(self.kv.compare_and_set("d", "v1", "v2"))
        self.assertTrue(self.kv.compare_and_set("d", "v2", "v3"))
        self.assertEqual(self.kv.get("d"), "v3")
        self.assertFalse(self.kv.compare_and_set("d", "v1", "v4"))
        self.assertEqual(self.kv.get("d"), "v3")        # 冲突不写

        # 一条与 InMemoryKV 对齐的语义细节：不带 ttl 的写入**清掉**旧 TTL
        # （真 Redis 的 SET 就是这行为，要保留 TTL 得用 KEEPTTL）。
        self.kv.set("e", "v1", ttl_s=0.1)
        self.assertTrue(self.kv.compare_and_set("e", "v1", "v2"))
        time.sleep(0.2)
        self.assertEqual(self.kv.get("e"), "v2")        # TTL 被清掉，不该过期

    def test_16_delete(self) -> None:
        self.kv.set("d", "v")
        self.assertTrue(self.kv.delete("d"))
        self.assertIsNone(self.kv.get("d"))
        self.assertFalse(self.kv.delete("d"))

    # ── 连接与错误 ─────────────────────────────────────────
    def test_17_connection_refused_is_kvunavailable(self) -> None:
        port = free_port()
        kv = RESPKV(port=port, timeout_s=0.3)
        self.addCleanup(kv.close)
        with self.assertRaises(KVUnavailable) as ctx:
            kv.get("x")
        self.assertIsInstance(ctx.exception, KVError)   # 也必须是 KVError 的子类
        self.assertIn(str(port), str(ctx.exception))    # 错误信息要能定位到目标
        # 库的异常类型**不许**漏到上层来
        self.assertNotIsInstance(ctx.exception, redis.exceptions.RedisError)
        with self.assertRaises(KVUnavailable):
            kv.set("x", "1")
        with self.assertRaises(KVUnavailable):
            kv.incr("x")

    def test_18_reconnect_after_server_returns(self) -> None:
        kv = self.client()
        kv.set("k", "v1")
        baseline = kv.connects
        self.redis.stop()
        # 服务器没了：必须报 KVUnavailable，而不是裸 ConnectionResetError
        with self.assertRaises(KVUnavailable):
            kv.get("k")
        self.redis.start(self.redis.port)               # 同端口重启，数据仍在
        self.assertEqual(kv.get("k"), "v1")             # 连接池自动重连 → 恢复
        self.assertGreater(kv.connects, baseline,
                           "连接池没有重建 socket：'自动重连'只是说法")

    def test_19_info_shared(self) -> None:
        info = self.kv.info()
        self.assertEqual(info.backend, "redis")
        self.assertTrue(info.atomic)
        self.assertTrue(info.shared)                    # 跨副本可见 —— 这是它的卖点
        self.assertIn(str(self.redis.port), info.note)
        self.assertIn("redis-py", info.note)            # 自述要写清"用的是哪个库"

    # ── 协议边界 ───────────────────────────────────────────
    def test_20_large_value_100kb(self) -> None:
        big = "x" * 102400
        self.assertTrue(self.kv.set("big", big))
        got = self.kv.get("big")
        self.assertEqual(len(got or ""), 102400)
        self.assertEqual(got, big)
        # 大值也要能参与 CAS（协议层不能只在小包上正确）
        self.assertTrue(self.kv.compare_and_set("big", big, big + "!"))
        self.assertEqual(self.kv.get("big"), big + "!")

    def test_21_key_prefix_isolation(self) -> None:
        a = self.client("ns_a:")
        b = self.client("ns_b:")
        self.assertTrue(a.set("same", "A"))
        self.assertTrue(b.set("same", "B"))
        self.assertEqual(a.get("same"), "A")
        self.assertEqual(b.get("same"), "B")
        self.assertEqual(a.physical_key("same"), "ns_a:same")
        # 前缀也让 SCAN 能按命名空间捞 key
        cursor, keys = a.scan(0, match="ns_a:*", count=100)
        self.assertEqual(cursor, 0)
        self.assertEqual(keys, ["ns_a:same"])

    def test_22_error_reply_raises_kverror(self) -> None:
        with self.assertRaises(KVError) as ctx:
            self.kv.command("NO_SUCH_COMMAND")
        self.assertIn("unknown command", str(ctx.exception))
        # 错误回复之后协议仍然同步：下一条命令必须正常工作（不重连、不错位）
        self.assertTrue(self.kv.set("after", "ok"))
        self.assertEqual(self.kv.get("after"), "ok")
        # 服务端语义错误也是 KVError
        self.kv.set("str", "abc")
        with self.assertRaises(KVError):
            self.kv.command("INCRBY", self.kv.physical_key("str"), "1")

    # ── WATCH 存在的理由（正向 + 负向）──────────────────────
    def test_23_watch_detects_concurrent_modification(self) -> None:
        """把 SET 手工插到 WATCH 与 EXEC 之间：EXEC 必须返回 nil（库 → WatchError）。

        这条测的是「真客户端 × 真协议 × 我们的假服务端」的乐观锁语义 ——
        `RESPKV.compare_and_set` 正是靠这条通路保证原子（见模块 docstring）。
        """
        a = self.raw()
        b = self.raw()
        physical = "bb"

        a.set(physical, "v1")
        pipe = a.pipeline()
        try:
            pipe.watch(physical)                          # WATCH：服务端记版本
            self.assertEqual(pipe.get(physical), "v1")    # GET：同一连接、立即执行
            b.set(physical, "v2")                         # B 从另一条连接改了它
            pipe.multi()                                  # MULTI：进入队列模式
            pipe.set(physical, "v3")                      # 只是排队
            with self.assertRaises(redis.WatchError):     # EXEC → nil
                pipe.execute()
        finally:
            pipe.reset()
        self.assertEqual(b.get(physical), "v2")           # A 的写一条都没落地

        # 反向对照：期间没人动过 → EXEC 真的执行，且返回每条命令的结果
        pipe = a.pipeline()
        try:
            pipe.watch(physical)
            pipe.multi()
            pipe.set(physical, "v4")
            self.assertEqual(pipe.execute(), [True])
        finally:
            pipe.reset()
        self.assertEqual(a.get(physical), "v4")

    def test_24_naive_get_then_set_loses_updates(self) -> None:
        """负向对照：GET+SET 冒充 CAS 会丢更新。这就是不能用它替代 WATCH 的原因。"""
        key = "naive"
        self.kv.set(key, "v0")
        workers = 6
        barrier = threading.Barrier(workers)
        wins: list[int] = []
        lock = threading.Lock()

        def naive_cas(idx: int) -> None:
            client = RESPKV(port=self.redis.port, key_prefix=self.ns)
            try:
                current = client.get(key)          # ① 读
                barrier.wait(5)                    # 让所有读者都停在"读完之后"
                if current != "v0":                # ② 判定（用的是过期的观察）
                    return
                client.set(key, f"w{idx}")         # ③ 写（覆盖掉别人刚写的）
                with lock:
                    wins.append(idx)
            finally:
                client.close()

        threads = [threading.Thread(target=naive_cas, args=(i,)) for i in range(workers)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
            self.assertFalse(t.is_alive(), "naive CAS 用例超时")

        # 每个客户端都"确认"自己没冲突 —— 而实际上只有最后一次写入活了下来
        self.assertEqual(len(wins), workers,
                         "这个对照用例的前提就是大家都会成功；若不再成立说明时序需要重写")
        self.assertIn(self.kv.get(key), {f"w{i}" for i in wins})
        # 结论：naive 版本报出的成功次数是假的；真 CAS（test_13）只会有一个赢家。

    # ── 非幂等命令与重发 ───────────────────────────────────
    def test_31_lost_reply_does_not_double_count(self) -> None:
        """回包丢失时 INCRBY **不许**重发：配额多算一次是不可观察的 bug。"""
        physical = self.kv.physical_key("quota")
        self.redis.data.drop_incr_reply.add(physical)
        with self.assertRaises(KVUnavailable) as ctx:
            self.kv.incr("quota")            # 服务端执行了，但回包丢了
        self.assertIn("INCRBY", str(ctx.exception))

        other = self.client()
        self.assertEqual(other.get("quota"), "1")     # 恰好一次，没有重复计数
        self.assertEqual(other.incr("quota"), 2)      # 断线后下一次调用正常

    def test_32_library_default_retry_would_double_count(self) -> None:
        """负向对照：redis-py 默认 `retries=10` 会自动重发 INCRBY → 必然多算。

        这就是 `RESPKV` 把客户端 pin 成 `Retry(NoBackoff(), 0)` 的原因：
        "简单重试"在非幂等命令上是错的，而库的默认值对幂等性一无所知。
        """
        raw = self.raw()                              # 故意用**库的默认**重试策略
        physical = f"{self.ns}quota_default"
        self.assertEqual(raw.incrby(physical, 1), 1)
        self.redis.data.drop_incr_reply.add(physical)  # 下一次 INCRBY：生效了但回包丢
        value = raw.incrby(physical, 1)                # 库自己重发了一次
        self.assertEqual(value, 3, "库的默认重试应当把同一条 INCRBY 重发（=多算一次）")
        self.assertEqual(raw.get(physical), "3",
                         "加了两下：正确值应是 2，说明重发把配额多算了一次")

    def test_33_disconnect_during_watch_is_unavailable_not_conflict(self) -> None:
        """WATCH 期间断线：结果**未知**，必须是 KVUnavailable，不能是 False。"""
        key = "watch_drop"
        self.kv.set(key, "v0")
        self.redis.data.drop_after_watch = True       # WATCH 应答完就掐连接
        with self.assertRaises(KVUnavailable):
            self.kv.compare_and_set(key, "v0", "v1")
        self.assertFalse(self.redis.data.drop_after_watch)
        # 没写进去，也没被误报成"冲突"：值仍是旧的
        self.assertEqual(self.kv.get(key), "v0")
        # 后续调用正常（连接池自愈）
        self.assertTrue(self.kv.compare_and_set(key, "v0", "v2"))
        self.assertEqual(self.kv.get(key), "v2")

    # ── 换库带来的可测性 / 可观测性 ────────────────────────
    def test_34_injected_client_is_used(self) -> None:
        """注入客户端：`RESPKV` 用它，而不是自己建连接（可测性的来源）。"""
        stub = _StubClient({"inj:a": "A"})
        # 端口是死的：只要注入生效，就不该有任何网络动作
        kv = RESPKV(port=free_port(), timeout_s=0.05, key_prefix="inj:", client=stub)
        self.assertEqual(kv.get("a"), "A")
        self.assertEqual(stub.calls["get"], 1)
        self.assertEqual(stub.last_keys, ["inj:a"], "前缀仍然由本层拼")
        self.assertEqual(kv.connects, 0, "注入客户端时本层不该建任何连接")

        self.assertTrue(kv.set("b", "B"))
        self.assertEqual(stub.values["inj:b"], "B")
        # 谁建的池谁收尾：注入的客户端不归我们关
        kv.close()
        self.assertEqual(stub.calls.get("close", 0), 0)

    def test_35_missing_redis_library(self) -> None:
        """没装 redis 库：`RESPKV` 明确报错，`InMemoryKV` 照常可用。"""
        with mock.patch.object(kv_module, "redis", None):
            with self.assertRaises(KVUnavailable) as ctx:
                RESPKV()
            self.assertIn("redis 库未安装", str(ctx.exception))
            # 降级路径不受影响
            mem = InMemoryKV()
            mem.set("k", "v")
            self.assertEqual(mem.get("k"), "v")
            # 注入客户端也不需要库
            stub = _StubClient({"x:a": "A"})
            self.assertEqual(RESPKV(key_prefix="x:", client=stub).get("a"), "A")

    def test_36_pool_is_reused_across_100_gets(self) -> None:
        """连接池复用：100 次读不新建连接（库的池 vs 注入替身的计数器）。"""
        stub = _StubClient({"pool:a": "v"})
        kv = RESPKV(key_prefix="pool:", client=stub)
        for _ in range(100):
            self.assertEqual(kv.get("a"), "v")
        self.assertEqual(stub.calls["get"], 100)      # 100 次读，一个客户端实例
        self.assertEqual(kv.connects, 0)              # 复用注入的那一个，没自建

        real = self.client()
        real.set("k", "v")
        self.assertEqual(real.connects, 1)            # 一次握手一条连接
        for _ in range(100):
            self.assertEqual(real.get("k"), "v")
        self.assertEqual(real.connects, 1, "100 次 get 又建了连接：池没复用")


# ══════════════════════════════════════════════════════════════
# ③ RedisStore（5）
# ══════════════════════════════════════════════════════════════
def _ctx_payload(phase: str = "WAITING_USER", tenant: str = "acme",
                 task_id: str = "task_1") -> str:
    """最小但形状正确的 Context payload（与 ContextHandle.serialize() 同形）。"""
    return json.dumps({
        "schema_version": 1,
        "version": 3,
        "data": {"phase": phase, "task_id": task_id,
                 "principal": {"tenant": tenant}},
        "patch_log": [],
    }, ensure_ascii=False)


class TestRedisStore(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.redis = FakeRedis().start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.redis.stop()

    def setUp(self) -> None:
        self.prefix = f"s{id(self) % 100000}:"
        self.kv = RESPKV(port=self.redis.port, key_prefix="rt:")
        self.store = RedisStore(self.kv, prefix=self.prefix)

    def tearDown(self) -> None:
        self.kv.close()

    def test_25_save_load_delete_list_pending(self) -> None:
        payload = _ctx_payload()
        self.assertIsNone(self.store.load("ctx_a"))
        self.store.save("ctx_a", payload)
        self.assertEqual(self.store.load("ctx_a"), payload)
        self.assertEqual(self.store.list_pending(), ["ctx_a"])

        self.store.save("ctx_b", _ctx_payload(task_id="task_2"))
        self.assertEqual(self.store.list_pending(), ["ctx_a", "ctx_b"])

        # 后写覆盖先写（Context 是状态，不是流水账）
        self.store.save("ctx_a", payload + " ")
        self.assertTrue((self.store.load("ctx_a") or "").endswith(" "))

        self.assertTrue(self.store.delete("ctx_b"))
        self.assertFalse(self.store.delete("ctx_b"))
        self.assertIsNone(self.store.load("ctx_b"))
        self.assertEqual(self.store.list_pending(), ["ctx_a"])
        # 索引真的被清了（不是"列表时过滤掉"）
        self.assertEqual(self.kv.smembers(f"{self.prefix}index"), ["ctx_a"])
        # 非法 ctx_id 与非法 payload 的口径与其他 store 一致
        with self.assertRaises(ValueError):
            self.store.save("../evil", payload)
        with self.assertRaises(StoreError):
            self.store.save("ctx_c", b"bytes")          # type: ignore[arg-type]

    def test_26_list_by_tenant(self) -> None:
        self.store.save("a1", _ctx_payload(tenant="acme"))
        self.store.save("a2", _ctx_payload(tenant="acme", task_id="task_2"))
        self.store.save("g1", _ctx_payload(tenant="globex"))
        acme = self.store.list_by_tenant("acme")
        self.assertEqual(sorted(row["ctx_id"] for row in acme), ["a1", "a2"])
        self.assertEqual([row["ctx_id"] for row in self.store.list_by_tenant("globex")], ["g1"])
        self.assertEqual(self.store.list_by_tenant("nobody"), [])
        self.assertEqual(self.store.list_by_tenant("acme", limit=1)[0]["ctx_id"], "a2")
        # 租户变更：旧索引必须失效（否则会跨租户泄露）
        self.store.save("a1", _ctx_payload(tenant="globex"))
        self.assertEqual([row["ctx_id"] for row in self.store.list_by_tenant("acme")], ["a2"])
        self.assertEqual(sorted(row["ctx_id"] for row in self.store.list_by_tenant("globex")),
                         ["a1", "g1"])

    def test_27_stores_share_the_same_assertions(self) -> None:
        """四个实现跑**同一组**断言：不对齐就在这里炸。"""
        workdir = tempfile.mkdtemp(prefix="kv-store-parity-")
        sqlite_store = SQLiteStore()
        cases = {
            "MemoryStore": MemoryStore(),
            "FileStore": FileStore(workdir),
            "SQLiteStore": sqlite_store,
            "RedisStore(InMemoryKV)": RedisStore(InMemoryKV(), prefix=self.prefix + "mem:"),
            "RedisStore(RESPKV)": RedisStore(RESPKV(port=self.redis.port),
                                             prefix=self.prefix + "resp:"),
        }
        payload = _ctx_payload()
        try:
            for name, store in cases.items():
                with self.subTest(store=name):
                    self.assertIsNone(store.load("ctx_a"))
                    store.save("ctx_a", payload)
                    self.assertEqual(store.load("ctx_a"), payload)
                    self.assertEqual(store.list_pending(), ["ctx_a"])
                    store.save("ctx_b", _ctx_payload(task_id="task_2"))
                    self.assertEqual(sorted(store.list_pending()), ["ctx_a", "ctx_b"])
                    store.save("ctx_a", payload + " ")            # 覆盖
                    self.assertTrue((store.load("ctx_a") or "").endswith(" "))
                    self.assertIsNone(store.load("ctx_missing"))
                    self.assertTrue(store.delete("ctx_b"))
                    self.assertFalse(store.delete("ctx_b"))
                    self.assertIsNone(store.load("ctx_b"))
                    self.assertEqual(store.list_pending(), ["ctx_a"])
                    with self.assertRaises(ValueError):
                        store.save("../evil", payload)
                    with self.assertRaises(StoreError):
                        store.save("ctx_c", b"bytes")             # type: ignore[arg-type]
        finally:
            for store in cases.values():
                closer = getattr(store, "close", None)
                if callable(closer):
                    closer()
            shutil.rmtree(workdir, ignore_errors=True)

    def test_28_ttl_expiry(self) -> None:
        store = RedisStore(RESPKV(port=self.redis.port), ttl_s=0.15,
                           prefix=self.prefix + "ttl:")
        try:
            store.save("ctx_a", _ctx_payload())
            self.assertIsNotNone(store.load("ctx_a"))
            self.assertEqual(store.list_pending(), ["ctx_a"])
            time.sleep(0.3)
            self.assertIsNone(store.load("ctx_a"))          # TTL 到期
            self.assertEqual(store.list_pending(), [])      # 索引自愈（不只是过滤）
        finally:
            store.close()

    def test_29_cross_replica_visibility(self) -> None:
        """副本 A 写的，副本 B 立刻能看见并接手 —— 这就是整个模块的目的。"""
        replica_a = RedisStore(RESPKV(port=self.redis.port), prefix=self.prefix)
        replica_b = RedisStore(RESPKV(port=self.redis.port), prefix=self.prefix)
        try:
            payload = _ctx_payload(phase="WAITING_USER")
            replica_a.save("ctx_suspended", payload)
            self.assertEqual(replica_b.load("ctx_suspended"), payload)
            self.assertEqual(replica_b.list_pending(), ["ctx_suspended"])
            self.assertEqual(replica_b.list_by_tenant("acme")[0]["phase"], "WAITING_USER")
            # B 接手后删除，A 也立刻看不见
            self.assertTrue(replica_b.delete("ctx_suspended"))
            self.assertIsNone(replica_a.load("ctx_suspended"))
            self.assertEqual(replica_a.list_pending(), [])
        finally:
            replica_a.close()
            replica_b.close()

    def test_30_meta_parsing_is_best_effort(self) -> None:
        """解析不出 phase/tenant 的 payload 也必须能存能取（Store 层不做语义准入）。"""
        self.store.save("junk", "not-json-at-all")
        self.assertEqual(self.store.load("junk"), "not-json-at-all")
        self.assertEqual(self.store.list_pending(), ["junk"])
        row = self.store.list_recent(limit=1)[0]
        self.assertIsNone(row["phase"])
        self.assertIsNone(row["tenant"])
        stats = self.store.stats()
        self.assertEqual(stats["total"], 1)
        self.assertEqual(stats["by_tenant"], {"<none>": 1})
        self.assertIsNotNone(stats["newest"])
        # 好 payload 的索引字段要从 payload 里抠出来
        self.store.save("good", _ctx_payload(phase="DONE", tenant="acme"))
        row = [r for r in self.store.list_recent(limit=5) if r["ctx_id"] == "good"][0]
        self.assertEqual(row["phase"], "DONE")
        self.assertEqual(row["tenant"], "acme")
        self.assertEqual(row["task_id"], "task_1")
        self.assertEqual(sorted(self.store.stats()["by_tenant"]), ["<none>", "acme"])
        self.assertEqual(self.store.list_pending(), ["junk", "good"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
