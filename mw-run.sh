#!/bin/bash
# 在容器内跑：起服务 + wrk 压测
set -u
CODE=/workspace/zyj/mwtest
cd "$CODE" || exit 1
export PYTHONPATH="$CODE:$PYTHONPATH"
export AGENT_RUNTIME_ALLOW_ANONYMOUS=1
export AGENT_RUNTIME_RATE_LIMIT=0
export AGENT_RUNTIME_GOVERNANCE_QUOTA=0
export AGENT_RUNTIME_HTTP_RATE=0
PORT="${1:-19000}"
WORKERS="${2:-8}"
export AGENT_RUNTIME_PORT="$PORT"
export AGENT_RUNTIME_WORKERS="$WORKERS"
echo "=== 启动 serve.py: port=$PORT workers=$WORKERS ==="
python3 serve.py > /tmp/serve-$PORT.log 2>&1 &
echo "启动中..."
for i in $(seq 1 40); do
  sleep 2
  code=$(curl -s -m 3 -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PORT/healthz" 2>/dev/null)
  if [ "$code" = "200" ]; then echo "就绪（${i}0% 内）"; break; fi
done
curl -s -m 5 "http://127.0.0.1:$PORT/v1/info" | head -c 200
echo
echo "worker 进程数: $(pgrep -fc 'serve.py' 2>/dev/null || echo '?')"
