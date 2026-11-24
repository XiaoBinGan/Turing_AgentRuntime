#!/usr/bin/env bash
# verify_langfuse.sh — 起本地 Langfuse 并**真投递**一条 trace，然后读回来。
#
# 为什么需要它：`tests/test_otel_delivery.py` 验证的是"OTLP HTTP 能发出去"
# （本地接收器），但那证明不了**Langfuse 能接受我们的数据形状**。
# 这个脚本用真 Langfuse 服务端做端到端：投递 → 207 → 读回 → 校验。
#
# 版本说明（重要）：
#   Langfuse **v3** 自托管需要 postgres + clickhouse(+keeper) + redis + S3
#   五件套，且 clickhouse 迁移要 ReplicatedMergeTree（需 Keeper）——
#   单机跑不起来。**v2 只需 postgres**，所以本脚本固定用 v2 做验证。
#   我们的 SDK 是 langfuse 5.x（讲 v3 的 OTLP 协议），对 v2 服务端会 404；
#   因此这里直接走 v2 的 **ingestion REST API**（trace-create / span-create），
#   用 `trace_summary()` 产出的 Langfuse 形状数据喂进去 —— 验证的是
#   "我们的 span 数据能否被真 Langfuse 接受"，这正是要证明的东西。
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

PORT="${LANGFUSE_PORT:-3100}"
DIR="${COMPOSE_DIR:-/tmp/lf}"
MARK="${MARK:-docker.m.daocloud.io}"
PK="${LANGFUSE_PK:-pk-lf-demo-0001}"
SK="${LANGFUSE_SK:-sk-lf-demo-0001}"
PY="${PY:-.venv/bin/python}"

echo "① 准备镜像"
docker pull -q "$MARK/langfuse/langfuse:2.95.11" >/dev/null 2>&1 \
  && docker tag "$MARK/langfuse/langfuse:2.95.11" langfuse/langfuse:2.95.11 \
  || { echo "  langfuse 镜像拉取失败"; exit 1; }
docker pull -q postgres:16-alpine >/dev/null 2>&1 || true

echo "② 起 Langfuse v2（postgres + web）"
mkdir -p "$DIR" && cd "$DIR"
cat > compose.yml <<YML
services:
  db:
    image: postgres:16-alpine
    container_name: lf-db
    environment: {POSTGRES_USER: postgres, POSTGRES_PASSWORD: postgres, POSTGRES_DB: langfuse}
    healthcheck: {test: ["CMD-SHELL", "pg_isready -U postgres"], interval: 5s, retries: 10}
  langfuse:
    image: langfuse/langfuse:2.95.11
    container_name: lf-web
    ports: ["${PORT}:3000"]
    environment:
      DATABASE_URL: postgresql://postgres:postgres@db:5432/langfuse
      NEXTAUTH_URL: http://localhost:${PORT}
      NEXTAUTH_SECRET: dev-secret-please-change-0123456789
      SALT: dev-salt-please-change-0123456789
      TELEMETRY_ENABLED: "false"
      LANGFUSE_INIT_ORG_ID: demo-org
      LANGFUSE_INIT_ORG_NAME: Demo
      LANGFUSE_INIT_PROJECT_ID: demo-proj
      LANGFUSE_INIT_PROJECT_NAME: agent-runtime
      LANGFUSE_INIT_PROJECT_PUBLIC_KEY: ${PK}
      LANGFUSE_INIT_PROJECT_SECRET_KEY: ${SK}
      LANGFUSE_INIT_USER_EMAIL: dev@example.com
      LANGFUSE_INIT_USER_NAME: dev
      LANGFUSE_INIT_USER_PASSWORD: devpassword123
    depends_on:
      db: {condition: service_healthy}
YML
docker compose -f compose.yml up -d >/dev/null 2>&1 || { echo "  启动失败"; exit 1; }

echo "③ 等就绪（最多 120s）"
for i in $(seq 1 24); do
  code=$(curl -s -m 5 -o /dev/null -w "%{http_code}" "http://127.0.0.1:${PORT}/api/public/health" 2>/dev/null)
  [ "$code" = "200" ] && { echo "  ready"; break; }
  sleep 5
  [ "$i" = "24" ] && { echo "  超时，看 docker logs lf-web"; exit 1; }
done

echo "④ 真投递（trace + span → /api/public/ingestion）并读回"
cd "$ROOT"
LANGFUSE_HOST="http://127.0.0.1:${PORT}" LANGFUSE_PK="$PK" LANGFUSE_SK="$SK" \
  "$PY" scripts/langfuse_roundtrip.py
exit $?
