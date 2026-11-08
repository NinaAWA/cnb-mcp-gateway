#!/usr/bin/env bash
# CNB MCP Gateway v1.1 — 只读打印部署后的连接信息。
#
#   ./scripts/show-config.sh                       # 自动探测原生 systemd / Docker Compose
#   ./scripts/show-config.sh --mode docker        # 强制 Docker Compose 模式
#   ./scripts/show-config.sh --mode native        # 强制原生 systemd 模式
#   ./scripts/show-config.sh --help
#
# 环境变量覆盖：
#   MCP_ENV_FILE          原生部署配置文件（默认 /etc/mcp-gateway/env）
#   MCP_CONTAINER         网关容器名（默认 cnb-mcp-gateway）
#   MCP_ADMIN_CONTAINER   后台容器名（默认 cnb-mcp-admin-web）
#   MCP_PUBLIC_BASE_URL   固定公网 HTTPS 基址，如 https://mcp.example.com
#   ADMIN_PUBLIC_BASE_URL 管理网站固定公网基址
#
# 只读：不写文件、不启停容器、不改环境变量，也不对配置文件求值。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MODE="auto"
MCP_ENV_FILE="${MCP_ENV_FILE:-/etc/mcp-gateway/env}"
MCP_CONTAINER="${MCP_CONTAINER:-cnb-mcp-gateway}"
MCP_ADMIN_CONTAINER="${MCP_ADMIN_CONTAINER:-cnb-mcp-admin-web}"
DOTENV_FILE="$ROOT/.env"

usage() {
  sed -n '2,16p' "$0" | sed 's/^# \{0,1\}//'
}

die() {
  printf '错误：%s\n' "$1" >&2
  exit 1
}

while [ $# -gt 0 ]; do
  case "$1" in
    --mode)
      [ $# -ge 2 ] || die "--mode 需要参数 native|docker|auto"
      MODE="$2"
      shift 2
      ;;
    --mode=*)
      MODE="${1#--mode=}"
      shift
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    *)
      printf '未知参数: %s\n' "$1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

case "$MODE" in
  native|docker|auto) ;;
  *) die "--mode 只支持 native|docker|auto，收到: $MODE" ;;
esac

trim() {
  local s="$1"
  s="${s%$'\r'}"
  s="${s#"${s%%[![:space:]]*}"}"
  s="${s%"${s##*[![:space:]]}"}"
  if [ "${#s}" -ge 2 ]; then
    case "$s" in
      \"*\") s="${s:1:${#s}-2}" ;;
      \'*\') s="${s:1:${#s}-2}" ;;
    esac
  fi
  printf '%s' "$s"
}

# 逐键提取：配置文件内容只作为文本流过 sed，绝不对它求值。
env_value_text() {
  local key="$1" text="$2" value
  value="$(printf '%s\n' "$text" | sed -n "s/^${key}=//p" | head -n 1 || true)"
  trim "$value"
}

file_text() {
  if [ -r "$1" ]; then
    cat "$1"
  fi
}

container_running() {
  command -v docker >/dev/null 2>&1 || return 1
  [ "$(docker inspect -f '{{.State.Running}}' "$1" 2>/dev/null || true)" = "true" ]
}

docker_read() {
  docker exec "$1" cat "$2" 2>/dev/null || true
}

json_escape() {
  local s="$1"
  s="${s//\\/\\\\}"
  s="${s//\"/\\\"}"
  s="${s//$'\n'/\\n}"
  s="${s//$'\r'/\\r}"
  s="${s//$'\t'/\\t}"
  printf '%s' "$s"
}

display_host() {
  case "$1" in
    ""|0.0.0.0|::) printf '127.0.0.1' ;;
    *) printf '%s' "$1" ;;
  esac
}

CONFIG_TEXT=""
DOTENV_TEXT=""
TOKEN=""
ADMIN_PASSWORD_VALUE=""

if [ "$MODE" = "auto" ]; then
  if [ -r "$MCP_ENV_FILE" ]; then
    MODE_RESOLVED="native"
  elif container_running "$MCP_CONTAINER"; then
    MODE_RESOLVED="docker"
  else
    die "未发现已部署实例：找不到原生配置 $MCP_ENV_FILE，也找不到运行中的容器 $MCP_CONTAINER。请先部署，或用 --mode 指定。"
  fi
else
  MODE_RESOLVED="$MODE"
fi

if [ "$MODE_RESOLVED" = "native" ]; then
  [ -r "$MCP_ENV_FILE" ] || die "找不到原生部署配置 $MCP_ENV_FILE（可用 MCP_ENV_FILE 指定路径）。"
  CONFIG_TEXT="$(file_text "$MCP_ENV_FILE")"
  DOTENV_TEXT="$(file_text "$DOTENV_FILE")"
  TOKEN="$(env_value_text MCP_TOKEN "$CONFIG_TEXT")"
  ADMIN_PASSWORD_VALUE="$(env_value_text ADMIN_PASSWORD "$CONFIG_TEXT")"
else
  container_running "$MCP_CONTAINER" || die "容器 $MCP_CONTAINER 未运行（可用 MCP_CONTAINER 覆盖）。请先执行 docker compose up -d --build。"
  CONFIG_TEXT="$(docker_read "$MCP_CONTAINER" /app/.env)"
  DOTENV_TEXT="$(file_text "$DOTENV_FILE")"
  TOKEN="$(env_value_text MCP_TOKEN "$CONFIG_TEXT")"
  if [ -z "$TOKEN" ]; then
    TOKEN="$(trim "$(docker_read "$MCP_CONTAINER" /data/audit/.mcp_token)")"
  fi
  if container_running "$MCP_ADMIN_CONTAINER"; then
    ADMIN_PASSWORD_VALUE="$(trim "$(docker_read "$MCP_ADMIN_CONTAINER" /data/audit/.admin_password)")"
  fi
  if [ -z "$ADMIN_PASSWORD_VALUE" ]; then
    ADMIN_PASSWORD_VALUE="$(env_value_text ADMIN_PASSWORD "$DOTENV_TEXT")"
  fi
fi

[ -n "$TOKEN" ] || die "未能读取 MCP_TOKEN；请确认服务已就绪，且容器内 /app/.env 或 $MCP_ENV_FILE 可读。"
[ -n "$ADMIN_PASSWORD_VALUE" ] || die "未能读取管理员口令；请确认 /data/audit/.admin_password 或 $MCP_ENV_FILE 可读。"

# 优先级：显式环境变量 > 生效配置 > 宿主机 .env > 内置默认值。
setting() {
  local key="$1" default="$2" v
  if [ -n "${!key:-}" ]; then
    printf '%s' "${!key}"
    return
  fi
  v="$(env_value_text "$key" "$CONFIG_TEXT")"
  if [ -n "$v" ]; then printf '%s' "$v"; return; fi
  v="$(env_value_text "$key" "$DOTENV_TEXT")"
  if [ -n "$v" ]; then printf '%s' "$v"; return; fi
  printf '%s' "$default"
}

ADMIN_USER_VALUE="$(setting ADMIN_USERNAME admin)"
PUBLIC_BASE="$(setting MCP_PUBLIC_BASE_URL '')"
ADMIN_PUBLIC="$(setting ADMIN_PUBLIC_BASE_URL '')"
TUNNEL_DISABLED="$(setting MCP_TUNNEL_DISABLED 0)"

if [ "$MODE_RESOLVED" = "docker" ]; then
  MCP_PORT_NUM="$(setting MCP_HOST_PORT 8765)"
  ADMIN_PORT_NUM="$(setting ADMIN_HOST_PORT 8788)"
  MCP_BIND_VAL="$(setting MCP_BIND_HOST 127.0.0.1)"
  ADMIN_BIND_VAL="$(setting ADMIN_BIND_HOST 127.0.0.1)"
else
  MCP_PORT_NUM="$(setting MCP_PORT 8765)"
  ADMIN_PORT_NUM="$(setting ADMIN_PORT 8788)"
  MCP_BIND_VAL="$(setting MCP_BIND 127.0.0.1)"
  ADMIN_BIND_VAL="$(setting ADMIN_BIND 127.0.0.1)"
fi

LOCAL_BASE="http://$(display_host "$MCP_BIND_VAL"):${MCP_PORT_NUM}"
TUNNEL_URL=""
if [ -n "$PUBLIC_BASE" ]; then
  BASE="${PUBLIC_BASE%/}"
  ENTRY_KIND="固定公网入口（来自 MCP_PUBLIC_BASE_URL）"
  ENTRY_NOTE="已配置固定 HTTPS 基址；请确保反向代理或命名隧道把该域名转发到本机 ${MCP_PORT_NUM} 端口。"
elif [ "$TUNNEL_DISABLED" != "1" ] && [ "$MODE_RESOLVED" = "docker" ]; then
  # 隧道域名由 cloudflared 异步写入容器日志；部署刚完成时轮询等待，避免打印 127.0.0.1。
  for i in $(seq 1 30); do
    TUNNEL_URL="$(docker logs "$MCP_CONTAINER" 2>&1 | grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' | head -n 1 || true)"
    [ -n "$TUNNEL_URL" ] && break
    sleep 2
  done
  if [ -n "$TUNNEL_URL" ]; then
    BASE="$TUNNEL_URL"
    ENTRY_KIND="随机公网隧道（可远程连接；域名在容器重建后会变化）"
    ENTRY_NOTE="已把默认管理员模式网关暴露到公网；请确认使用强令牌，并尽快改用固定 HTTPS 入口。"
  else
    BASE="$LOCAL_BASE"
    ENTRY_KIND="仅本机或内网可达（已启用隧道但未解析到公网地址）"
    ENTRY_NOTE="未从容器日志解析到 trycloudflare 地址，请查看 docker logs $MCP_CONTAINER；未伪造公网网址。"
  fi
else
  BASE="$LOCAL_BASE"
  ENTRY_KIND="仅本机或内网可达，未配置公网入口"
  ENTRY_NOTE="默认只把端口发布到 127.0.0.1；如需公网 HTTPS，见下方 [6]。"
fi
MCP_URL="${BASE}/mcp"

if [ -n "$ADMIN_PUBLIC" ]; then
  ADMIN_URL="${ADMIN_PUBLIC%/}/"
  ADMIN_NOTE="管理网站固定公网入口（来自 ADMIN_PUBLIC_BASE_URL）"
else
  ADMIN_URL="http://$(display_host "$ADMIN_BIND_VAL"):${ADMIN_PORT_NUM}/"
  ADMIN_NOTE="只绑定 127.0.0.1；远程访问需按 deploy/reverse-proxy.example.conf 配置 admin 域名反代"
fi

CLIENT_JSON="{\"mcpServers\":{\"cnb-gateway\":{\"url\":\"$(json_escape "$MCP_URL")\",\"headers\":{\"Authorization\":\"Bearer $(json_escape "$TOKEN")\"}}}}"

printf '%s\n' \
  "======================================================================" \
  " CNB MCP Gateway v1.1 · 连接信息（只读生成；部署形态：$MODE_RESOLVED）" \
  "======================================================================" \
  "[1] 入口类型      $ENTRY_KIND" \
  "    说明          $ENTRY_NOTE" \
  "" \
  "[2] MCP 服务地址  $MCP_URL" \
  "    Bearer Token  $TOKEN" \
  "    健康检查      http://127.0.0.1:${MCP_PORT_NUM}/health" \
  "" \
  "[3] MCP 客户端配置（一行，可直接复制）" \
  "$CLIENT_JSON" \
  "" \
  "[4] 管理网站      $ADMIN_URL" \
  "    说明          $ADMIN_NOTE" \
  "    管理员账号    $ADMIN_USER_VALUE" \
  "    管理员密码    $ADMIN_PASSWORD_VALUE" \
  "    健康检查      http://127.0.0.1:${ADMIN_PORT_NUM}/healthz" \
  "" \
  "[5] 重新打印      $ROOT/scripts/show-config.sh" \
  "                  （--mode docker / --mode native 可强制指定部署形态）" \
  "" \
  "[6] 升级为固定公网 HTTPS（随机隧道域名每次重建会变）" \
  "    在 .env 设置 MCP_PUBLIC_BASE_URL=https://mcp.example.com，按" \
  "    deploy/reverse-proxy.example.conf 配置 nginx 反代；或填" \
  "    CF_TUNNEL_TOKEN 并设 MCP_TUNNEL_DISABLED=1，再执行" \
  "    docker compose --profile tunnel up -d。管理网站用" \
  "    ADMIN_PUBLIC_BASE_URL 单独指定。关闭公网入口请设" \
  "    MCP_TUNNEL_DISABLED=1 并重建；网关是默认管理员模式，务必用强令牌。" \
  "----------------------------------------------------------------------" \
  "  警告：本块含明文 Bearer Token 与管理员口令，请勿粘贴到公开工单、日志或聊天。" \
  "======================================================================"
