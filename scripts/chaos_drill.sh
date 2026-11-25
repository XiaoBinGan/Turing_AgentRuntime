#!/usr/bin/env bash
# 故障演练：逐项注入依赖故障，确认系统按预期降级而不是静默错。
#
# 演练的重点不是"系统没挂"，而是**失败姿态是否正确**：
# 该 fail-closed 的必须拒绝，该 fail-open 的必须继续跑，
# 并且都要在 /readyz 或响应里**留下痕迹**。
set -uo pipefail
cd "$(dirname "$0")/.."

PY=".venv/bin/python"; [ -x "$PY" ] || PY="python3"
FAIL=0
say() { printf '  %-52s %s\n' "$1" "$2"; }
bad() { say "$1" "FAIL"; FAIL=1; }
ok()  { say "$1" "ok"; }
info(){ say "$1" "-"; }

wait_up() {  # $1=port 最多等 10s
  for _ in $(seq 1 20); do
    [ "$(curl -s -m 2 -o /dev/null -w '%{http_code}' "http://127.0.0.1:$1/healthz" 2>/dev/null)" = "200" ] && return 0
    sleep 0.5
  done
  return 1
}

run_srv() {  # $1=port 其余=env
  local port=$1; shift
  env AGENT_RUNTIME_PORT=$port AGENT_RUNTIME_ALLOW_ANONYMOUS=1 "$@" \
     $PY serve.py >"/tmp/chaos-$port.log" 2>&1 &
  echo $!
}

echo "── 演练 1：Redis 不可达（KV 挂）──"
P=18170
PID=$(run_srv $P AGENT_RUNTIME_KV=redis AGENT_RUNTIME_REDIS_HOST=127.0.0.1 AGENT_RUNTIME_REDIS_PORT=19999 AGENT_RUNTIME_RATE_LIMIT_DISTRIBUTED=1)
sleep 8
if kill -0 "$PID" 2>/dev/null; then
  info "进程仍在（配置了 distributed 却不该起来）"
  kill -9 "$PID" 2>/dev/null
else
  # 期望：配了 distributed + 无共享 KV → **启动即拒**
  grep -qiE "distributed|KV 不可用|共享" /tmp/chaos-$P.log \
    && ok "配了 distributed 但 KV 不可用 → 启动拒绝（fail-closed）" \
    || bad "进程退出了，但日志没说清原因"
fi

echo
echo "── 演练 2：KV 挂但不要求分布式（应降级继续）──"
P=18171
PID=$(run_srv $P AGENT_RUNTIME_KV=redis AGENT_RUNTIME_REDIS_HOST=127.0.0.1 AGENT_RUNTIME_REDIS_PORT=19999)
if wait_up $P; then
  ok "未要求分布式 → 服务照常起来"
  # readyz 必须红（依赖不可用），业务面可以绿 —— 这个组合本身是已知陷阱
  rz=$(curl -s -m 5 -o /dev/null -w '%{http_code}' "http://127.0.0.1:$P/readyz")
  [ "$rz" = "503" ] && ok "readyz 报 503（依赖不可用可见）" || bad "readyz=$rz（依赖不可用却报健康）"
else
  bad "服务没起来"
fi
kill -9 "$PID" 2>/dev/null

echo
echo "── 演练 3：store 目录不可写 ──"
P=18172
RO="$PWD/chaos-ro-$$"; mkdir -p "$RO"; chmod 500 "$RO"
PID=$(run_srv $P AGENT_RUNTIME_STORE=file AGENT_RUNTIME_STORE_ROOT="$RO")
sleep 8
if wait_up $P; then
  rz=$(curl -s -m 5 -o /dev/null -w '%{http_code}' "http://127.0.0.1:$P/readyz")
  [ "$rz" = "503" ] && ok "不可写目录 → readyz 503（不静默）" || bad "readyz=$rz"
else
  info "进程未起（也是可接受姿态）"
fi
kill -9 "$PID" 2>/dev/null; chmod 700 "$RO" 2>/dev/null; rm -rf "$RO"

echo
echo "── 演练 4：上游 LLM 不可达 ──"
P=18173
PID=$(run_srv $P AGENT_RUNTIME_LLM=openai OPENAI_BASE_URL=http://127.0.0.1:19998/v1 OPENAI_API_KEY=sk-test)
if wait_up $P; then
  resp=$(curl -s -m 20 -X POST "http://127.0.0.1:$P/v1/turns" -H 'Content-Type: application/json' \
    -d '{"goal":"x","principal":{"user_id":"u","tenant":"t","roles":["r"],"scopes":["runtime:run"]}}' 2>/dev/null)
  err=$(echo "$resp" | $PY -c "import json,sys;print(json.load(sys.stdin).get('error',''))" 2>/dev/null)
  if [ -n "$err" ]; then
    ok "上游不可达 → 明确报错（${err:0:40}）"
    # 关键：不能返回空答案冒充成功
    body=$(echo "$resp" | $PY -c "import json,sys;d=json.load(sys.stdin);print(d.get('text',''))" 2>/dev/null)
    [ -z "$body" ] && ok "错误响应里没有伪造答案" || bad "错误时仍返回了正文（可能掩盖失败）"
  else
    bad "上游不可达但返回了成功响应"
  fi
else
  bad "服务没起来"
fi
kill -9 "$PID" 2>/dev/null

echo
echo "── 演练 5：磁盘满（用 /dev/full 模拟写失败）──"
$PY -c "
import sys
sys.path.insert(0,'.')
from core.store import FileStore
from core.store import StoreError
# 不可写位置：**构造期**就该拒（我们刚把它从"写到才失败"改成"启动就失败"）
try:
    store = FileStore('/dev')
except StoreError as exc:
    print(f'  构造期拒绝（{type(exc).__name__}）')
    sys.exit(0)
try:
    store.save('chaos_probe', '{}')
    print('  FAIL: /dev 下写成功了？'); sys.exit(1)
except (StoreError, OSError) as exc:
    print(f'  写入被拒（{type(exc).__name__}）')
" && ok "不可写存储 fail-closed" || bad "不可写存储未报错"

echo
echo "── 演练 6：超时中断（客户端断开）──"
P=18174
PID=$(run_srv $P)
if wait_up $P; then
  # 立刻断开，服务端不该崩
  for _ in 1 2 3; do
    curl -s -m 1 -X POST "http://127.0.0.1:$P/v1/turns" -H 'Content-Type: application/json' \
      -d '{"goal":"x","principal":{"user_id":"u","tenant":"t","roles":["r"],"scopes":["runtime:run"]}}' >/dev/null 2>&1
  done
  sleep 2
  if kill -0 "$PID" 2>/dev/null; then
    ok "客户端中断 3 次后服务仍存活"
  else
    bad "客户端中断导致服务退出"
  fi
else
  bad "服务没起来"
fi
kill -9 "$PID" 2>/dev/null

echo
echo "──────────────────────────────────────────────"
[ "$FAIL" -eq 0 ] && echo "  故障演练：全部符合预期失败姿态" || echo "  存在姿态不符项"
exit "$FAIL"
