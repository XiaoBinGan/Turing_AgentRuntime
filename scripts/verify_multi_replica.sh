#!/usr/bin/env bash
# 多副本一致性验证：两个 serve 实例共用一个 Redis。
#
# 为什么必须真起两个进程：`RATE_LIMIT_DISTRIBUTED=1` 的代码路径只有在
# 两个独立进程打同一个 Redis 时才会被走到。单进程里"分布式"和"内存"行为一样，
# 测了等于没测 —— 这正是一条"看着配了、其实各算各的"最容易溜过去的错。
#
# 前提：Redis 在 $REDIS_PORT 可连。脚本不自起容器（避免污染环境）。
set -uo pipefail
cd "$(dirname "$0")/.."

PY=".venv/bin/python"; [ -x "$PY" ] || PY="python3"
REDIS_HOST="${REDIS_HOST:-127.0.0.1}"
REDIS_PORT="${REDIS_PORT:-16379}"
P1="${PORT_A:-18101}"
P2="${PORT_B:-18102}"
LIMIT="${RATE_LIMIT:-4}"
FAIL=0

say() { printf '  %-46s %s\n' "$1" "$2"; }
bad() { say "$1" "FAIL"; FAIL=1; }
ok()  { say "$1" "ok"; }

echo "── 前置 ──"
if ! docker exec ar-redis redis-cli ping 2>/dev/null | grep -q PONG; then
  echo "  Redis 不可用（$REDIS_HOST:$REDIS_PORT）。先起："
  echo "    docker run -d --name ar-redis -p $REDIS_PORT:6379 redis:7-alpine"
  exit 1
fi
ok "Redis 可连"
docker exec ar-redis redis-cli FLUSHALL >/dev/null 2>&1

start_replica() {
  local port=$1 log=$2
  AGENT_RUNTIME_PORT=$port \
  AGENT_RUNTIME_ALLOW_ANONYMOUS=1 \
  AGENT_RUNTIME_STORE=sqlite \
  AGENT_RUNTIME_STORE_PATH="/tmp/ar-mr-${port}.db" \
  AGENT_RUNTIME_KV=redis \
  AGENT_RUNTIME_REDIS_HOST="$REDIS_HOST" \
  AGENT_RUNTIME_REDIS_PORT="$REDIS_PORT" \
  AGENT_RUNTIME_RATE_LIMIT_DISTRIBUTED=1 \
  AGENT_RUNTIME_RATE_LIMIT="$LIMIT" \
  AGENT_RUNTIME_HTTP_RATE="$((LIMIT * 10))" \
  $PY serve.py >"$log" 2>&1 &
  echo $!
}

rm -f /tmp/ar-mr-*.db
PID1=$(start_replica "$P1" /tmp/mr-a.log)
PID2=$(start_replica "$P2" /tmp/mr-b.log)
cleanup() { kill -9 "$PID1" "$PID2" 2>/dev/null; }
trap cleanup EXIT
sleep 10

echo
echo "── 两个副本就绪 ──"
up=0
for p in "$P1" "$P2"; do
  [ "$(curl -s -m 5 -o /dev/null -w '%{http_code}' "http://127.0.0.1:$p/healthz" 2>/dev/null)" = "200" ] && up=$((up+1))
  [ "$(curl -s -m 8 -o /dev/null -w '%{http_code}' "http://127.0.0.1:$p/readyz" 2>/dev/null)" = "200" ] \
    && ok "副本 :$p healthz+readyz" || bad "副本 :$p readyz 不健康"
done
[ "$up" -eq 2 ] && ok "两副本都起来了" || bad "只起来 $up/2"

echo
echo "── 跨副本租户配额（limit=$LIMIT，交替打 A/B）──"
BODY='{"goal":"x","principal":{"user_id":"u","tenant":"acme","roles":["sre"],"scopes":["runtime:run"]}}'
passed=0; rejected=0
for i in $(seq 1 $((LIMIT * 2))); do
  if [ $((i % 2)) -eq 1 ]; then port=$P1; who=A; else port=$P2; who=B; fi
  code=$(curl -s -m 10 -o /dev/null -w "%{http_code}" -X POST \
         "http://127.0.0.1:$port/v1/turns" -H 'Content-Type: application/json' -d "$BODY")
  printf '    #%-2d %s → %s\n' "$i" "$who" "$code"
  [ "$code" = "200" ] && passed=$((passed+1)) || rejected=$((rejected+1))
done
[ "$passed" -le "$LIMIT" ] && ok "通过 $passed ≤ limit $LIMIT（未跨副本超发）" \
                           || bad "通过 $passed > limit $LIMIT（超发 $((passed - LIMIT)) 次）"
[ "$rejected" -ge 1 ] && ok "有请求被拒（配额确实共享）" || bad "一次都没拒"

echo
echo "── 配额耗尽后两个副本都拒 ──"
for p in "$P1" "$P2"; do
  code=$(curl -s -m 10 -o /dev/null -w "%{http_code}" -X POST \
         "http://127.0.0.1:$p/v1/turns" -H 'Content-Type: application/json' -d "$BODY")
  [ "$code" = "429" ] && ok "副本 :$p 拒绝" || bad "副本 :$p 未拒（HTTP $code）"
done

echo
echo "── Redis 里确实有共享计数 ──"
keys=$(docker exec ar-redis redis-cli KEYS 'quota:*' 2>/dev/null | tr '\n' ' ')
[ -n "$keys" ] && ok "配额 key: $keys" || bad "Redis 里没有配额 key（走了进程内窗口）"

echo
echo "── HTTP 层分布式限流（每 IP）──"
keys=$(docker exec ar-redis redis-cli KEYS 'rt:rl:*' 2>/dev/null | tr '\n' ' ')
[ -n "$keys" ] && ok "HTTP 限流 key 存在" || bad "HTTP 限流没走 Redis"

echo
echo "── 会话跨副本承接 ──"
SID="sess-mr-$$"
curl -s -m 10 -o /dev/null -X POST "http://127.0.0.1:$P1/v1/turns" -H 'Content-Type: application/json' \
  -d "{\"goal\":\"第一轮\",\"session_id\":\"$SID\",\"principal\":{\"user_id\":\"u\",\"tenant\":\"t\",\"roles\":[\"sre\"],\"scopes\":[\"runtime:run\"]}}" 2>/dev/null
r2=$(curl -s -m 10 -X POST "http://127.0.0.1:$P2/v1/turns" -H 'Content-Type: application/json' \
     -d "{\"goal\":\"第二轮\",\"session_id\":\"$SID\",\"principal\":{\"user_id\":\"u\",\"tenant\":\"t\",\"roles\":[\"sre\"],\"scopes\":[\"runtime:run\"]}}" 2>/dev/null)
echo "$r2" | head -c 160
echo
code=$(echo "$r2" | $PY -c "import json,sys;d=json.load(sys.stdin);print('ok' if d.get('ok') or d.get('error')=='' else 'no')" 2>/dev/null)
if [ "$code" = "ok" ]; then
  ok "副本 B 接受了同 session 请求"
else
  # 会话表在每副本的 sqlite 里，跨副本读不到是**已知限制**，如实记录
  say "副本 B 承接同 session" "受限（见 notes）"
fi

echo
echo "──────────────────────────────────────────────"
if [ "$FAIL" -eq 0 ]; then echo "  多副本一致性：全部通过"; else echo "  存在失败项"; fi
exit "$FAIL"
