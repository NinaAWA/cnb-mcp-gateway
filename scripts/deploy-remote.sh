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

# 仅清理当前部署所属的旧容器；同名但属于其他项目的容器一律不删除。
for name in cnb-mcp-gateway cnb-mcp-admin-web cnb-audit-storage \
            cnb-mcp-cloudflared cnb-mcp-postgres \
            cnb-mcp-gateway-tunnel cnb-mcp-admin-tunnel; do
  if docker container inspect "$name" >/dev/null 2>&1; then
    owner="$(docker inspect -f '{{ index .Config.Labels "com.docker.compose.project" }}' "$name")"
    [ "$owner" = "$PROJECT" ] || fail "容器 $name 属于项目 ${owner:-未知}；拒绝自动接管或删除"
  fi
done

# --remove-orphans 也会影响该 Compose 项目的历史服务；逐个核对工作目录。
managed_containers="$(docker ps -a --filter "label=com.docker.compose.project=$PROJECT" --format '{{.Names}}')" \
  || fail '无法列出旧 Compose 容器；中止以避免误删除'
while IFS= read -r container_name; do
  [ -n "$container_name" ] || continue
  deployed_from="$(docker inspect -f '{{ index .Config.Labels "com.docker.compose.project.working_dir" }}' "$container_name")" \
    || fail "无法核验容器 $container_name 的项目工作目录"
  [ "$deployed_from" = "$ROOT" ] \
    || fail "容器 $container_name 的部署目录为 ${deployed_from:-未知}，与 $ROOT 不同；拒绝清理"
done <<< "$managed_containers"

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

# Fail before stopping the old deployment if registry/network/build is broken.
# The previous curl -sSL could silently store a GitHub 504 HTML page as the
# cloudflared executable; now we require a real Cloudflare registry image.
printf '\n[部署] 预检：拉取 Cloudflare 官方镜像（失败不会删除旧服务）...\n'
tunnel_image='cloudflare/cloudflared:2026.10.7'
image_ready=0
for attempt in 1 2 3; do
  if docker pull "$tunnel_image"; then image_ready=1; break; fi
  printf '[部署] 官方镜像拉取失败：尝试 %s/3\n' "$attempt" >&2
  [ "$attempt" = 3 ] || sleep 3
done
[ "$image_ready" = 1 ] || fail '无法拉取 cloudflared 官方镜像；旧服务未清理'
docker run --rm --network none --entrypoint /usr/local/bin/cloudflared \
  "$tunnel_image" version >/dev/null \
  || fail 'Cloudflare 镜像无法运行有效 cloudflared 二进制；旧服务未清理'

printf '[部署] 预检：构建并校验新版 MCP 镜像（失败不会删除旧服务）...\n'
"${COMPOSE[@]}" build mcp-gateway \
  || fail '新版 MCP 镜像构建失败；旧服务未清理'

# Full container replacement without -v preserves credentials, task logs,
# uploaded files and audit database. Never touch the research workspace.
printf '\n[部署] 预检通过；停止并移除旧版 %s 服务容器（保留数据卷）...\n' "$PROJECT"
"${COMPOSE[@]}" down --remove-orphans
printf '[部署] 使用已校验镜像重新启动全部服务...\n'
"${COMPOSE[@]}" up -d --no-build --force-recreate

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
  local name="$1" url='' status=''
  for _ in $(seq 1 60); do
    # cloudflared prints its quick-tunnel URL only once. --tail 200 loses the
    # URL after verbose reconnect/precheck logs; inspect the complete log.
    url="$(docker logs "$name" 2>&1 | grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' | tail -n 1 || true)"
    if [ -n "$url" ]; then printf '%s' "$url"; return 0; fi
    status="$(docker inspect -f '{{.State.Status}}' "$name" 2>/dev/null || true)"
    if [ "$status" != running ]; then
      printf '隧道容器 %s 状态=%s；最近日志：\n' "$name" "${status:-unknown}" >&2
      docker logs --tail 30 "$name" >&2 || true
      return 1
    fi
    sleep 2
  done
  printf '隧道容器 %s 仍在运行，但未发现 trycloudflare.com 地址；最近日志：\n' "$name" >&2
  docker logs --tail 30 "$name" >&2 || true
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
