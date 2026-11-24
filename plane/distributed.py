"""plane/distributed.py — 跨副本一致性：黑板 CAS / 配额窗口 / singleflight。

================================================================================
这个模块只回答一个问题：**把「单进程假设」从三个地方拆掉**
================================================================================

`plane/blackboard.py` 用 `threading.RLock` 保证 CAS 原子；
`Governance.QuotaGuard` 用进程内 deque 做窗口；
`ResourceRuntime` 的 LRU 在 cache miss 时**没有**「只有一个调用者去打下游」的约束。

三者在单进程里都正确，一部署成 N 个副本就同时变成三种**静默**失效：

| 缺口 | 单进程 | 4 副本的真实现象 | 谁能沉默 |
|------|--------|------------------|----------|
| 黑板 CAS | 锁内「比版本 → 写」 | 两个副本各写同一 key，后写覆盖前写，**没有一条日志证明发生过** | 丢失更新 |
| 配额窗口 | 进程内 deque | 每副本独立窗口 → 实际限额 **× N**（限流静默失效） | 限流放水 |
| resources 缓存 | 进程内 LRU | 并发首次读同一 uri → reader 被调 **N** 次（N=并发数） | 下游被打穿 |

「静默」是这三条的共同点，也是本模块存在的全部理由：
**错的不是精度，是「配置在、效果不在」**——限流配了却是 4 倍，比没配更危险。

================================================================================
① DistributedBlackboard：原子 CAS 必须由存储端完成，不能由调用端拼
================================================================================
最诱人的实现是「get → 比版本 → set」。它在一台机器上跑一万次都过，
**但它正是要修的那个竞态**：两次 get 之间别人写了一次，你的 set 就把它抹了。

所以这里只调用 KV 暴露的**服务端原子动作**（`set_nx` / `compare_and_set`）：

    create            → kv.set_nx(k, entry_json)          # 天然「期望不存在」
    compare_and_set   → kv.compare_and_set(k, old_json, new_json)
                        # 比较对象是**整个序列化条目**，版本号就在里面
    delete            → kv.compare_and_set(k, old_json, TOMBSTONE) 然后 kv.delete(k)

**为什么 CAS 的比较对象是整段 JSON 而不是一个「版本字段」**：
KV 协议只接受「期望值」而不是「期望版本」，所以把版本号写进序列化文本，
让「值变了」和「版本变了」变成同一件事——两种漂移（改了值没改版本、
改了版本没改值）在存储端都不可能悄悄发生。序列化用
`sort_keys=True, separators=(",",":")` 固定，否则同一份条目的两种 JSON 写法
会让 CAS 永远失败（这是最容易踩的坑：字典键序不同 = 不同的字符串）。

**为什么失败时要重新读一次**：`CASResult.conflict(expected, current)` 里的
`current` 是调用方做三向合并的唯一输入。返回一个猜的版本号，等于让调用方
基于假数据重试——那比不给还糟。重读只是「取当前真相」，**它不参与判定**，
判定已经在存储端做完了。

**delete 的诚实边界**：KV Protocol 没有 compare-and-delete。
所以删除是「CAS 到墓碑 + delete」两步：
  - CAS 成功 = 我们确实拿到了那一版的所有权，别人基于旧值的 CAS 一律失败；
  - 墓碑窗口内（CAS 成功 → delete 之间）读到墓碑**等同不存在**，不会复活旧值；
  - delete 失败（网络抖动）时墓碑留在原地，语义上仍然是「已删除」——墓碑是
    可自愈的终态，不是中间态。
这里不假装它是单指令原子删除，因为假装比缺功能更贵。

================================================================================
② DistributedQuota：固定窗口 + INCRBY 才是跨副本精确的最小实现
================================================================================
滑动窗口在单进程里用 deque 很精确，但跨副本没法精确——「每个副本各自数
自己的时间戳」永远凑不出一致的分母。

所以换成**固定窗口**：key 里带 `window_id = int(now/60)`，
`INCRBY` 是原子的，所有副本落在同一个窗口 key 上，N 个副本合计就**必然**
不超过限额。代价是窗口边界处最多 2× 的瞬时桶效应（经典取舍，写在明处）：
  - T 窗口最后 1ms 用满 10 次 + T+1 窗口头 1ms 用满 10 次 → 2ms 内 20 次；
  - 换成滑动窗口需要「跨副本时间戳集合」，成本是 O(N) 的读写放大。
对「防打爆下游」这个目的，2× 瞬时比 1× 精确更划算。

**先加后判，超了回滚**是这里唯一正确的顺序：
先判「会不会超」再 INCRBY，就是「读-判断-写」三段竞态（并发时都判 10 < 10 都过）；
先 INCRBY 再判、超了 `DECRBY` 回滚，是原子判定 —— 代价是被拒的请求有一瞬间
占了额度，所以**必须回滚**，否则「被拒的请求也计费」会让一次攻击永久抬高水位。
对应的用例（12）断言的不是「返回 block」，而是 `used()` 在拒绝前后**不变**。

================================================================================
③ SingleFlight：并发首次读同一 uri，下游只该挨一次
================================================================================
`ResourceRuntime` 的 LRU 只解决「读完之后」，不解决「读的同时」：
10 个并发首次读同一 uri → 10 次 miss → 10 次 reader → 缓存**写完就丢**，
下游白白挨 10 次。这不是缓存命中率问题，是对下游的**瞬时放大**。

约束按契约来：
  - 同 key：1 次执行，其余**等**同一个结果（不是各跑一遍）；
  - fn 抛异常：所有等待者拿到**同一个异常对象**（不吞、不转成 None）；
  - 不同 key：互不阻塞（否则 singleflight 自己变成串行化瓶颈）；
  - 超时：等待者拿 `SingleFlightTimeout`，**leader 不受影响**（它本来就在跑）。

一个刻意的设计：`wrap_with_singleflight` 的 key **不是裸 uri**，而是
`uri + 主体指纹`。裸 uri 会让「A 有权限读完、B 无权限」的并发请求共享同一次
reader 调用，等于用缓存预热的方式把授权旁路掉（`resources.py` 里第 3 步
「缓存不是权限的旁路」是同一个道理，这里是它的并发版）。
主体指纹里带 scope 集合，所以「宽 scope 主体替窄 scope 主体取数」也不可能发生。

================================================================================
依赖与边界
================================================================================
只 import `contracts.py`（KV / BlackboardEntry / CASResult / Partition /
GuardVerdict / DistributedInfo）与标准库。
**不改** `plane/blackboard.py` / `plane/governance.py` / `plane/resources.py`：
分布式版是**并列实现**，不是补丁——单进程版继续负责「本机最快路径」，
两者共用同一套契约形状与同一套语义（私有分区前缀 `@owner:`、遮蔽顺序
私有→工作→全局），只是原子性的来源从 RLock 换成了 KV 服务端原语。

KV 实现方（另一个 worker 的 `core/kv.py`）由调用方注入，本模块不 import 它：
测试里用自带 `FakeKV` 就能独立验证，不给并行施工添依赖。

用哪个后端更合适（诚实说法）：
  * `InMemoryKV`：单进程内 CAS/INCR 原子、多实例共享，**跨进程不可见**。
    两个 DistributedBlackboard 共用它在本进程里确实一致（用例 5/6/14 就是这么建的），
    但把它当分布式用 = 「配置在、效果不在」——`info()` 会明确说 `shared=False`。
  * `RESPKV`（真 Redis）：`atomic=True, shared=True`，本模块的语义才真正跨副本成立。
"""
from __future__ import annotations

import json
import threading
import time
from typing import Any, Callable

from contracts import (
    BlackboardEntry,
    CASResult,
    DistributedInfo,
    GuardVerdict,
    Partition,
    now_ms,
)

try:  # 与单进程版共用同一个异常类型：调用方 catch 一种就够（不复制第二份定义）
    from plane.blackboard import PartitionViolation
except Exception:  # pragma: no cover - 单进程模块不可用时退回本地定义

    class PartitionViolation(Exception):  # type: ignore[no-redef]
        """分区块写法非法（GLOBAL 区不接受 create）。"""


__all__ = [
    "DistributedError",
    "DistributedBlackboard",
    "DistributedQuota",
    "SingleFlight",
    "SingleFlightTimeout",
    "SingleFlightResourceRuntime",
    "wrap_with_singleflight",
    "PartitionViolation",
    "TOMBSTONE",
]

#: 删除的墓碑。读到它一律等同「不存在」——**不是**一条 version=0 的旧值。
TOMBSTONE = '{"__tombstone__":true}'

#: 不带 partition 时的解析顺序：越私有越先命中（与单进程版一致的遮蔽语义）
_RESOLVE_ORDER: tuple[Partition, ...] = (Partition.PRIVATE, Partition.WORKING, Partition.GLOBAL)

#: 配额固定窗口长度（秒）。写在常量里，方便测试用假时钟推 61 秒。
WINDOW_SECONDS = 60.0


class DistributedError(Exception):
    """分布式层操作失败的基类。"""


# ══════════════════════════════════════════════════════════════
# 内部小工具
# ══════════════════════════════════════════════════════════════
def _private_prefix(owner: str) -> str:
    return f"@{owner}:"


def _private_key(owner: str, key: str) -> str:
    """PRIVATE 区的命名空间：`@owner:` 前缀，幂等（已带前缀不重复加）。"""
    prefix = _private_prefix(owner)
    return key if key.startswith(prefix) else prefix + key


def _dumps(key: str, entry: BlackboardEntry) -> str:
    """条目 → KV 字符串。**序列化必须确定**，否则 CAS 永远失败。

    键序不同 = 不同字符串 = `compare_and_set` 的期望值永远不匹配。
    所以 `sort_keys=True` 不是风格问题，是正确性问题。
    """
    try:
        return json.dumps(entry.to_dict(), sort_keys=True, ensure_ascii=False,
                          separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        # 指名 key：跨副本场景里「哪个 key 写不进去」是排查的唯一线索
        raise TypeError(
            f"distributed blackboard: value for key {key!r} "
            f"(partition={entry.partition.value}) is not JSON-serializable: {exc}"
        ) from exc


def _loads(raw: str | None, *, expected_key: str = "") -> BlackboardEntry | None:
    """KV 字符串 → 条目。墓碑 / 空 / 坏数据 → None（坏数据不静默当有效值）。"""
    if not raw or raw == TOMBSTONE:
        return None
    try:
        row = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(row, dict) or "__tombstone__" in row:
        return None
    try:
        version = int(row.get("version", 0))
    except (TypeError, ValueError):
        return None
    if version <= 0:
        return None
    raw_part = row.get("partition", Partition.WORKING.value)
    try:
        part = raw_part if isinstance(raw_part, Partition) else Partition(str(raw_part))
    except ValueError:
        part = Partition.WORKING
    return BlackboardEntry(
        key=row.get("key") or expected_key,
        value=row.get("value"),
        partition=part,
        version=version,
        owner=row.get("owner", ""),
        updated_at=float(row.get("updated_at", 0.0) or 0.0),
        writers=tuple(row.get("writers", ()) or ()),
    )


def _as_partition(partition: Partition | str) -> Partition:
    if isinstance(partition, Partition):
        return partition
    try:
        return Partition(str(partition))
    except ValueError as exc:
        raise ValueError(
            f"unknown partition {partition!r}; expected one of {[p.value for p in Partition]}"
        ) from exc


def _view(entry: BlackboardEntry) -> BlackboardEntry:
    """对外副本：调用方改返回值不能改坏版本表（同单进程版的防御性拷贝）。"""
    return BlackboardEntry(
        key=entry.key,
        value=entry.value,
        partition=entry.partition,
        version=entry.version,
        owner=entry.owner,
        updated_at=entry.updated_at,
        writers=tuple(entry.writers),
    )


def _kv_keys(kv: Any, prefix: str) -> list[str] | None:
    """尽力枚举 KV key：只有部分后端支持。拿不到 → None（呼叫方必须诚实降级）。

    返回值 None 与「空列表」是两件事：前者是「后端不支持枚举」，
    后者是「确实没有 key」。混成一种会让 snapshot 静默看起来是空的。

    兼容三种形状（都是真实后端里会遇到的）：
      * `keys(prefix)`            → 简单后端的直接枚举
      * `scan(cursor, match=...)` → Redis 语义，返回 `(下一游标, 本页 key)`
      * `scan(match=...)`         → 无游标的简化版
    """
    import inspect

    def params_of(fn: Callable[..., Any]) -> dict[str, Any]:
        try:
            return dict(inspect.signature(fn).parameters)
        except (TypeError, ValueError):      # 内建函数没有签名
            return {}

    keys_fn = getattr(kv, "keys", None)
    if callable(keys_fn):
        try:
            got = keys_fn(prefix) if params_of(keys_fn) else keys_fn()
            return [str(k) for k in got if str(k).startswith(prefix)]
        except Exception:  # noqa: BLE001 - 换个形状再试，不在这里判失败
            pass

    scan_fn = getattr(kv, "scan", None)
    if callable(scan_fn):
        params = params_of(scan_fn)
        pattern = f"{prefix}*"
        try:
            if "match" in params:
                cursor: Any = 0
                found: list[str] = []
                for _ in range(64):          # 防后端游标不收敛（真 Redis 会收敛）
                    got = scan_fn(cursor, match=pattern)
                    if isinstance(got, tuple) and len(got) == 2:
                        cursor, page = got
                        found.extend(str(k) for k in page)
                        if int(cursor) == 0:
                            break
                    else:
                        # 无游标：直接一页全给
                        found.extend(str(k) for k in got)
                        break
                return [k for k in found if k.startswith(prefix)]
            got = scan_fn()                  # 无 match 参数的简化扫描
            return [str(k) for k in got if str(k).startswith(prefix)]
        except Exception:  # noqa: BLE001
            pass
    return None


def _kv_info(kv: Any) -> DistributedInfo:
    """KV 的能力自述。**绝不替后端吹牛**。

    顺序：后端自己的 `info()` → 显式属性 → 类名启发。
    启发式只往**保守**方向倒：认不出来就说 `shared=False, atomic=False`，
    因为把单进程 dict 当分布式用，正是本模块要消灭的那种静默失效。
    """
    # 1) 后端主动自述（唯一可信的强信号）
    hook = getattr(kv, "info", None)
    if callable(hook):
        try:
            got = hook()
        except Exception:  # noqa: BLE001
            got = None
        if isinstance(got, DistributedInfo):
            return got
        if isinstance(got, dict):
            return DistributedInfo(
                backend=str(got.get("backend", "unknown")),
                atomic=bool(got.get("atomic", False)),
                shared=bool(got.get("shared", False)),
                note=str(got.get("note", "")),
            )
    # 2) 显式属性（测试里的 FakeKV / 精简后端只暴露两个 bool）
    shared_attr = getattr(kv, "shared", None)
    atomic_attr = getattr(kv, "atomic", None)
    if isinstance(shared_attr, bool) or isinstance(atomic_attr, bool):
        shared = bool(shared_attr) if isinstance(shared_attr, bool) else False
        atomic = bool(atomic_attr) if isinstance(atomic_attr, bool) else shared
        name = type(kv).__name__
        note = getattr(kv, "note", "") or (
            f"{name} 自述 shared={shared} atomic={atomic}"
        )
        return DistributedInfo(backend=name, atomic=atomic, shared=shared, note=str(note))
    # 3) 类名启发：认得出是网络后端才敢说 shared
    name = type(kv).__name__
    lowered = name.lower()
    if any(tag in lowered for tag in ("resp", "redis", "etcd", "remote", "net", "http")):
        return DistributedInfo(backend=name, atomic=True, shared=True,
                               note=f"{name}：按类名识别为网络后端")
    return DistributedInfo(
        backend=name or "in-memory",
        atomic=False,
        shared=False,
        note=(f"{name} 未自述分布式能力 → 按「进程内」处理。"
              f"进程内 CAS/INCR 仍然原子（用例可证），但跨副本不可见："
              f"多副本部署必须换 RESPKV，否则黑板丢更新、配额 × 副本数、"
              f"resources 重新 stampede —— 都是静默失效。"),
    )


# ══════════════════════════════════════════════════════════════
# ① 跨副本黑板
# ══════════════════════════════════════════════════════════════
class DistributedBlackboard:
    """用 KV 的原子 CAS 实现跨副本黑板。

    与 `plane.blackboard.Blackboard` 的语义对齐（私有前缀 / 遮蔽顺序 / 冲突
    不抛异常），差别只在原子性的来源：这里**没有任何一把本地锁参与判定**，
    所有「比较并交换」都由 KV 服务端完成，所以两个副本各持一个实例时
    CAS 依然只有一方成功。

    典型 OCC 闭环（和单进程版一模一样，所以上层代码不用改）::

        a = DistributedBlackboard(kv); b = DistributedBlackboard(kv)
        a.create("plan", {"steps": ["A"]}, "leader")        # v1
        ra = a.compare_and_set("plan", 1, {"steps": ["A", "B"]}, "coder")   # v2 成功
        rb = b.compare_and_set("plan", 1, {"steps": ["A", "C"]}, "reviewer")
        # rb.ok is False, rb.current_version == 2 → 调用方读 v2 后语义合并再提交
    """

    def __init__(self, kv: Any, *, prefix: str = "bb:", ttl_s: float | None = None) -> None:
        if kv is None:
            raise ValueError("DistributedBlackboard 需要 KV 后端（见 contracts.KV）")
        self._kv = kv
        self._prefix = str(prefix)
        self._ttl = ttl_s
        # 观测计数（进程内口径：只统计「本实例发出的操作」，跨副本总量要看 KV）
        self.writes = 0
        self.deletes = 0
        self.conflicts = 0
        self.race_retries = 0      # 「比版本通过但 CAS 失败」的次数 = 真竞态证据

    # ── 键空间 ────────────────────────────────────────────────
    def _store_key(self, part: Partition, key: str, owner: str) -> str:
        """分区内的存储 key：PRIVATE 加 `@owner:` 前缀，其它分区原样。"""
        if part is Partition.PRIVATE:
            return _private_key(owner, key)
        return key

    def _kv_key(self, part: Partition, skey: str) -> str:
        """KV 里的物理 key：`{prefix}{partition}:{store_key}`。

        分区必须进 key：同名 key 在三个区是**三个条目**，扁平化后会互相撞车。
        """
        return f"{self._prefix}{part.value}:{skey}"

    def _kv_key_candidates(self, key: str, agent: str) -> list[tuple[Partition, str, str]]:
        """不带 partition 时的候选链（私有→工作→全局），已过滤空 owner 的私有区。"""
        out: list[tuple[Partition, str, str]] = []
        for part in _RESOLVE_ORDER:
            if part is Partition.PRIVATE and not agent:
                continue  # 没有 agent 就没有私有空间，不发无意义的 get
            skey = self._store_key(part, key, agent)
            out.append((part, skey, self._kv_key(part, skey)))
        return out

    # ── 内部：读原始字符串 ────────────────────────────────────
    def _raw_at(self, key: str, owner: str,
                partition: Partition | str | None) -> tuple[Partition | None, str, str, str | None]:
        """定位 → (分区, 存储 key, KV key, 原始字符串|None)。

        返回原始字符串而不是解析后的条目：CAS 需要**字节级**的期望值，
        重新 `json.dumps` 一份「看起来一样」的会因编码细节永远比不中。
        """
        if partition is not None:
            part = _as_partition(partition)
            skey = self._store_key(part, key, owner)
            kvkey = self._kv_key(part, skey)
            return part, skey, kvkey, self._kv.get(kvkey)
        for part, skey, kvkey in self._kv_key_candidates(key, owner):
            raw = self._kv.get(kvkey)
            if raw is not None and raw != TOMBSTONE:
                return part, skey, kvkey, raw
        return None, key, "", None

    # ══════════════════════════════════════════════════════════
    # 读
    # ══════════════════════════════════════════════════════════
    def read(self, key: str, agent: str = "",
             partition: Partition | str | None = None) -> BlackboardEntry | None:
        """读一个条目（不存在 / 墓碑 / 无权看 → None）。

        私有区第二道防线：即便有人在 KV 里手工塞了一个 `bb:private:@bob:x`，
        `agent="alice"` 也解析不到它（键空间天然隔离 + owner 校验）。
        """
        _part, skey, kvkey, raw = self._raw_at(key, agent, partition)
        if partition is not None and raw is None:
            return None
        if not kvkey:
            return None
        entry = _loads(raw, expected_key=skey)
        if entry is None:
            return None
        if entry.partition is Partition.PRIVATE and entry.owner != agent:
            return None
        return _view(entry)

    # ══════════════════════════════════════════════════════════
    # 写（全部由 KV 原子完成）
    # ══════════════════════════════════════════════════════════
    def create(self, key: str, value: Any, owner: str,
               partition: Partition | str = Partition.WORKING) -> CASResult:
        """新建条目（version=1）。**已存在 → conflict，绝不静默覆盖**。

        `set_nx` 本身就是「期望不存在」的 CAS，所以这里不需要先读一次：
        先 `get` 再 `set_nx` 只是多一次无意义的往返（并给出错误的时序直觉）。
        """
        part = _as_partition(partition)
        if part is Partition.GLOBAL:
            raise PartitionViolation(
                f"GLOBAL 区不接受 create(key={key!r})：全局规范经 publish_global 一次成型；"
                f"改版请 compare_and_set(..., partition=Partition.GLOBAL)"
            )
        skey = self._store_key(part, key, owner)
        kvkey = self._kv_key(part, skey)
        entry = BlackboardEntry(
            key=skey, value=value, partition=part, version=1,
            owner=owner, updated_at=now_ms(), writers=(owner,),
        )
        payload = _dumps(skey, entry)
        if self._kv.set_nx(kvkey, payload, ttl_s=self._ttl):
            self.writes += 1
            return CASResult.success(1)
        # 冲突时重新读一次：current_version 是调用方合并的唯一输入，不许猜
        self.conflicts += 1
        current = _loads(self._kv.get(kvkey), expected_key=skey)
        cur_v = current.version if current is not None else 0
        return CASResult(
            ok=False,
            current_version=cur_v,
            reason=(f"create conflict: key {skey!r} already exists at v{cur_v} "
                    f"(partition={part.value}); use compare_and_set with "
                    f"expected_version={cur_v}"),
        )

    def compare_and_set(self, key: str, expected_version: int, value: Any, owner: str,
                        partition: Partition | str | None = None) -> CASResult:
        """CAS：版本匹配才写，写入即 `version += 1` 且追加 writers。

        三步，且**第三步才是判定**：

          1. `get` 拿当前原始字符串（为了得到 CAS 的期望值，不参与判定）；
          2. 版本不符 → 立刻 conflict（省一次必然失败的往返，语义不变——
             因为即便这里放行，第 3 步的 CAS 也会失败）；
          3. `compare_and_set(期望原始串, 新串)` —— 唯一有决定权的一步。
             它失败 = 在 1 和 3 之间有人写过 → 重新读一次拿 current_version。

        第 2 步是**优化而不是判定**：把判定写在第 2 步（`if version == expected: set`）
        就退化成「读-判断-写」，那正是本模块要消灭的竞态。
        """
        part0, skey, kvkey, raw = self._raw_at(key, owner, partition)
        if raw is None:
            self.conflicts += 1
            where = _as_partition(partition).value if partition is not None else "any"
            return CASResult(
                ok=False, current_version=0,
                reason=(f"CAS conflict: key not found (key={skey!r}, "
                        f"partition={where}, expected v{expected_version})"),
            )
        current = _loads(raw, expected_key=skey)
        if current is None:
            # 墓碑 / 坏数据：当作不存在，别拿它当 base 去写
            self.conflicts += 1
            return CASResult(
                ok=False, current_version=0,
                reason=(f"CAS conflict: key not materialized (key={skey!r}, "
                        f"expected v{expected_version})"),
            )
        if current.version != expected_version:
            self.conflicts += 1
            return CASResult.conflict(expected_version, current.version)

        new_entry = BlackboardEntry(
            key=current.key, value=value, partition=current.partition,
            version=current.version + 1, owner=owner, updated_at=now_ms(),
            writers=tuple(current.writers) + (owner,),
        )
        new_raw = _dumps(skey, new_entry)
        if self._kv.compare_and_set(kvkey, raw, new_raw, ttl_s=self._ttl):
            self.writes += 1
            return CASResult.success(new_entry.version)

        # 竞态：比版本时还是 v(expected)，真正 CAS 时已被别人改掉
        self.race_retries += 1
        self.conflicts += 1
        latest = _loads(self._kv.get(kvkey), expected_key=skey)
        cur_v = latest.version if latest is not None else 0
        return CASResult.conflict(expected_version, cur_v)

    def publish_global(self, key: str, value: Any, owner: str) -> CASResult:
        """GLOBAL 区：一次成型（`set_nx`），改版必须走 CAS。"""
        skey = key
        kvkey = self._kv_key(Partition.GLOBAL, skey)
        entry = BlackboardEntry(
            key=skey, value=value, partition=Partition.GLOBAL, version=1,
            owner=owner, updated_at=now_ms(), writers=(owner,),
        )
        if self._kv.set_nx(kvkey, _dumps(skey, entry), ttl_s=self._ttl):
            self.writes += 1
            return CASResult.success(1)
        self.conflicts += 1
        current = _loads(self._kv.get(kvkey), expected_key=skey)
        cur_v = current.version if current is not None else 0
        return CASResult(
            ok=False, current_version=cur_v,
            reason=(f"publish conflict: global key {skey!r} already published at v{cur_v}; "
                    f"GLOBAL 一次成型，改版请 compare_and_set(key, {cur_v}, ..., "
                    f"partition=Partition.GLOBAL)"),
        )

    def write_private(self, key: str, value: Any, owner: str) -> CASResult:
        """PRIVATE 区：单写者 → upsert，但版本照旧 +1（私有探索也要演进轨迹）。

        单写者不等于单进程：同一个 owner 的两个副本仍可能并发写同一私有 key。
        所以这里也不是「读-写」，而是 CAS 循环**有限次**：单写者前提下预期
        一次就成，真撞上了说明部署有问题，宁可 conflict 也不覆盖。
        """
        if not owner:
            raise ValueError("write_private 需要 owner（PRIVATE 区必须有写者身份）")
        for _ in range(3):
            _part, skey, kvkey, raw = self._raw_at(key, owner, Partition.PRIVATE)
            current = _loads(raw, expected_key=skey)
            new_v = (current.version + 1) if current is not None else 1
            entry = BlackboardEntry(
                key=skey, value=value, partition=Partition.PRIVATE, version=new_v,
                owner=owner, updated_at=now_ms(),
                writers=(tuple(current.writers) + (owner,)) if current else (owner,),
            )
            if raw is None:
                if self._kv.set_nx(kvkey, _dumps(skey, entry), ttl_s=self._ttl):
                    self.writes += 1
                    return CASResult.success(1)
                continue
            if self._kv.compare_and_set(kvkey, raw, _dumps(skey, entry), ttl_s=self._ttl):
                self.writes += 1
                return CASResult.success(new_v)
        self.conflicts += 1
        latest = _loads(self._raw_at(key, owner, Partition.PRIVATE)[3], expected_key=key)
        return CASResult.conflict(new_v, latest.version if latest else 0)

    # ══════════════════════════════════════════════════════════
    # 删除（CAS 到墓碑 + delete）
    # ══════════════════════════════════════════════════════════
    def delete(self, key: str, expected_version: int, owner: str,
               partition: Partition | str | None = None) -> CASResult:
        """带版本号删除。版本不符 → conflict（**别人刚改过，删了就是数据丢失**）。

        KV 没有 compare-and-delete，所以用「CAS 到墓碑」取得独占权，再 delete：
        CAS 成功的那一刻起，任何基于旧值的写都会失败，删除因此是**决定性**的；
        delete 之后 key 彻底消失，重新 create 从 v1 起（与单进程版一致）。
        """
        _part, skey, kvkey, raw = self._raw_at(key, owner, partition)
        if raw is None:
            self.conflicts += 1
            where = _as_partition(partition).value if partition is not None else "any"
            return CASResult(
                ok=False, current_version=0,
                reason=(f"delete conflict: key not found (key={skey!r}, "
                        f"partition={where}, expected v{expected_version})"),
            )
        current = _loads(raw, expected_key=skey)
        if current is None or current.version != expected_version:
            self.conflicts += 1
            return CASResult.conflict(
                expected_version, current.version if current is not None else 0)
        if not self._kv.compare_and_set(kvkey, raw, TOMBSTONE, ttl_s=self._ttl):
            self.race_retries += 1
            self.conflicts += 1
            latest = _loads(self._kv.get(kvkey), expected_key=skey)
            return CASResult.conflict(
                expected_version, latest.version if latest is not None else 0)
        self._kv.delete(kvkey)
        self.deletes += 1
        return CASResult.success(current.version)

    # ══════════════════════════════════════════════════════════
    # 快照 / 自述
    # ══════════════════════════════════════════════════════════
    def snapshot(self) -> dict:
        """全量快照（`json.dumps` 友好），键形如 `"{partition}:{store_key}"`。

        需要 KV 支持 key 枚举。后端不支持时**不伪造空快照**：
        返回 `enumeration="unsupported"` + 空 entries，让运维看到的是
        「这个后端不能枚举」而不是「黑板是空的」——两者结论完全相反。
        """
        keys = _kv_keys(self._kv, self._prefix)
        entries: dict[str, dict] = {}
        partitions: dict[str, list[str]] = {p.value: [] for p in Partition}
        versions: dict[str, int] = {}
        enumeration = "kv.keys" if keys is not None else "unsupported"
        for kvkey in keys or ():
            raw = self._kv.get(kvkey)
            entry = _loads(raw, expected_key=kvkey)
            if entry is None:
                continue
            composite = f"{entry.partition.value}:{entry.key}"
            entries[composite] = entry.to_dict()
            partitions[entry.partition.value].append(composite)
            versions[composite] = entry.version
        return {
            "schema": "distributed-blackboard/1",
            "entries": entries,
            "partitions": {k: sorted(v) for k, v in partitions.items()},
            "versions": versions,
            "enumeration": enumeration,
            "info": self._info_dict(),
        }

    def _info_dict(self) -> dict:
        info = self.info()
        return {"backend": info.backend, "atomic": info.atomic,
                "shared": info.shared, "note": info.note}

    def info(self) -> DistributedInfo:
        """能力自述。`shared` 只信后端自己的话，见 `_kv_info`。"""
        return _kv_info(self._kv)

    def stats(self) -> dict:
        return {"writes": self.writes, "deletes": self.deletes,
                "conflicts": self.conflicts, "race_retries": self.race_retries}

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return (f"DistributedBlackboard({type(self._kv).__name__}, prefix={self._prefix!r}, "
                f"writes={self.writes}, conflicts={self.conflicts})")


# ══════════════════════════════════════════════════════════════
# ② 跨副本配额
# ══════════════════════════════════════════════════════════════
class DistributedQuota:
    """集中式固定窗口配额（跨副本精确）。

    与 `Governance.QuotaGuard` 的关系：**同一个对外契约**（`charge → GuardVerdict`），
    不同的分母来源。QuotaGuard 数进程内 deque（每副本一份 → 限额 × N），
    这里数 KV 里的一个计数器（所有副本一份 → 限额就是限额）。

    顺序铁律（见模块 docstring）：先 `INCRBY` 再判，超了 `DECRBY` 回滚。
    """

    DEFAULT_LIMIT = 60

    def __init__(self, kv: Any, *, per_minute: dict[str, int] | None = None,
                 prefix: str = "quota:", clock: Callable[[], float] | None = None) -> None:
        if kv is None:
            raise ValueError("DistributedQuota 需要 KV 后端（见 contracts.KV）")
        self._kv = kv
        self._prefix = str(prefix)
        self.per_minute: dict[str, int] = (
            dict(per_minute) if per_minute is not None
            else {"tool_call": 60, "llm_call": 30}
        )
        #: 秒级时钟；测试注入假时钟即可推窗口（不 sleep 60 秒）
        self._clock: Callable[[], float] = clock or time.time
        self.window_seconds: float = WINDOW_SECONDS
        self.denied_total = 0
        self.rollbacks = 0
        #: 本实例见过的窗口（只用于 snapshot 枚举的后备，不是判定依据）
        self._seen: set[tuple[str, str]] = set()
        self._lock = threading.RLock()

    # ── 内部 ────────────────────────────────────────────────
    def _limit_of(self, kind: str) -> int:
        try:
            return int(self.per_minute.get(kind, self.DEFAULT_LIMIT))
        except Exception:  # noqa: BLE001
            return 0                     # 配置坏了 → 上限 0 → 全拦（fail-closed）

    def _window_id(self) -> int:
        return int(float(self._clock()) // self.window_seconds)

    def _key(self, tenant: str, kind: str, window_id: int | None = None) -> str:
        wid = self._window_id() if window_id is None else window_id
        return f"{self._prefix}{tenant}:{kind}:{wid}"

    # ── 对外 ────────────────────────────────────────────────
    def charge(self, tenant: str, kind: str = "tool_call", n: int = 1) -> GuardVerdict:
        """计费 n 次。允许 → `GuardVerdict.ok(payload=...)`；超限 → block。

        payload 形状与 `QuotaGuard` 对齐（tenant/kind/used/limit/remaining/
        retry_after_s），上层换实现不用改取值路径。
        """
        try:
            if not isinstance(tenant, str) or not tenant.strip():
                return GuardVerdict.block(
                    "quota denied (fail-closed): missing tenant", hits=("quota",))
            if not isinstance(kind, str) or not kind.strip():
                return GuardVerdict.block(
                    "quota denied (fail-closed): missing quota kind", hits=("quota",))
            if not isinstance(n, int) or isinstance(n, bool):
                return GuardVerdict.block(
                    f"quota denied (fail-closed): invalid charge n={n!r}", hits=("quota",))
            if n < 0:
                return GuardVerdict.block(
                    f"quota denied (fail-closed): negative charge n={n}", hits=("quota",))

            tenant = tenant.strip()
            kind = kind.strip()
            limit = self._limit_of(kind)
            window_id = self._window_id()
            key = self._key(tenant, kind, window_id)
            with self._lock:
                self._seen.add((tenant, kind))

            if n == 0:
                # 纯查询：绝不 INCRBY 0（那会凭空创建 key，把「查过」变成「用过」）
                used_now = self.used(tenant, kind)
                return GuardVerdict.ok(
                    payload={"tenant": tenant, "kind": kind, "used": used_now,
                             "limit": limit, "remaining": max(0, limit - used_now),
                             "window_id": window_id},
                    hits=("quota",))

            total = int(self._kv.incr(key, n))
            if total <= n:
                # 本次是这轮窗口的第一次写 → 由我们设过期（只设一次，过期时间不被反复续命）
                self._kv.expire(key, self.window_seconds)

            if total > limit:
                # 回滚：被拒的请求不许占额度，否则一次刷量就永久抬高水位
                self._kv.incr(key, -n)
                self.denied_total += 1
                self.rollbacks += 1
                used_now = max(0, total - n)
                retry_after = self.window_seconds - (
                    float(self._clock()) - window_id * self.window_seconds)
                return GuardVerdict.block(
                    f"quota exceeded: {kind} for tenant {tenant}: "
                    f"{total}/{limit} per {self.window_seconds:.0f}s (window {window_id})",
                    payload={"tenant": tenant, "kind": kind, "used": used_now,
                             "requested": n, "limit": limit,
                             "retry_after_s": round(max(0.0, retry_after), 3),
                             "window_id": window_id},
                    hits=("quota_exceeded",),
                )

            return GuardVerdict.ok(
                payload={"tenant": tenant, "kind": kind, "used": total, "limit": limit,
                         "remaining": max(0, limit - total), "window_id": window_id},
                hits=("quota",))
        except Exception as exc:  # noqa: BLE001 - fail-closed：算不出配额就不放行
            self.denied_total += 1
            return GuardVerdict.block(
                f"quota denied (fail-closed): {type(exc).__name__}: {exc}",
                hits=("quota_fail_closed",))

    def used(self, tenant: str, kind: str = "tool_call") -> int:
        """当前窗口已用次数（观测口径；放行判定只在 `charge` 里）。"""
        try:
            raw = self._kv.get(self._key(str(tenant).strip(), str(kind).strip()))
            return max(0, int(raw)) if raw is not None else 0
        except (TypeError, ValueError):
            return 0
        except Exception:  # noqa: BLE001
            return 0

    def snapshot(self) -> dict:
        """窗口只读快照。枚举不到跨副本的窗口就明说 `enumeration`，不假装空。"""
        windows: dict[str, dict] = {}
        with self._lock:
            seen = sorted(self._seen)
        enumeration = "local-seen"
        keys = _kv_keys(self._kv, self._prefix)
        pairs: set[tuple[str, str]] = set(seen)
        if keys is not None:
            enumeration = "kv.keys"
            for k in keys:
                body = k[len(self._prefix):]
                bits = body.rsplit(":", 2)
                if len(bits) == 3 and bits[2].lstrip("-").isdigit():
                    pairs.add((bits[0], bits[1]))
        for tenant, kind in sorted(pairs):
            used_now = self.used(tenant, kind)
            limit = self._limit_of(kind)
            windows[f"{tenant}:{kind}"] = {
                "used": used_now, "limit": limit,
                "remaining": max(0, limit - used_now),
                "window_id": self._window_id(),
            }
        return {"window_seconds": self.window_seconds,
                "per_minute": dict(self.per_minute),
                "windows": windows,
                "enumeration": enumeration,
                "denied_total": self.denied_total,
                "rollbacks": self.rollbacks}

    def info(self) -> DistributedInfo:
        base = _kv_info(self._kv)
        note = base.note
        if not base.shared:
            note = (note + " | 配额跨副本精确的前提是 KV 共享：当前后端 shared=False，"
                            "多副本下限额仍会 × 副本数。")
        return DistributedInfo(backend=base.backend, atomic=base.atomic,
                               shared=base.shared, note=note)

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return (f"DistributedQuota({type(self._kv).__name__}, "
                f"window={self.window_seconds:.0f}s, denied={self.denied_total})")


# ══════════════════════════════════════════════════════════════
# ③ singleflight
# ══════════════════════════════════════════════════════════════
class SingleFlightTimeout(Exception):
    """等待在飞请求超时（leader 仍在跑，等待者先退场）。"""

    def __init__(self, key: str, timeout_s: float) -> None:
        super().__init__(f"singleflight timeout after {timeout_s:.3f}s for key {key!r}")
        self.key = key
        self.timeout_s = timeout_s


class _InFlight:
    """一次在飞调用的共享信箱：结果 / 异常 / 完成事件。"""

    __slots__ = ("event", "value", "error", "waiters")

    def __init__(self) -> None:
        self.event = threading.Event()
        self.value: Any = None
        self.error: BaseException | None = None
        self.waiters = 0


class SingleFlight:
    """同一 key 的并发请求只真正执行一次，其余等结果。

    - leader：第一个到达者执行 `fn`，把结果/异常放进共享信箱；
    - follower：后来者**不执行** `fn`，只等信箱（拿到的是**同一个对象**，
      不是深拷贝：调用方按只读使用，深拷贝会把大结果的开销乘 N）；
    - `fn` 抛异常 → 所有等待者拿到**同一个异常对象**（`raise call.error`），
      不吞成 None、不换类型——上层 catch 一种类型就够；
    - 不同 key 各有信箱，互不阻塞；
    - follower 超时 → `SingleFlightTimeout`；leader 不受影响（它本来在跑）。

    leader 在**唤醒等待者之前**就把 key 从在飞表里摘掉：这样后续请求会开新一轮，
    而不是拿到一个已经完成的历史结果（singleflight 不是缓存，过期语义归缓存管）。
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._calls: dict[str, _InFlight] = {}
        self._calls_total = 0
        self._hits = 0        # follower 并入次数（= 省掉的 fn 执行次数）
        self._shared = 0      # 「确实有 follower 共享」的 leader 批次数量
        self._executions = 0  # fn 真实执行次数
        self._errors = 0      # leader fn 抛异常次数
        self._timeouts = 0    # follower 超时次数

    def do(self, key: str, fn: Callable[[], Any], *, timeout_s: float = 5.0) -> Any:
        if not callable(fn):
            raise TypeError("SingleFlight.do 需要可调用对象 fn")
        key = str(key)
        with self._lock:
            self._calls_total += 1
            call = self._calls.get(key)
            leader = call is None
            if leader:
                call = _InFlight()
                self._calls[key] = call
            else:
                call.waiters += 1
                self._hits += 1

        if leader:
            try:
                call.value = fn()
                self._executions += 1
            except BaseException as exc:  # 连 KeyboardInterrupt 也要传给等待者，否则它们会挂死
                call.error = exc
                self._errors += 1
            finally:
                with self._lock:
                    self._calls.pop(key, None)   # 先摘牌：后来的请求开新一轮
                    if call.waiters:
                        self._shared += 1
                call.event.set()
            if call.error is not None:
                raise call.error
            return call.value

        if not call.event.wait(timeout_s):
            with self._lock:
                self._timeouts += 1
            raise SingleFlightTimeout(key, timeout_s)
        if call.error is not None:
            raise call.error
        return call.value

    def stats(self) -> dict:
        """`calls` = do() 总次数；`hits` = 并入在飞请求的次数；
        `shared` = 有 follower 共享的 leader 批次数；`errors` = leader 抛异常次数。"""
        with self._lock:
            return {"calls": self._calls_total, "hits": self._hits,
                    "shared": self._shared, "errors": self._errors,
                    "executions": self._executions, "timeouts": self._timeouts,
                    "inflight": len(self._calls)}

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        s = self.stats()
        return (f"SingleFlight(calls={s['calls']}, hits={s['hits']}, "
                f"executions={s['executions']}, inflight={s['inflight']})")


# ══════════════════════════════════════════════════════════════
# ④ resources 的 cache stampede 修复（组合，不改 resources.py）
# ══════════════════════════════════════════════════════════════
class SingleFlightResourceRuntime:
    """`ResourceRuntime` 的 singleflight 装饰层：**组合**，不改原文件。

    为什么用组合而不是继承：`ResourceRuntime.read` 的八步顺序是安全语义
    （授权先于缓存、护栏每次出口都算），继承后一旦有人 `super().read()`
    改顺序，安全语义就跟着漂。组合只包住「谁去调 reader」这一件事，
    内部的八步一步不动 —— **授权、截断、脱敏全部原样生效**（用例 24 证）。

    `__getattr__` 转发其余全部属性（`stats` / `registry` / `cache_len` /
    `invalidate` ...），所以对调用方来说它「看起来就是」原来的 runtime；
    唯一拿不到的是 `isinstance(x, ResourceRuntime)`，需要类型判断时用 `.inner`。
    """

    def __init__(self, runtime: Any, *, timeout_s: float = 5.0,
                 flight: SingleFlight | None = None) -> None:
        if runtime is None:
            raise ValueError("wrap_with_singleflight 需要一个 ResourceRuntime")
        self._runtime = runtime
        self._timeout_s = float(timeout_s)
        self.flight = flight if flight is not None else SingleFlight()

    # ── 覆写：读 ────────────────────────────────────────────
    def read(self, uri: str, e: Any) -> dict:
        """同 key 并发只打一次 reader；失败/异常在等待者之间共享。"""
        key = self.flight_key(uri, e)
        return self.flight.do(key, lambda: self._runtime.read(uri, e),
                              timeout_s=self._timeout_s)

    # ── key 的构造（**不是裸 uri**） ────────────────────────
    @staticmethod
    def flight_key(uri: str, e: Any) -> str:
        """`uri + 主体指纹`。

        裸 uri 会把「有权限的 A 正在读」和「无权限的 B 同时读」并成一次调用，
        B 就白拿了一份未经授权检查的结果 —— 也就是 `resources.py` 里
        「缓存不是权限的旁路」那条规则的并发版。
        指纹带 scope 集合，所以宽 scope 主体也替不了窄 scope 主体取数。
        """
        principal = getattr(e, "principal", None)
        if principal is None:
            return f"{uri}\x00anonymous"
        user = getattr(principal, "user_id", "") or ""
        tenant = getattr(principal, "tenant", "") or ""
        scopes = ""
        raw_scopes = getattr(principal, "scopes", None)
        if raw_scopes is not None:
            try:
                scopes = "|".join(sorted(str(s) for s in raw_scopes))
            except Exception:  # noqa: BLE001 - 指纹算不出来就退化成更保守的 key
                scopes = repr(raw_scopes)
        roles = ""
        raw_roles = getattr(principal, "roles", None)
        if raw_roles is not None:
            try:
                roles = "|".join(str(r) for r in raw_roles)
            except Exception:  # noqa: BLE001
                roles = repr(raw_roles)
        return f"{uri}\x00{tenant}\x00{user}\x00{roles}\x00{scopes}"

    @property
    def inner(self) -> Any:
        """被包裹的原始 runtime（需要 isinstance / 直接调方法时用）。"""
        return self._runtime

    def flight_stats(self) -> dict:
        """singleflight 自己的计数（**不覆盖** runtime 的 `stats` 字典）。

        `ResourceRuntime.stats` 是个 dict 属性（`rt.stats["hits"]`），
        包装层若把它换成同名方法，所有既有调用方都会在
        `TypeError: 'function' object is not subscriptable` 上炸掉 ——
        「包装」的底线是**不改变被包装对象的可见接口**。
        """
        return self.flight.stats()

    def __getattr__(self, name: str) -> Any:
        # 只有「自己没定义」的属性才走到这里 → 天然转发 runtime 的一切
        return getattr(self._runtime, name)

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return (f"SingleFlightResourceRuntime({type(self._runtime).__name__}, "
                f"timeout={self._timeout_s:.3f}s, flight={self.flight!r})")


def wrap_with_singleflight(runtime: Any, *, timeout_s: float = 5.0) -> SingleFlightResourceRuntime:
    """包住 ResourceRuntime，让同 uri（同一主体）的并发读只调一次 reader。

    用法::

        rt = ResourceRuntime(registry, governance=gov)
        rt = wrap_with_singleflight(rt, timeout_s=5.0)
        rt.read("file://a.txt", ctx)     # 其余接口原样可用（__getattr__ 转发）

    为什么不去改 `plane/resources.py`：缓存与 singleflight 是**两个不同的问题**，
    混进一个类会让「命中缓存」和「加入在飞请求」的计数口径纠缠在一起
    （`hits/misses` 与 `hits/shared` 会互相污染），排查时无法区分
    「真的打了一次下游」和「等了一次别人的下游」。
    """
    return SingleFlightResourceRuntime(runtime, timeout_s=timeout_s)
