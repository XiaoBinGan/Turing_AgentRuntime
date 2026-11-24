"""plane/blackboard.py — Workflow Plane 的共享黑板：**星型拓扑 + CAS 乐观并发**。

================================================================================
核心立场（写死在代码里，不是口号）
================================================================================
**点对点通信是网状的**：N 个 Agent 两两直连就是 N(N-1)/2 条信道，
每个 Agent 都要知道「谁是当前权威」——一多就是 O(N²) 的通信风暴，
而且没有人能回答「系统此刻的整体状态是什么」。

**黑板是星型的**：所有 Agent 只跟一块共享状态打交道，复杂度降回 O(N)。
Agent 之间不再互相喊话，而是各自对同一份状态做「读 → 改 → 提交」。
这条拓扑选择直接来自架构文档 §4.4「网状 O(N²) 降为星型 O(N)」。

**但没有版本控制的黑板是并发灾难。** 星型只解决了信道数量，没解决写冲突：
两个 Agent 同时基于同一份旧快照提交，后写的那个会静默覆盖前者——
这类 bug 在生产里表现为「我明明改过，怎么回去又变回去了」，
并且**没有任何日志能证明它发生过**。所以：

    CAS（比较并交换）是必需品，不是优化项。

本模块的每一次写都必须带期望版本号（`create` 是「期望不存在」的特例），
版本不匹配就返回 `CASResult.conflict(...)`，把冲突**抛回调用方**。

================================================================================
为什么不自动重试合并（这条是刻意不做）
================================================================================
最诱人的「贴心」实现是：冲突时自动重读 → 自动重放 → 再提交，调用方完全无感。
**这是错的**，因为黑板上存的是语义对象，不是自增计数器：

- 重放一个 `set` 会把对方的整个修改**原地丢掉**，冲突被吞，事后无人知晓；
- 重放一个 `append` 会把同一段内容**追加两遍**，状态看起来「成功」但是脏的；
- 两个 Agent 都自动重试，可能来回覆盖出「活锁」——版本一直在涨，语义一直不动。

只有调用方知道「这次修改的语义意图」，才做得出正确的三向合并
（base=v1、mine、theirs）。所以本模块**永不自动重试**，只做一件事：
告诉你「你基于的 vN 已经过期，现在是 vM」，然后退场。

================================================================================
分区语义（严格执行，越权直接拒绝）
================================================================================
| 分区      | 谁能读      | 谁能写                                  | 语义                |
|-----------|-------------|-----------------------------------------|---------------------|
| GLOBAL    | 所有 Agent  | 只能 `publish_global`（一次成型）；改版走 CAS | 全局规范、约定 |
| WORKING   | 所有 Agent  | 必须 CAS（`compare_and_set`）           | 协同工作区，最易冲突 |
| PRIVATE   | **只有 owner** | 只有 owner（`write_private`，自动加前缀） | 私有探索，不污染全局 |

- PRIVATE 的 key 自动加前缀 `@owner:`；`read` / `list` 天然只看得到自己的。
- `create(..., partition=GLOBAL)` 直接 `PartitionViolation`：
  入口错了就当场报错，别指望运行时兜底——全局规范的「一次成型」是语义，不是建议。
- 同名 key 在不同区是**不同条目**（互不干扰）；`read` 不带 partition 时按
  **私有 → 工作 → 全局** 的遮蔽顺序解析（和变量作用域一个直觉：
  越私有越先命中）。要精确判定请显式传 partition。

================================================================================
fail-open 的通知（黑板是状态，不是观测）
================================================================================
订阅回调抛异常必须被吞掉并计数（`callback_errors`）。
理由对称于 Governance 的 fail-closed：观测面坏了不能反噬状态面。
如果回调能把写入弄失败，那任何一个写错日志的订阅者都能卡死整个多 Agent 编排。

================================================================================
与其它模块的边界
================================================================================
只依赖 `contracts.py`（Partition / BlackboardEntry / CASResult / now_ms）。
不 import core/*、不 import plane/*：黑板不认识 Context、不认识 Agent 实现，
它只认识 key / version / owner 三样东西——这样将来换存储（Redis / etcd）不用改语义。

CAS 的原子性：进程内用 `threading.RLock` 保证「读版本 + 比版本 + 写」不可分割；
跨进程部署时必须换成分区存储原语（etcd 的 txn、Redis 的 WATCH/MULTI），
这一点写在 `_lock` 的定义处，别用这把锁假装分布式安全。

锁覆盖面（W25 逐条核过，不是"看着有锁就算"）
────────────────────────────────────────────────────────────
| 入口                                   | 临界区内容                                     |
|----------------------------------------|------------------------------------------------|
| `create` / `publish_global`            | 查存在性 → 建条目 → `writes` → 写 history      |
| `compare_and_set`                      | `_resolve` → 比版本 → 改 `_data` → history     |
| `write_private`                        | 读旧版本 → 升版本 → 写 → history               |
| `delete`                               | `_resolve` → 比版本 → `del` → history          |
| `read` / `list` / `history` / `snapshot`| 读 `_data` + 深拷贝                             |
| `subscribe` / `unsubscribe`            | 改 `_subs`                                     |
| `stats` / `__len__` / `__repr__`       | 读计数器                                       |
| `_emit` / `_call`                      | 只锁「抄一份回调列表」与「计数器 +1」          |

两条**刻意**在锁外做的事：
  1. **回调派发**（`_emit` 里 `self._call(...)`）。慢回调若在锁内跑，一个写错日志的
     订阅者就能把所有 Agent 的写入串行化成一个队列 —— 状态面被观测面拖死。
     代价是回调看到的事件顺序可能交错，这没问题：payload 自带 `version`，
     消费方按版本号排序即可，而**状态本身**仍然是严格串行的。
  2. **`snapshot()` 的 JSON 可序列化校验**。值是先在锁内 `_copy_value` 拷出来的
     副本，校验只碰副本、不碰共享状态；把它挪出锁外，是为了让"一个不可序列化的值"
     的报错路径不占用写路径的临界区。
"""
from __future__ import annotations

import copy
import json
import threading
from typing import Any, Callable

from contracts import (
    CASResult,
    BlackboardEntry,
    Partition,
    now_ms,
)

__all__ = [
    "WILDCARD",
    "Blackboard",
    "BlackboardError",
    "PartitionViolation",
]

#: 通配订阅：审计视角，收所有分区所有 key 的变更
WILDCARD = "*"

#: 不带 partition 时的解析顺序：越私有越先命中（遮蔽语义）
_RESOLVE_ORDER: tuple[Partition, ...] = (Partition.PRIVATE, Partition.WORKING, Partition.GLOBAL)


# ══════════════════════════════════════════════════════════════
# 错误
# ══════════════════════════════════════════════════════════════
class BlackboardError(Exception):
    """黑板操作失败的基类。"""


class PartitionViolation(BlackboardError):
    """分区写入方式不合法（例如绕过 publish_global 直接 create 到 GLOBAL）。"""


# ══════════════════════════════════════════════════════════════
# 内部小工具
# ══════════════════════════════════════════════════════════════
def _private_prefix(owner: str) -> str:
    return f"@{owner}:"


def _copy_value(value: Any) -> Any:
    """尽力深拷贝：不可拷贝的对象（锁、连接、生成器）按引用返回。

    黑板不承诺「值一定隔离」，只承诺「能隔离的都隔离了」——
    宁可拷贝失败退化成引用，也不为了拷贝而拒绝写入。
    """
    try:
        return copy.deepcopy(value)
    except Exception:  # noqa: BLE001 - 拷贝失败不是业务错误
        return value


class Blackboard:
    """共享黑板：三区 + 版本号 + CAS + 订阅。

    典型用法（标准 OCC 闭环）::

        bb.create("plan", {"steps": ["A"]}, "leader")       # v1
        r = bb.compare_and_set("plan", 1, {...}, "coder_a")  # v2
        if not r.ok:
            cur = bb.read("plan")            # 重读
            merged = semantic_merge(cur.value, mine)   # 语义合并（调用方负责）
            bb.compare_and_set("plan", cur.version, merged, "coder_a")  # 再提交
    """

    def __init__(self, now: Callable[[], float] = now_ms, notifier: Callable[[dict], None] | None = None):
        self._now = now
        # notifier：全局变更汇（trace / 指标 / 事件总线）。语义等同 WILDCARD 订阅的
        # 便捷入口，同样 fail-open。和订阅的区别只在「谁注册的」：订阅是业务回调，
        # notifier 是 runtime 注入的观测管道。
        self._notifier = notifier
        #: 进程内互斥：保证「比版本 → 写」原子。
        #: 注意：**跨进程部署时这把锁不成立**，必须换成分区存储的 CAS 原语
        #: （etcd txn / Redis WATCH-MULTI / DynamoDB ConditionalPut）。
        #: 本模块只保证单进程内的 CAS 语义，别拿它冒充分布式一致性。
        self._lock = threading.RLock()
        self._data: dict[Partition, dict[str, BlackboardEntry]] = {p: {} for p in Partition}
        #: (partition.value, 存储 key) → 版本演进轨迹（删除后仍保留，供审计）
        self._history: dict[tuple[str, str], list[dict]] = {}
        self._subs: dict[str, list[Callable[[dict], None]]] = {}

        # 计数器（公开可读；写入失败不影响状态面的那些也要留痕）
        self.writes = 0            # 成功写入次数（create + 更新）
        self.deletes = 0           # 成功删除次数
        self.conflicts = 0         # CAS/create/delete 冲突次数（只统计「版本或存在性不符」）
        self.notifications = 0     # 回调派发次数（含抛异常的）
        self.callback_errors = 0   # 回调抛异常被吞掉的次数（fail-open 证据）

    # ── 基础工具 ──────────────────────────────────────────────
    @staticmethod
    def _partition(partition: Partition | str) -> Partition:
        """接受 Partition 或裸字符串（"working"），非法值直接报错。"""
        if isinstance(partition, Partition):
            return partition
        try:
            return Partition(str(partition))
        except ValueError as exc:
            raise ValueError(
                f"unknown partition {partition!r}; expected one of "
                f"{[p.value for p in Partition]}"
            ) from exc

    @staticmethod
    def _private_key(owner: str, key: str) -> str:
        """PRIVATE 区的 key 命名空间：`@owner:` 前缀，幂等（已带前缀不重复加）。"""
        prefix = _private_prefix(owner)
        return key if key.startswith(prefix) else prefix + key

    def _store_key(self, partition: Partition, key: str, owner: str) -> str:
        if partition is Partition.PRIVATE:
            return self._private_key(owner, key)
        return key

    def _resolve(
        self, key: str, agent: str, partition: Partition | str | None
    ) -> tuple[Partition | None, str, BlackboardEntry | None]:
        """定位条目 → (分区, 存储 key, 条目|None)。

        显式 partition：只在该区找（跨区同名 key 因此完全解耦）。
        不给 partition：按 私有 → 工作 → 全局 遮蔽顺序找第一个命中的。
        """
        if partition is not None:
            part = self._partition(partition)
            skey = self._store_key(part, key, agent)
            return part, skey, self._data[part].get(skey)
        for part in _RESOLVE_ORDER:
            skey = self._store_key(part, key, agent)
            entry = self._data[part].get(skey)
            if entry is None:
                continue
            # 纵深防御：私有条目必须 owner 匹配（前缀已经绑定了 owner，这里是第二道）
            if part is Partition.PRIVATE and entry.owner != agent:
                continue
            return part, skey, entry
        return None, key, None

    @staticmethod
    def _view(entry: BlackboardEntry) -> BlackboardEntry:
        """对外返回的防御性副本。

        若直接把内部对象交出去，调用方一句 `entry.version = 99` 就能绕过 CAS
        把版本表改坏（而且是不可审计的）。所以读路径一律给副本。
        """
        return BlackboardEntry(
            key=entry.key,
            value=_copy_value(entry.value),
            partition=entry.partition,
            version=entry.version,
            owner=entry.owner,
            updated_at=entry.updated_at,
            writers=tuple(entry.writers),
        )

    def _payload(self, part: Partition, entry: BlackboardEntry, value: Any, op: str) -> dict:
        return {
            "key": entry.key,
            "version": entry.version,
            "owner": entry.owner,
            "value": _copy_value(value),
            "op": op,
            "partition": part.value,
            "at": entry.updated_at,
        }

    def _record(self, part: Partition, skey: str, event: str,
                entry: BlackboardEntry, value: Any) -> None:
        """写轨迹。**每一次版本变化都必须留痕**，否则 history 还原不出演进。"""
        trail = self._history.setdefault((part.value, skey), [])
        trail.append({
            "event": event,          # create / update / publish / private / delete
            "version": entry.version,
            "owner": entry.owner,
            "at": entry.updated_at,
            "value": _copy_value(value),
        })

    # ── 通知（fail-open） ─────────────────────────────────────
    def _emit(self, skey: str, payload: dict) -> None:
        """派发变更：精确订阅 + 通配订阅 + 全局 notifier。任何异常都吞掉并计数。"""
        with self._lock:
            callbacks = list(self._subs.get(skey, ())) + list(self._subs.get(WILDCARD, ()))
            notifier = self._notifier
        for cb in callbacks:
            self._call(cb, payload)
        if notifier is not None:
            self._call(notifier, payload)

    def _call(self, cb: Callable[[dict], None], payload: dict) -> None:
        with self._lock:
            self.notifications += 1
        try:
            # 每个回调拿独立副本：一个回调改坏 payload 不该影响下一个
            cb(_copy_value(payload))
        except Exception:  # noqa: BLE001
            # fail-open：黑板是状态，不是观测。观测坏了也必须让状态写成功。
            with self._lock:
                self.callback_errors += 1

    # ══════════════════════════════════════════════════════════
    # 读
    # ══════════════════════════════════════════════════════════
    def read(self, key: str, agent: str = "",
             partition: Partition | str | None = None) -> BlackboardEntry | None:
        """读一个条目。不存在（或无权看）→ None。

        `agent` 既用于 PRIVATE 区的前缀解析，也用于遮蔽顺序里「谁的私有空间」。
        """
        with self._lock:
            _, _, entry = self._resolve(key, agent, partition)
            if entry is None:
                return None
            # 显式指定 PRIVATE 且不是自己的 key：不存在就是不存在（返回 None，不报错——
            # 报错本身就是一种信息泄露：能探测出「别人有没有这个 key」）
            if entry.partition is Partition.PRIVATE and entry.owner != agent:
                return None
            return self._view(entry)

    def list(self, partition: Partition | str | None = None,
             agent: str = "") -> list[BlackboardEntry]:
        """列条目。

        - `partition=PRIVATE, agent=X` → 只返回 X 自己的（别人永远看不到）；
        - `partition=None` → 全局 + 工作 + 自己的私有，按 私有→工作→全局 排序；
        - 顺序确定（分区内按 key 字典序），便于快照比对与测试断言。
        """
        wanted = [self._partition(partition)] if partition is not None else list(_RESOLVE_ORDER)
        out: list[BlackboardEntry] = []
        with self._lock:
            for part in wanted:
                for skey in sorted(self._data[part]):
                    entry = self._data[part][skey]
                    if part is Partition.PRIVATE:
                        if entry.owner != agent or not skey.startswith(_private_prefix(agent)):
                            continue
                    out.append(self._view(entry))
        return out

    def history(self, key: str, partition: Partition | str | None = None,
                agent: str = "") -> list[dict]:
        """版本演进轨迹（create/update/publish/private/delete 时间序列）。

        删除后轨迹仍保留（墓碑），这是审计与「中断恢复」的原料：
        你能回答「这个 key 什么时候被谁改了、改成了什么、后来为什么没了」。
        重建同名 key 时版本从 v1 重新开始，用 `event` 字段区分生命周期，
        别假设 version 单调递增到永远。
        """
        events: list[dict] = []
        with self._lock:
            if partition is not None:
                part = self._partition(partition)
                skey = self._store_key(part, key, agent)
                events = self._history.get((part.value, skey), [])
            else:
                for part in _RESOLVE_ORDER:
                    skey = self._store_key(part, key, agent)
                    found = self._history.get((part.value, skey))
                    if found:
                        events = found
                        break
                if not events:
                    # 已删除的私有 key（形如 "@alice:x"）直接按存储 key 精确匹配
                    for (_, skey), found in sorted(self._history.items()):
                        if skey == key and found:
                            events = found
                            break
            return _copy_value(list(events))

    # ══════════════════════════════════════════════════════════
    # 写（全部走 CAS）
    # ══════════════════════════════════════════════════════════
    def create(self, key: str, value: Any, owner: str,
               partition: Partition | str = Partition.WORKING) -> CASResult:
        """新建条目（version=1）。**key 已存在 → conflict，绝不静默覆盖。**

        静默覆盖是最坏的一种「友好」：调用方以为自己在初始化，实际把别人的
        成果抹掉了，而且返回值还是 ok=True。
        """
        part = self._partition(partition)
        if part is Partition.GLOBAL:
            raise PartitionViolation(
                f"GLOBAL 区不接受 create(key={key!r})：全局规范必须经 publish_global 一次成型；"
                f"改版请 compare_and_set(..., partition=Partition.GLOBAL)"
            )
        return self._create(part, key, value, owner, op="create")

    def _create(self, part: Partition, key: str, value: Any, owner: str, op: str) -> CASResult:
        with self._lock:
            skey = self._store_key(part, key, owner)
            store = self._data[part]
            existing = store.get(skey)
            if existing is not None:
                self.conflicts += 1
                return CASResult(
                    ok=False,
                    current_version=existing.version,
                    reason=(f"create conflict: key {skey!r} already exists at "
                            f"v{existing.version} (partition={part.value}); "
                            f"use compare_and_set with expected_version={existing.version}"),
                )
            entry = BlackboardEntry(
                key=skey, value=value, partition=part, version=1,
                owner=owner, updated_at=self._now(), writers=(owner,),
            )
            store[skey] = entry
            self.writes += 1
            self._record(part, skey, op, entry, value)
            payload = self._payload(part, entry, value, op)
        self._emit(skey, payload)
        return CASResult.success(1)

    def compare_and_set(self, key: str, expected_version: int, value: Any, owner: str,
                        partition: Partition | str | None = None) -> CASResult:
        """CAS：版本匹配才写，写入即 `version += 1` 并追加 writers。

        三种结果，语义清楚且互斥：
          - key 不存在 → conflict(expected, current=0)，reason 明写 `key not found`
                        （「你以为在改一个存在的东西」——这本身就是必须暴露的错误）
          - 版本不符 → conflict(expected, current)，**值保持原样**
          - 版本匹配 → success(new_version)，触发订阅

        冲突不抛异常：`conflict` 是控制流，不是 failure（抛异常会诱使调用方
        用 try/except 包一层然后「重试一次算了」，那正是我们要杜绝的模式）。
        """
        payload: dict | None = None
        with self._lock:
            part, skey, entry = self._resolve(key, owner, partition)
            if entry is None:
                self.conflicts += 1
                where = self._partition(partition).value if partition is not None else "any"
                return CASResult(
                    ok=False,
                    current_version=0,
                    reason=(f"CAS conflict: key not found (key={skey!r}, "
                            f"partition={where}, expected v{expected_version})"),
                )
            if entry.version != expected_version:
                self.conflicts += 1
                return CASResult.conflict(expected_version, entry.version)
            new_entry = BlackboardEntry(
                key=skey, value=value, partition=entry.partition,
                version=entry.version + 1, owner=owner,
                updated_at=self._now(), writers=tuple(entry.writers) + (owner,),
            )
            self._data[entry.partition][skey] = new_entry
            self.writes += 1
            self._record(entry.partition, skey, "update", new_entry, value)
            payload = self._payload(entry.partition, new_entry, value, "update")
        self._emit(skey, payload)  # 回调在锁外，且 fail-open
        return CASResult.success(new_entry.version)

    def publish_global(self, key: str, value: Any, owner: str) -> CASResult:
        """GLOBAL 区写法：**一次成型**。已存在则 conflict（改版必须 CAS 升版）。

        「写后不可改」不是洁癖：全局规范一旦被反复覆写，所有基于它做决策的
        Agent 都在追一个移动靶——冲突必须显式化，让改规范这件事被看见。
        """
        with self._lock:  # RLock 可重入：存在性检查与写入必须在同一临界区
            existing = self._data[Partition.GLOBAL].get(key)
            if existing is not None:
                self.conflicts += 1
                return CASResult(
                    ok=False,
                    current_version=existing.version,
                    reason=(f"publish conflict: global key {key!r} already published at "
                            f"v{existing.version}; GLOBAL 一次成型，改版请 "
                            f"compare_and_set(key, {existing.version}, ..., partition=GLOBAL)"),
                )
            return self._create(Partition.GLOBAL, key, value, owner, op="publish")

    def write_private(self, key: str, value: Any, owner: str) -> CASResult:
        """PRIVATE 区写法：key 自动加前缀 `@owner:`，单写者 → **upsert**。

        这里不做 CAS 是刻意的：PRIVATE 区只有一个写者，不存在「基于过期版本提交」
        的可能，强行要求 CAS 只是给调用方增加无意义的噪声。但版本号照旧 +1——
        私有探索也需要演进轨迹（「我上一版想的什么」往往比当前值更有用）。
        """
        with self._lock:
            skey = self._private_key(owner, key)
            store = self._data[Partition.PRIVATE]
            old = store.get(skey)
            if old is None:
                entry = BlackboardEntry(
                    key=skey, value=value, partition=Partition.PRIVATE, version=1,
                    owner=owner, updated_at=self._now(), writers=(owner,),
                )
                version = 1
            else:
                entry = BlackboardEntry(
                    key=skey, value=value, partition=Partition.PRIVATE,
                    version=old.version + 1, owner=owner,
                    updated_at=self._now(), writers=tuple(old.writers) + (owner,),
                )
                version = entry.version
            store[skey] = entry
            self.writes += 1
            self._record(Partition.PRIVATE, skey, "private", entry, value)
            payload = self._payload(Partition.PRIVATE, entry, value, "private")
        self._emit(skey, payload)
        return CASResult.success(version)

    # ══════════════════════════════════════════════════════════
    # 删除
    # ══════════════════════════════════════════════════════════
    def delete(self, key: str, expected_version: int, owner: str,
               partition: Partition | str | None = None) -> CASResult:
        """带版本号删除。版本不符 → conflict（**别人刚改过，删了就是数据丢失**）。

        成功时返回 delete 前的版本号；同名 key 重新 create 会从 v1 起，
        历史轨迹保留（墓碑），history() 里能看到完整生命周期。
        """
        payload: dict | None = None
        with self._lock:
            part, skey, entry = self._resolve(key, owner, partition)
            if entry is None:
                self.conflicts += 1
                where = self._partition(partition).value if partition is not None else "any"
                return CASResult(
                    ok=False,
                    current_version=0,
                    reason=(f"delete conflict: key not found (key={skey!r}, "
                            f"partition={where}, expected v{expected_version})"),
                )
            if entry.version != expected_version:
                self.conflicts += 1
                return CASResult.conflict(expected_version, entry.version)
            del self._data[entry.partition][skey]
            self.deletes += 1
            self._record(entry.partition, skey, "delete", entry, None)
            payload = self._payload(entry.partition, entry, None, "delete")
        self._emit(skey, payload)
        return CASResult.success(entry.version)

    # ══════════════════════════════════════════════════════════
    # 快照 / 恢复
    # ══════════════════════════════════════════════════════════
    def snapshot(self) -> dict:
        """可 `json.dumps` 的全量快照（序列化 / 中断恢复 / 审计）。

        结构::

            {"schema": "blackboard/1",
             "entries":    {"working:plan": {key,value,partition,version,owner,updated_at,writers}},
             "partitions": {"global": [...], "working": [...], "private": [...]},
             "versions":   {"working:plan": 3}}

        entries 的键是 `"{partition}:{store_key}"`：同名 key 在不同分区必须是
        不同条目，扁平化后若只用裸 key 就会互相撞车（这正是分区语义要防的事）。
        value 不可 JSON 序列化 → `TypeError`，且错误信息里必须指出是哪个 key，
        否则一个坏值能让整个快照流程无从定位。
        """
        entries: dict[str, dict] = {}
        partitions: dict[str, list[str]] = {p.value: [] for p in Partition}
        versions: dict[str, int] = {}
        with self._lock:
            for part in Partition:
                for skey in sorted(self._data[part]):
                    entry = self._data[part][skey]
                    composite = f"{part.value}:{skey}"
                    row = entry.to_dict()
                    row["value"] = _copy_value(row["value"])
                    entries[composite] = row
                    partitions[part.value].append(composite)
                    versions[composite] = entry.version

        # 序列化校验放最后一次性做：先让状态抽出来，再对每个 value 精确报错
        for composite, row in entries.items():
            try:
                json.dumps(row["value"])
            except (TypeError, ValueError) as exc:
                raise TypeError(
                    f"blackboard snapshot: value for key {row['key']!r} "
                    f"(partition={row['partition']}) is not JSON-serializable: {exc}"
                ) from exc

        return {
            "schema": "blackboard/1",
            "entries": entries,
            "partitions": partitions,
            "versions": versions,
        }

    @classmethod
    def restore(cls, snapshot: dict, now: Callable[[], float] = now_ms,
                notifier: Callable[[dict], None] | None = None) -> "Blackboard":
        """从 `snapshot()` 的产物重建黑板（中断恢复）。

        只恢复**当前状态**，不重放历史：历史属于审计流（另存），
        恢复现场时重放历史反而会把「已删除条目」复活。
        恢复不触发订阅（没有任何 Agent 在监听一个刚刚诞生的黑板）。
        """
        bb = cls(now=now, notifier=notifier)
        with bb._lock:                       # 新对象，但恢复也走同一把锁：口径统一
            for composite, row in snapshot.get("entries", {}).items():
                part = bb._partition(row.get("partition", composite.split(":", 1)[0]))
                entry = BlackboardEntry(
                    key=row["key"],
                    value=_copy_value(row.get("value")),
                    partition=part,
                    version=int(row.get("version", 1)),
                    owner=row.get("owner", ""),
                    updated_at=float(row.get("updated_at", 0.0)),
                    writers=tuple(row.get("writers", ())),
                )
                bb._data[part][entry.key] = entry
        return bb

    # ══════════════════════════════════════════════════════════
    # 订阅
    # ══════════════════════════════════════════════════════════
    def subscribe(self, key: str, callback: Callable[[dict], None]) -> None:
        """订阅 key 的变更（`"*"` = 全部）。

        回调 payload：`{"key","version","owner","value","op","partition","at"}`。
        约束：
          - 在写锁**之外**调用（慢回调不阻塞写入，更不会死锁）；
          - 抛异常一律吞掉并计入 `callback_errors`（fail-open）；
          - 通配订阅是**观测视角**，能看到 PRIVATE 区——观测者不是 Agent，
            如果连审计都看不到私有区，「私有探索」就会变成不可追责的黑洞。
        """
        if not isinstance(key, str) or not key:
            raise ValueError("subscribe() 需要非空 str key（或 '*')")
        if not callable(callback):
            raise TypeError("subscribe() 的 callback 必须可调用")
        with self._lock:
            self._subs.setdefault(key, []).append(callback)

    def unsubscribe(self, key: str, callback: Callable[[dict], None]) -> bool:
        """退订；返回是否真的移除了一个注册。"""
        with self._lock:
            bucket = self._subs.get(key)
            if not bucket or callback not in bucket:
                return False
            bucket.remove(callback)
            if not bucket:
                self._subs.pop(key, None)
            return True

    # ══════════════════════════════════════════════════════════
    # 观测
    # ══════════════════════════════════════════════════════════
    def stats(self) -> dict:
        """运行计数（demo / trace / 断言用）。"""
        with self._lock:
            return {
                "entries": sum(len(store) for store in self._data.values()),
                "writes": self.writes,
                "deletes": self.deletes,
                "conflicts": self.conflicts,
                "notifications": self.notifications,
                "callback_errors": self.callback_errors,
                "subscribers": sum(len(bucket) for bucket in self._subs.values()),
            }

    def __len__(self) -> int:
        with self._lock:
            return sum(len(store) for store in self._data.values())

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        with self._lock:
            parts = {p.value: len(store) for p, store in self._data.items()}
        return f"Blackboard({parts}, writes={self.writes}, conflicts={self.conflicts})"
