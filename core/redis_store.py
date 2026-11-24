"""core/redis_store.py — W28：`ContextStore` 的 Redis 实现（零第三方依赖，走 `core.kv`）。

===============================================================================
为什么 Context 需要"跨副本可见"的存法
===============================================================================
`FileStore` / `SQLiteStore` 解决的是「**本机**重启后还能续跑」。
多副本部署解决的是另一个问题：「副本 A 挂起的审批，副本 B 能不能接手」。

本机盘做不到这一点：B 的 `list_pending()` 永远看不到 A 写的文件。而
「挂起 4 小时后换一台机器续跑」在副本被调度走、被滚动重启时是**常态**。

Redis 版把三件事同时摆平：
    跨副本可见   → `save` 之后同一 Redis 上任何副本都能 `load`
    写入原子     → 主写入是**单条 SET**（新值全有或旧值全有，不存在半个 payload）
    索引查询     → 二级索引集合，`list_pending` 是 `SMEMBERS`，不是 `SCAN` 全库

===============================================================================
索引设计（派生数据，永远不是真相来源）
===============================================================================
    {prefix}payload:{ctx_id}   → Context.serialize() 的原样字符串   ← **唯一真相**
    {prefix}meta:{ctx_id}      → {phase, task_id, tenant, saved_at, schema_version}
    {prefix}index              → 所有 ctx_id 的集合（list_pending 用）
    {prefix}tenant:{tenant}    → 该租户的 ctx_id 集合（list_by_tenant 用）

三条不变量：

1. **真相只有 payload 一个。** 索引是加速器，任何时候坏掉/落后，`load` 都不受影响。
2. **`list_*` 的判据是 meta，不是集合成员。** 集合里残留一个已经过期/被改租户的
   成员时，以 meta 为准过滤 —— 索引错了最多多读几行，不会返回错的数据。
3. **索引会自愈。** `list_pending()` 发现某个成员既没有 meta 也没有 payload
   （TTL 到期 / 被别的副本删了），顺手 `SREM` 掉。读路径顺便做清理，
   比后台 compaction 简单得多，也不需要多一个定时任务。

===============================================================================
与 `FileStore` / `SQLiteStore` 的对齐口径
===============================================================================
* `ctx_id` 校验复用 `core.store.validate_ctx_id`（同一口径，换 store 不能绕过路径规则）；
* payload 必须是 str（`Context.serialize()` 的输出），不是就抛 `StoreError`；
* `phase/task_id/tenant` 用 `core.sqlite_store.extract_meta` 解析，**解析失败不报错**
  （坏 payload 仍然要能存、能取、能被 `ContextHandle.restore()` 去拒绝）；
* `schema_version` 不认识 → `StoreSchemaMismatch`（不猜旧版本怎么读）；
* `list_pending()` 按 `(saved_at, ctx_id)` 升序；`list_by_tenant` 按 `saved_at DESC`。

一致性的**边界**（诚实写）：
`save` 的 payload 写入是原子的，但"payload + meta + 两个索引集合"这一组不是
一个事务（Redis 单机可以用 MULTI，跨 slot 的 Cluster 不行；本模块刻意不用事务，
因为索引只是派生数据）。所以存在极窄的窗口：payload 已写、索引还没更新。
此时 `load` 正确，`list_by_tenant` 可能暂时少一条 —— 下一次 `save` 或
`list_pending()` 的自愈会补上。**这是刻意的取舍**：索引的权威性低于可用性。
"""
from __future__ import annotations

import json
from typing import Any

from contracts import now_ms
from core.kv import KVError, _ttl_arg
from core.sqlite_store import NONE_TENANT, extract_meta
from core.store import (
    STORE_SCHEMA_VERSION,
    StoreCorrupt,
    StoreError,
    StoreSchemaMismatch,
    validate_ctx_id,
)

__all__ = ["RedisStore"]

#: 单页读取 meta 时的上限（只影响 `_entries()` 的取数方式，不影响语义）
_DEFAULT_LIMIT = 100


def _require_payload(payload: Any) -> str:
    """与 `core/store._require_payload` 同口径（错误类型一致，三个实现可互换）。"""
    if not isinstance(payload, str):
        raise StoreError(
            f"payload 必须是 str（Context.serialize() 的输出），"
            f"收到 {type(payload).__name__}"
        )
    return payload


def _limit_of(limit: Any) -> int:
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise TypeError(f"limit 必须是 int，收到 {type(limit).__name__}")
    if limit < 0:
        raise ValueError(f"limit 不能为负：{limit}")
    return limit


def _checked_ttl(ttl_s: Any) -> float | None:
    """构造期校验 TTL：非法值立刻炸，而不是等到第一次 save 才炸。"""
    try:
        return _ttl_arg(ttl_s)
    except KVError as exc:
        raise ValueError(str(exc)) from exc


class RedisStore:
    """`ContextStore` 协议的 Redis 实现。

    构造::

        store = RedisStore(RESPKV())                          # 默认 127.0.0.1:6379
        store = RedisStore(InMemoryKV())                      # 单进程替身，形状一致
        store = RedisStore(RESPKV(host=HOST, port=PORT), ttl_s=3600, prefix="ctx:")

    `ttl_s` 是**自动过期**（给"挂起 4 小时没人管就该清掉"这种场景）：
    `None` = 永不过期，删不删由调用方决定。
    """

    def __init__(self, kv: Any, *, ttl_s: float | None = None,
                 prefix: str = "ctx:") -> None:
        for name in ("get", "set", "delete"):
            if not callable(getattr(kv, name, None)):
                raise TypeError(f"kv 必须实现 {name}()（contracts.KV 的最小子集）")
        if not isinstance(prefix, str):
            raise TypeError(f"prefix 必须是 str，收到 {type(prefix).__name__}")
        self.kv = kv
        self.ttl_s = _checked_ttl(ttl_s)
        self.prefix = prefix
        #: 观测计数（与 FileStore / SQLiteStore 同名同义）
        self.saves = 0
        self.loads = 0
        self.deletes = 0

    # ── key 布局 ───────────────────────────────────────────
    def _k_payload(self, ctx_id: str) -> str:
        return f"{self.prefix}payload:{ctx_id}"

    def _k_meta(self, ctx_id: str) -> str:
        return f"{self.prefix}meta:{ctx_id}"

    def _k_index(self) -> str:
        return f"{self.prefix}index"

    def _k_tenant(self, tenant: str) -> str:
        return f"{self.prefix}tenant:{tenant}"

    # ── 索引原语（集合优先，SCAN 兜底）──────────────────────
    def _can_use_sets(self) -> bool:
        return all(callable(getattr(self.kv, n, None))
                   for n in ("sadd", "srem", "smembers"))

    def _scan_pattern(self, suffix: str) -> str:
        """拼出物理 match 模式：KV 自己的命名空间前缀 + 本 store 的前缀。"""
        outer = getattr(self.kv, "key_prefix", "") or ""
        return f"{outer}{self.prefix}{suffix}*"

    def _index_members(self) -> list[str]:
        if self._can_use_sets():
            return self.kv.smembers(self._k_index())
        scan = getattr(self.kv, "scan", None)
        if not callable(scan):
            raise KVError(
                "kv 既不支持 sadd/smembers 也不支持 scan：无法维护 list_pending 索引"
            )
        pattern = self._scan_pattern("payload:")
        found: list[str] = []
        cursor = 0
        while True:
            cursor, keys = scan(cursor, match=pattern, count=200)
            found.extend(k[len(pattern) - 1:] for k in keys)
            if cursor == 0:
                break
        return sorted(found)

    def _index_add(self, ctx_id: str) -> None:
        if self._can_use_sets():
            self.kv.sadd(self._k_index(), ctx_id)

    def _index_remove(self, *ctx_ids: str) -> None:
        if not ctx_ids:
            return
        if self._can_use_sets():
            self.kv.srem(self._k_index(), *ctx_ids)

    def _tenant_add(self, tenant: str, ctx_id: str) -> None:
        if tenant and self._can_use_sets():
            self.kv.sadd(self._k_tenant(tenant), ctx_id)

    def _tenant_remove(self, tenant: str | None, ctx_id: str) -> None:
        if tenant and self._can_use_sets():
            self.kv.srem(self._k_tenant(tenant), ctx_id)

    # ── meta 编解码 ────────────────────────────────────────
    @staticmethod
    def _empty_row(ctx_id: str) -> dict:
        return {"ctx_id": ctx_id, "phase": None, "task_id": None, "tenant": None,
                "saved_at": 0.0, "schema_version": STORE_SCHEMA_VERSION}

    def _read_meta(self, ctx_id: str, *, strict: bool = True) -> dict | None:
        """读 meta 行；不存在 → None。

        `strict=True`（读路径）时校验 `schema_version`，不认识就 `StoreSchemaMismatch`；
        `strict=False`（写路径）时只用来捞"旧租户是谁"以便清理索引 ——
        一个旧版本写下的 meta 不该阻止新版本**覆盖**它。
        """
        raw = self.kv.get(self._k_meta(ctx_id))
        if raw is None:
            return None
        try:
            doc = json.loads(raw)
        except (json.JSONDecodeError, ValueError) as exc:
            raise StoreCorrupt(f"{self._k_meta(ctx_id)} 不是合法 JSON：{exc}") from exc
        if not isinstance(doc, dict):
            raise StoreCorrupt(
                f"{self._k_meta(ctx_id)} 顶层不是 JSON object（{type(doc).__name__}）"
            )
        version = doc.get("schema_version", STORE_SCHEMA_VERSION)
        if isinstance(version, bool) or not isinstance(version, int):
            raise StoreSchemaMismatch(
                f"{self._k_meta(ctx_id)} 的 schema_version 不是整数：{version!r}"
            )
        if version != STORE_SCHEMA_VERSION and strict:
            raise StoreSchemaMismatch(
                f"{self._k_meta(ctx_id)} 的 schema_version 不认识："
                f"期望 {STORE_SCHEMA_VERSION}，收到 {version}（不猜旧版本怎么读）"
            )
        row = self._empty_row(ctx_id)
        row.update({
            "phase": doc.get("phase"),
            "task_id": doc.get("task_id"),
            "tenant": doc.get("tenant"),
            "saved_at": float(doc.get("saved_at") or 0.0),
            "schema_version": version,
        })
        return row

    def _write_meta(self, ctx_id: str, *, phase: str | None, task_id: str | None,
                    tenant: str | None, saved_at: float) -> None:
        blob = json.dumps({
            "phase": phase,
            "task_id": task_id,
            "tenant": tenant,
            "saved_at": saved_at,
            "schema_version": STORE_SCHEMA_VERSION,
        }, ensure_ascii=False)
        self.kv.set(self._k_meta(ctx_id), blob, ttl_s=self.ttl_s)

    # ── 协议四件套 ─────────────────────────────────────────
    def save(self, ctx_id: str, payload: str) -> None:
        """存一个 Context。

        **主写入是单条 `SET`**：payload 要么是完整的旧版本，要么是完整的新版本 ——
        Redis 的 SET 单命令原子性直接兑现了 `ContextStore` 契约里那句"sava 必须原子"。
        剩下的 meta / 索引是派生数据，写在主写入之后，坏掉也不影响 `load`。
        """
        validate_ctx_id(ctx_id)
        _require_payload(payload)
        phase, task_id, tenant = extract_meta(payload)
        saved_at = now_ms()

        # 旧租户要在覆盖前捞出来，否则索引里会留一条"该租户仍有这个 ctx"的谎话
        try:
            old = self._read_meta(ctx_id, strict=False)
        except StoreCorrupt:
            old = None                       # 旧 meta 坏了：照样覆盖，别让它拦住写入
        old_tenant = None if old is None else old.get("tenant")

        self.kv.set(self._k_payload(ctx_id), payload, ttl_s=self.ttl_s)
        self._write_meta(ctx_id, phase=phase, task_id=task_id, tenant=tenant,
                         saved_at=saved_at)
        self._index_add(ctx_id)
        self._tenant_add(tenant, ctx_id)
        if old_tenant and old_tenant != tenant:
            self._tenant_remove(old_tenant, ctx_id)
        self.saves += 1

    def load(self, ctx_id: str) -> str | None:
        """读一个 Context；不存在（含已过期）→ None。"""
        validate_ctx_id(ctx_id)
        payload = self.kv.get(self._k_payload(ctx_id))
        self.loads += 1
        if payload is None:
            return None
        # 信封校验：meta 在就校验版本（与 FileStore/SQLiteStore 同口径）；
        # meta 不在（索引落后/手工塞的 key）→ 仍然返回 payload，不凭空拒绝
        self._read_meta(ctx_id)
        return payload if isinstance(payload, str) else str(payload)

    def delete(self, ctx_id: str) -> bool:
        """删除；返回是否真的删掉了一份 payload（幂等：索引残留顺带清掉）。"""
        validate_ctx_id(ctx_id)
        try:
            old = self._read_meta(ctx_id, strict=False)
        except StoreCorrupt:
            old = None
        removed = self.kv.delete(self._k_payload(ctx_id))
        self.kv.delete(self._k_meta(ctx_id))
        self._index_remove(ctx_id)
        if old is not None:
            self._tenant_remove(old.get("tenant"), ctx_id)
        if removed:
            self.deletes += 1
        return bool(removed)

    def list_pending(self) -> list[str]:
        """按 `(saved_at, ctx_id)` **升序**（与 FileStore / SQLiteStore 一致）。

        走索引集合，不做全库 SCAN；顺手把"既没 meta 也没 payload"的成员踢掉。
        """
        rows = self._entries()
        rows.sort(key=lambda r: (r["saved_at"], r["ctx_id"]))
        return [r["ctx_id"] for r in rows]

    # ── 扩展查询 ───────────────────────────────────────────
    def list_by_tenant(self, tenant: str, *, limit: int = _DEFAULT_LIMIT) -> list[dict]:
        """某租户最近写入的 N 条（倒序）。**只返回该租户**（按 meta 判定，不按集合）。"""
        if not isinstance(tenant, str):
            raise TypeError(f"tenant 必须是 str，收到 {type(tenant).__name__}")
        n = _limit_of(limit)
        rows = [r for r in self._entries() if r["tenant"] == tenant]
        rows.sort(key=lambda r: (-r["saved_at"], r["ctx_id"]))
        return [self._row_dict(r) for r in rows[:n]]

    def list_recent(self, *, limit: int = 50) -> list[dict]:
        """最近写入的 N 条（倒序，不读 payload）。与 SQLiteStore 同名同形。"""
        n = _limit_of(limit)
        rows = self._entries()
        rows.sort(key=lambda r: (-r["saved_at"], r["ctx_id"]))
        return [self._row_dict(r) for r in rows[:n]]

    def stats(self) -> dict:
        """计数 + 租户分布 + 时间跨度（空库不崩，oldest/newest 为 None）。"""
        rows = self._entries()
        by_tenant: dict[str, int] = {}
        for row in rows:
            key = NONE_TENANT if row["tenant"] is None else str(row["tenant"])
            by_tenant[key] = by_tenant.get(key, 0) + 1
        stamps = [r["saved_at"] for r in rows if r["saved_at"]]
        return {
            "total": len(rows),
            "by_tenant": by_tenant,
            "oldest": min(stamps) if stamps else None,
            "newest": max(stamps) if stamps else None,
        }

    # ── 附加查询（与 FileStore / SQLiteStore 同名，便于替换）──
    def saved_at(self, ctx_id: str) -> float | None:
        validate_ctx_id(ctx_id)
        row = self._read_meta(ctx_id)
        return None if row is None else float(row["saved_at"])

    def exists(self, ctx_id: str) -> bool:
        validate_ctx_id(ctx_id)
        return self.kv.get(self._k_payload(ctx_id)) is not None

    def __len__(self) -> int:
        return len(self._entries())

    def __contains__(self, ctx_id: object) -> bool:
        if not isinstance(ctx_id, str):
            return False
        try:
            return self.exists(ctx_id)
        except ValueError:
            return False

    # ── 生命周期 ───────────────────────────────────────────
    def close(self) -> None:
        """关掉底层连接（幂等）。**不删数据**：Redis 的数据活得比本进程长。"""
        closer = getattr(self.kv, "close", None)
        if callable(closer):
            closer()

    def __enter__(self) -> "RedisStore":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # ── 内部：索引 → 行 ────────────────────────────────────
    def _entries(self) -> list[dict]:
        """索引成员 → 行；`payload` 也没了的成员视为"过期残留"，顺手自愈。"""
        rows: list[dict] = []
        stale: list[str] = []
        for ctx_id in self._index_members():
            row = self._read_meta(ctx_id)
            if row is None:
                if self.kv.get(self._k_payload(ctx_id)) is None:
                    stale.append(ctx_id)
                    continue
                row = self._empty_row(ctx_id)
            row["ctx_id"] = ctx_id
            rows.append(row)
        if stale:
            self._index_remove(*stale)
        return rows

    @staticmethod
    def _row_dict(row: dict) -> dict:
        return {
            "ctx_id": row["ctx_id"],
            "saved_at": float(row["saved_at"]),
            "phase": row["phase"],
            "task_id": row["task_id"],
            "tenant": row["tenant"],
            "schema_version": int(row["schema_version"]),
        }

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return (f"<RedisStore prefix={self.prefix!r} ttl_s={self.ttl_s} "
                f"kv={type(self.kv).__name__} saves={self.saves} loads={self.loads}>")
