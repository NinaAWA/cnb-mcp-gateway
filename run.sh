#!/usr/bin/env bash
set -euo pipefail

# 一键启动 CNB MCP Gateway
# 镜像默认从 GHCR 拉取；如需本地构建，先执行:
#   docker build -f docker/Dockerfile -t cnb-mcp-gateway:local .
# 需要审计管理后台请改用: docker compose up -d --build
# v1.1 制品使用独立的 plus 包与 v1.1.0 标签；旧 package / v3.1.0 不被覆盖。
IMG="${MCP_IMAGE:-ghcr.io/ninaawa/cnb-mcp-gateway-plus:v1.1.0}"

# 拉取（若镜像不存在/私有则需先 docker login ghcr.io）
docker pull "$IMG" 2>/dev/null || true

docker rm -f cnb-mcp-gateway 2>/dev/null || true

docker run -d --name cnb-mcp-gateway --restart unless-stopped \
  -e MCP_TOKEN="${MCP_TOKEN:-}" \
  -e MCP_ALLOWED_PREFIXES="${MCP_ALLOWED_PREFIXES:-}" \
  -e CF_TUNNEL_TOKEN="${CF_TUNNEL_TOKEN:-}" \
  -e MCP_WORKDIR="${MCP_WORKDIR:-/workspace}" \
  -e MCP_FILE_ROOT="${MCP_FILE_ROOT:-/data/files}" \
  -e MCP_TASK_LOG_DIR="${MCP_TASK_LOG_DIR:-/data/audit/task-logs}" \
  -e MCP_PUBLIC_BASE_URL="${MCP_PUBLIC_BASE_URL:-}" \
  -e MCP_MAX_OUTPUT_BYTES="${MCP_MAX_OUTPUT_BYTES:-2000000}" \
  -e AUDIT_DB_PATH=/data/audit/audit.db \
  -v "${MCP_WORKDIR_MOUNT:-$PWD}:/workspace" \
  -v "${MCP_DATA_VOLUME:-mcp-gateway-data}:/data/audit" \
  -v "${MCP_FILES_VOLUME:-mcp-gateway-files}:/data/files" \
  "$IMG"

echo "== 等待启动并读取公网地址 / 密钥 =="
sleep 12
docker logs cnb-mcp-gateway
