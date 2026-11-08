#!/usr/bin/env bash
set -euo pipefail

REPO='https://github.com/NinaAWA/cnb-mcp-gateway.git'
ROOT="${MCP_DEPLOY_DIR:-/workspace/mcp-gateway-v11}"
PROJECT='mcp-gateway-v11'
export COMPOSE_PROJECT_NAME="$PROJECT"
fail() { printf '\n部署终止：%s\n' "$*" >&2; exit 1; }

for cmd in git docker curl python3; do
  command -v "$cmd" >/dev/null 2>&1 || fail "缺少命令：$cmd"
done
docker compose version >/dev/null 2>&1 || fail '需要 Docker Compose v2'
[ -S /var/run/docker.sock ] || fail '未发现 /var/run/docker.sock；当前模式需要访问宿主机 Docker'
case "$ROOT" in /*) ;; *) fail 'MCP_DEPLOY_DIR 必须是绝对路径' ;; esac

# 固定容器名不能接管其他 Compose 项目；绝不自动删除旧容器或数据卷。
for name in cnb-mcp-gateway cnb-mcp-admin-web cnb-audit-storage \
            cnb-mcp-cloudflared cnb-mcp-postgres \
            cnb-mcp-gateway-tunnel cnb-mcp-admin-tunnel; do
  if docker container inspect "$name" >/dev/null 2>&1; then
    owner="$(docker inspect -f '{{ index .Config.Labels "com.docker.compose.project" }}' "$name")"
    [ "$owner" = "$PROJECT" ] || fail "容器 $name 属于项目 ${owner:-未知}；拒绝自动接管或删除"
  fi
done

if [ -d "$ROOT/.git" ]; then
  [ "$(git -C "$ROOT" branch --show-current)" = main ] || fail '现有部署目录不在 main 分支'
  git -C "$ROOT" fetch origin main
  git -C "$ROOT" merge --ff-only origin/main
elif [ -e "$ROOT" ]; then
  fail "路径 $ROOT 已存在，但不是 Git 仓库"
else
  git clone --branch main --single-branch "$REPO" "$ROOT"
fi
cd "$ROOT"
[ -f .env ] || (umask 077; cp .env.example .env)

# 运行态模式：映射真实宿主工作区和 Docker Socket，并创建两个独立公网隧道。
export MCP_HOST_WORKSPACE="${MCP_HOST_WORKSPACE:-/workspace}"
export MCP_HOST_DOCKER_CLI="$(command -v docker)"
[ -d "$MCP_HOST_WORKSPACE" ] || fail "宿主工作区不存在：$MCP_HOST_WORKSPACE"
COMPOSE=(docker compose -p "$PROJECT" -f docker-compose.yml -f docker-compose.runtime.yml)
"${COMPOSE[@]}" config --quiet
"${COMPOSE[@]}" up -d --build

# 先确认本地两个服务都确实启动，再读取 Cloudflare 分配的真实域名。
ready=0
for _ in $(seq 1 60); do
  if curl -fsS --max-time 3 http://127.0.0.1:8765/health >/dev/null 2>&1 &&
     curl -fsS --max-time 3 http://127.0.0.1:8788/healthz >/dev/null 2>&1; then
    ready=1; break
  fi
  sleep 2
done
[ "$ready" = 1 ] || fail '本地健康检查失败；查看 docker compose logs'

get_tunnel() {
  local name="$1" url=''
  for _ in $(seq 1 60); do
    url="$(docker logs --tail 200 "$name" 2>&1 | grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' | tail -n 1 || true)"
    if [ -n "$url" ]; then printf '%s' "$url"; return 0; fi
    sleep 2
  done
  return 1
}
MCP_PUBLIC="$(get_tunnel cnb-mcp-gateway-tunnel)" || fail 'MCP 公网隧道没有生成 HTTPS 地址'
ADMIN_PUBLIC="$(get_tunnel cnb-mcp-admin-tunnel)" || fail '管理后台公网隧道没有生成 HTTPS 地址'

# 写回公网域名，避免文件下载链接错误地指向 127.0.0.1；同时为 HTTPS 启用 Secure Cookie。
python3 - "$MCP_PUBLIC" "$ADMIN_PUBLIC" <<'PY'
import os, pathlib, sys
p = pathlib.Path('.env')
changes = {'MCP_PUBLIC_BASE_URL': sys.argv[1],
           'ADMIN_PUBLIC_BASE_URL': sys.argv[2],
           'ADMIN_COOKIE_SECURE': '1'}
lines = p.read_text(encoding='utf-8').splitlines()
seen = set()
out = []
for line in lines:
    key = line.split('=', 1)[0].strip()
    if key in changes:
        if key not in seen:
            out.append(f'{key}={changes[key]}')
            seen.add(key)
    else:
        out.append(line)
for key, value in changes.items():
    if key not in seen:
        out.append(f'{key}={value}')
tmp = p.with_name('.env.next')
tmp.write_text('\n'.join(out) + '\n', encoding='utf-8')
os.chmod(tmp, 0o600)
os.replace(tmp, p)
PY

# 只重建应用，保持两条隧道容器运行，避免分配的新域名失效。
"${COMPOSE[@]}" up -d --no-deps --force-recreate mcp-gateway mcp-admin-web

# 只有实际公网入口也能访问时才声明部署成功。
remote_ok=0
for _ in $(seq 1 30); do
  if curl -fsS --max-time 8 "$MCP_PUBLIC/health" >/dev/null 2>&1 &&
     curl -fsS --max-time 8 "$ADMIN_PUBLIC/healthz" >/dev/null 2>&1; then
    remote_ok=1; break
  fi
  sleep 2
done
[ "$remote_ok" = 1 ] || fail '隧道已生成但公网健康检查失败；暂不宣称远程可访问'

printf '\n==================== 部署成功 ====================\n'
bash scripts/show-config.sh --mode docker
