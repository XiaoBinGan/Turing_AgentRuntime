#!/usr/bin/env bash
# verify.sh — 一键验收。任何一步失败即整体失败。
#
#   ./verify.sh          全量
#   ./verify.sh -q       只跑测试，不跑 demo
set -uo pipefail
cd "$(dirname "$0")"

QUIET="${1:-}"

# 项目现在依赖 tiktoken / prometheus_client / jsonschema / redis。
# 有 venv 就用 venv 的 python —— 否则依赖相关的用例会静默退化（"配置了没生效"的经典形态）。
if [ -x ".venv/bin/python" ]; then
  PY=".venv/bin/python"
  PY_NOTE="(venv)"
else
  PY="python3"
  PY_NOTE="(system python：未装依赖，部分能力会降级)"
fi
echo "  解释器: $PY $PY_NOTE"
FAIL=0
line() { printf '%s\n' "────────────────────────────────────────────────────────────────"; }
title() { line; printf '  %s\n' "$1"; line; }

title "Agent Runtime 骨架验收"

# ① 语法
printf '① 语法编译 ... '
if out=$("$PY" -m compileall -q . 2>&1); then
  echo "OK"
else
  echo "FAIL"; echo "$out" | head -20; FAIL=1
fi

# ② 全量测试
printf '② 单元 + 集成测试 ... '
out=$("$PY" -m unittest discover -s tests -t . 2>&1)
if echo "$out" | tail -1 | grep -q '^OK'; then
  echo "$(echo "$out" | grep -E '^Ran ' | tail -1)"
else
  echo "FAIL"; echo "$out" | tail -30; FAIL=1
fi

# ③ 契约自洽性（单独点名，因为这是最容易被忽略的一类）
printf '③ 契约自洽性 ... '
if "$PY" -m unittest tests.test_integration -q 2>&1 | tail -1 | grep -q '^OK'; then
  echo "OK"
else
  echo "FAIL"; FAIL=1
fi

# ④ 端到端 demo
if [ "$QUIET" != "-q" ]; then
  printf '④ 端到端场景 ... '
  if out=$("$PY" demo/run.py 2>&1); then
    n_pass=$(echo "$out" | grep -c '断言:.*→ PASS' || true)
    n_fail=$(echo "$out" | grep -c '断言:.*→ FAIL' || true)
    echo "$(echo "$out" | grep -E '完成：' | tail -1) (断言 PASS=$n_pass FAIL=$n_fail)"
    [ "$n_fail" -ne 0 ] && FAIL=1
  else
    echo "FAIL"; echo "$out" | tail -30; FAIL=1
  fi
fi

# ⑤ HTTP 服务层（用 serve 的用例做门禁：端点/状态码/SSE/探针）
if [ "$QUIET" != "-q" ]; then
  printf '⑤ HTTP 服务面 ... '
  if out=$("$PY" -m unittest tests.test_serve 2>&1) && echo "$out" | tail -1 | grep -q '^OK'; then
    echo "$(echo "$out" | grep -E '^Ran ' | tail -1)"
  else
    echo "FAIL"; echo "$out" | tail -20; FAIL=1
  fi
fi

# ⑥ 可选：真集群 / 真 Langfuse（需显式开启，否则跳过）
#    VERIFY_CLUSTER=1   起 Milvus 三容器集群并跑 46 个契约用例（约 3 分钟）
#    VERIFY_LANGFUSE=1  起 Langfuse 并真投递 + 读回（约 1 分钟）
if [ "${VERIFY_CLUSTER:-0}" = "1" ]; then
  printf '⑥ 真 Milvus 集群 ... '
  if out=$(bash scripts/verify_milvus_cluster.sh 2>&1) && echo "$out" | grep -q '集群契约全绿'; then
    echo "$(echo "$out" | grep -E '^Ran ' | tail -1)"
  else
    echo "FAIL"; echo "$out" | tail -15; FAIL=1
  fi
fi
if [ "${VERIFY_LANGFUSE:-0}" = "1" ]; then
  printf '⑦ 真 Langfuse 投递 ... '
  if out=$(bash scripts/verify_langfuse.sh 2>&1) && echo "$out" | grep -q '真投递 + 读回通过'; then
    echo "OK"
  else
    echo "FAIL"; echo "$out" | tail -15; FAIL=1
  fi
fi

line
if [ "$FAIL" -eq 0 ]; then
  echo "  ✅ 全部通过"
else
  echo "  ❌ 有失败项"
fi
line
exit "$FAIL"
