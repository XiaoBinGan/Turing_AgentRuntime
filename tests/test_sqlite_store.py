"""tests/test_sqlite_store.py — W25：sqlite 版 `ContextStore`（stdlib unittest）。

覆盖任务书 ① 的 24 条验收 + 与 `FileStore` 的**同组断言参数化**（真·行为对齐）:

    1  save/load 闭环                     13 并发写不同 ctx_id：20×10 = 200
    2  load 不存在 → None                 14 并发写同一 ctx_id：20×20，最终一条且可 parse
    3  delete 存在/不存在                 15 并发读写混合：无 database is locked
    4  同 ctx_id 二次 save 覆盖           16 与 FileStore 语义一致（mixin 参数化）
    5  list_pending 按 saved_at 升序      17 1MB payload
    6  坏 payload → 列 NULL 但 save 成功  18 schema_version 不认识 → 抛错
    7  list_by_tenant 只返回该租户        19 WAL 已启用
    8  list_recent 限制条数               20 断电模拟（不 close 直接新开 / 子进程 os._exit）
    9  stats 计数与租户分布               21 :memory: 可用
    10 migrate 幂等（跑两次一致）         22 文件模式（tmp 目录）可用
    11 事务性：中途抛异常 → 表里无该行    23 stats 空库不崩
    12 ctx_id 非法 → ValueError           24 跨实例：A 写 B 读

额外（本文件自己加的，因为它们是上面几条的**证伪面**）：
- migrate 对「有表但缺列」的老库补列（ALTER 路径，否则幂等只是空谈）
- 事务回滚**保留旧行**、失败后连接仍可用（不留悬空事务）
- `ctx_id` 同 saved_at 时按 ctx_id 兜底排序（确定性）
- 真实 `ContextHandle` 的 save_checkpoint / load_checkpoint / list_resumable 闭环
- `busy_timeout` / `limit` / 计数器 / close 语义

跑法：
    cd /Users/supre/Documents/tmp/agent-runtime
    python3 -m unittest tests.test_sqlite_store -v
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from contracts import Budget, Owner, Patch, Phase  # noqa: E402
from core.context import ContextHandle  # noqa: E402
from core.sqlite_store import (  # noqa: E402
    NONE_TENANT,
    SQLITE_SCHEMA_VERSION,
    SQLiteStore,
    extract_meta,
)
from core.store import (  # noqa: E402
    STORE_SCHEMA_VERSION,
    FileStore,
    MemoryStore,
    StoreCorrupt,
    StoreError,
    StoreSchemaMismatch,
    list_resumable,
    load_checkpoint,
    save_checkpoint,
)

# ══════════════════════════════════════════════════════════════
# 夹具
# ══════════════════════════════════════════════════════════════
def payload(phase: str = "WAITING_USER", task_id: str = "task_1",
            tenant: str = "acme", *, version: int = 1, note: str = "x") -> str:
    """一份**形状与 `ContextHandle.serialize()` 一致**的 payload。

    刻意保持真实形状（`data.phase` / `data.task_id` / `data.principal.tenant`），
    否则索引列的实现会被"随便编一个 JSON"测成过拟合。
    """
    return json.dumps({
        "schema_version": 1,
        "version": version,
        "parent_version": 0,
        "data": {
            "goal": "把上季度营收按区域汇总",
            "phase": phase,
            "task_id": task_id,
            "principal": {"user_id": "u_1", "roles": ["engineer"], "tenant": tenant},
            "note": note,
        },
        "budget": {},
        "patch_log": [],
    }, ensure_ascii=False, sort_keys=True)


def run_parallel(workers, *, timeout: float = 10.0) -> list[str]:
    """跑一组 worker，返回它们抛出的异常（空列表 = 全部干净结束）。

    统一的 `join(timeout=...)`：超时即失败（死锁探测的第一道闸）。
    任何 worker 里的异常都被收集成字符串，而不是让线程静默死掉 ——
    "线程崩了但主线程以为成功"是并发测试最爱骗人的那种绿。
    """
    errors: list[str] = []
    lock = threading.Lock()

    def wrap(fn):
        def inner():
            try:
                fn()
            except BaseException as exc:      # noqa: BLE001 —— 收集一切，包括 assert
                with lock:
                    errors.append(f"{type(exc).__name__}: {exc}")
        return inner

    threads = [threading.Thread(target=wrap(w), daemon=True) for w in workers]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout)
    alive = [t.name for t in threads if t.is_alive()]
    if alive:
        raise AssertionError(f"线程在 {timeout}s 内没结束（疑似死锁）：{alive}")
    return errors


class TempStoreCase(unittest.TestCase):
    """每个用例一个临时目录（文件模式的库与 FileStore 的根都在里面）。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def db_path(self, name: str = "ctx.db") -> str:
        return str(self.tmp / name)

    def file_store(self, name: str = "files") -> FileStore:
        return FileStore(self.tmp / name)


# ══════════════════════════════════════════════════════════════
# ① 索引列提取（纯函数，先把最容易出错的一层钉死）
# ══════════════════════════════════════════════════════════════
class TestExtractMeta(unittest.TestCase):
    def test_full_payload(self) -> None:
        self.assertEqual(extract_meta(payload()), ("WAITING_USER", "task_1", "acme"))

    def test_flat_tenant_at_top_level(self) -> None:
        raw = json.dumps({"phase": "DONE", "task_id": "t", "tenant": "globex"})
        self.assertEqual(extract_meta(raw), ("DONE", "t", "globex"))

    def test_principal_tenant_nested(self) -> None:
        raw = json.dumps({"data": {"principal": {"tenant": "initech"}}})
        self.assertEqual(extract_meta(raw)[2], "initech")

    def test_invalid_json_is_null_not_error(self) -> None:
        self.assertEqual(extract_meta("{not json"), (None, None, None))
        self.assertEqual(extract_meta(""), (None, None, None))
        self.assertEqual(extract_meta("\x00\x01"), (None, None, None))

    def test_non_object_json_is_null(self) -> None:
        self.assertEqual(extract_meta("[1,2,3]"), (None, None, None))
        self.assertEqual(extract_meta('"just a string"'), (None, None, None))
        self.assertEqual(extract_meta("42"), (None, None, None))

    def test_wrong_types_are_null(self) -> None:
        raw = json.dumps({"phase": 123, "task_id": ["x"], "tenant": {"a": 1}})
        self.assertEqual(extract_meta(raw), (None, None, None))

    def test_blank_strings_are_null(self) -> None:
        raw = json.dumps({"phase": "   ", "task_id": "", "tenant": ""})
        self.assertEqual(extract_meta(raw), (None, None, None))

    def test_non_str_payload(self) -> None:
        self.assertEqual(extract_meta(None), (None, None, None))       # type: ignore[arg-type]
        self.assertEqual(extract_meta(b"{}"), (None, None, None))      # type: ignore[arg-type]


# ══════════════════════════════════════════════════════════════
# ② 协议参数化：同一组断言跑 FileStore 与 SQLiteStore
# ══════════════════════════════════════════════════════════════
class _ProtocolCases:
    """`ContextStore` 的语义断言集。子类只负责"怎么造一个 store"和"怎么注入坏行"。

    这不是"两个测试文件写着差不多的断言"，而是**同一份断言对象**跑两遍：
    将来 FileStore 改了语义，这里会立刻红 —— 对齐是被执行的，不是被声明的。
    """

    impl_name = "?"

    def make_store(self):                       # pragma: no cover - 由子类实现
        raise NotImplementedError

    def inject_raw(self, store, ctx_id: str, body: str, schema_version: int) -> None:
        raise NotImplementedError               # pragma: no cover

    # ── 1 / 2 / 4 ──────────────────────────────────────────
    def test_roundtrip(self) -> None:
        store = self.make_store()
        body = payload(note="闭环")
        store.save("ctx_a", body)
        self.assertEqual(store.load("ctx_a"), body)

    def test_load_missing_returns_none(self) -> None:
        self.assertIsNone(self.make_store().load("ctx_never_saved"))

    def test_second_save_overwrites(self) -> None:
        store = self.make_store()
        store.save("ctx_a", payload(note="第一版"))
        store.save("ctx_a", payload(note="第二版"))
        self.assertEqual(store.load("ctx_a"), payload(note="第二版"))
        self.assertEqual(store.list_pending(), ["ctx_a"])

    # ── 3 ──────────────────────────────────────────────────
    def test_delete_existing_returns_true(self) -> None:
        store = self.make_store()
        store.save("ctx_a", payload())
        self.assertTrue(store.delete("ctx_a"))
        self.assertIsNone(store.load("ctx_a"))

    def test_delete_missing_returns_false(self) -> None:
        self.assertFalse(self.make_store().delete("ctx_never_saved"))

    # ── 5 ──────────────────────────────────────────────────
    def test_list_pending_ascending_by_saved_at(self) -> None:
        store = self.make_store()
        order = ["ctx_e", "ctx_a", "ctx_d", "ctx_b", "ctx_c"]
        for name in order:
            store.save(name, payload())
            time.sleep(0.002)                   # 让 saved_at 严格递增（毫秒级时钟）
        self.assertEqual(store.list_pending(), order)

    def test_list_pending_after_delete(self) -> None:
        store = self.make_store()
        store.save("ctx_a", payload())
        store.save("ctx_b", payload())
        store.delete("ctx_a")
        self.assertEqual(store.list_pending(), ["ctx_b"])

    # ── 12 ─────────────────────────────────────────────────
    def test_invalid_ctx_id_raises_value_error(self) -> None:
        store = self.make_store()
        bad = ["", "..", "../evil", "a/b", "a\\b", ".hidden", "a\x00b", "*", "x" * 200]
        for ctx_id in bad:
            with self.subTest(ctx_id=ctx_id):
                with self.assertRaises(ValueError):
                    store.save(ctx_id, payload())
                with self.assertRaises(ValueError):
                    store.load(ctx_id)
                with self.assertRaises(ValueError):
                    store.delete(ctx_id)

    # ── 18 ─────────────────────────────────────────────────
    def test_unknown_schema_version_raises(self) -> None:
        store = self.make_store()
        self.inject_raw(store, "ctx_future", payload(), STORE_SCHEMA_VERSION + 41)
        with self.assertRaises(StoreSchemaMismatch):
            store.load("ctx_future")

    def test_non_int_schema_version_raises(self) -> None:
        store = self.make_store()
        # 注意：注入 "1" 不行 —— INTEGER 亲和性会把它**存成整数 1**。
        # 要真的造出"不是整数"的行，得用亲和性转不动的字符串。
        self.inject_raw(store, "ctx_weird", payload(), "not-a-version")
        with self.assertRaises(StoreSchemaMismatch):
            store.load("ctx_weird")

    def test_non_str_payload_rejected_on_save(self) -> None:
        store = self.make_store()
        for bad in (None, 123, b"bytes", {"a": 1}):
            with self.subTest(bad=type(bad).__name__):
                with self.assertRaises(StoreError):
                    store.save("ctx_a", bad)        # type: ignore[arg-type]


class TestFileStoreProtocol(TempStoreCase, _ProtocolCases):
    impl_name = "FileStore"

    def make_store(self):
        return self.file_store()

    def inject_raw(self, store, ctx_id: str, body: str, schema_version) -> None:
        """FileStore 的信封是文件内容，直接写一份（不经 save）就能造出坏版本。"""
        env = {"ctx_id": ctx_id, "saved_at": 1.0,
               "schema_version": schema_version, "payload": body}
        (store.root / f"{ctx_id}.json").write_text(
            json.dumps(env), encoding="utf-8")


class TestSQLiteStoreProtocol(TempStoreCase, _ProtocolCases):
    impl_name = "SQLiteStore"

    def make_store(self):
        return SQLiteStore(self.db_path())

    def inject_raw(self, store, ctx_id: str, body: str, schema_version) -> None:
        with store._use() as conn, conn:
            conn.execute(
                "INSERT OR REPLACE INTO contexts "
                "(ctx_id, payload, schema_version, saved_at) VALUES (?,?,?,?)",
                (ctx_id, body, schema_version, 1.0),
            )


class TestMemoryStoreSharesBasicSemantics(TempStoreCase):
    """`MemoryStore` 只对齐「4 个方法 + 覆盖语义」。

    它**刻意**不做信封校验（`core/store.py` 的边界里写明：不跨进程就没有"半写"
    可防、没有版本可猜），所以版本不认识的用例只覆盖 FileStore / SQLiteStore。
    把这条差别写成用例，比写在注释里可靠。
    """

    def test_basic_semantics_match(self) -> None:
        store = MemoryStore()
        store.save("ctx_a", payload(note="1"))
        self.assertEqual(store.load("ctx_a"), payload(note="1"))
        store.save("ctx_a", payload(note="2"))
        self.assertEqual(store.load("ctx_a"), payload(note="2"))
        self.assertEqual(store.list_pending(), ["ctx_a"])
        self.assertTrue(store.delete("ctx_a"))
        self.assertFalse(store.delete("ctx_a"))
        self.assertIsNone(store.load("ctx_a"))
        with self.assertRaises(ValueError):
            store.save("../evil", payload())

    def test_memory_store_has_no_schema_guard(self) -> None:
        store = MemoryStore()
        store.save("ctx_a", payload())
        store._entries["ctx_a"]["schema_version"] = 999
        self.assertEqual(store.load("ctx_a"), payload())   # 不校验 = 不猜 = 也不拦


# ══════════════════════════════════════════════════════════════
# ③ 索引列与扩展查询（FileStore 做不到的那些）
# ══════════════════════════════════════════════════════════════
class TestIndexColumns(TempStoreCase):
    def test_columns_are_populated(self) -> None:
        store = SQLiteStore(self.db_path())
        store.save("ctx_a", payload(task_id="task_9", tenant="globex"))
        row = store.list_recent(limit=1)[0]
        self.assertEqual(row["phase"], "WAITING_USER")
        self.assertEqual(row["task_id"], "task_9")
        self.assertEqual(row["tenant"], "globex")
        self.assertEqual(row["schema_version"], STORE_SCHEMA_VERSION)

    def test_bad_payload_saves_with_null_columns(self) -> None:
        """6) 坏 payload：save 成功、三列 NULL、**不抛错**。"""
        store = SQLiteStore(self.db_path())
        store.save("ctx_bad", "这不是 JSON，只是垃圾")
        self.assertEqual(store.load("ctx_bad"), "这不是 JSON，只是垃圾")
        row = store.list_recent(limit=1)[0]
        self.assertEqual(row["ctx_id"], "ctx_bad")
        self.assertIsNone(row["phase"])
        self.assertIsNone(row["task_id"])
        self.assertIsNone(row["tenant"])
        self.assertEqual(store.stats()["by_tenant"], {NONE_TENANT: 1})

    def test_bad_payload_does_not_break_queries(self) -> None:
        store = SQLiteStore(self.db_path())
        store.save("ctx_bad", "{")
        store.save("ctx_ok", payload(tenant="acme"))
        self.assertEqual(sorted(store.list_pending()), ["ctx_bad", "ctx_ok"])
        self.assertEqual(len(store.list_recent(limit=10)), 2)
        self.assertEqual([r["ctx_id"] for r in store.list_by_tenant("acme")], ["ctx_ok"])

    def test_list_by_tenant_filters(self) -> None:
        """7) 只返回该租户。"""
        store = SQLiteStore(self.db_path())
        for i in range(3):
            store.save(f"acme_{i}", payload(tenant="acme"))
            time.sleep(0.002)
        for i in range(2):
            store.save(f"globex_{i}", payload(tenant="globex"))
            time.sleep(0.002)
        store.save("anon", "not json")

        rows = store.list_by_tenant("acme")
        self.assertEqual([r["ctx_id"] for r in rows], ["acme_2", "acme_1", "acme_0"])
        self.assertTrue(all(r["tenant"] == "acme" for r in rows))
        self.assertEqual(store.list_by_tenant("globex")[0]["ctx_id"], "globex_1")
        self.assertEqual(store.list_by_tenant("nobody"), [])
        # NULL 不是空串：`tenant=""` 不该命中「抠不出租户」的那批
        self.assertEqual(store.list_by_tenant(""), [])
        self.assertEqual(store.list_by_tenant("acme", limit=1)[0]["ctx_id"], "acme_2")

    def test_list_by_tenant_rejects_non_str(self) -> None:
        store = SQLiteStore(self.db_path())
        for bad in (None, 1, b"acme"):
            with self.subTest(bad=bad):
                with self.assertRaises(TypeError):
                    store.list_by_tenant(bad)      # type: ignore[arg-type]

    def test_list_recent_limit(self) -> None:
        """8) 限制条数 + 最新在前。"""
        store = SQLiteStore(self.db_path())
        for i in range(10):
            store.save(f"ctx_{i:02d}", payload(note=str(i)))
            time.sleep(0.002)
        rows = store.list_recent(limit=3)
        self.assertEqual([r["ctx_id"] for r in rows], ["ctx_09", "ctx_08", "ctx_07"])
        self.assertEqual(store.list_recent(limit=0), [])
        self.assertEqual(len(store.list_recent(limit=100)), 10)

    def test_list_limit_validation(self) -> None:
        store = SQLiteStore(self.db_path())
        for bad in (-1,):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    store.list_recent(limit=bad)
        for bad in ("3", None, 3.5, True):
            with self.subTest(bad=bad):
                with self.assertRaises(TypeError):
                    store.list_recent(limit=bad)   # type: ignore[arg-type]

    def test_list_pending_tie_breaks_by_ctx_id(self) -> None:
        store = SQLiteStore(self.db_path())
        with store._use() as conn, conn:
            for name in ("ctx_c", "ctx_a", "ctx_b"):
                conn.execute(
                    "INSERT INTO contexts (ctx_id, payload, schema_version, saved_at) "
                    "VALUES (?,?,?,?)", (name, payload(), STORE_SCHEMA_VERSION, 42.0))
        self.assertEqual(store.list_pending(), ["ctx_a", "ctx_b", "ctx_c"])

    def test_stats_counts_and_tenants(self) -> None:
        """9) 计数与租户分布。"""
        store = SQLiteStore(self.db_path())
        store.save("a1", payload(tenant="acme"))
        time.sleep(0.002)
        store.save("a2", payload(tenant="acme"))
        time.sleep(0.002)
        store.save("g1", payload(tenant="globex"))
        time.sleep(0.002)
        store.save("anon", "garbage")

        stats = store.stats()
        self.assertEqual(stats["total"], 4)
        self.assertEqual(stats["by_tenant"], {"acme": 2, "globex": 1, NONE_TENANT: 1})
        self.assertIsNotNone(stats["oldest"])
        self.assertIsNotNone(stats["newest"])
        self.assertLess(stats["oldest"], stats["newest"])

    def test_stats_empty_db(self) -> None:
        """23) 空库不崩。"""
        store = SQLiteStore(self.db_path())
        self.assertEqual(store.stats(),
                         {"total": 0, "by_tenant": {}, "oldest": None, "newest": None})
        self.assertEqual(store.list_pending(), [])
        self.assertEqual(store.list_recent(), [])
        self.assertEqual(len(store), 0)
        self.assertEqual(store.journal_mode(), "wal")


# ══════════════════════════════════════════════════════════════
# ④ 迁移（幂等 + 老库补列）
# ══════════════════════════════════════════════════════════════
class TestMigrate(TempStoreCase):
    def test_fresh_db_applies_all_then_idempotent(self) -> None:
        """10) migrate 幂等：第二次 applied 为空、version 不变。"""
        store = SQLiteStore(self.db_path())
        first = store.last_migration
        self.assertEqual(first["applied"],
                         ["create_contexts", "index_saved_at", "index_tenant_saved_at"])
        self.assertEqual(first["version"], SQLITE_SCHEMA_VERSION)

        second = store.migrate()
        self.assertEqual(second["applied"], [])
        self.assertEqual(second["version"], first["version"])

        third = store.migrate()
        self.assertEqual(third["applied"], [])
        self.assertEqual(third["version"], first["version"])
        self.assertEqual(second, third)

    def test_indexes_exist(self) -> None:
        store = SQLiteStore(self.db_path())
        with store._use() as conn:
            names = {row[1] for row in conn.execute("PRAGMA index_list(contexts)")}
        self.assertIn("idx_contexts_saved_at", names)
        self.assertIn("idx_contexts_tenant_saved_at", names)

    def test_legacy_table_gets_missing_columns(self) -> None:
        """老库（只有 ctx_id/payload）→ migrate 用 ALTER 补齐，且随后能正常读写。"""
        path = self.db_path("legacy.db")
        raw = sqlite3.connect(path)
        with raw:
            raw.execute("CREATE TABLE contexts (ctx_id TEXT PRIMARY KEY, payload TEXT)")
        raw.close()

        store = SQLiteStore(path)
        applied = store.last_migration["applied"]
        self.assertIn("add_column:schema_version", applied)
        self.assertIn("add_column:saved_at", applied)
        self.assertIn("add_column:phase", applied)
        self.assertIn("add_column:task_id", applied)
        self.assertIn("add_column:tenant", applied)
        self.assertEqual(store.last_migration["version"], SQLITE_SCHEMA_VERSION)
        self.assertEqual(store.migrate()["applied"], [])

        store.save("ctx_a", payload(tenant="acme"))
        self.assertEqual(store.load("ctx_a"), payload(tenant="acme"))
        self.assertEqual(store.list_by_tenant("acme")[0]["task_id"], "task_1")

    def test_twice_open_same_file_is_stable(self) -> None:
        path = self.db_path()
        a = SQLiteStore(path)
        b = SQLiteStore(path)
        self.assertEqual(b.last_migration["applied"], [])
        self.assertEqual(a.last_migration["version"], b.last_migration["version"])


# ══════════════════════════════════════════════════════════════
# ⑤ 事务性（能被证伪的那种）
# ══════════════════════════════════════════════════════════════
class TestTransaction(TempStoreCase):
    def test_midway_failure_leaves_no_row(self) -> None:
        """11) save 中途抛异常 → 表里没有该 ctx_id（有就是半个 payload）。"""
        store = SQLiteStore(self.db_path())

        def boom(conn, ctx_id):
            raise RuntimeError("模拟事务中途失败")

        store._after_upsert = boom
        with self.assertRaises(RuntimeError):
            store.save("ctx_tx", payload())
        self.assertIsNone(store.load("ctx_tx"))
        self.assertEqual(store.list_pending(), [])
        self.assertEqual(store.stats()["total"], 0)
        self.assertEqual(store.saves, 0)          # 失败不进成功计数

    def test_midway_failure_keeps_previous_row(self) -> None:
        """回滚不是"清空"：失败的那次写不该把上一版抹掉。"""
        store = SQLiteStore(self.db_path())
        old = payload(note="旧的完整版本")
        store.save("ctx_tx", old)

        def boom(conn, ctx_id):
            raise RuntimeError("回滚")

        store._after_upsert = boom
        with self.assertRaises(RuntimeError):
            store.save("ctx_tx", payload(note="新的半成品"))
        self.assertEqual(store.load("ctx_tx"), old)

    def test_connection_usable_after_rollback(self) -> None:
        """回滚后不能留下悬空事务（否则下一次 save 会撞 "cannot start a transaction"）。"""
        store = SQLiteStore(self.db_path())

        def boom(conn, ctx_id):
            raise RuntimeError("x")

        store._after_upsert = boom
        with self.assertRaises(RuntimeError):
            store.save("ctx_bad", payload())
        del store._after_upsert                   # 恢复正常路径
        store.save("ctx_ok", payload(note="after rollback"))
        self.assertEqual(store.load("ctx_ok"), payload(note="after rollback"))
        self.assertEqual(store.saves, 1)

    def test_upsert_is_single_statement_atomic(self) -> None:
        """upsert 语义：同一 ctx_id 只有一行（不是 append）。"""
        store = SQLiteStore(self.db_path())
        for i in range(5):
            store.save("ctx_a", payload(version=i))
        with store._use() as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM contexts WHERE ctx_id = ?", ("ctx_a",)
            ).fetchone()[0]
        self.assertEqual(count, 1)
        self.assertEqual(json.loads(store.load("ctx_a"))["version"], 4)


# ══════════════════════════════════════════════════════════════
# ⑥ 并发（sqlite 这层真正的卖点）
# ══════════════════════════════════════════════════════════════
class TestConcurrency(TempStoreCase):
    def test_concurrent_distinct_ctx_ids(self) -> None:
        """13) 20 线程 × 各 10 个不同 ctx_id → 200 条，全部成功。"""
        store = SQLiteStore(self.db_path())
        errors: list[str] = []
        errors_lock = threading.Lock()

        def worker(tid: int):
            def inner():
                for i in range(10):
                    ctx_id = f"ctx_t{tid:02d}_{i:02d}"
                    store.save(ctx_id, payload(note=f"t{tid}-{i}", version=i))
            return inner

        def wrapped(tid):
            def inner():
                try:
                    worker(tid)()
                except BaseException as exc:   # noqa: BLE001
                    with errors_lock:
                        errors.append(f"t{tid}: {type(exc).__name__}: {exc}")
            return inner

        run_parallel([wrapped(t) for t in range(20)])
        self.assertEqual(errors, [])
        self.assertEqual(store.stats()["total"], 200)
        self.assertEqual(len(store.list_pending()), 200)
        # 抽查每个线程最后一笔：内容必须是自己写的（不能被别人覆盖）
        for tid in range(20):
            loaded = store.load(f"ctx_t{tid:02d}_09")
            self.assertIsNotNone(loaded)
            self.assertEqual(json.loads(loaded)["data"]["note"], f"t{tid}-9")

    def test_concurrent_same_ctx_id(self) -> None:
        """14) 20 线程 × 各 20 次写同一个 ctx_id → 不崩、只剩一条、payload 可 parse。"""
        store = SQLiteStore(self.db_path())
        errors: list[str] = []

        def make(tid: int):
            def inner():
                for i in range(20):
                    store.save("ctx_race", payload(note=f"t{tid}-{i}", version=i))
            return inner

        run_parallel([make(t) for t in range(20)])
        self.assertEqual(errors, [])
        self.assertEqual(store.stats()["total"], 1)
        loaded = store.load("ctx_race")
        self.assertIsInstance(loaded, str)
        doc = json.loads(loaded)                       # 必须能 parse（不是半个 payload）
        notes = {f"t{t}-{i}" for t in range(20) for i in range(20)}
        self.assertIn(doc["data"]["note"], notes)
        self.assertEqual(store.list_pending(), ["ctx_race"])

    def test_concurrent_read_write_mix(self) -> None:
        """15) 10 写 + 10 读并发，不允许出现 database is locked。"""
        store = SQLiteStore(self.db_path())
        store.save("shared", payload(note="seed"))
        bad: list[str] = []
        bad_lock = threading.Lock()

        def writer(tid: int):
            def inner():
                try:
                    for i in range(25):
                        store.save(f"w{tid}_{i:02d}", payload(note=f"w{tid}-{i}"))
                except BaseException as exc:   # noqa: BLE001
                    with bad_lock:
                        bad.append(f"writer{tid}: {type(exc).__name__}: {exc}")
            return inner

        def reader(tid: int):
            def inner():
                try:
                    for _ in range(25):
                        # 先读一个必然存在的，再读一个可能不存在的
                        self.assertIsInstance(store.load("shared"), str)
                        value = store.load(f"w00_{tid:02d}")
                        if value is not None:
                            self.assertEqual(json.loads(value)["data"]["note"],
                                             f"w0-{tid}")
                        store.list_pending()
                        store.list_recent(limit=5)
                        store.stats()
                except BaseException as exc:   # noqa: BLE001
                    with bad_lock:
                        bad.append(f"reader{tid}: {type(exc).__name__}: {exc}")
            return inner

        workers = [writer(t) for t in range(10)] + [reader(t) for t in range(10)]
        run_parallel(workers)
        self.assertEqual(bad, [])
        self.assertFalse([m for m in bad if "locked" in m.lower()])
        self.assertEqual(store.stats()["total"], 1 + 10 * 25)

    def test_concurrent_writes_in_memory_mode(self) -> None:
        """内存库靠单连接 + 锁：20 线程各写 10 次，不许串味、不许炸。"""
        store = SQLiteStore()
        errors: list[str] = []

        def make(tid: int):
            def inner():
                for i in range(10):
                    store.save(f"m_{tid}_{i}", payload(note=f"m{tid}-{i}"))
            return inner

        run_parallel([make(t) for t in range(20)])
        self.assertEqual(errors, [])
        self.assertEqual(store.stats()["total"], 200)
        self.assertEqual(json.loads(store.load("m_7_3"))["data"]["note"], "m7-3")


# ══════════════════════════════════════════════════════════════
# ⑦ WAL / 持久化 / 模式
# ══════════════════════════════════════════════════════════════
class TestDurability(TempStoreCase):
    def test_wal_enabled(self) -> None:
        """19) WAL 已启用（`PRAGMA journal_mode` 返回 wal）。"""
        store = SQLiteStore(self.db_path())
        self.assertEqual(store.journal_mode(), "wal")
        with store._use() as conn:
            self.assertEqual(str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower(),
                             "wal")

    def test_busy_timeout_applied(self) -> None:
        store = SQLiteStore(self.db_path(), timeout_s=2.5)
        with store._use() as conn:
            self.assertGreaterEqual(int(conn.execute("PRAGMA busy_timeout").fetchone()[0]),
                                    2000)

    def test_powerloss_without_close(self) -> None:
        """20) 不 close 直接开新实例 → 数据仍在（提交即持久）。"""
        path = self.db_path()
        a = SQLiteStore(path)
        a.save("ctx_durable", payload(note="提交后就该看得见"))
        b = SQLiteStore(path)                    # a 还开着，也没 close
        self.assertEqual(b.load("ctx_durable"), payload(note="提交后就该看得见"))
        self.assertEqual(b.list_pending(), ["ctx_durable"])

    def test_powerloss_subprocess_os_exit(self) -> None:
        """更狠的断电：子进程 save 完直接 os._exit(0)，一行清理代码都不跑。"""
        path = self.db_path("crash.db")
        body = payload(note="被杀之前已经提交")
        script = (
            "import os, sys\n"
            f"sys.path.insert(0, {ROOT!r})\n"
            "from core.sqlite_store import SQLiteStore\n"
            f"s = SQLiteStore({path!r})\n"
            f"s.save('ctx_killed', {body!r})\n"
            "os._exit(0)\n"
        )
        subprocess.run([sys.executable, "-c", script], check=True, timeout=60)
        reopened = SQLiteStore(path)
        self.assertEqual(reopened.load("ctx_killed"), body)

    def test_memory_mode(self) -> None:
        """21) `:memory:` 可用（且它是**连接私有**的 —— 第二个实例看不到）。"""
        store = SQLiteStore()
        store.save("ctx_a", payload())
        self.assertEqual(store.load("ctx_a"), payload())
        self.assertEqual(store.journal_mode(), "memory")
        self.assertEqual(SQLiteStore().load("ctx_a"), None)

    def test_file_mode(self) -> None:
        """22) 文件模式：库文件真的落在磁盘上，delete 之后条目消失。"""
        path = self.db_path("real.db")
        store = SQLiteStore(path)
        store.save("ctx_a", payload())
        self.assertTrue(Path(path).exists())
        self.assertTrue(store.delete("ctx_a"))
        self.assertFalse(store.delete("ctx_a"))
        self.assertFalse(store.exists("ctx_a"))

    def test_cross_instance_visibility(self) -> None:
        """24) A 写 B 读（同一文件）。"""
        path = self.db_path("shared.db")
        a = SQLiteStore(path)
        b = SQLiteStore(path)
        a.save("ctx_from_a", payload(tenant="acme", task_id="task_a"))
        self.assertEqual(b.load("ctx_from_a"), payload(tenant="acme", task_id="task_a"))
        self.assertEqual(b.list_pending(), ["ctx_from_a"])
        self.assertEqual(b.list_by_tenant("acme")[0]["task_id"], "task_a")
        b.save("ctx_from_b", payload(tenant="acme", task_id="task_b"))
        self.assertEqual(a.list_by_tenant("acme")[0]["task_id"], "task_b")

    def test_large_payload_1mb(self) -> None:
        """17) 1MB payload 能存能取（sqlite 的 TEXT 不是问题，缓存才是）。"""
        store = SQLiteStore(self.db_path())
        big = "键" * (341 * 1024)                 # ~1MiB（UTF-8 下 3 字节/字）
        body = payload(note=big)
        self.assertGreater(len(body.encode("utf-8")), 1024 * 1024 - 4096)
        store.save("ctx_big", body)
        loaded = store.load("ctx_big")
        self.assertEqual(len(loaded), len(body))
        self.assertEqual(loaded, body)

    def test_close_then_reopen(self) -> None:
        store = SQLiteStore(self.db_path())
        store.save("ctx_a", payload())
        store.close()
        store.close()                             # 幂等
        self.assertEqual(store.load("ctx_a"), payload())   # 自动重开，不是 use-after-close
        with SQLiteStore(self.db_path()) as ctx_store:     # 上下文管理协议
            self.assertEqual(ctx_store.load("ctx_a"), payload())


# ══════════════════════════════════════════════════════════════
# ⑧ 与真实 Context 编排的闭环（core/store.py 的 save_checkpoint 家族）
# ══════════════════════════════════════════════════════════════
class TestRealContextRoundtrip(TempStoreCase):
    def _ctx(self) -> ContextHandle:
        ctx = ContextHandle(
            initial={
                "goal": "把上季度营收按区域汇总",
                "principal": {"user_id": "u_1", "roles": ["engineer"], "tenant": "acme"},
                "task_id": "task_real",
                "turn_id": "turn_1",
            },
            budget=Budget(),
        )
        ctx.apply(Patch("set", "phase", Owner.LOOP, Phase.WAITING_USER))
        ctx.apply(Patch("append", "history", Owner.LOOP,
                        {"role": "user", "text": "汇总一下"}))
        return ctx

    def test_checkpoint_roundtrip(self) -> None:
        store = SQLiteStore(self.db_path())
        ctx = self._ctx()
        save_checkpoint(ctx, store, "ctx_real", reason="挂起等审批")

        loaded = load_checkpoint(store, "ctx_real")
        self.assertEqual(loaded.version, ctx.version)
        self.assertEqual(loaded.serialize(), ctx.serialize())
        self.assertEqual(loaded.get("goal"), "把上季度营收按区域汇总")

    def test_index_columns_come_from_real_payload(self) -> None:
        store = SQLiteStore(self.db_path())
        save_checkpoint(self._ctx(), store, "ctx_real", reason="挂起等审批")
        rows = store.list_by_tenant("acme")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["ctx_id"], "ctx_real")
        self.assertEqual(rows[0]["phase"], Phase.WAITING_USER.value)
        self.assertEqual(rows[0]["task_id"], "task_real")

    def test_list_resumable_reads_columns(self) -> None:
        store = SQLiteStore(self.db_path())
        save_checkpoint(self._ctx(), store, "ctx_real", reason="挂起")
        items = list_resumable(store)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["ctx_id"], "ctx_real")
        self.assertEqual(items[0]["phase"], Phase.WAITING_USER.value)
        self.assertEqual(items[0]["task_id"], "task_real")
        self.assertNotIn("error", items[0])


class TestCountersAndErrors(TempStoreCase):
    def test_counters(self) -> None:
        store = SQLiteStore(self.db_path())
        store.save("a", payload())
        store.save("b", payload())
        store.load("a")
        store.load("missing")
        store.delete("a")
        self.assertEqual((store.saves, store.loads, store.deletes), (2, 2, 1))

    def test_bad_path_is_rejected(self) -> None:
        with self.assertRaises(StoreError):
            SQLiteStore(str(self.tmp))            # 目录当库路径

    def test_bad_timeout_is_rejected(self) -> None:
        for bad in (0, -1, "5"):
            with self.subTest(bad=bad):
                with self.assertRaises((ValueError, TypeError)):
                    SQLiteStore(self.db_path(), timeout_s=bad)   # type: ignore[arg-type]

    def test_corrupt_row_detected(self) -> None:
        """ctx_id 对不上 → StoreCorrupt（与 FileStore「文件名与信封不一致」同语义）。"""
        store = SQLiteStore(self.db_path())
        with store._use() as conn, conn:
            conn.execute(
                "INSERT OR REPLACE INTO contexts (ctx_id, payload, schema_version, saved_at) "
                "VALUES (?,?,?,?)", ("ctx_a", payload(), STORE_SCHEMA_VERSION, 1.0))
        # 正常路径读没问题
        self.assertEqual(store.load("ctx_a"), payload())
        # 把 payload 改成 BLOB（NOT NULL 允许 BLOB）→ 不是 str → StoreCorrupt，
        # 而不是把 bytes 当 payload 交给 ContextHandle.restore()
        with store._use() as conn, conn:
            conn.execute(
                "UPDATE contexts SET payload = CAST(x'00ff' AS BLOB) "
                "WHERE ctx_id = 'ctx_a'")
        with self.assertRaises(StoreCorrupt):
            store.load("ctx_a")


if __name__ == "__main__":       # pragma: no cover
    unittest.main(verbosity=2)
