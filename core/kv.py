"""core/kv.py — W28：分布式原子 KV（redis-py 客户端 + 单进程替身）。

===============================================================================
本模块的两个实现：一个自己写，一个换成了库 —— 理由分别是什么
===============================================================================
* `InMemoryKV`：**仍然自己写**，而且应该继续自己写。它是 60 行、零依赖的单进程
  替身：一把 `RLock` + 一个 dict + 一个可注入的假时钟（TTL 用例不用 sleep）。
  自写它的成本远低于引入一个"内存版 Redis"依赖，语义漂移由同一组断言
  （`tests/test_kv.py`）挡住。它的职责是让一千多条单测**不必连 Redis**。

* `RESPKV`：**换成了官方 `redis` 库（redis-py）的薄适配层**。它曾经是手写
  RESP2 客户端（socket + 自己实现 `SET NX` / `WATCH` / `MULTI` / `EXEC`）。
  能用、有并发证伪测试，但手写协议客户端是典型的「不该自己写」：

      连接池 / socket 超时与半包 / 断线重连时机 / RESP3 / Cluster 重定向 /
      Sentinel 选主 / TLS / pipeline 批量往返 / 重试策略

  每一项都是独立的坑，而我们的核心价值不在协议实现里。现在本类只保留四件事：
  key 前缀命名空间、入参校验、TTL 单位换算（`ex=` / `px=`）、**异常归一化**
  （库异常一律不外泄，上层只认 `KVError` / `KVUnavailable`）。

===============================================================================
为什么需要这一层：单进程里的"协调"在多副本部署下**全部失效**
==============================================================================
黑板用 `threading.RLock`、配额是进程内窗口、Context 落本地 SQLite —— 这些在
一个进程里都对。两个副本一起跑就同时坏掉两件事：

* **配额翻倍**：副本 A 和副本 B 各有一个"本窗口已用 20 次"的计数器，
  真实用量是 40。限流器变成了「限流 × 副本数」。
* **后写覆盖**：两个副本读同一份 Context、各自 patch、各自 save，
  后到的那个把前一个的修改**静默吃掉**，且没有任何痕迹。

跨副本唯一可靠的东西是**服务端原子操作**。所以本模块只暴露三类服务端能自己
保证原子的动作：

    SETNX / INCRBY / (WATCH+MULTI+EXEC 组成的 CAS)

**刻意不提供的**：`get` 之后 `set` 的"读-改-写"组合。它看起来是 CAS，
实际是把竞态从调用方搬到了网络延迟上 —— 那正是要修的 bug，不能当接口卖出去。

===============================================================================
CAS 为什么必须是 WATCH/MULTI/EXEC（以及换库后它靠哪个 API 原子）
==============================================================================
乐观锁的正确形态是「读的时候登记一个版本，写的时候让服务端检查版本没变」：

    WATCH key                 ← 服务端记下 key 的当前版本（不是客户端记）
    GET key                   ← 拿到 expected 对应的那个值
    （不匹配 → UNWATCH，返回 False）
    MULTI                     ← 进入队列模式
    SET key value             ← 只是排队，不执行
    EXEC                      ← 服务端在这**一个**时刻检查 WATCH 的 key 有没有被改过：
                                  改过 → 返回 nil（整个事务丢弃，一条都没写）
                                  没改 → 原子执行队列，返回结果数组

实现落点：`redis-py` 的 `client.pipeline(transaction=True)`。`watch()` /
`multi()` / `execute()` 分别对应 `WATCH` / `MULTI` / `EXEC`，`execute()` 把
`MULTI + SET + EXEC` **打包成一次写入**发出；`EXEC` 返回 nil 时库抛
`WatchError`。原子性来自 Redis 服务端的 EXEC 判定，不是库的封装 —— 库只是
把三段协议帧一次发完，并保证 WATCH/GET/MULTI/EXEC 都落在**同一条连接**上
（这是 `Pipeline` 的核心承诺，也是手写版最容易写错的地方）。

而 `GET` + `SET` 的顺序是：

    A: GET  → v1          B: GET  → v1
    A: SET v2             B: SET v3        ← A 的修改被 B 静默覆盖

两个客户端都能拿到 `ok=True`，都以为自己赢了。这不是"概率很低的竞态"：
它和并发度成正比，且**没有任何信号**（没有异常、没有冲突计数），
只会在事后审计里表现为「这条记录怎么少了一次更新」。

===============================================================================
换库时踩到的两个坑（踩过就写下来，否则下一个人还得再踩）
===============================================================================
1. **库的默认重试会重发非幂等命令。** redis-py 8.x 的客户端默认
   `Retry(ExponentialWithJitterBackoff(), retries=10)`，对 `ConnectionError` /
   `TimeoutError` **自动重发整条命令**。对 `GET`/`SET` 没问题；对 `INCRBY`
   就是"配额多算一次"，对 `SET NX` 就是"明明抢到了报成没抢到" —— 正是本模块
   存在的理由。所以这里显式把客户端 pin 成 `Retry(NoBackoff(), 0)`，并把
   "要不要重发"收回本层：幂等命令重试一次，非幂等命令一次都不重发。
   `tests/test_kv.py::test_32` 是这条结论的负向对照：库的默认策略下，
   一次 `INCRBY` 在回包丢失后会真的把计数加到 2。

2. **`WatchError` 有两种含义。** `EXEC` 返回 nil（真冲突）和 "WATCH 期间连接
   断了"（结果未知）在库里都是 `WatchError`。把后者映射成 `False` 就是把
   "不知道"说成"没抢到" —— 那正是静默错误。所以只把消息为
   `Watched variable changed.` 的那种判成冲突，其余一律 `KVUnavailable`：
   宁可报错，不可假装。

===============================================================================
连接与错误（换库后的分工）
===============================================================================
* 连接池、socket 超时、握手（`CLIENT SETINFO` 等）、陈旧连接检测
  （`health_check_interval`）与重连：**全部交给库**。
* 本层只做异常归一化：`redis.exceptions.ConnectionError` / `TimeoutError` /
  `OSError` → `KVUnavailable`；服务端 `-ERR`（`ResponseError`）→ `KVError`。
  上层永远不需要 `import redis` 去 `except redis.exceptions.*`。
* CAS 序列中途不许重试：`WATCH` 状态挂在**连接**上，重连后 `EXEC` 会无条件
  成功 —— 那等于把 CAS 悄悄降级成盲写。所以 CAS 路径的每一次调用都是
  `retry=False`，断线直接 `KVUnavailable`，让调用方重试**整个** CAS。

===============================================================================
边界（诚实写在这里）
===============================================================================
* 值按 UTF-8 解码成 `str`（SPI 是字符串语义）。非 UTF-8 字节用
  `errors="replace"` 兜住 —— 要存二进制请 base64 一层，不要指望本模块。
* 集合（`sadd` / `srem` / `smembers`）是 `contracts.KV` 之外的**扩展**：
  `RedisStore` 的二级索引需要它。`smembers` 返回**排序后**的 list（Redis 的
  集合本身无序，排序只是为了断言稳定）。
* `InMemoryKV` 的集合不参与 TTL 过期（只有标量 key 有 deadline）。
* `redis` 是**可选依赖**：没装库时 `InMemoryKV` 照常可用，只有构造 `RESPKV`
  才抛 `KVUnavailable("redis 库未安装")`。
"""
from __future__ import annotations

import fnmatch
import threading
import time
from typing import Any, Callable

from contracts import DistributedInfo

try:                                     # 可选依赖：InMemoryKV 不需要它
    import redis
    from redis import exceptions as _redis_exc
except ImportError:                      # pragma: no cover - venv 里装了库
    redis = None                         # type: ignore[assignment]
    _redis_exc = None                    # type: ignore[assignment]

__all__ = [
    "KVError",
    "KVUnavailable",
    "InMemoryKV",
    "RESPKV",
]

#: 连接层故障（库的 ConnectionError / TimeoutError 都是 RedisError 的子类，
#: 但语义上与"--ERR 服务端拒绝了命令"完全不同：前者可以重试，后者重试没用）。
_CONN_ERRORS: tuple[type[BaseException], ...] = (
    (OSError,) if _redis_exc is None
    else (OSError, _redis_exc.ConnectionError, _redis_exc.TimeoutError)
)

#: 服务端错误回复（`-ERR ...`）。协议此时仍然同步，**不需要**重连。
_SERVER_ERRORS: tuple[type[BaseException], ...] = (
    (OSError,) if _redis_exc is None else (_redis_exc.RedisError,)
)

#: `EXEC` 被否时 redis-py 抛出的 `WatchError` 文案（版本差异会让这条失效，
#: 失效的方向是"冲突被报成不可用"—— 报错，而不是静默当成没抢到）。
_ABORT_TEXT = "Watched variable changed"

#: 陈旧连接探测间隔（秒）：库用它决定下次借出连接前要不要 PING 一下。
#: 这是"断线重连交给库"的那一半；另一半是借出连接时的 `can_read()` 探测。
_HEALTH_CHECK_S = 30


# ══════════════════════════════════════════════════════════════
# 错误
# ══════════════════════════════════════════════════════════════
class KVError(Exception):
    """KV 操作失败的基类（协议错误、类型错误、服务端错误回复）。"""


class KVUnavailable(KVError):
    """连不上 / 连上了又断 / 重连失败 / 库没装。**上层只需认这一个异常**。"""


# ══════════════════════════════════════════════════════════════
# 参数校验小工具（两个实现共用同一口径）
# ══════════════════════════════════════════════════════════════
def _str_arg(value: Any, name: str) -> str:
    if not isinstance(value, str):
        raise KVError(f"{name} 必须是 str，收到 {type(value).__name__}")
    if not value:
        raise KVError(f"{name} 不能是空串")
    return value


def _int_arg(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise KVError(f"{name} 必须是 int，收到 {type(value).__name__}")
    return value


def _ttl_arg(ttl_s: Any) -> float | None:
    """规整 TTL：`None` 或正数秒。0/负数一律拒绝（删 key 请走 `delete`/`expire`）。"""
    if ttl_s is None:
        return None
    if isinstance(ttl_s, bool) or not isinstance(ttl_s, (int, float)):
        raise KVError(f"ttl_s 必须是数值或 None，收到 {type(ttl_s).__name__}")
    value = float(ttl_s)
    if value <= 0:
        raise KVError(f"ttl_s 必须 > 0，收到 {ttl_s!r}（要删除请用 delete）")
    return value


def _ttl_command(ttl_s: Any) -> tuple[str, int]:
    """TTL → `("EX", 秒)` 或 `("PX", 毫秒)`。

    真 Redis 的 `EX` 只吃整数秒，所以亚秒 TTL 必须走 `PX`；
    这条不是洁癖 —— 配额窗口的 TTL 常常是 0.5s 级，用 EX 会把 0.5 变成 0（非法）。
    """
    value = _ttl_arg(ttl_s)
    if value is None:
        raise KVError("内部错误：_ttl_command 不接受 None")
    if value >= 1 and value == int(value):
        return "EX", int(value)
    return "PX", max(1, int(round(value * 1000)))


# ══════════════════════════════════════════════════════════════
# InMemoryKV：单进程替身（语义与真 Redis 对齐）
# ══════════════════════════════════════════════════════════════
class InMemoryKV:
    """进程内原子 KV。**不跨进程、不跨副本** —— 它自己会承认（`shared=False`）。

    存在的理由不是"省一个 Redis"，而是：
    ① 单测不该依赖外部服务；② 语义对齐后，用它写的代码换 `RESPKV` 一行就能上分布式；
    ③ 两套实现的测试是**同一组断言**（见 tests/test_kv.py），语义漂移会立刻暴露。

    原子性来自一把 `RLock`：所有"读-判定-写"的序列都在锁内完成，
    所以 `compare_and_set` 在这里天然是原子的 —— 这与"跨副本不原子"不矛盾：
    `info().shared` 就是用来区分这两件事的。
    """

    def __init__(self, *, clock: Callable[[], float] | None = None) -> None:
        #: 时钟可注入：TTL 要能在测试里"推进"，否则每个 ttl 用例都得 sleep
        self._clock: Callable[[], float] = clock or time.monotonic
        self._lock = threading.RLock()
        #: key → (value, deadline)；deadline=None 表示永不过期
        self._data: dict[str, tuple[str, float | None]] = {}
        #: 集合（二级索引用），不参与 TTL
        self._sets: dict[str, set[str]] = {}

    # ── 内部 ───────────────────────────────────────────────
    def _now(self) -> float:
        return self._clock()

    def _alive(self, key: str) -> str | None:
        """读并顺手清掉过期的（调用方必须已持锁）。"""
        item = self._data.get(key)
        if item is None:
            return None
        value, deadline = item
        if deadline is not None and self._now() >= deadline:
            del self._data[key]
            return None
        return value

    def _deadline_for(self, key: str, ttl_s: float | None) -> float | None:
        """写一个新值时该配的 deadline（`ttl_s=None` → 永久，顺手清掉旧 TTL）。"""
        ttl = _ttl_arg(ttl_s)
        return None if ttl is None else self._now() + ttl

    # ── contracts.KV ───────────────────────────────────────
    def get(self, key: str) -> str | None:
        _str_arg(key, "key")
        with self._lock:
            return self._alive(key)

    def set(self, key: str, value: str, *, ttl_s: float | None = None) -> bool:
        _str_arg(key, "key")
        if not isinstance(value, str):
            raise KVError(f"value 必须是 str，收到 {type(value).__name__}")
        with self._lock:
            self._data[key] = (value, self._deadline_for(key, ttl_s))
            return True

    def set_nx(self, key: str, value: str, *, ttl_s: float | None = None) -> bool:
        """`SET NX`：**首次成功，之后一律失败**（与真 Redis 一致，不刷新 TTL）。"""
        _str_arg(key, "key")
        if not isinstance(value, str):
            raise KVError(f"value 必须是 str，收到 {type(value).__name__}")
        with self._lock:
            if self._alive(key) is not None:
                return False
            self._data[key] = (value, self._deadline_for(key, ttl_s))
            return True

    def compare_and_set(self, key: str, expected: str, value: str,
                        *, ttl_s: float | None = None) -> bool:
        """CAS：当前值 == `expected` 才写。

        `expected=None` 的用法是"仅当 key 不存在时创建"（与 RESP 的 nil 对齐）——
        `GET` 一个不存在的 key 返回 nil，nil 只等于 `None`。
        """
        _str_arg(key, "key")
        if expected is not None and not isinstance(expected, str):
            raise KVError(f"expected 必须是 str 或 None，收到 {type(expected).__name__}")
        if not isinstance(value, str):
            raise KVError(f"value 必须是 str，收到 {type(value).__name__}")
        with self._lock:
            if self._alive(key) != expected:
                return False
            self._data[key] = (value, self._deadline_for(key, ttl_s))
            return True

    def incr(self, key: str, delta: int = 1, *, ttl_s: float | None = None) -> int:
        """`INCRBY`。**从 0 起**；TTL 只在"这次是创建"时设置（窗口不随写入延长）。"""
        _str_arg(key, "key")
        step = _int_arg(delta, "delta")
        ttl = _ttl_arg(ttl_s)
        with self._lock:
            current = self._alive(key)
            if current is None:
                base, existed = 0, False
            else:
                try:
                    base = int(current)
                except (TypeError, ValueError) as exc:
                    raise KVError(f"key={key!r} 的值不是整数：{current!r}") from exc
                existed = True
            new_value = base + step
            if existed:
                # 已有计数器：沿用原 deadline（TTL 不因写入而续命）
                deadline = self._data[key][1]
            else:
                deadline = None if ttl is None else self._now() + ttl
            self._data[key] = (str(new_value), deadline)
            return new_value

    def delete(self, key: str) -> bool:
        _str_arg(key, "key")
        with self._lock:
            removed = self._data.pop(key, None) is not None
            # 集合与标量同名时一起清掉（Redis 里同名只可能是其中一种类型）
            removed = self._sets.pop(key, None) is not None or removed
            return removed

    def expire(self, key: str, ttl_s: float) -> bool:
        """给已存在的 key 设 TTL。key 不存在 → False（不凭空造 key）。"""
        _str_arg(key, "key")
        ttl = _ttl_arg(ttl_s)
        assert ttl is not None
        with self._lock:
            if self._alive(key) is None:
                return False
            value = self._data[key][0]
            self._data[key] = (value, self._now() + ttl)
            return True

    # ── 扩展：集合（RedisStore 的二级索引要用）──────────────
    def sadd(self, key: str, *members: str) -> int:
        _str_arg(key, "key")
        with self._lock:
            bucket = self._sets.setdefault(key, set())
            before = len(bucket)
            for m in members:
                bucket.add(_str_arg(m, "member"))
            return len(bucket) - before

    def srem(self, key: str, *members: str) -> int:
        _str_arg(key, "key")
        with self._lock:
            bucket = self._sets.get(key)
            if not bucket:
                return 0
            before = len(bucket)
            for m in members:
                bucket.discard(_str_arg(m, "member"))
            if not bucket:
                self._sets.pop(key, None)
            return before - len(bucket)

    def smembers(self, key: str) -> list[str]:
        _str_arg(key, "key")
        with self._lock:
            return sorted(self._sets.get(key, ()))

    # ── 扩展：扫描（不实现集合的 KV 可用它兜底）──────────────
    def scan(self, cursor: int = 0, *, match: str | None = None,
             count: int = 100) -> tuple[int, list[str]]:
        """返回 `(下一个游标, 本页 key)`；内存实现一次返回全部，游标恒为 0。"""
        del count                       # 内存里没必要分页，保留参数只为接口一致
        with self._lock:
            keys = set(self._data) | set(self._sets)
            live = sorted(k for k in keys if self._alive(k) is not None or k in self._sets)
        if match:
            live = [k for k in live if fnmatch.fnmatchcase(k, match)]
        return 0, live

    # ── 自述 ───────────────────────────────────────────────
    def info(self) -> DistributedInfo:
        """`atomic=True`（进程内靠锁原子）+ `shared=False`（跨副本看不见）。"""
        return DistributedInfo(
            backend="in-memory",
            atomic=True,
            shared=False,
            note="单进程原子；多副本部署会各自计数/各自覆盖，需换 RESPKV",
        )

    def __len__(self) -> int:
        with self._lock:
            return sum(1 for k in list(self._data) if self._alive(k) is not None)

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"<InMemoryKV keys={len(self)} sets={len(self._sets)}>"


# ══════════════════════════════════════════════════════════════
# RESPKV：真 Redis，基于官方 redis-py
# ══════════════════════════════════════════════════════════════
class RESPKV:
    """基于 `redis-py` 的 KV 实现（RESP2，连接池由库管理）。

    本类**不含任何协议代码**：不拼 RESP 帧、不管 socket、不判断该不该重连。
    它只做四件与业务有关的事：

    1. key 前缀命名空间（同一台 Redis 上多个 Runtime / 多个测试互不踩）；
    2. 入参校验（`str` / `int` / TTL 口径与 `InMemoryKV` 完全一致）；
    3. TTL 换算（整数秒走 `ex=`，亚秒走 `px=`）；
    4. 异常归一化（库异常绝不外泄，上层只认 `KVError` / `KVUnavailable`）。

    可注入：`RESPKV(client=fake)` 直接使用外部客户端（测试替身、共享连接池、
    或 Cluster / Sentinel 客户端）。注入的客户端**不由本类关闭**。

    命令映射（每一行都是"我们的语义 → 库的调用"）::

        get              → GET                      （值由库按 UTF-8 解码成 str）
        set              → SET   [EX n | PX ms]     （ttl_s 换算成库的 ex= / px=）
        set_nx           → SET   NX [EX|PX]         （返回 None = 竞争失败）
        compare_and_set  → WATCH + GET + MULTI + SET + EXEC（库的 Pipeline；EXEC=nil → False）
        incr             → EXISTS → INCRBY → [PEXPIRE|EXPIRE]（只有首次创建才设 TTL）
        delete           → DEL
        expire           → EXPIRE / PEXPIRE
        sadd/srem/smembers → SADD / SREM / SMEMBERS（契约外的扩展，RedisStore 要用）
        scan             → SCAN cursor MATCH pat COUNT n
    """

    def __init__(self, host: str = "127.0.0.1", port: int = 6379, *,
                 db: int = 0, password: str = "", timeout_s: float = 2.0,
                 key_prefix: str = "rt:",
                 client: "redis.Redis | None" = None) -> None:
        self.host = host
        self.port = int(port)
        self.db = _int_arg(db, "db")
        self.password = password or ""
        if isinstance(timeout_s, bool) or not isinstance(timeout_s, (int, float)):
            raise KVError(f"timeout_s 必须是数值，收到 {type(timeout_s).__name__}")
        if float(timeout_s) <= 0:
            raise KVError(f"timeout_s 必须 > 0，收到 {timeout_s!r}")
        self.timeout_s = float(timeout_s)
        if not isinstance(key_prefix, str):
            raise KVError(f"key_prefix 必须是 str，收到 {type(key_prefix).__name__}")
        #: 命名空间隔离：同一台 Redis 上多个 Runtime 不互相踩
        self.key_prefix = key_prefix

        #: 本实例建了几条 TCP 连接（**只为可观测性**：测试用它证明"真的重连了"）
        self._connects = 0

        if client is not None:
            self._verify_client(client)
            self._client: Any = client
            #: 注入的客户端归注入方所有：本类不替别人关连接池
            self._owns_client = False
        else:
            if redis is None:
                raise KVUnavailable(
                    "redis 库未安装：RESPKV 需要 redis-py（pip install redis）；"
                    "单进程场景可以直接用 InMemoryKV"
                )
            self._client = self._build_client()
            self._owns_client = True

    # ── 构造 ───────────────────────────────────────────────
    @staticmethod
    def _verify_client(client: Any) -> None:
        """注入的客户端至少得能做本 SPI 的事（早失败好过 AttributeError 四处飘）。"""
        for name in ("get", "set", "pipeline", "ping"):
            if not callable(getattr(client, name, None)):
                raise TypeError(
                    f"注入的 client 必须提供 {name}()（redis.Redis 接口的最小子集），"
                    f"收到 {type(client).__name__}"
                )

    def _build_client(self) -> Any:
        """建连接池 + 客户端。**重试策略在这里被显式 pin 死**（见模块 docstring 坑 1）。"""
        kv = self

        class _CountedConnection(redis.Connection):  # type: ignore[misc]
            """只加一个观测钩子：数一数这条连接**建了几次 socket**。

            库不暴露"到底重连了没有"，而"我以为它重连了"必须能被测试证伪
            （见 tests/test_kv.py 的 restart 用例）。钩子里不做任何重试/超时决策，
            那些仍然完全归库所有。
            """

            def connect(self) -> None:      # noqa: D401 - 覆写库方法
                fresh = self._sock is None
                super().connect()
                if fresh:
                    kv._connects += 1

        pool = redis.ConnectionPool(
            connection_class=_CountedConnection,
            host=self.host,
            port=self.port,
            db=self.db,
            password=self.password or None,
            socket_timeout=self.timeout_s,
            socket_connect_timeout=self.timeout_s,
            max_connections=32,
            # 值按 UTF-8 解成 str（SPI 是字符串语义）；坏字节替换而不是抛异常
            decode_responses=True,
            encoding="utf-8",
            encoding_errors="replace",
            # 断线重连的另一半：借出连接前若超过该间隔就 PING 一次
            health_check_interval=_HEALTH_CHECK_S,
            # RESP2：与上游 Redis 5/6 都兼容；RESP3 需要服务端 >= 6 且我们没用到
            protocol=2,
            legacy_responses=True,
            # 关键：**零自动重发**。库默认 retries=10，会把 INCRBY / SET NX
            # 在回包丢失后重新执行一遍 —— 配额多算一次，且无人察觉。
            retry=redis.retry.Retry(redis.backoff.NoBackoff(), 0),
            retry_on_error=[],
            # driver_info 走库默认值 → 握手时会发 CLIENT SETINFO 声明"我是 redis-py"；
            # 老服务端回 -ERR 时库自己忽略，不影响可用性
        )
        return redis.Redis(connection_pool=pool)

    # ── key 命名空间 ───────────────────────────────────────
    def physical_key(self, key: str) -> str:
        """逻辑 key → 真正落到 Redis 上的 key（前缀拼接）。测试/运维排查要用。"""
        return self.key_prefix + _str_arg(key, "key")

    # ── 异常归一化 ─────────────────────────────────────────
    def _unavailable(self, what: str, exc: BaseException) -> KVUnavailable:
        return KVUnavailable(
            f"redis {self.host}:{self.port} 不可用（{what}："
            f"{type(exc).__name__}: {exc}）"
        )

    def _server_error(self, what: str, exc: BaseException) -> KVError:
        return KVError(
            f"redis {self.host}:{self.port} 错误回复（{what}）：{exc}"
        )

    def _call(self, what: str, fn: Callable[[], Any], *,
              retry: bool = True) -> Any:
        """执行一次库调用并把异常翻译成我们的口径。

        `retry=True`（默认）只给**幂等**命令用：GET / SET / DEL / EXISTS /
        EXPIRE / SADD / SREM / SMEMBERS / SCAN / PING。断线重试一次是安全的
        （重发不改变结果），而且"重连"本身是库做的（下次借出连接时会 connect）。

        `retry=False` 给**非幂等**命令（INCRBY / SET NX）和 CAS 序列用：
        回包丢失说明服务端可能已经执行了，重发会把配额多算一次，或把"明明
        抢到的锁"报成失败。宁可报 `KVUnavailable` —— 它是可观察、可重试的。
        """
        attempts = 2 if retry else 1
        last: BaseException | None = None
        for attempt in range(attempts):
            try:
                return fn()
            except KVError:
                raise                       # 注入的替身可能直接抛我们的口径
            except _CONN_ERRORS as exc:
                last = exc
                if attempt + 1 >= attempts:
                    break
            except _SERVER_ERRORS as exc:
                raise self._server_error(what, exc) from exc
        assert last is not None
        raise self._unavailable(what, last) from last

    def command(self, *args: Any) -> Any:
        """裸命令逃生口：**原样发送，不加前缀、不改写**。

        给测试构造精确交错、以及给运维手工排查用。业务代码不该走这里 ——
        走它意味着你在绕过本层的命名空间与语义。
        """
        what = str(args[0]) if args else "COMMAND"
        return self._call(what, lambda: self._client.execute_command(*args))

    # ── 自述与健康检查 ─────────────────────────────────────
    def ping(self) -> bool:
        """True = 服务端活着。连不上 / 超时 → False（探测不该抛异常）。"""
        try:
            return bool(self._call("PING", lambda: self._client.ping()))
        except KVError:
            return False

    def info(self) -> DistributedInfo:
        version = getattr(redis, "__version__", "?") if redis is not None else "?"
        return DistributedInfo(
            backend="redis",
            atomic=True,
            shared=True,
            note=(f"redis-py {version} {self.host}:{self.port} db={self.db} "
                  f"protocol=2 prefix={self.key_prefix!r}"),
        )

    @property
    def connects(self) -> int:
        """本实例建过的 TCP 连接数（观测用；注入客户端时恒为 0）。"""
        return self._connects

    # ── contracts.KV ───────────────────────────────────────
    def get(self, key: str) -> str | None:
        physical = self.physical_key(key)
        value = self._call("GET", lambda: self._client.get(physical))
        return None if value is None else str(value)

    def set(self, key: str, value: str, *, ttl_s: float | None = None) -> bool:
        if not isinstance(value, str):
            raise KVError(f"value 必须是 str，收到 {type(value).__name__}")
        physical = self.physical_key(key)
        ttl = self._ttl_kwargs(ttl_s)
        # SET 幂等：断线重发安全 → 交给 _call 重试一次
        return bool(self._call(
            "SET", lambda: self._client.set(physical, value, **ttl)
        ))

    def set_nx(self, key: str, value: str, *, ttl_s: float | None = None) -> bool:
        """`SET key value NX`：真 Redis 在 NX 失败时返回 nil。**不重发**。"""
        if not isinstance(value, str):
            raise KVError(f"value 必须是 str，收到 {type(value).__name__}")
        physical = self.physical_key(key)
        ttl = self._ttl_kwargs(ttl_s)
        wrote = self._call(
            "SET NX",
            lambda: self._client.set(physical, value, nx=True, **ttl),
            retry=False,
        )
        return wrote is not None and bool(wrote)

    def compare_and_set(self, key: str, expected: str, value: str,
                        *, ttl_s: float | None = None) -> bool:
        """真正的原子 CAS：库的 `Pipeline`（`WATCH` + `GET` + `MULTI` + `SET` + `EXEC`）。

        返回 False 的两种情况都**没有写入任何东西**：
        ① `GET` 出来的值与 `expected` 不一致（我们主动 UNWATCH）；
        ② `EXEC` 返回 nil —— 说明 WATCH 之后有别人改了这个 key（被抢占）。

        整段序列走同一个 `Pipeline`（同一条连接，库负责钉住），且每一步都不许
        重试：重连后 WATCH 状态会丢，`EXEC` 就变成无条件成功 —— 那是静默降级。
        """
        physical = self.physical_key(key)
        if expected is not None and not isinstance(expected, str):
            raise KVError(f"expected 必须是 str 或 None，收到 {type(expected).__name__}")
        if not isinstance(value, str):
            raise KVError(f"value 必须是 str，收到 {type(value).__name__}")
        ttl = self._ttl_kwargs(ttl_s)

        pipe = None
        try:
            pipe = self._client.pipeline(transaction=True)
            pipe.watch(physical)                # 服务端记下版本（在专用连接上立即执行）
            current = pipe.get(physical)         # WATCH 之后的命令即时执行，不排队
            current = None if current is None else str(current)
            if current != expected:
                pipe.unwatch()
                return False

            pipe.multi()                        # 进入队列模式
            pipe.set(physical, value, **ttl)    # 只是排队
            pipe.execute()                       # MULTI+SET+EXEC 一次发出；被抢占 → 抛
            return True
        except _CONN_ERRORS as exc:
            raise self._unavailable("CAS", exc) from exc
        except _redis_exc.WatchError as exc:     # type: ignore[union-attr]
            if _is_abort(exc):
                return False                     # EXEC 返回 nil：服务端否掉了整个事务
            # WATCH 期间连接断了：结果**未知**，不能说成"没抢到"
            raise self._unavailable("CAS", exc) from exc
        except _SERVER_ERRORS as exc:
            raise self._server_error("CAS", exc) from exc
        finally:
            if pipe is not None:
                pipe.reset()                     # 幂等：把连接还回池、清掉 WATCH 状态

    def incr(self, key: str, delta: int = 1, *, ttl_s: float | None = None) -> int:
        """`INCRBY`；TTL 只在"这次创建了计数器"时设置。

        `EXISTS` 与 `INCRBY` 之间不是原子的：两个副本可能都看到"不存在"、
        都设一次 EXPIRE。两次设的是同一个 TTL 值，结果一致 —— 这个竞态是**良性**的。
        真正必须原子的是计数本身，那个由 `INCRBY` 自己保证。
        """
        physical = self.physical_key(key)
        step = _int_arg(delta, "delta")
        ttl = self._ttl_kwargs(ttl_s)

        existed = bool(self._call("EXISTS", lambda: self._client.exists(physical)))
        # 非幂等：**不许重发**。回包丢了就是 KVUnavailable，绝不是"再算一次"
        new_value = int(self._call(
            "INCRBY", lambda: self._client.incrby(physical, step), retry=False
        ))
        if not existed and ttl:
            self._apply_ttl(physical, ttl)
        return new_value

    def delete(self, key: str) -> bool:
        physical = self.physical_key(key)
        return int(self._call("DEL", lambda: self._client.delete(physical))) > 0

    def expire(self, key: str, ttl_s: float) -> bool:
        physical = self.physical_key(key)
        ttl = self._ttl_kwargs(ttl_s)
        return self._apply_ttl(physical, ttl) > 0

    # ── 扩展：集合 ─────────────────────────────────────────
    def sadd(self, key: str, *members: str) -> int:
        if not members:
            return 0
        physical = self.physical_key(key)
        names = [_str_arg(m, "member") for m in members]
        return int(self._call("SADD", lambda: self._client.sadd(physical, *names)))

    def srem(self, key: str, *members: str) -> int:
        if not members:
            return 0
        physical = self.physical_key(key)
        names = [_str_arg(m, "member") for m in members]
        return int(self._call("SREM", lambda: self._client.srem(physical, *names)))

    def smembers(self, key: str) -> list[str]:
        """集合成员，**排序后**返回（Redis 的集合无序；排序只为断言稳定）。"""
        physical = self.physical_key(key)
        members = self._call("SMEMBERS", lambda: self._client.smembers(physical))
        return sorted(str(m) for m in (members or ()))

    # ── 扩展：SCAN ─────────────────────────────────────────
    def scan(self, cursor: int = 0, *, match: str | None = None,
             count: int = 100) -> tuple[int, list[str]]:
        """`SCAN cursor [MATCH pat] [COUNT n]` → `(下一个游标, 本页 key)`。

        注意 `match` 是**物理 key** 上的模式：调用方要自己拼上 `key_prefix`。
        返回的 key 也是物理 key（不做前缀剥离），保持"你看到的即 Redis 里的"。
        """
        reply = self._call(
            "SCAN",
            lambda: self._client.scan(cursor=int(cursor), match=match or None,
                                      count=max(1, int(count))),
        )
        try:
            next_cursor, keys = reply
            return int(next_cursor), [str(k) for k in keys]
        except (TypeError, ValueError) as exc:
            raise KVError(f"SCAN 回复形状不对：{reply!r}") from exc

    # ── 内部：TTL 与清理 ───────────────────────────────────
    @staticmethod
    def _ttl_kwargs(ttl_s: Any) -> dict[str, int]:
        """TTL → 库的关键字（`ex=` 秒 / `px=` 毫秒由库拼成 `EX` / `PX`）。"""
        if ttl_s is None:
            return {}
        kind, amount = _ttl_command(ttl_s)
        return {"ex": amount} if kind == "EX" else {"px": amount}

    def _apply_ttl(self, physical: str, ttl: dict[str, int]) -> int:
        if "px" in ttl:
            return int(self._call(
                "PEXPIRE", lambda: self._client.pexpire(physical, ttl["px"])
            ))
        return int(self._call(
            "EXPIRE", lambda: self._client.expire(physical, ttl["ex"])
        ))

    # ── 生命周期 ───────────────────────────────────────────
    def close(self) -> None:
        """关连接池（幂等）。**不删数据**：Redis 的数据活得比本进程长。

        注入的客户端不由本类关闭 —— 谁建的池谁负责收尾。
        """
        if not self._owns_client:
            return
        try:
            self._client.close()
        except Exception:                # 清理路径上的错误没有信息量
            pass
        pool = getattr(self._client, "connection_pool", None)
        if pool is not None:
            try:
                pool.disconnect()        # 我们建的池，必须自己断，否则 fd 泄漏
            except Exception:
                pass

    def __enter__(self) -> "RESPKV":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        owner = "owned" if self._owns_client else "injected"
        return (f"<RESPKV {self.host}:{self.port} db={self.db} "
                f"prefix={self.key_prefix!r} client={owner} "
                f"connects={self._connects}>")


def _is_abort(exc: BaseException) -> bool:
    """`EXEC` 返回 nil（真冲突）vs WATCH 期间传输层断了（结果未知）。

    redis-py 两种都抛 `WatchError`：前者文案 `Watched variable changed.`，
    后者是 `A ConnectionError occurred while watching one or more keys`。
    只认第一种，其余一律当"不可用" —— 把未知说成"没抢到"是静默丢更新。
    """
    return _ABORT_TEXT in str(exc)
