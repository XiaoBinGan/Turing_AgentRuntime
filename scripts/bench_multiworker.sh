#!/usr/bin/env bash
# =============================================================================
# bench_multiworker.sh —— 用真多线程压测工具（wrk）排除「客户端只有单进程」这个
# 混淆变量，判断 Turing Agent Runtime 的 AGENT_RUNTIME_WORKERS=N 到底有没有吞吐收益。
#
# 为什么不用 scripts/loadtest.py：
#   它是**单进程** asyncio + 单个 httpx.AsyncClient。事件循环只有一个线程，所有
#   请求的 JSON 编解码 / 连接复用 / 计时都挤在一个 GIL 上。当服务端单 worker 只需
#   ~10ms CPU/请求时，客户端先饱和 —— 于是"1 worker 和 4 worker 都是 111-131 RPS"
#   这个观测无法区分"服务端有共享瓶颈"和"压测端只有一份 GIL"。
#   wrk 是 C + 每线程独立 epoll + 多线程，客户端不再是单点。
#
# 用法：
#   bash scripts/bench_multiworker.sh                 # 跑完整矩阵
#   TIERS="2:32 8:128" ROUNDS=2 bash scripts/...       # 自定档位/轮次
#
# 产物：
#   /tmp/mw-raw.tsv      每次运行的原始行（配置 / 线程 / 连接 / RPS / 分位数 / CPU）
#   /tmp/mw-serve-*.log  两个被测进程的日志
#
# 硬纪律：
#   * 不动仓库里任何已有文件；本脚本只读 serve.py、只写 /tmp。
#   * 结束必定杀干净自己起的进程（父 + 全部 worker 子进程），用端口兜底核对。
#   * 端口默认 18310/18311 而非 18300/18301：18300 上已有别的进程在跑，占了会起不来。
# =============================================================================
set -uo pipefail

REPO="${REPO:-/Users/supre/Documents/tmp/agent-runtime}"
PY="${REPO}/.venv/bin/python"
SINGLE_PORT="${SINGLE_PORT:-18310}"
MULTI_PORT="${MULTI_PORT:-18311}"
WORKERS="${WORKERS:-4}"
DURATION="${DURATION:-20}"          # 每档每轮 20 秒
ROUNDS="${ROUNDS:-2}"               # 每档至少 2 轮：这台机器有外部负载，单次不可信
# 档位格式 "线程:连接"，用空格分隔。三档是为了看到拐点，而不只是两个点。
TIERS="${TIERS:-2:32 8:128 16:256}"
WARMUP="${WARMUP:-5}"               # 每档先空跑 WARMUP 秒，不计入统计
OUT="${OUT:-/tmp/mw-raw.tsv}"
LUA="${LUA:-/tmp/wrk-post.lua}"
URL_PATH="${URL_PATH:-/v1/turns}"

# 限额全关：配额/限流一旦生效，压出来的是「限流器的速度」，不是服务端的容量
export AGENT_RUNTIME_ALLOW_ANONYMOUS=1
export AGENT_RUNTIME_RATE_LIMIT=0
export AGENT_RUNTIME_GOVERNANCE_QUOTA=0
export AGENT_RUNTIME_HTTP_RATE=0

SINGLE_PID=""
MULTI_PID=""

log() { printf '[bench] %s\n' "$*" >&2; }

# ── 依赖检查 ────────────────────────────────────────────────────────────────
if ! command -v wrk >/dev/null 2>&1; then
  log "缺少 wrk。先跑：brew install wrk（或 k6 / oha）"
  exit 1
fi
if [[ ! -f "$LUA" ]]; then
  # wrk 命令行不能直接发 body，必须走 Lua 脚本
  cat > "$LUA" <<'LUAEOF'
wrk.method = "POST"
wrk.body = '{"goal":"查一下 api 服务的 p99 指标","principal":{"user_id":"load","tenant":"loadtest","roles":["sre"],"scopes":["runtime:run","tool.read"]}}'
wrk.headers["Content-Type"] = "application/json"
LUAEOF
fi

# ── 进程树 / CPU 采样 ───────────────────────────────────────────────────────
# 取某进程的全部后代 pid（uvicorn 多 worker 下子进程是父的直接子进程，
# 但递归一层更稳）。**不能**用 pgrep -f serve.py 认进程：多 worker 的子进程
# 命令行是 multiprocessing 的 spawn 形式，名字里没有 serve.py。
tree_pids() {
  local root="$1" all="$1" frontier="$1" kids
  while [[ -n "$frontier" ]]; do
    kids=""
    for p in $frontier; do
      kids="$kids $(pgrep -P "$p" 2>/dev/null | tr '\n' ' ')"
    done
    frontier="$kids"
    all="$all $kids"
  done
  echo $all | tr ' ' '\n' | grep -E '^[0-9]+$' | sort -u | tr '\n' ' '
}

# ps -o time= 的 "分:秒.厘秒" / "时:分:秒.厘秒" → 累计 CPU 秒
cpu_secs() {
  local pids="$1" total=0
  for p in $pids; do
    [[ -d "/proc/$p" ]] || kill -0 "$p" 2>/dev/null || continue
    local t
    t=$(ps -o time= -p "$p" 2>/dev/null | tr -d ' ')
    [[ -z "$t" ]] && continue
    total=$(awk -v acc="$total" -v t="$t" 'BEGIN{
      n=split(t, a, /[:.]/);
      if (n==3)  s = a[1]*60 + a[2] + a[3]/100;        # M:SS.cc
      else if (n==4) s = a[1]*3600 + a[2]*60 + a[3] + a[4]/100;  # H:MM:SS.cc
      else s = 0;
      printf "%.2f", acc + s }')
  done
  echo "$total"
}

# macOS 的 ps -o pcpu= 是**进程生命周期均值**，不是瞬时值 —— 长跑进程的启动期
# 会被摊薄。按任务要求仍然求和上报，但结论以 delta-CPU 为准。
pcpu_sum() {
  local pids="$1" total=0
  for p in $pids; do
    local v
    v=$(ps -o pcpu= -p "$p" 2>/dev/null | tr -d ' ')
    [[ -z "$v" ]] && continue
    total=$(awk -v acc="$total" -v v="$v" 'BEGIN{printf "%.1f", acc+v}')
  done
  echo "$total"
}

# ── 起服务 / 等就绪 ─────────────────────────────────────────────────────────
wait_ready() {
  local port="$1" i
  for i in $(seq 1 120); do
    if curl -sf -o /dev/null "http://127.0.0.1:${port}/healthz"; then return 0; fi
    sleep 0.5
  done
  log "端口 ${port} 等了 60s 还没 200"
  return 1
}

start_single() {
  log "起单 worker → :${SINGLE_PORT}"
  # 不能用 `( cd X && nohup Y & echo $! )`：`A && B &` 把整条 AND-list 丢进子
  # shell，$! 拿到的是子 shell 而不是 python，清理时会杀错进程、留下孤儿。
  # 先 cd，再用单条简单命令起后台进程，$! 才是 uvicorn 进程本身。
  pushd "$REPO" >/dev/null || return 1
  AGENT_RUNTIME_PORT="$SINGLE_PORT" nohup "$PY" serve.py >"/tmp/mw-serve-${SINGLE_PORT}.log" 2>&1 &
  SINGLE_PID=$!
  popd >/dev/null
  wait_ready "$SINGLE_PORT" || return 1
  log "单 worker 就绪 pid=${SINGLE_PID}"
}

start_multi() {
  log "起 ${WORKERS} worker → :${MULTI_PORT}"
  pushd "$REPO" >/dev/null || return 1
  AGENT_RUNTIME_PORT="$MULTI_PORT" AGENT_RUNTIME_WORKERS="$WORKERS" \
    nohup "$PY" serve.py >"/tmp/mw-serve-${MULTI_PORT}.log" 2>&1 &
  MULTI_PID=$!
  popd >/dev/null
  wait_ready "$MULTI_PORT" || return 1
  log "多 worker 就绪 pid=${MULTI_PID}（树：$(tree_pids "$MULTI_PID")）"
}

# ── 清理：只杀自己起的，用端口兜底核对 ───────────────────────────────────────
cleanup() {
  log "清理进程……"
  local root pids
  for root in "$SINGLE_PID" "$MULTI_PID"; do
    [[ -z "$root" ]] && continue
    pids=$(tree_pids "$root")
    [[ -n "$pids" ]] && kill -TERM $pids 2>/dev/null
  done
  # uvicorn 多 worker 的父进程收到 TERM 会逐个等子进程排空（drain 上限 10s），
  # 实测经常停在 "Waiting for child process"。给 8s 窗口，之后直接 KILL。
  local i
  for i in $(seq 1 16); do
    lsof -iTCP -sTCP:LISTEN -P 2>/dev/null | grep -qE ":(${SINGLE_PORT}|${MULTI_PORT}) " || break
    sleep 0.5
  done
  for root in "$SINGLE_PID" "$MULTI_PID"; do
    [[ -z "$root" ]] && continue
    pids=$(tree_pids "$root")
    [[ -n "$pids" ]] && kill -KILL $pids 2>/dev/null
  done
  sleep 1
  # 端口兜底：我起的端口上不许再有人 LISTEN
  local left
  left=$(lsof -iTCP -sTCP:LISTEN -P 2>/dev/null | grep -E ":(${SINGLE_PORT}|${MULTI_PORT}) " || true)
  if [[ -n "$left" ]]; then
    log "端口上仍有残留，强杀："
    echo "$left" >&2
    echo "$left" | awk '{print $2}' | sort -u | xargs -r kill -KILL 2>/dev/null
  fi
  log "清理完成：端口 $(lsof -iTCP -sTCP:LISTEN -P 2>/dev/null | grep -cE ":(${SINGLE_PORT}|${MULTI_PORT}) ") 个监听残留"
}
trap cleanup EXIT INT TERM

# ── 单次压测 ────────────────────────────────────────────────────────────────
# run_one <标签> <端口> <线程> <连接> <轮次>
run_one() {
  local label="$1" port="$2" thr="$3" conn="$4" round="$5"
  local url="http://127.0.0.1:${port}${URL_PATH}"
  local root pids t0 tmid t1 wall cpu0 cpumid cpu1 out
  local rps p50 p99 avg errs reqs ccl_mid ccl_pct
  if [[ "$label" == "${WORKERS}w" ]]; then root="$MULTI_PID"; else root="$SINGLE_PID"; fi
  pids=$(tree_pids "$root")

  local tmpout
  tmpout=$(mktemp /tmp/mw-wrk-XXXXXX)

  cpu0=$(cpu_secs "$pids")
  t0=$(python3 -c 'import time;print(time.time())')

  # 注意：不能写 `out=$(wrk ...) &` —— 那是把「赋值」整个丢进子 shell，
  # 父 shell 里的 $out 永远是空的。必须落文件。
  wrk -t"$thr" -c"$conn" -d"${DURATION}s" --latency -s "$LUA" "$url" >"$tmpout" 2>&1 &
  local wrk_pid=$!
  # 跑到一半采一次：稳态占用比「含建连爬坡」的全窗口均值更接近真实 CPU 需求
  sleep "$(( DURATION / 2 ))"
  cpumid=$(cpu_secs "$pids")
  # 同时采压测端自己的 CPU：如果客户端也贴满若干核，那"服务端没涨"就还是客户端问题
  ccl_mid=$(cpu_secs "$wrk_pid")
  tmid=$(python3 -c 'import time;print(time.time())')
  wait "$wrk_pid"
  out=$(cat "$tmpout")

  cpu1=$(cpu_secs "$pids")
  t1=$(python3 -c 'import time;print(time.time())')
  wall=$(awk -v a="$t0" -v b="$t1" 'BEGIN{printf "%.2f", b-a}')
  # 前半段（含爬坡）与全窗口的 CPU 占用率，按「占满一个核 = 100%」求和
  local cpu_mid_pct cpu_all_pct
  cpu_mid_pct=$(awk -v c0="$cpu0" -v c1="$cpumid" -v a="$t0" -v b="$tmid" \
    'BEGIN{ w=b-a; if (w<=0) w=1; printf "%.0f", (c1-c0)/w*100 }')
  cpu_all_pct=$(awk -v c0="$cpu0" -v c1="$cpu1" -v w="$wall" \
    'BEGIN{ if (w<=0) w=1; printf "%.0f", (c1-c0)/w*100 }')
  # 压测端 CPU 只有前半段能采（wrk 结束后进程没了），按「同样的窗口」给百分比
  ccl_pct=$(awk -v c1="${ccl_mid:-0}" -v a="$t0" -v b="$tmid" \
    'BEGIN{ w=b-a; if (w<=0) w=1; printf "%.0f", c1/w*100 }')

  rps=$(awk '/^Requests\/sec:/{print $2}' <<<"$out")
  p50=$(awk '/^ +50%/{print $2}' <<<"$out" | head -1)
  p99=$(awk '/^ +99%/{print $2}' <<<"$out" | head -1)
  avg=$(awk '/^ +Latency/{print $2}' <<<"$out" | head -1)
  errs=$(awk '/Socket errors/{sub(/^ +/,"");print}' <<<"$out" | head -1)
  reqs=$(awk '/requests in/{print $1}' <<<"$out" | head -1)

  printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
    "$label" "$thr" "$conn" "$round" "${rps:-ERR}" "${p50:-ERR}" "${p99:-ERR}" \
    "${avg:-ERR}" "${cpu_mid_pct:-ERR}" "${cpu_all_pct:-ERR}" "${ccl_pct:-ERR}" \
    "${reqs:-ERR}" "${errs:-none}" >> "$OUT"
  log "$label t$thr/c$conn r$round → ${rps:-ERR} rps | p50=${p50:-?} p99=${p99:-?} | cpu(mid)=${cpu_mid_pct}% cpu(all)=${cpu_all_pct}% client_cpu=${ccl_pct}%"
  # 出错时把 wrk 原文落盘，别把失败伪装成低吞吐
  if [[ -z "$rps" || "$rps" == "ERR" ]]; then
    { echo "=== wrk 原文 ($label t$thr/c$conn r$round) ==="; echo "$out"; } >> /tmp/mw-wrk-fail.log
  fi
  rm -f "$tmpout"
}

# ── 主流程 ──────────────────────────────────────────────────────────────────
: > "$OUT"
printf 'cfg\tthreads\tconns\tround\trps\tp50\tp99\tavg\tsrv_cpu_mid_pct\tsrv_cpu_all_pct\tclient_cpu_pct\trequests\tsocket_errors\n' >> "$OUT"

start_single || { log "单 worker 起不来，看 /tmp/mw-serve-${SINGLE_PORT}.log"; exit 1; }
start_multi  || { log "多 worker 起不来，看 /tmp/mw-serve-${MULTI_PORT}.log"; exit 1; }

for label in 1w "${WORKERS}w"; do
  port=$([[ "$label" == "1w" ]] && echo "$SINGLE_PORT" || echo "$MULTI_PORT")
  for tier in $TIERS; do
    thr="${tier%%:*}"; conn="${tier##*:}"
    # 预热：让连接池、线程池、JIT 热起来再计分
    wrk -t"$thr" -c"$conn" -d"${WARMUP}s" -s "$LUA" \
        "http://127.0.0.1:${port}${URL_PATH}" >/dev/null 2>&1
    for r in $(seq 1 "$ROUNDS"); do
      run_one "$label" "$port" "$thr" "$conn" "$r"
    done
  done
done

echo
log "原始数据：$OUT"
column -t -s $'\t' "$OUT" || cat "$OUT"
