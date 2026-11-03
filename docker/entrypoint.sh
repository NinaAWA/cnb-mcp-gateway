#!/usr/bin/env bash
set -euo pipefail

PORT="${MCP_PORT:-8765}"
BIND="${MCP_BIND:-127.0.0.1}"
WORKDIR="${MCP_WORKDIR:-/workspace}"
ALLOWED="${MCP_ALLOWED_PREFIXES:-}"
# 确保工作目录存在（新服务器未挂载时也不至于命令失败）
mkdir -p "$WORKDIR"

# 密钥：未提供则随机生成（容器重建会变化，请从 docker logs 记录）
if [ -z "${MCP_TOKEN:-}" ]; then
  MCP_TOKEN="$(python3 -c 'import secrets,string;print("".join(secrets.choice(string.ascii_letters+string.digits) for _ in range(48)))')"
  echo "==================================================================="
  echo " [自动生成] MCP_TOKEN（重开容器会变化，请妥善保存）:"
  echo "   $MCP_TOKEN"
  echo "==================================================================="
fi
export MCP_TOKEN MCP_PORT="$PORT" MCP_BIND="$BIND" MCP_WORKDIR="$WORKDIR" MCP_ALLOWED_PREFIXES="$ALLOWED"

# 写 .env 供 server.py 加载
cat > /app/.env <<EOF
MCP_TOKEN=$MCP_TOKEN
MCP_PORT=$PORT
MCP_BIND=$BIND
MCP_WORKDIR=$WORKDIR
MCP_ALLOWED_PREFIXES=$ALLOWED
EOF
chmod 600 /app/.env

echo "[entrypoint] 启动 MCP server @ $BIND:$PORT"
cd /app
uvicorn server:app --host "$BIND" --port "$PORT" --log-level info &
SERVER_PID=$!

if [ -n "${CF_TUNNEL_TOKEN:-}" ]; then
  echo "[entrypoint] 命名隧道模式（固定 URL；需在 Cloudflare 把 ingress 指向 http://localhost:$PORT）"
  cloudflared tunnel run --token "$CF_TUNNEL_TOKEN" &
  TUNNEL_PID=$!
else
  echo "[entrypoint] quick tunnel 模式（随机 URL，每次重开变化）"
  cloudflared tunnel --url "http://localhost:$PORT" --no-autoupdate > /tmp/cloudflared.log 2>&1 &
  TUNNEL_PID=$!
  URL=""
  for i in $(seq 1 40); do
    URL="$(grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' /tmp/cloudflared.log | head -1 || true)"
    [ -n "$URL" ] && break
    sleep 1
  done
  if [ -n "$URL" ]; then
    echo "==================================================================="
    echo " 命令网关已就绪（quick tunnel，URL 每次重开随机）"
    echo ""
    echo " 公网根地址 (BASE_URL):"
    echo "   $URL"
    echo " MCP 连接器地址 (MCP_URL):"
    echo "   ${URL}/mcp"
    echo " ChatGPT 插件 OpenAPI 清单:"
    echo "   $URL/openapi.yaml"
    echo "   $URL/.well-known/ai-plugin.json"
    echo " 密钥 (MCP_TOKEN):"
    echo "   $MCP_TOKEN"
    echo ""
    echo " 用法: ChatGPT 上传插件包(zip) 或 添加自定义 MCP 服务器 ${URL}/mcp"
    echo "       密钥在安装/连接时粘贴上面的 MCP_TOKEN"
    echo "==================================================================="
  else
    echo "[entrypoint] 未能解析隧道 URL，请查看: docker logs cnb-mcp-gateway 或 docker exec cnb-mcp-gateway tail -f /tmp/cloudflared.log"
  fi
fi

trap 'kill -TERM $SERVER_PID $TUNNEL_PID 2>/dev/null; wait' TERM INT
wait
