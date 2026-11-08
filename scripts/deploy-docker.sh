#!/usr/bin/env bash
# Safe, repeatable Docker Compose deployment of CNB MCP Gateway.
# Never removes containers or data volumes. Refuses name collisions with another project.
set -euo pipefail

REPO_URL="${MCP_REPO_URL:-https://github.com/NinaAWA/cnb-mcp-gateway.git}"
DEST="${MCP_DEPLOY_DIR:-/workspace/mcp-gateway-v11}"
PROJECT="${COMPOSE_PROJECT_NAME:-mcp-gateway-v11}"
export COMPOSE_PROJECT_NAME="$PROJECT"

die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
command -v git >/dev/null || die "git is required"
command -v docker >/dev/null || die "docker is required"
command -v curl >/dev/null || die "curl is required"
docker compose version >/dev/null 2>&1 || die "docker compose v2 is required"
case "$DEST" in /*) ;; *) die "MCP_DEPLOY_DIR must be an absolute path";; esac
case "$PROJECT" in *[!a-zA-Z0-9_-]*|"") die "Invalid COMPOSE_PROJECT_NAME";; esac

# Fixed container_name values in upstream Compose are a collision risk.
# Container labels, not names, establish ownership.
for name in cnb-mcp-gateway cnb-mcp-admin-web cnb-audit-storage \
            cnb-mcp-cloudflared cnb-mcp-postgres; do
    if docker container inspect "$name" >/dev/null 2>&1; then
        owner="$(docker inspect -f '{{ index .Config.Labels "com.docker.compose.project" }}' "$name")"
        if [ "$owner" != "$PROJECT" ]; then
            die "Container '$name' belongs to project '${owner:-unknown}', not '$PROJECT'. Refusing to stop/remove it. Resolve manually or use the correct COMPOSE_PROJECT_NAME."
        fi
    fi
done

if [ -d "$DEST/.git" ]; then
    git -C "$DEST" pull --ff-only
elif [ -e "$DEST" ]; then
    die "Destination '$DEST' exists but is not a Git checkout"
else
    git clone "$REPO_URL" "$DEST"
fi

cd "$DEST"
test -f .env.example || die "Missing .env.example"
test -f docker-compose.yml || die "Missing docker-compose.yml"
if [ ! -e .env ]; then
    (umask 077; cp .env.example .env)
    printf 'Created .env with restricted permissions\n'
fi

docker compose config --quiet
docker compose up -d --build

ready=0
for _ in $(seq 1 60); do
    if curl -fsS http://127.0.0.1:8765/health >/dev/null 2>&1 &&
       curl -fsS http://127.0.0.1:8788/healthz >/dev/null 2>&1; then
        ready=1
        break
    fi
    sleep 2
done
[ "$ready" = 1 ] || die "Gateway/admin did not become healthy; inspect docker compose logs"

bash scripts/show-config.sh --mode docker
printf 'Deployment complete; no foreign containers or data volumes were removed.\n'
