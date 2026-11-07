#!/usr/bin/env bash
set -euo pipefail

PORT="${MCP_PORT:-8765}"
BIND="${MCP_BIND:-127.0.0.1}"
WORKDIR="${MCP_WORKDIR:-/workspace}"
ALLOWED="${MCP_ALLOWED_PREFIXES:-}"

# 检索 / 命令安全相关的可选变量。仅当外部显式传入且非空时 export，
# 避免空值覆盖 server.py 从 /app/.env 读取到的配置。
SEARCH_VARS=(
  MCP_SEARCH_TIMEOUT
  MCP_SEARCH_MAX_RESULTS
  MCP_SEARCH_MAX_BYTES
  MCP_SEARCH_RETRIES
  MCP_SEARCH_CACHE_TTL
  MCP_SEARCH_ARXIV_MIN_INTERVAL
  MCP_SEARCH_MAILTO
  MCP_SEARCH_WEB_PROVIDERS
  MCP_SEARCH_SEARXNG_URL
  MCP_SEARCH_TAVILY_KEY
  MCP_SEARCH_FETCH_ENABLED
  MCP_SEARCH_FETCH_MAX_CHARS
  MCP_SEARCH_FETCH_ALLOW_HOSTS
  MCP_DENY_PREFIXES
  MCP_REQUIRE_CONFIRM
)
for name in "${SEARCH_VARS[@]}"; do
  value="${!name:-}"
  if [ -n "$value" ]; then
    export "$name=$value"
  fi
done

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

# 写 .env 供 server.py 加载；可选变量只在显式配置时写入，
# 这样未配置的项会回落到 server.py 的默认值而不是空字符串。
{
  cat <<EOF
MCP_TOKEN=$MCP_TOKEN
MCP_PORT=$PORT
MCP_BIND=$BIND
MCP_WORKDIR=$WORKDIR
MCP_ALLOWED_PREFIXES=$ALLOWED
EOF
  for name in "${SEARCH_VARS[@]}"; do
    value="${!name:-}"
    if [ -n "$value" ]; then
      printf '%s=%s\n' "$name" "$value"
    fi
  done
} > /app/.env
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
