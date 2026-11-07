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
    # 镜像默认口令 admin；显式传入空串也回落到默认值。旧部署的共享卷里可能残留
    # 随机口令文件，升级后必须忽略而不是读取，否则固定口令不会生效。
    # 仍以 600 权限写入最终生效口令，保证既有「cat $PASS_FILE」取口令方式可用。
    if [ -z "${ADMIN_PASSWORD:-}" ]; then
      ADMIN_PASSWORD="admin"
      echo "[entrypoint] 未配置 ADMIN_PASSWORD，使用镜像默认口令 'admin'（仅限隔离环境）"
      echo "  建议启动时加 -e ADMIN_PASSWORD='<强口令>' 覆盖"
    fi
    (umask 077; printf '%s\n' "$ADMIN_PASSWORD" > "$PASS_FILE")
    export ADMIN_PASSWORD ADMIN_USERNAME
    if [ "${MCP_PRINT_SECRETS:-0}" = "1" ]; then
      echo "==================================================================="
      echo " Admin Web 管理员口令（已保存到 $PASS_FILE）"
      echo "   账号: $ADMIN_USERNAME"
      echo "   密码: $ADMIN_PASSWORD"
      echo "==================================================================="
    else
      echo "[entrypoint] 管理员口令已保存到 $PASS_FILE（默认不写入容器日志）"
      echo "  读取方式: docker exec <容器名> cat $PASS_FILE"
      echo "  如需在日志中显示，可设置 MCP_PRINT_SECRETS=1（仅建议在隔离环境使用）"
    fi
    echo "[entrypoint] 启动 Admin Web @ ${ADMIN_BIND:-0.0.0.0}:${ADMIN_PORT:-8788}"
    exec uvicorn gateway.admin.api:app \
      --host "${ADMIN_BIND:-0.0.0.0}" --port "${ADMIN_PORT:-8788}" --log-level info
    ;;

  maintenance)
    echo "[entrypoint] 启动 audit-storage 维护服务"
    exec python -m gateway.maintenance
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
  MCP_TOKEN="$(python -c 'import secrets,string; alphabet=string.ascii_letters+string.digits; print("".join(secrets.choice(alphabet) for _ in range(48)))')"
  if [ "${MCP_PRINT_SECRETS:-0}" = "1" ]; then
    echo "==================================================================="
    echo " [自动生成] MCP_TOKEN（重开容器会变化，请妥善保存）:"
    echo "   $MCP_TOKEN"
    echo "==================================================================="
  else
    echo "[entrypoint] 已生成 MCP_TOKEN 并写入 /app/.env（默认不写入容器日志）"
    echo "  读取方式: docker exec <容器名> cat /app/.env（例如 cnb-mcp-gateway-plus）"
  fi
fi
export MCP_TOKEN MCP_PORT="$PORT" MCP_BIND="$BIND" MCP_WORKDIR="$WORKDIR" MCP_ALLOWED_PREFIXES="$ALLOWED"

# 自动生成的令牌同步到共享卷，管理后台才能读取需鉴权的 /internal/metrics。
# 单容器模式没有该挂载点，跳过即可。
if [ -d /data/audit ]; then
  (umask 077; printf '%s\n' "$MCP_TOKEN" > /data/audit/.mcp_token)
fi

# 容器内没有宿主机 /etc/gitconfig：显式提供 Git 身份与凭据，
# 否则 git_commit 会报 "Please tell me who you are"、git_push 无法通过远端认证。
# 凭据请通过只读卷挂载文件，绝不要放进环境变量、命令参数或日志。
if [ -z "${GIT_CREDENTIALS_FILE:-}" ] && [ -f /root/.git-credentials ]; then
  GIT_CREDENTIALS_FILE=/root/.git-credentials
fi
if [ -n "${GIT_USER_NAME:-}" ]; then
  git config --global user.name "$GIT_USER_NAME"
fi
if [ -n "${GIT_USER_EMAIL:-}" ]; then
  git config --global user.email "$GIT_USER_EMAIL"
fi
if [ -n "${GIT_CREDENTIALS_FILE:-}" ] && [ -f "$GIT_CREDENTIALS_FILE" ]; then
  git config --global credential.helper "store --file=$GIT_CREDENTIALS_FILE"
fi

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
    if [ "${MCP_PRINT_SECRETS:-0}" = "1" ]; then
      echo " 密钥 (MCP_TOKEN):"
      echo "   $MCP_TOKEN"
    else
      echo " 密钥 (MCP_TOKEN): 已写入 /app/.env，默认不在日志打印"
      echo "   docker exec <容器名> cat /app/.env（例如 cnb-mcp-gateway-plus）"
    fi
    echo ""
    echo " 用法: ChatGPT 上传插件包(zip) 或 添加自定义 MCP 服务器 ${URL}/mcp"
    echo "       密钥在安装/连接时粘贴上面的 MCP_TOKEN"
    echo "==================================================================="
  else
    echo "[entrypoint] 未能解析隧道 URL，请查看: docker logs <容器名> 或 docker exec <容器名> tail -f /tmp/cloudflared.log"
  fi
fi

trap 'kill -TERM "$SERVER_PID" "$TUNNEL_PID" 2>/dev/null; wait' TERM INT
wait
