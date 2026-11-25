#!/usr/bin/env bash
# 备份恢复演练：sqlite 与 file 两种 store 的备份 → 销毁 → 恢复 → 校验。
#
# 为什么必须演练而不只是"有这个能力"：备份文件损坏、恢复后 schema 不兼容、
# WAL 未 checkpoint 导致丢最后几条 —— 这些都只有真做一次才会暴露。
# 演练里"恢复了但数据少一条"比"没备份"更危险，因为它让人以为有保障。
set -uo pipefail
cd "$(dirname "$0")/.."

PY=".venv/bin/python"; [ -x "$PY" ] || PY="python3"
WORK=$(mktemp -d /tmp/ar-backup-XXXXXX)
FAIL=0
say() { printf '  %-46s %s\n' "$1" "$2"; }
bad() { say "$1" "FAIL"; FAIL=1; }
ok()  { say "$1" "ok"; }
cleanup() { rm -rf "$WORK"; }
trap cleanup EXIT

echo "── sqlite store ──"
DB="$WORK/runtime.db"
"$PY" - "$DB" <<'PYEOF'
import sys
from core.sqlite_store import SQLiteStore
from core.context import ContextHandle
from core.store import save_checkpoint
store = SQLiteStore(sys.argv[1])
for i in range(20):
    ctx = ContextHandle(initial={"goal": f"task-{i}", "idx": i}, budget=None)
    save_checkpoint(ctx, store, f"ctx_warm_{i:02d}", reason=f"seed {i}")
print(f"  写入 20 条，store 计数 {len(store.list_pending())}")
PYEOF
n_before=$("$PY" -c "
from core.sqlite_store import SQLiteStore
print(len(SQLiteStore('$DB').list_pending()))
")
[ "$n_before" -eq 20 ] && ok "备份前 20 条" || bad "备份前只有 $n_before 条"

# 用 sqlite 官方 backup API（比 cp 安全：能处理 WAL 中未落盘的事务）
BAK="$WORK/runtime.db.bak"
"$PY" -c "
import sqlite3
src = sqlite3.connect('$DB'); dst = sqlite3.connect('$BAK')
with dst: src.backup(dst)
src.close(); dst.close()
print('  backup API 完成')
"
[ -s "$BAK" ] && ok "备份文件非空 ($(du -h "$BAK" | awk '{print $1}'))" || bad "备份文件为空"

# 模拟灾难：删掉原库
rm -f "$DB" "$DB-wal" "$DB-shm"
[ ! -f "$DB" ] && ok "原库已销毁（模拟灾难）" || bad "原库未销毁"

# 恢复
cp "$BAK" "$DB"
n_after=$("$PY" -c "
from core.sqlite_store import SQLiteStore
print(len(SQLiteStore('$DB').list_pending()))
")
[ "$n_after" -eq "$n_before" ] && ok "恢复后 $n_after 条（一致）" || bad "恢复后 $n_after 条 ≠ $n_before"

# 校验内容不只条数
"$PY" -c "
import sys
from core.sqlite_store import SQLiteStore
from core.store import load_checkpoint
store = SQLiteStore('$DB')
ctx = load_checkpoint(store, 'ctx_warm_00')
assert ctx.get('goal') == 'task-0', ctx.get('goal')
assert ctx.get('idx') == 0
print('  内容校验通过: goal=%s idx=%s' % (ctx.get('goal'), ctx.get('idx')))
" && ok "恢复后内容正确（不只条数）" || bad "恢复后内容不对"

echo
echo "── file store ──"
ROOT="$WORK/file-store"
mkdir -p "$ROOT"
"$PY" - "$ROOT" <<'PYEOF'
import sys
from core.store import FileStore, save_checkpoint
from core.context import ContextHandle
store = FileStore(sys.argv[1])
for i in range(10):
    ctx = ContextHandle(initial={"goal": f"f-{i}"}, budget=None)
    save_checkpoint(ctx, store, f"ctx_file_{i:02d}", reason=f"seed {i}")
print(f"  写入 10 条")
PYEOF
n_before=$(ls "$ROOT"/*.json 2>/dev/null | wc -l | tr -d ' ')
[ "$n_before" -eq 10 ] && ok "备份前 10 个文件" || bad "文件数 $n_before"

BAKDIR="$WORK/file-store.bak"
cp -a "$ROOT" "$BAKDIR"
rm -rf "$ROOT"
[ ! -d "$ROOT" ] && ok "目录已销毁" || bad "目录未销毁"
cp -a "$BAKDIR" "$ROOT"
n_after=$(ls "$ROOT"/*.json 2>/dev/null | wc -l | tr -d ' ')
[ "$n_after" -eq "$n_before" ] && ok "恢复后 $n_after 个（一致）" || bad "恢复后 $n_after ≠ $n_before"

echo
echo "── 损坏检测：坏备份必须被拒绝，不能静默读成空 ──"
echo 'not valid json at all {{{' > "$ROOT/ctx_file_00.json"
"$PY" -c "
from core.store import FileStore
from core.store import load_checkpoint
from core.store import StoreCorrupt
store = FileStore('$ROOT')
try:
    load_checkpoint(store, 'ctx_file_00')
    print('  FAIL: 坏文件被当成正常读了')
    raise SystemExit(1)
except StoreCorrupt:
    print('  坏文件被明确拒绝（StoreCorrupt）')
except Exception as exc:
    print(f'  坏文件被拒绝（{type(exc).__name__}）')
" && ok "坏备份 fail-closed" || bad "坏备份未被拒绝"

echo
echo "── 备份完整性校验工具 ──"
"$PY" -c "
import sqlite3, sys
con = sqlite3.connect('$BAK')
r = con.execute('PRAGMA integrity_check').fetchone()[0]
con.close()
print(f'  PRAGMA integrity_check: {r}')
sys.exit(0 if r == 'ok' else 1)
" && ok "sqlite integrity_check 通过" || bad "备份库完整性损坏"

echo
echo "──────────────────────────────────────────────"
[ "$FAIL" -eq 0 ] && echo "  备份恢复演练：全部通过" || echo "  存在失败项"
exit "$FAIL"
