#!/usr/bin/env bash
# CNB MCP Gateway v1.1 — 原生 Linux/systemd 一键安装。
#
#   sudo ./scripts/install-native.sh                 # 首次安装
#   sudo ./scripts/install-native.sh --upgrade      # 升级代码，保留 .env 与数据
#   sudo ./scripts/install-native.sh --rollback     # 回滚到升级前备份的代码
#   sudo MCP_SERVICE_USER=mcp ./scripts/install-native.sh   # 降权运行（会失去 apt 等能力）
#
# 不绑定任何科研项目/目录/仓库；不引入 Redis/Celery/Kubernetes 等编排组件。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PREFIX="${MCP_PREFIX:-/opt/mcp-gateway}"
APP_DIR="$PREFIX/app"
VENV="$PREFIX/venv"
STATE_DIR="${MCP_STATE_DIR:-/var/lib/mcp-gateway}"
ENV_FILE="${MCP_ENV_FILE:-/etc/mcp-gateway/env}"
SERVICE_USER="${MCP_SERVICE_USER:-root}"
MODE="install"

usage() {
  sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'
}

while [ $# -gt 0 ]; do
  case "$1" in
    --upgrade) MODE="upgrade" ;;
    --rollback) MODE="rollback" ;;
    --help|-h) usage; exit 0 ;;
    *) echo "未知参数: $1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

if [ "$(id -u)" -ne 0 ]; then
  echo "需要 root 权限（安装系统包与 systemd 单元）；请用 sudo 运行。" >&2
  exit 1
fi

if [ "$MODE" = "rollback" ]; then
  if [ ! -d "$APP_DIR.previous" ]; then
    echo "没有可回滚的代码备份：$APP_DIR.previous" >&2
    exit 1
  fi
  rm -rf "$APP_DIR"
  mv "$APP_DIR.previous" "$APP_DIR"
  systemctl restart mcp-gateway.service mcp-admin-web.service
  echo "已回滚到备份代码并重启服务；数据卷与 $ENV_FILE 保持不变。"
  echo "如需回退镜像部署，把 MCP_IMAGE 改回上一可用标签后重新 up -d。"
  exit 0
fi

# ---------------------------------------------------------------- 系统依赖
. /etc/os-release
ID_LIKE="${ID_LIKE:-$ID}"
PKGS="python3 python3-venv python3-pip git git-lfs curl ca-certificates procps"
case "$ID_LIKE" in
  *debian*|*ubuntu*)
    apt-get update -qq
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends $PKGS
    ;;
  *rhel*|*fedora*|*centos*)
    PKGMGR="$(command -v dnf || command -v yum)"
    $PKGMGR install -y python3 python3-pip git git-lfs curl ca-certificates procps-ng
    ;;
  *arch*)
    pacman -Sy --noconfirm python git git-lfs curl ca-certificates procps-ng
    ;;
  *alpine*)
    apk add --no-cache python3 py3-pip py3-virtualenv git git-lfs curl ca-certificates procps
    ;;
  *)
    echo "未识别的发行版 ($ID_LIKE)；请手动安装：python3 venv pip git git-lfs curl 后重跑。" >&2
    exit 1
    ;;
esac

if command -v git-lfs >/dev/null 2>&1; then
  git-lfs install --system >/dev/null 2>&1 || {
    echo "[提示] git-lfs 系统级初始化失败；可在 MCP 里执行 run_command('git lfs install')" >&2
    echo "        为仓库补装 LFS 钩子（网关已放行该命令，不会触发 Git 删除保护）。" >&2
  }
fi

# ---------------------------------------------------------------- 目录与代码
mkdir -p "$PREFIX" "$STATE_DIR/files" "$STATE_DIR/task-logs" "$STATE_DIR/audit/exports" \
         "$STATE_DIR/workspace" "$(dirname "$ENV_FILE")"
chmod 700 "$(dirname "$ENV_FILE")"

if [ "$MODE" = "upgrade" ] && [ -d "$APP_DIR" ]; then
  rm -rf "$APP_DIR.previous"
  cp -a "$APP_DIR" "$APP_DIR.previous"
  echo "已备份旧代码到 $APP_DIR.previous（--rollback 可恢复）"
fi

if [ ! -x "$VENV/bin/python" ]; then
  python3 -m venv "$VENV"
fi
"$VENV/bin/pip" install --quiet --upgrade pip
"$VENV/bin/pip" install --quiet -r "$ROOT/docker/requirements.txt"

rm -rf "$APP_DIR"
mkdir -p "$APP_DIR"
cp "$ROOT/docker/server.py" "$APP_DIR/server.py"
cp -a "$ROOT/gateway" "$APP_DIR/gateway"
chmod 755 "$APP_DIR/server.py"

# ---------------------------------------------------------------- 配置与密钥
if [ ! -f "$ENV_FILE" ]; then
  MCP_TOKEN="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
  ADMIN_PASSWORD="$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')"
  SESSION_SECRET="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
  umask 077
  cat > "$ENV_FILE" <<EOF
MCP_TOKEN=$MCP_TOKEN
ADMIN_PASSWORD=$ADMIN_PASSWORD
ADMIN_SESSION_SECRET=$SESSION_SECRET
MCP_PORT=8765
MCP_BIND=127.0.0.1
MCP_WORKDIR=$STATE_DIR/workspace
MCP_ALLOWED_PREFIXES=
MCP_MAX_OUTPUT_BYTES=2000000
MCP_FILE_ROOT=$STATE_DIR/files
MCP_FILE_CHUNK_BYTES=1048576
MCP_TASK_LOG_DIR=$STATE_DIR/task-logs
MCP_PUBLIC_BASE_URL=${MCP_PUBLIC_BASE_URL:-}
AUDIT_DB_PATH=$STATE_DIR/audit/audit.db
AUDIT_EXPORT_DIR=$STATE_DIR/audit/exports
ADMIN_BIND=127.0.0.1
ADMIN_PORT=8788
ADMIN_COOKIE_SECURE=${ADMIN_COOKIE_SECURE:-0}
EOF
  chmod 600 "$ENV_FILE"
  echo "=============================================================="
  echo " 已生成配置：$ENV_FILE（600 权限，未写入本仓库）"
  echo " MCP_TOKEN=$MCP_TOKEN"
  echo " ADMIN_PASSWORD=$ADMIN_PASSWORD"
  echo " 请立即保存，之后可用 sudo cat $ENV_FILE 查看。"
  echo "=============================================================="
else
  echo "保留已有配置：$ENV_FILE"
fi

# ---------------------------------------------------------------- systemd
for unit in mcp-gateway mcp-admin-web; do
  target="/etc/systemd/system/$unit.service"
  if [ "$SERVICE_USER" = "root" ]; then
    cp "$ROOT/deploy/$unit.service" "$target"
  else
    id "$SERVICE_USER" >/dev/null 2>&1 || useradd --system --create-home "$SERVICE_USER"
    chown -R "$SERVICE_USER":"$SERVICE_USER" "$STATE_DIR"
    sed "s/^User=root$/User=$SERVICE_USER/" "$ROOT/deploy/$unit.service" > "$target"
  fi
  chmod 644 "$target"
done

systemctl daemon-reload
systemctl enable --now mcp-gateway.service mcp-admin-web.service
systemctl restart mcp-gateway.service mcp-admin-web.service

echo
echo "安装完成（$MODE）。"
echo "  MCP:        http://127.0.0.1:8765/mcp   （Bearer /run_command）"
echo "  Admin Web:  http://127.0.0.1:8788/      （独立管理员登录）"
echo "  状态检查:   systemctl status mcp-gateway mcp-admin-web"
echo "  公网 HTTPS: 参考 deploy/reverse-proxy.example.conf 配置反代，并设置 MCP_PUBLIC_BASE_URL"
echo "  升级/回滚:  $0 --upgrade / --rollback"
echo
echo "安全边界：原生部署直接复用宿主机已有软件、GPU、驱动与 Docker。"
echo "  默认以 root 运行即默认管理员模式；如设置 MCP_SERVICE_USER 降权，apt/docker 等"
echo "  操作会失败，需要自行配置免密 sudo 与 docker 组。原生部署不替换 /usr/bin/git，"
echo "  因此 Git 删除保护由 check_command + 结构化工具 check_argv 两层提供（无容器 wrapper）。"
echo "  /var/run/docker.sock 的写权限等价于宿主机 root，请务必给公网入口配置 HTTPS 与强令牌。"
