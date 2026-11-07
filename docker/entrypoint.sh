#!/usr/bin/env bash
set -euo pipefail

# 同一镜像按角色启动：MCP 网关 / 管理后台 / 审计维护 / 可选隧道。
ROLE="${MCP_SERVICE_ROLE:-gateway}"
cd /app

case "$ROLE" in
  admin-web)
    mkdir -p /data/audit
    PASS_FILE="${ADMIN_PASSWORD_FILE:-/data/audit/.admin_password}"
    ADMIN_USERNAME="${ADMIN_USERNAME:-admin}"
    if [ -z "${ADMIN_PASSWORD:-}" ]; then
      if [ -f "$PASS_FILE" ]; then
        ADMIN_PASSWORD="$(cat "$PASS_FILE")"
        echo "[entrypoint] 使用数据卷中保存的管理员口令: $PASS_FILE"
      else
        ADMIN_PASSWORD="$(python -c 'import secrets;print(secrets.token_urlsafe(24))')"
        (umask 077; printf '%s\n' "$ADMIN_PASSWORD" > "$PASS_FILE")
      fi
      echo "==================================================================="
      echo " [自动生成] Admin Web 管理员口令（已保存到 $PASS_FILE，重启不变）"
      echo "   账号: $ADMIN_USERNAME"
      echo "   密码: $ADMIN_PASSWORD"
      echo "==================================================================="
    fi
    export ADMIN_PASSWORD ADMIN_USERNAME
    echo "[entrypoint] 启动 Admin Web @ ${ADMIN_BIND:-0.0.0.0}:${ADMIN_PORT:-8788}"
    exec uvicorn gateway.admin.api:app \
      --host "${ADMIN_BIND:-0.0.0.0}" --port "${ADMIN_PORT:-8788}" --log-level info
    ;;

  maintenance)
    echo "[entrypoint] 启动 audit-storage 维护服务"
    exec python -m gateway.maintenance
    ;;

  host-agent)
    # Host Agent：仅 Unix Socket（0600）+ HMAC，不监听任何 TCP 端口。
    # 真实宿主权限由部署时显式挂载目录并注入共享密钥授予，脚本不自动提权。
    if [ -z "${MCP_HOST_AGENT_TOKEN:-}" ]; then
      echo "[entrypoint] MCP_HOST_AGENT_TOKEN 为空，拒绝以无认证方式启动 Host Agent" >&2
      exit 1
    fi
    echo "[entrypoint] 启动 Host Agent（unix socket，无公网端口）"
    exec python -m gateway.host.agent
    ;;

  cloudflared)
    UPSTREAM="${MCP_UPSTREAM_URL:-http://localhost:8765}"
    if [ -n "${CF_TUNNEL_TOKEN:-}" ]; then
      echo "[entrypoint] 命名隧道模式（固定 HTTPS 域名）"
      exec cloudflared tunnel run --token "$CF_TUNNEL_TOKEN"
    fi
    echo "[entrypoint] quick tunnel 模式（随机 HTTPS 域名），上游 $UPSTREAM"
    exec cloudflared tunnel --url "$UPSTREAM" --no-autoupdate
    ;;

  gateway)
    ;;

  *)
    echo "[entrypoint] 未知 MCP_SERVICE_ROLE=$ROLE" >&2
    exit 1
    ;;
esac

# 其余角色都在上面 exec 退出，只有 gateway 会执行到这里。
# 共享 audit-data 卷上的内部只读指标令牌：仅供后台请求 /health/details，
# 绝不把 MCP_TOKEN（远程 shell 钥匙）交给后台，也不写入 .env 或日志。
if [ "$ROLE" = "gateway" ]; then
  METRICS_FILE="${GATEWAY_METRICS_TOKEN_FILE:-/data/audit/.metrics_token}"
  mkdir -p "$(dirname "$METRICS_FILE")"
  if [ -n "${GATEWAY_METRICS_TOKEN:-}" ]; then
    (umask 077; printf '%s\n' "$GATEWAY_METRICS_TOKEN" > "$METRICS_FILE")
  elif [ ! -s "$METRICS_FILE" ]; then
    (umask 077; python -c 'import secrets;print(secrets.token_urlsafe(32))' > "$METRICS_FILE")
    echo "[entrypoint] 已生成内部指标令牌: $METRICS_FILE（0600，仅用于 /health/details）"
  fi
  chmod 600 "$METRICS_FILE"
fi

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
  MCP_SEARCH_PIN_DNS
  MCP_PAPER_DIR
  MCP_PAPER_MAX_MB
  MCP_DOWNLOAD_CONCURRENCY
  MCP_PAPER_TEXT_CACHE_DIR
  MCP_PAPER_EXTRA_HOSTS
  MCP_PAPER_ALLOW_PRIVATE
  SEARXNG_SECRET
  MCP_ACCESS_MODE
  MCP_DANGER_GUARD
  MCP_INDEPENDENT_CONFIRM
  MCP_EXEC_BACKEND
  MCP_HOST_AGENT_TOKEN
  MCP_HOST_AGENT_SOCKET
  MCP_HOST_AGENT_ROOTS
  MCP_TRANSFER_DIR
  MCP_TRANSFER_SECRET
  MCP_TRANSFER_ALLOW_PRIVATE
  TRANSFER_MAX_MB
  MCP_TASK_LOG_DIR
  MCP_DENY_PREFIXES
  MCP_REQUIRE_CONFIRM
  # 阶段 A：显式审计策略、磁盘 spool、OAuth 持久化与会话上限
  AUDIT_REQUIRED
  AUDIT_ALLOW_UNAUDITED
  AUDIT_SPOOL_ENABLED
  AUDIT_SPOOL_MAX_MB
  AUDIT_SPOOL_MAX_FILES
  OAUTH_PERSIST_ENABLED
  ADMIN_MAX_SESSIONS
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
  MCP_TOKEN="$(python -c 'import secrets,string;print("".join(secrets.choice(string.ascii_letters+string.digits) for _ in range(48)))')"
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
uvicorn server:app --host "$BIND" --port "$PORT" --log-level info &
SERVER_PID=$!

if [ "${MCP_TUNNEL_DISABLED:-0}" = "1" ]; then
  echo "[entrypoint] 已禁用内置 cloudflared；公网入口由独立 tunnel 服务提供"
  trap 'kill -TERM "$SERVER_PID" 2>/dev/null; wait' TERM INT
  wait "$SERVER_PID"
  exit 0
fi

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

trap 'kill -TERM "$SERVER_PID" "$TUNNEL_PID" 2>/dev/null; wait' TERM INT
wait
