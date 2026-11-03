#!/usr/bin/env bash
set -euo pipefail

# 一键启动 CNB MCP Gateway
# 镜像默认从 GHCR 拉取；如需本地构建，先执行: docker build -t cnb-mcp-gateway:local ./docker
IMG="${MCP_IMAGE:-ghcr.io/ninaawa/cnb-mcp-gateway:latest}"

# 拉取（若镜像不存在/私有则需先 docker login ghcr.io）
docker pull "$IMG" 2>/dev/null || true

docker rm -f cnb-mcp-gateway 2>/dev/null || true

docker run -d --name cnb-mcp-gateway --restart unless-stopped \
  -e MCP_TOKEN="${MCP_TOKEN:-}" \
  -e MCP_ALLOWED_PREFIXES="${MCP_ALLOWED_PREFIXES:-}" \
  -e CF_TUNNEL_TOKEN="${CF_TUNNEL_TOKEN:-}" \
  -e MCP_WORKDIR="${MCP_WORKDIR:-/workspace}" \
  -v "${MCP_WORKDIR_MOUNT:-$PWD}:/workspace" \
  "$IMG"

echo "== 等待启动并读取公网地址 / 密钥 =="
sleep 12
docker logs cnb-mcp-gateway
