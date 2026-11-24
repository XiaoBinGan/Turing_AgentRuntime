#!/usr/bin/env bash
# verify_milvus_cluster.sh — 起一个真 Milvus 集群并跑契约测试。
#
# 为什么要这个脚本：milvus-lite 与集群**不是同一个部署形态**，
# 有两类问题只在集群上暴露（本轮实测各踩一次）：
#   1. 建完集合必须 `load_collection`，否则 search 报 collection not loaded
#   2. delete 是软删，`get_collection_stats` 把 tombstone 也算进 row_count
# 所以"lite 上全绿"不能替代集群验证。
#
# 镜像来源：Docker Hub 在本机被限流，改用可达的镜像源。
#   镜像源可用性会变，脚本里集中放在 MARK 变量，换源只改一处。
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

MILVUS_PORT="${MILVUS_PORT:-19531}"
COMPOSE_DIR="${COMPOSE_DIR:-/tmp/mv-cluster}"
MARK="${MARK:-docker.m.daocloud.io}"          # 镜像源（可换 hub.rat.dev）
PY="${PY:-.venv/bin/python}"

echo "① 准备镜像（源: $MARK）"
docker pull -q "$MARK/milvusdb/milvus:v2.5.4" >/dev/null 2>&1 \
  && docker tag "$MARK/milvusdb/milvus:v2.5.4" milvusdb/milvus:v2.5.4 \
  || { echo "  milvus 镜像拉取失败：换 MARK 或预先 docker load"; exit 1; }
docker pull -q quay.io/coreos/etcd:v3.5.16 >/dev/null 2>&1 || true
docker pull -q ghcr.io/coollabsio/minio:latest >/dev/null 2>&1 \
  && docker tag ghcr.io/coollabsio/minio:latest minio/minio:latest || true

echo "② 起集群（etcd + minio + milvus）"
mkdir -p "$COMPOSE_DIR" && cd "$COMPOSE_DIR"
cat > compose.yml <<YML
services:
  etcd:
    container_name: mvc-etcd
    image: quay.io/coreos/etcd:v3.5.16
    environment: ["ETCD_AUTO_COMPACTION_MODE=revision", "ETCD_QUOTA_BACKEND_BYTES=4294967296"]
    command: etcd -advertise-client-urls=http://etcd:2379 -listen-client-urls http://0.0.0.0:2379 --data-dir /etcd
    healthcheck: {test: ["CMD", "etcdctl", "endpoint", "health"], interval: 10s, retries: 6}
  minio:
    container_name: mvc-minio
    image: minio/minio:latest
    environment: {MINIO_ROOT_USER: minioadmin, MINIO_ROOT_PASSWORD: minioadmin}
    entrypoint: sh
    command: -c 'mkdir -p /data/milvus && minio server /data --console-address ":9001"'
    # 镜像里没有 curl，用 mc 自带探活
    healthcheck: {test: ["CMD", "mc", "ready", "local"], interval: 10s, retries: 8}
  milvus:
    container_name: mvc-milvus
    image: milvusdb/milvus:v2.5.4
    command: ["milvus", "run", "standalone"]
    security_opt: ["seccomp:unconfined"]
    environment:
      ETCD_ENDPOINTS: etcd:2379
      MINIO_ADDRESS: minio:9000
      MINIO_ACCESS_KEY_ID: minioadmin
      MINIO_SECRET_ACCESS_KEY: minioadmin
    ports: ["${MILVUS_PORT}:19530"]
    depends_on:
      etcd: {condition: service_healthy}
      minio: {condition: service_healthy}
    healthcheck: {test: ["CMD", "curl", "-f", "http://localhost:9091/healthz"], interval: 15s, start_period: 90s, retries: 10}
YML
docker compose -f compose.yml up -d >/dev/null 2>&1 || { echo "  集群启动失败"; exit 1; }

echo "③ 等就绪（最多 180s）"
for i in $(seq 1 36); do
  if docker exec mvc-milvus curl -sf http://localhost:9091/healthz >/dev/null 2>&1; then
    echo "  ready（${i}0s）"; break
  fi
  sleep 5
  [ "$i" = "36" ] && { echo "  超时，看 docker logs mvc-milvus"; exit 1; }
done

echo "④ 跑契约测试（真集群）"
cd "$ROOT"
MILVUS_TEST_URI="http://127.0.0.1:${MILVUS_PORT}" "$PY" -m unittest tests.test_vector_backends -v
rc=$?

echo "⑤ 收尾（docker compose down -v 可清数据）"
echo "  容器仍在运行：docker ps --filter name=mvc-"
[ $rc -ne 0 ] && echo "❌ 契约测试失败" || echo "✅ 集群契约全绿"
exit $rc
