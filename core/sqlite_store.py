"""core/sqlite_store.py — W25：`ContextStore` 的 sqlite3 实现（stdlib，零第三方依赖）。

===============================================================================
为什么要这一层：`FileStore` 的三个硬缺口（不是优化项，是能力缺口）
===============================================================================
1. **没有事务。** `FileStore` 靠「写临时文件 → rename」保证**单个文件**要么旧要么新，
   但跨条目没有任何原子性，也说不出「这次 save 到底提交了没有」。多写一步
   （比如同时落 payload 与索引）就没有回滚点。
2. **多进程并发是 last-writer-wins。** 同一个 `ctx_id` 两个进程同时 save，
   最后 rename 的那个赢：中间那次修改**静默消失**，且不留任何痕迹
   （W14 自己在 `core/store.py` 的边界里承认了这一点）。
3. **查询靠扫盘。** `list_pending()` 要 glob 目录 + 逐个 read + json.loads 才能
   拿到 `saved_at`；「给我最近 50 个待续跑」要读完整个目录，O(n) 次 IO。

sqlite 一条一条对上：

    事务        → 单条 DML 包在 `with conn:` 里，异常自动 ROLLBACK，不留半个 payload
    并发        → WAL（多读一写）+ busy_timeout（写锁排队而不是立刻 database is locked）
    索引查询    → (saved_at) 与 (tenant, saved_at) 两个索引 → 「最近 N 个」是一次
                  B-tree 尾部扫描，不再扫全表；`list_by_tenant` 是 FileStore 做不到的事

===============================================================================
并发模型（WAL 的正确用法，别想当然）
===============================================================================
* **文件库**：每线程一个连接（`threading.local`）。WAL 下读不挡写、写不挡读；
  写与写之间由 SQLite 的全库写锁串行化，`busy_timeout` 负责**排队等待**，
  而不是立刻抛 `database is locked`。
* **`:memory:` 库**：sqlite 的内存库是**连接私有**的 —— 开两个连接会各看各的空库。
  所以内存模式退化成「单连接 + 一把 RLock」（这不是偷懒，是 sqlite 的语义决定的，
  见 test_21 / test_24 的对照：跨实例共享只对**文件**成立）。
* **崩溃安全**：`synchronous=NORMAL` + WAL。已提交的数据在 `-wal` 里，重开连接即可见；
  断电最多丢最后一批**未提交**的事务，绝不丢已提交的。

===============================================================================
payload 与索引列的关系（本模块仍然不理解 payload 语义）
===============================================================================
`phase` / `task_id` / `tenant` 是**从 payload 里尽力抠出来的索引列**：
解析失败（非 JSON、不是 object、字段缺失、类型不对）→ 三列一律 NULL，
**绝不让 save 因此失败**。理由：Store 层只保证「存进去的字节能原样取回」，
把「payload 里有没有 phase」变成写入的准入条件，等于让一列索引拥有否决
整个持久化的权力 —— 那是本末倒置（坏 Context 该由 `restore()` 拒绝，不是由 Store 拒绝）。

===============================================================================
边界（诚实写在这里）
===============================================================================
* 跨进程的**公平性/写入顺序**不做承诺：SQLite 的写锁不保证 FIFO。
  要严格顺序请在业务层加序列号列，或换 Postgres。
* `WAL` 在网络文件系统（NFS / SMB / 部分 FUSE）上不成立：那类部署请把 path
  指到本地盘，否则会把「多个连接看到不一致的 -wal」变成一个玄学 bug。
* `ctx_id` 的校验与 `FileStore` **同一口径**（直接复用 `core.store.validate_ctx_id`）：
  sqlite 不涉路径，但接口必须一致 —— 否则「换个 store 就能塞 `../x`」。
"""
from __future__ import annotations

import contextlib
import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterator

from contracts import now_ms
from core.store import (
    STORE_SCHEMA_VERSION,
    StoreCorrupt,
    StoreError,
    StoreSchemaMismatch,
    validate_ctx_id,
)

__all__ = [
    "SQLiteStore",
    "SQLITE_SCHEMA_VERSION",
    "extract_meta",
    "NONE_TENANT",
]

#: 表结构版本（存在 `PRAGMA user_version` 里）。与 Store 信封版本是两件事：
#: 信封版本说「这一行的 payload 是按哪版契约写的」，表版本说「表长什么样」。
SQLITE_SCHEMA_VERSION = 3

#: `by_tenant` 里 NULL 租户的归并键（不跟真实租户名冲突：真实租户名走 validate 的口径，
#: 但 store 层不校验租户名，所以用一个显式的哨兵而不是空串，避免和 `tenant=""` 混淆）。
NONE_TENANT = "<none>"

# ── 迁移脚本：版本号单调，每条只跑一次（幂等由 IF NOT EXISTS + user_version 共同保证）──
#: (版本号, 迁移名, SQL 语句元组)
_MIGRATIONS: tuple[tuple[int, str, tuple[str, ...]], ...] = (
    (1, "create_contexts", (
        """
        CREATE TABLE IF NOT EXISTS contexts (
            ctx_id         TEXT PRIMARY KEY,
            payload        TEXT    NOT NULL,
            schema_version INTEGER NOT NULL,
            saved_at       REAL    NOT NULL,
            phase          TEXT,
            task_id        TEXT,
            tenant         TEXT
        )
        """,
    )),
    (2, "index_saved_at", (
        "CREATE INDEX IF NOT EXISTS idx_contexts_saved_at ON contexts(saved_at)",
    )),
    (3, "index_tenant_saved_at", (
        "CREATE INDEX IF NOT EXISTS idx_contexts_tenant_saved_at "
        "ON contexts(tenant, saved_at)",
    )),
)

#: 表应有的列（老库「有表但缺列」时由 `_ensure_columns` 用 ALTER TABLE 补齐）
_REQUIRED_COLUMNS: tuple[tuple[str, str], ...] = (
    ("payload", "TEXT NOT NULL DEFAULT ''"),
    ("schema_version", "INTEGER NOT NULL DEFAULT 1"),
    ("saved_at", "REAL NOT NULL DEFAULT 0"),
    ("phase", "TEXT"),
    ("task_id", "TEXT"),
    ("tenant", "TEXT"),
)

#: 「建表」这一条迁移的版本号。**索引必须排在补列之后**：老库可能没有 `saved_at`，
#: 先建索引会直接 `no such column: saved_at`，把一次可自动完成的升级变成启动失败。
_TABLE_MIGRATION_VERSION = 1

_SQL_UPSERT = """
INSERT INTO contexts (ctx_id, payload, schema_version, saved_at, phase, task_id, tenant)
VALUES (?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(ctx_id) DO UPDATE SET
    payload        = excluded.payload,
    schema_version = excluded.schema_version,
    saved_at       = excluded.saved_at,
    phase          = excluded.phase,
    task_id        = excluded.task_id,
    tenant         = excluded.tenant
"""

_SQL_SELECT_ONE = (
    "SELECT ctx_id, payload, schema_version, saved_at, phase, task_id, tenant "
    "FROM contexts WHERE ctx_id = ?"
)

_SQL_DELETE = "DELETE FROM contexts WHERE ctx_id = ?"

_SQL_LIST_PENDING = "SELECT ctx_id FROM contexts ORDER BY saved_at ASC, ctx_id ASC"

_SQL_LIST_RECENT = (
    "SELECT ctx_id, saved_at, phase, task_id, tenant, schema_version FROM contexts "
    "ORDER BY saved_at DESC, ctx_id ASC LIMIT ?"
)

_SQL_LIST_BY_TENANT = (
    "SELECT ctx_id, saved_at, phase, task_id, tenant, schema_version FROM contexts "
    "WHERE tenant = ? ORDER BY saved_at DESC, ctx_id ASC LIMIT ?"
)


# ══════════════════════════════════════════════════════════════
# payload → 索引列（尽力而为，绝不抛错）
# ══════════════════════════════════════════════════════════════
def _nonempty_str(value: Any) -> str | None:
    """只接受「有内容的 str」。其它（None / 数字 / 枚举对象 / 空串）→ None。"""
    if isinstance(value, str) and value.strip():
        return value
    return None


def _tenant_of(scope: dict) -> str | None:
    """租户在 payload 里可能出现在两处：`data.tenant` 或 `data.principal.tenant`。

    先看顶层 `tenant`（有人会直接摊平），再看 `principal.tenant`（Gateway 的实际形状）。
    认不出就是 None —— 不做「猜一个租户」这种会把审计归因弄脏的事。
    """
    direct = _nonempty_str(scope.get("tenant"))
    if direct is not None:
        return direct
    principal = scope.get("principal")
    if isinstance(principal, dict):
        return _nonempty_str(principal.get("tenant"))
    return None


def extract_meta(payload: str) -> tuple[str | None, str | None, str | None]:
    """从 payload（Context.serialize() 的产物）里尽力解析 `(phase, task_id, tenant)`。

    查找顺序：`data` 子对象优先，顶层兜底（两处都认，因为有人会把字段摊平）。
    任何解析问题（非 JSON / 顶层不是 object / 字段类型不对）一律返回 None 三元组，
    **不抛异常**：坏 payload 仍然要能存、能取、能被 `restore()` 去拒绝。
    """
    if not isinstance(payload, str):
        return None, None, None
    try:
        doc = json.loads(payload)
    except (json.JSONDecodeError, ValueError):
        return None, None, None
    if not isinstance(doc, dict):
        return None, None, None

    scopes: list[dict] = []
    data = doc.get("data")
    if isinstance(data, dict):
        scopes.append(data)
    scopes.append(doc)

    phase = task_id = tenant = None
    for scope in scopes:
        if phase is None:
            phase = _nonempty_str(scope.get("phase"))
        if task_id is None:
            task_id = _nonempty_str(scope.get("task_id"))
        if tenant is None:
            tenant = _tenant_of(scope)
    return phase, task_id, tenant


def _require_payload(payload: Any) -> str:
    """与 `core/store._require_payload` 同一口径（错误类型一致，便于替换两个实现）。"""
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


# ══════════════════════════════════════════════════════════════
# SQLiteStore
# ══════════════════════════════════════════════════════════════
class SQLiteStore:
    """`ContextStore` 协议的 sqlite 实现：事务、并发排队、索引查询。

    构造::

        store = SQLiteStore()                      # 内存库（单连接 + 锁）
        store = SQLiteStore("/tmp/ctx.db")         # 文件库（每线程一连接，WAL）
        store = SQLiteStore("/tmp/ctx.db", timeout_s=10.0)

    协议方法（`save` / `load` / `delete` / `list_pending`）与 `FileStore` 语义一致；
    其余是 FileStore 做不到的扩展查询（`list_by_tenant` / `list_recent` / `stats` / `migrate`）。
    """

    def __init__(self, path: str | Path = ":memory:", *, timeout_s: float = 5.0) -> None:
        if isinstance(timeout_s, bool) or not isinstance(timeout_s, (int, float)):
            raise TypeError(f"timeout_s 必须是数值，收到 {type(timeout_s).__name__}")
        if float(timeout_s) <= 0:
            raise ValueError(f"timeout_s 必须 > 0，收到 {timeout_s!r}")

        raw_path = str(path)
        self._is_memory = raw_path == ":memory:" or (
            raw_path.startswith("file:") and "mode=memory" in raw_path
        )
        self.timeout_s = float(timeout_s)
        #: 一次写等待多久才认输（毫秒）。给 sqlite 的忙等窗口，别让并发写靠抛错交流。
        self._busy_ms = int(self.timeout_s * 1000)

        if self._is_memory:
            self.path = raw_path
        else:
            p = Path(raw_path).expanduser()
            if p.exists() and p.is_dir():
                raise StoreError(f"sqlite 库路径已存在且是目录：{p}")
            try:
                p.parent.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise StoreError(f"无法创建 sqlite 库目录 {p.parent}：{exc}") from exc
            self.path = str(p)

        #: 保护「内存库的单连接」「计数器」「连接清单」「epoch」——不用它保护文件库的读写。
        self._lock = threading.RLock()
        #: 文件库的每线程连接
        self._local = threading.local()
        #: 内存库的唯一连接（连接私有语义决定的）
        self._shared: sqlite3.Connection | None = None
        #: 所有开出来的连接（close() 用）
        self._conns: list[sqlite3.Connection] = []
        #: close() 之后老的线程本地连接必须重开：靠 epoch 判断，而不是靠「用的时候炸」
        self._epoch = 0

        #: 观测计数（与 FileStore 同名同义）
        self.saves = 0
        self.loads = 0
        self.deletes = 0
        #: 最近一次 migrate() 的结果（排查「为什么用了索引还是慢」时先看这个）
        self.last_migration: dict = {}

        # 建表 + 索引：单线程做的，所以这里不可能撞锁
        self.last_migration = self.migrate()

    # ── 连接管理 ────────────────────────────────────────────
    def _open_conn(self) -> sqlite3.Connection:
        try:
            conn = sqlite3.connect(
                self.path,
                timeout=self.timeout_s,
                check_same_thread=False,   # 线程归属由我们自己管（见 _use）
            )
        except sqlite3.Error as exc:
            raise StoreError(f"打开 sqlite 库失败：{self.path}（{exc}）") from exc
        conn.row_factory = sqlite3.Row
        try:
            # busy_timeout 必须先设：后面那条 journal_mode 切换本身就可能撞上别人的写锁
            conn.execute(f"PRAGMA busy_timeout={self._busy_ms}")
            if not self._is_memory:
                self._enable_wal(conn)
                conn.execute("PRAGMA synchronous=NORMAL")
        except sqlite3.Error as exc:
            conn.close()
            raise StoreError(f"初始化 sqlite 连接失败：{self.path}（{exc}）") from exc
        return conn

    def _enable_wal(self, conn: sqlite3.Connection) -> None:
        """切 WAL（已是 WAL 就什么都不做）。

        WAL 是**库级持久设置**，写在库头里：第一次成功后，后续连接天然就是 WAL。
        但多个线程同时新开连接时，切换动作可能撞上别人的写锁 —— 退避重试三次，
        都失败才报错（那时确实该报错，而不是假装自己开了 WAL）。
        """
        last: Exception | None = None
        for attempt in range(3):
            try:
                mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
                if str(mode).lower() == "wal":
                    return
                conn.execute("PRAGMA journal_mode=WAL")
                return
            except sqlite3.OperationalError as exc:   # 只可能是 SQLITE_BUSY / locked
                last = exc
                time.sleep(0.02 * (attempt + 1))
        raise StoreError(
            f"无法把 {self.path} 切到 WAL 模式（{type(last).__name__}: {last}）；"
            f"WAL 必须落本地盘，网络文件系统上不要用"
        )

    def _shared_conn(self) -> sqlite3.Connection:
        """内存库的唯一连接（调用方必须已持有 `self._lock`）。"""
        if self._shared is None:
            self._shared = self._open_conn()
            self._conns.append(self._shared)
        return self._shared

    def _thread_conn(self) -> sqlite3.Connection:
        """文件库的每线程连接；close() 之后（epoch 变了）自动重开。"""
        cell = getattr(self._local, "cell", None)
        if cell is None or cell[0] != self._epoch:
            conn = self._open_conn()
            self._local.cell = (self._epoch, conn)
            with self._lock:
                self._conns.append(conn)
            return conn
        return cell[1]

    @contextlib.contextmanager
    def _use(self) -> Iterator[sqlite3.Connection]:
        """取一个可用连接。

        内存库：整段操作持 `self._lock`（单连接，不持锁就会两个线程抢同一个连接的事务；
        而且 sqlite3 的 legacy 事务状态是**连接级**的，串行化是唯一正确解）。
        文件库：直接用线程本地连接，**不持**实例锁 —— 并发靠 WAL + busy_timeout，
        不是靠把这层退化成串行。
        """
        if self._is_memory:
            with self._lock:
                yield self._shared_conn()
        else:
            yield self._thread_conn()

    # ── 迁移 ────────────────────────────────────────────────
    def migrate(self) -> dict:
        """幂等建表/加索引/补列；返回 `{"applied": [...], "version": n}`。

        `applied` 只列**这一次真的执行了**的迁移（第二次调用必然是空列表），
        `version` 是 `PRAGMA user_version`，两次调用必须相等 —— 这就是幂等的断言点。
        """
        applied: list[str] = []
        with self._use() as conn:
            try:
                with conn:
                    current = int(conn.execute("PRAGMA user_version").fetchone()[0])
                    had_table = conn.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' "
                        "AND name='contexts'"
                    ).fetchone() is not None
                    # 阶段 1：建表（表已存在就不谎报"我建了"）
                    for version, name, statements in _MIGRATIONS:
                        if version > _TABLE_MIGRATION_VERSION or version <= current:
                            continue
                        for sql in statements:
                            conn.execute(sql)
                        if not had_table:
                            applied.append(name)
                    # 阶段 2：补列（老库「有表但缺列」—— CREATE TABLE IF NOT EXISTS
                    # 不会补列，只有 ALTER 会；这一条正是幂等迁移的真正价值）
                    applied.extend(self._ensure_columns(conn))
                    # 阶段 3：建索引（必须在补列之后，见 _TABLE_MIGRATION_VERSION）
                    for version, name, statements in _MIGRATIONS:
                        if version <= _TABLE_MIGRATION_VERSION or version <= current:
                            continue
                        for sql in statements:
                            conn.execute(sql)
                        applied.append(name)
                    target = max(current, SQLITE_SCHEMA_VERSION)
                    # PRAGMA 不支持参数绑定，这里拼的是 int，不是外部输入
                    conn.execute(f"PRAGMA user_version={int(target)}")
            except sqlite3.Error as exc:
                raise StoreError(f"sqlite 迁移失败（{self.path}）：{exc}") from exc
            version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        return {"applied": applied, "version": version}

    @staticmethod
    def _ensure_columns(conn: sqlite3.Connection) -> list[str]:
        """补齐缺失的列（幂等：只 ALTER 真的缺的那些）。"""
        existing = {row[1] for row in conn.execute("PRAGMA table_info(contexts)")}
        added: list[str] = []
        for name, decl in _REQUIRED_COLUMNS:
            if name in existing:
                continue
            conn.execute(f"ALTER TABLE contexts ADD COLUMN {name} {decl}")
            added.append(f"add_column:{name}")
        return added

    def schema_version(self) -> int:
        """表结构版本（`PRAGMA user_version`）。"""
        with self._use() as conn:
            return int(conn.execute("PRAGMA user_version").fetchone()[0])

    def journal_mode(self) -> str:
        """当前 journal_mode（文件库应为 `wal`；内存库只能是 `memory`）。"""
        with self._use() as conn:
            return str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower()

    # ── 协议四件套 ─────────────────────────────────────────
    def save(self, ctx_id: str, payload: str) -> None:
        """写一个 Context（upsert）。**事务性**：异常自动回滚，不留半个 payload。"""
        validate_ctx_id(ctx_id)
        _require_payload(payload)
        phase, task_id, tenant = extract_meta(payload)
        row = (ctx_id, payload, int(STORE_SCHEMA_VERSION), float(now_ms()),
               phase, task_id, tenant)
        with self._use() as conn:
            with conn:                       # 事务：正常退出 COMMIT，异常 ROLLBACK
                conn.execute(_SQL_UPSERT, row)
                # 钩子：测试/子类在这里抛异常，用来证明「写了一半也能整体回滚」
                self._after_upsert(conn, ctx_id)
        with self._lock:
            self.saves += 1

    def _after_upsert(self, conn: sqlite3.Connection, ctx_id: str) -> None:
        """事务内的最后一步（默认什么都不做）。

        留这个钩子是因为「事务性」必须能被**证伪**：测试把它换成一个会抛的函数，
        然后断言表里查不到这个 ctx_id。没有钩子就只能靠注入坏数据去猜，
        而猜出来的坑位（比如 NOT NULL 冲突）和真实的「写一半崩了」不是一回事。
        """

    def load(self, ctx_id: str) -> str | None:
        """读一个 Context；不存在 → None；版本不认识/信封坏了 → StoreError 家族。"""
        validate_ctx_id(ctx_id)
        with self._use() as conn:
            row = conn.execute(_SQL_SELECT_ONE, (ctx_id,)).fetchone()
        with self._lock:
            self.loads += 1
        if row is None:
            return None
        return self._decode(row, ctx_id)

    @staticmethod
    def _decode(row: sqlite3.Row, ctx_id: str) -> str:
        """校验一行；与 FileStore 的 `_decode_envelope` 同语义（不猜版本）。"""
        where = f"contexts[{ctx_id}]"
        stored_id = row["ctx_id"]
        if not isinstance(stored_id, str) or stored_id != ctx_id:
            raise StoreCorrupt(
                f"{where} 里的 ctx_id={stored_id!r} 与查询用的 {ctx_id!r} 不一致")

        schema = row["schema_version"]
        if not isinstance(schema, int) or isinstance(schema, bool):
            raise StoreSchemaMismatch(f"{where} 的 schema_version 不是整数：{schema!r}")
        if schema != STORE_SCHEMA_VERSION:
            raise StoreSchemaMismatch(
                f"{where} 的 schema_version 不认识：期望 {STORE_SCHEMA_VERSION}，"
                f"收到 {schema}（不猜旧版本怎么读）"
            )

        payload = row["payload"]
        if not isinstance(payload, str):
            raise StoreCorrupt(
                f"{where} 的 payload 不是字符串（{type(payload).__name__}）")
        return payload

    def delete(self, ctx_id: str) -> bool:
        """删除；返回是否真的删掉了一行（幂等清理不抛错）。"""
        validate_ctx_id(ctx_id)
        with self._use() as conn:
            with conn:
                cursor = conn.execute(_SQL_DELETE, (ctx_id,))
                removed = cursor.rowcount > 0
        if removed:
            with self._lock:
                self.deletes += 1
        return removed

    def list_pending(self) -> list[str]:
        """按 `saved_at` 升序（同毫秒按 ctx_id 兜底，保证确定性）。"""
        with self._use() as conn:
            rows = conn.execute(_SQL_LIST_PENDING).fetchall()
        return [row[0] for row in rows]

    # ── 扩展查询（FileStore 做不到的那些）─────────────────────
    def list_recent(self, *, limit: int = 50) -> list[dict]:
        """最近写入的 N 条（**倒序**：最新的在前），**不读 payload**。

        每条：`{ctx_id, saved_at, phase, task_id, tenant, schema_version}`。
        这正是「列出最近 N 个待续跑」要的形状，且走 `saved_at` 索引，
        与库里总条数无关（FileStore 的等价操作是读整个目录）。
        """
        n = _limit_of(limit)
        with self._use() as conn:
            rows = conn.execute(_SQL_LIST_RECENT, (n,)).fetchall()
        return [self._row_dict(row) for row in rows]

    def list_by_tenant(self, tenant: str, *, limit: int = 100) -> list[dict]:
        """某个租户最近写入的 N 条（走 `(tenant, saved_at)` 复合索引）。

        `tenant=""` 与 NULL 是**两件事**：NULL（payload 里抠不出租户）不会被
        `list_by_tenant("")` 命中 —— 前者是"不知道归属"，后者是"归属叫空串"。
        想看 NULL 那批，用 `stats()["by_tenant"][NONE_TENANT]` 计数，或 `list_recent`。
        """
        if not isinstance(tenant, str):
            raise TypeError(f"tenant 必须是 str，收到 {type(tenant).__name__}")
        n = _limit_of(limit)
        with self._use() as conn:
            rows = conn.execute(_SQL_LIST_BY_TENANT, (tenant, n)).fetchall()
        return [self._row_dict(row) for row in rows]

    @staticmethod
    def _row_dict(row: sqlite3.Row) -> dict:
        return {
            "ctx_id": row["ctx_id"],
            "saved_at": float(row["saved_at"]),
            "phase": row["phase"],
            "task_id": row["task_id"],
            "tenant": row["tenant"],
            "schema_version": int(row["schema_version"]),
        }

    def stats(self) -> dict:
        """计数 + 租户分布 + 时间跨度。**空库不崩**（oldest/newest 为 None）。"""
        with self._use() as conn:
            total = int(conn.execute("SELECT COUNT(*) FROM contexts").fetchone()[0])
            grouped = conn.execute(
                "SELECT tenant, COUNT(*) FROM contexts GROUP BY tenant"
            ).fetchall()
            bounds = conn.execute(
                "SELECT MIN(saved_at), MAX(saved_at) FROM contexts"
            ).fetchone()
        by_tenant: dict[str, int] = {}
        for tenant, count in grouped:
            key = NONE_TENANT if tenant is None else str(tenant)
            by_tenant[key] = by_tenant.get(key, 0) + int(count)
        oldest = bounds[0]
        newest = bounds[1]
        return {
            "total": total,
            "by_tenant": by_tenant,
            "oldest": None if oldest is None else float(oldest),
            "newest": None if newest is None else float(newest),
        }

    # ── 附加查询（与 FileStore 同名，便于替换）──────────────
    def saved_at(self, ctx_id: str) -> float | None:
        validate_ctx_id(ctx_id)
        with self._use() as conn:
            row = conn.execute(
                "SELECT saved_at FROM contexts WHERE ctx_id = ?", (ctx_id,)
            ).fetchone()
        return None if row is None else float(row[0])

    def exists(self, ctx_id: str) -> bool:
        validate_ctx_id(ctx_id)
        with self._use() as conn:
            row = conn.execute(
                "SELECT 1 FROM contexts WHERE ctx_id = ?", (ctx_id,)
            ).fetchone()
        return row is not None

    def __len__(self) -> int:
        with self._use() as conn:
            return int(conn.execute("SELECT COUNT(*) FROM contexts").fetchone()[0])

    def __contains__(self, ctx_id: object) -> bool:
        if not isinstance(ctx_id, str):
            return False
        try:
            return self.exists(ctx_id)
        except ValueError:
            return False

    # ── 生命周期 ────────────────────────────────────────────
    def close(self) -> None:
        """关掉所有连接（幂等）。关掉后本实例仍可继续用：下次访问自动重开。"""
        with self._lock:
            self._epoch += 1
            conns, self._conns = self._conns, []
            self._shared = None
            self._local.cell = None
        for conn in conns:
            try:
                conn.close()
            except sqlite3.Error:      # 关闭失败不改变「已经关了」这个事实
                pass

    def __enter__(self) -> "SQLiteStore":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        kind = "memory" if self._is_memory else "file"
        return (f"<SQLiteStore {kind} path={self.path} saves={self.saves} "
                f"loads={self.loads} deletes={self.deletes}>")
