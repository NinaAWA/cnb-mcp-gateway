# CNB MCP Gateway

> 📖 English | [中文文档](./README.zh-CN.md)

A self-hosted **MCP (Model Context Protocol) server** that exposes a controlled
toolkit — shell commands, files, processes, images, plus web and academic paper
search — on your own machine/server, reachable by **any** MCP client (Claude
Desktop, Cursor, VS Code, Codex, ChatGPT connectors, or your own agents) over
the public internet via a Cloudflare quick tunnel.

```
 your server  ──►  commands / files / processes / images / web & paper search  ◄──  any MCP client
   │
   └─ cloudflared quick tunnel (random https URL) ◄── auth: Bearer token / OAuth2 PKCE
```

## v1.1 — generic self-hosted Linux remote control

Two deployment modes, one tool surface. Nothing is bound to a particular research
project, directory, repository or dependency stack; no task orchestration, no
multi-tenancy, no Redis/Celery/Kubernetes.

- **Default administrator mode.** `MCP_ALLOWED_PREFIXES` is now only an *optional*
  tightening switch — leaving it empty allows all everyday admin commands
  (`apt`/`dpkg`, `pip`/`uv`/`conda`, `bash`, Python, `gcc`/`cmake`, Git/Git LFS,
  Docker/Compose). Permanently retained: Bearer/OAuth authentication, full audit,
  hard-blocked Git branch/repository deletion (including shell, Python and encoded
  bypasses), catastrophic blacklist, and the `confirm=true` gate for destructive
  commands. `run_command`, `run_command_background` and REST `/run_command` all
  share one `_authorize()` gate, so rules cannot drift between entry points.
- **Honest command results.** `run_command` returns `exit_code`, `duration_ms`,
  `timed_out`, `stdout`, `stderr`, and accepts `env` + `stdin`. Output is capped by
  `MCP_MAX_OUTPUT_BYTES`. A timeout returns convention code `124` and explicitly
  states that it is not the process's real exit code. `env` values, `stdin` and
  chunk payloads are never written to the audit log.
- **Binary file transfer.** `upload_file` / `download_file` / `file_info` /
  `file_checksum`: base64 chunks (1 MiB default), `.part` staging, streaming
  SHA-256, atomic `os.replace()`, overwrite detection and failure rollback that
  keeps the original file. `download_file` also returns an absolute HMAC-signed,
  short-lived link (`GET /files/download`) that works in a browser or `curl` and
  supports HTTP `Range`. All paths are confined to `MCP_FILE_ROOT`; `.git`,
  `.ssh`, `.env`, private keys and token files are refused.
- **Git.** 19 structured, `shell=False` tools. `git_add` takes an explicit path
  array (`.` and wildcards are refused). `git_push` verifies with `git rev-parse
  HEAD` + `git ls-remote` before reporting success. New: `git_merge`,
  `git_rebase` (safe forms), `git_stash`, `git_show`, `git_cherry_pick`,
  `git_checkout`. Deleting a local/remote branch, forced refspec rewriting and
  `git lfs prune`/`uninstall`/`migrate` are always denied. The `.git` URL false
  positive that broke `sh -c 'git lfs pre-push ... repo.git'` is fixed.
- **Long tasks.** Metadata lives in SQLite WAL (`mcp_tasks`) and logs in
  `MCP_TASK_LOG_DIR`, so logs stay readable after a restart. A `running` task
  whose process cannot be confirmed (pid + `/proc` start time) is marked
  `unknown` with a NULL exit code — **never** `done` or success. Cancellation
  uses the same pid identity check to avoid signalling a reused pid.
- **Docker.** Normal Docker operations are covered by `run_command`; an optional
  lightweight `docker_status` gives a status view. In container deployments the
  host Docker socket is **not** mounted by default.
- **Admin Web.** Home dashboard, full call log, background tasks, Git records and
  file transfer records; disk metrics for the managed file root. All views read
  the same redacted audit store: no passwords, private keys, full tokens, `env`
  values, `stdin`, base64 chunks or signature material.

### Deploy v1.1 — one command, nothing to pre-fill

> **Which `main`?** Only GitHub `main` contains v1.1. CNB `main` is still commit
> `9266f9f` and has no `.env.example` / `scripts/` — cloning it fails with
> `cp: cannot stat '.env.example'`. Use the GitHub URL below. If you must use CNB:
> `git clone -b feat/mcp-v1.1 https://cnb.cool/webgamer-2026/cnb-mcp-gateway.git`.
> No `sudo` is needed when already root, and the Docker path needs no systemd.

**Docker / Compose** — paste the whole block. It deploys, waits for both health
endpoints, then prints every value you need to copy:

```bash
(
set -e
DEST=/workspace/mcp-gateway-v11

# Remove any previous deployment first: container names are hardcoded in
# docker-compose.yml, so an old instance from another Compose project conflicts.
for name in cnb-mcp-gateway cnb-mcp-admin-web cnb-audit-storage cnb-mcp-cloudflared cnb-mcp-postgres; do
  docker rm -f "$name" >/dev/null 2>&1 || true
done

if [ -d "$DEST/.git" ]; then git -C "$DEST" pull --ff-only; else git clone https://github.com/NinaAWA/cnb-mcp-gateway.git "$DEST"; fi
cd "$DEST"
for f in .env.example docker-compose.yml; do [ -f "$f" ] || { echo "缺少 $f：请确认代码来源包含 v1.1"; exit 1; }; done
# PURGE_DATA=1 also deletes named volumes (audit records, files, generated credentials).
if [ "${PURGE_DATA:-0}" = "1" ]; then docker compose down -v --remove-orphans; fi
[ -f .env ] || cp .env.example .env && docker compose up -d --build
for i in $(seq 1 60); do curl -fsS http://127.0.0.1:8765/health >/dev/null 2>&1 && curl -fsS http://127.0.0.1:8788/healthz >/dev/null 2>&1 && break; sleep 2; done
if [ -f scripts/show-config.sh ]; then
  bash scripts/show-config.sh
else
  # A main without show-config.sh: print the same values inline (default ports).
  TOKEN="$(docker exec cnb-mcp-gateway sed -n 's/^MCP_TOKEN=//p' /app/.env | head -1)"
  PASS="$(docker exec cnb-mcp-admin-web cat /data/audit/.admin_password)"
  printf 'MCP_URL    http://127.0.0.1:8765/mcp\nBearer     %s\nClient config:\n{\n  "mcpServers": {\n    "cnb-gateway": {\n      "url": "http://127.0.0.1:8765/mcp",\n      "headers": {\n        "Authorization": "Bearer %s"\n      }\n    }\n  }\n}\nAdmin Web  http://127.0.0.1:8788/\nUsername   admin\nPassword   %s\n' "$TOKEN" "$TOKEN" "$PASS"
fi
)
```

The printed block contains the **public** MCP URL (a random
`https://<name>.trycloudflare.com/mcp` by default, reachable by remote agents),
Bearer token, a formatted, indented `mcpServers` JSON block, the Admin Web URL, admin
username and password, and both health checks. Re-running the block reuses the
same directory and never overwrites `.env`.

Re-running it **removes the previous deployment first** (containers with the
fixed `cnb-*` names, regardless of which directory hosted them), so there is no
container-name conflict. Named volumes are kept by default; run with
`PURGE_DATA=1` to also wipe audit records, files and generated credentials.

**Native Linux + systemd** (most complete: reuses host toolchain, GPU, drivers
and Docker; needs a real systemd and root, not `sudo`):

```bash
git clone https://github.com/NinaAWA/cnb-mcp-gateway.git && cd cnb-mcp-gateway && ./scripts/install-native.sh
```

It prints the same connection block when it finishes. For Compose deployments,
`python3 scripts/selfcheck_v11.py` runs the T01–T12 real self-check with explicit
PASS / FAIL / SKIP; that script hard-codes Compose paths and is not a generic
native-deployment check.

### Reprint the connection info (read-only)

```bash
./scripts/show-config.sh                    # auto-detect native systemd / Compose
./scripts/show-config.sh --mode docker     # force Compose
./scripts/install-native.sh --show-config  # native deployment
```

Set `MCP_PUBLIC_BASE_URL` (and optionally `ADMIN_PUBLIC_BASE_URL`) in `.env` for
fixed public HTTPS addresses. Without them the script labels addresses as
local-only; it never invents a public URL.

For a **fixed public HTTPS entry**, set `MCP_PUBLIC_BASE_URL=https://mcp.example.com`
and put nginx or a Cloudflare named tunnel in front (see
`deploy/reverse-proxy.example.conf`); only `127.0.0.1` is published otherwise.
Without it, `download_file` still returns a working link but marks it
`link_scope=local` instead of pretending to be a public URL.

Both modes keep Bearer + OAuth 2.1 PKCE/DCR, built-in Git LFS, SQLite WAL audit
and persistent volumes for files / task logs. Upgrade and rollback are below;
optional host Docker control needs the explicit override file described below.

### Runtime override: real host workspace, host Docker, two public tunnels

The stock Compose file mounts `./workspace:/workspace` (a subdirectory of the
deployment directory) and publishes only loopback ports. When the gateway must
operate on the real host checkout, add `docker-compose.runtime.yml` explicitly:

```bash
# in .env (keep existing MCP_TOKEN / ADMIN_PASSWORD untouched; leave the two
# public base URLs empty until the tunnel containers have reported their domains)
MCP_HOST_WORKSPACE=/workspace
MCP_DOCKER_SOCKET=1
MCP_PUBLIC_BASE_URL=
ADMIN_PUBLIC_BASE_URL=

docker compose -p <project> -f docker-compose.yml -f docker-compose.runtime.yml \
  up -d --force-recreate
# each tunnel logs its own random HTTPS domain
docker logs <gateway-tunnel-container>
docker logs <admin-tunnel-container>
# write the two domains back into .env, then recreate ONLY the business containers
# so the tunnel containers (and their already-distributed domains) stay untouched
docker compose -p <project> -f docker-compose.yml -f docker-compose.runtime.yml \
  up -d --force-recreate --no-deps mcp-gateway mcp-admin-web
```

The override bind-mounts `MCP_HOST_WORKSPACE` to `/workspace` (so `MCP_WORKDIR`
and the structured `git_*` tools see the real checkout) **and** to
`/data/files/host-workspace` (so the file tools, whose root stays `/data/files`,
reach the same files through `host-workspace/...`). `MCP_FILE_ROOT` is unchanged,
so the existing `files-data` volume and the HMAC signing secret shared with the
admin web keep working. The file tools still hard-deny `.git`, `.env`, key files
and credential directories; `run_command` remains full administrator mode.

It also mounts `/var/run/docker.sock` and the host's static `docker` CLI, which
is equivalent to host root (see the permission section below). Two independent
quick-tunnel containers forward to `mcp-gateway:8765` and `mcp-admin-web:8788`;
keeping them separate means recreating the business containers does not change
the published HTTPS URLs. For the Admin Web URL, `show-config.sh` prefers
`ADMIN_PUBLIC_BASE_URL`, then parses the admin tunnel container's logs, and only
then falls back to loopback. The MCP URL is printed from `MCP_PUBLIC_BASE_URL`
(or the local address while it is empty), so the gateway tunnel domain must be
written back into `.env` as shown above.

Random tunnel domains change only when the tunnel containers themselves are
recreated. To verify volume persistence, recreate just `mcp-gateway` and
`mcp-admin-web` with `--no-deps` (as above); a bare `up -d --force-recreate`
also recreates both tunnel containers and therefore invalidates the two URLs
you already distributed. Re-read both `docker logs` outputs and update the two
`.env` entries whenever a tunnel container really is recreated. Never pass
`-v`, so `audit-data` (the SQLite audit DB, `task-logs/`, generated credentials)
and `files-data` are preserved.

### Upgrade and rollback

- Native: `./scripts/install-native.sh --upgrade` backs up the previous code
  to `/opt/mcp-gateway/app.previous`; `--rollback` restores it and restarts both
  units. Data and `/etc/mcp-gateway/env` are never touched.
- Compose: change `MCP_IMAGE` in `.env` to the new tag, then
  `docker compose up -d`; roll back by switching the tag back (e.g.
  `ghcr.io/ninaawa/cnb-mcp-gateway-plus:v3.1.1`) and re-running `up -d`. Record
  the current image digest and `docker inspect` arguments before upgrading.
- The `v3.1.0` tag and the old `cnb-mcp-gateway` package are deliberately not
  overwritten.

### Docker socket permission (read this)

Container root is **not** host root. Only mounting `/var/run/docker.sock` grants
effective host-root power (privileged containers, mounting the host filesystem,
reading any file). It is off by default and requires both `MCP_DOCKER_SOCKET=1`
and the explicit override file. The override also mounts the host's static `docker`
CLI read-only (set `MCP_HOST_DOCKER_CLI` if `command -v docker` differs), so
`run_command`'s `docker ps` / `docker build` really work once authorized. Even
without that CLI mount, `docker_status` reads the Engine API directly from the
socket using only the standard library. Native/systemd deployments already use the
host Docker directly.

### Verification

`python3 scripts/selfcheck_v11.py` runs T01–T12 against a live service and prints
the real command, output excerpt and PASS/FAIL/SKIP for each. Credentials come
from `MCP_TOKEN` / `ADMIN_PASSWORD` when set, otherwise from the auto-generated
`/app/.env` and `/data/audit/.admin_password` inside the containers. Any failure
or skip is reported explicitly; nothing is simulated. `--skip-restart` omits the
real container restart in T10 and marks it SKIP. Unit/regression tests: `pytest -q`.

## Why

You want to let an AI agent run commands **on your server** from anywhere, but
you don't want to hand out an open shell. This gateway exposes a controlled
toolkit with a token gate, an optional command-prefix allow-list, and a full
audit log.

## Features

- **Universal MCP** — works with every client that speaks MCP:
  - **Bearer direct** (simplest): send `Authorization: Bearer <MCP_TOKEN>` to
    `/mcp`. No OAuth round-trip needed. Claude Desktop / Cursor / VS Code / SDKs.
  - **OAuth 2.1 (PKCE + DCR)** for clients that only support OAuth (e.g. ChatGPT
    connectors). S256 PKCE is required by default; `redirect_uri` must match the
    DCR registration, and unregistered clients may only use HTTPS or loopback HTTP
    (custom schemes must be registered first). Issued tokens expire and are pruned.
- **Remote-control toolkit** (inspired by Remote Desktop Commander, adapted to Linux) —
  run experiments, set up environments, view data **and images**, exactly like RDC:
  - `run_command` — execute a shell command (default **open**; destructive commands
    need `confirm=true`; catastrophic commands are hard-blocked by a safety net).
  - `run_command_background` + `get_task_status` / `get_task_output` / `tail_task` /
    `stop_task` — run **long experiments** (training/inference) without a timeout kill.
  - `list_directory` / `read_file` / `write_file` — browse & edit the filesystem.
  - `search_files` / `grep_content` — find files by name or search their contents.
  - `list_processes` / `kill_process` — inspect and terminate processes (PID 1 protected).
  - `read_image` — return an image (plots, screenshots) so a multimodal agent can **see** it.
  - `get_system_info` — kernel, uptime, CPU, memory, disk.
  - `get_environment_variables` — current env (secret values masked).
- **Web & paper search (zero API key)** — see the
  [free/open-source source survey](./docs/search-sources-research.md):
  - `search_papers` — aggregate OpenAlex, arXiv, CrossRef, PubMed and Europe PMC
    (plus optional OpenReview / dblp / Semantic Scholar); results are interleaved
    across sources with DOI, citations, abstract and open-access links.
  - `search_arxiv` / `get_paper` — field-specific arXiv queries and single-paper
    lookup by DOI / arXiv ID / PMID.
  - `search_web` — pluggable providers, default Hacker News + StackExchange
    (no key); optional GitHub, self-hosted SearXNG, Tavily.
  - `read_web_page` — optional HTTPS page text extraction with SSRF guards
    (disabled by default).
- **Safety model**: default open (no allow-list) + a **confirm gate** for destructive
  commands + a **catastrophic blacklist** (e.g. `rm -rf /`, `mkfs`, `shutdown`) that
  can't be bypassed even with confirmation. Full audit log of every action.
- **Sandbox knobs**: optional `MCP_ALLOWED_PREFIXES` allow-list, `MCP_DENY_PREFIXES`,
  `MCP_REQUIRE_CONFIRM`, working dir, audit log.
- **Git LFS built in**: images ship `git-lfs` installed and initialized at system level,
  so repositories with LFS `pre-push` hooks can be pushed without granting the agent
  package-install permissions or weakening the command allow-list.
- **Zero config ingress**: cloudflared quick tunnel gives you a public HTTPS URL
  automatically (URL rotates each restart; set `CF_TUNNEL_TOKEN` for a fixed URL).
- **Self-contained Docker image**: server + tunnel in one container.
- **Git deletion protection (hard constraint)**: deleting branches/repositories,
  delete-style refspecs, git aliases and obfuscated execution are hard-blocked at the
  command layer, by an in-image `git` wrapper and by file-tool path checks. Structured
  `git_*` tools provide commit / switch / push with no delete API. See
  [Git deletion protection](#git-deletion-protection).
- **MCP Admin Dashboard (multi-service Compose deployment, 3 containers by default +
  2 optional profiles)**: separate admin login,
  live call monitoring, complete call audit, Git/experiment views, per-container metrics,
  charts and exports. SQLite WAL storage with redaction by default; large outputs are stored
  as protected references. See the [Chinese deployment guide](./docs/admin-dashboard.zh-CN.md).

## Quick start (Docker)

```bash
# 1) (one time) login to GitHub Container Registry if you hit a pull auth error
#    docker login ghcr.io   # public images usually don't need this

# 2) run it (default is fully open; tighten with -e MCP_ALLOWED_PREFIXES="git|python3|ls" if you like)
docker run -d --name cnb-mcp-gateway --restart unless-stopped \
  -v "$PWD:/workspace" \
  ghcr.io/ninaawa/cnb-mcp-gateway:latest

docker logs -f cnb-mcp-gateway              # prints BASE_URL, /mcp, openapi (no secrets by default)
docker exec cnb-mcp-gateway cat /app/.env   # read MCP_TOKEN
```

Or use the bundled helper (env-overridable):

```bash
./run.sh
```

### Multi-service deployment with the audit dashboard

Use the one-command block above. In an existing v1.1 checkout the short form is:

```bash
cp .env.example .env && docker compose up -d --build && ./scripts/show-config.sh
```

`./scripts/show-config.sh` is the single place that reads the generated
`MCP_TOKEN` and admin password and prints the MCP URL, client config and Admin
Web URL. The dashboard listens on `http://127.0.0.1:8788` only and uses a
**separate admin account** (unrelated to `MCP_TOKEN`); both entry points have
independent access control. Configuration, optional HTTPS ingress, PostgreSQL,
backup and the acceptance checklist are in the
[deployment guide](./docs/admin-dashboard.zh-CN.md).

The built-in quick tunnel is **on by default** (`MCP_TUNNEL_DISABLED=0`), so the
one-command deploy already yields a public `https://<random>.trycloudflare.com/mcp`
URL that a remote agent can reach; `./scripts/show-config.sh` prints that URL,
the Bearer token and the client JSON. The random domain changes whenever the
container is recreated. For a **fixed** public HTTPS address, set
`MCP_PUBLIC_BASE_URL` plus a reverse proxy / named tunnel (see above) — the
script then labels the entry as fixed. Generated credentials never appear in
container logs; use `./scripts/show-config.sh` (or `set MCP_PRINT_SECRETS=1`
only in isolated environments). Set `MCP_TUNNEL_DISABLED=1` and recreate to
close public ingress.

## Connect your agent

### A. Bearer direct (recommended, any client that can set a header)

Point the MCP client at `https://<BASE_URL>/mcp` and attach the header:

```
Authorization: Bearer <MCP_TOKEN>
```

Example `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "cnb-gateway": {
      "url": "https://xxxx.trycloudflare.com/mcp",
      "headers": { "Authorization": "Bearer <MCP_TOKEN>" }
    }
  }
}
```

### B. OAuth (ChatGPT connector / OAuth-only clients)

- Service URL: `https://<BASE_URL>/mcp`
- Authorize URL: `https://<BASE_URL>/authorize`
- Token URL: `https://<BASE_URL>/token`
- Registration URL: `https://<BASE_URL>/register`
- On the consent page: username = anything, **password = `MCP_TOKEN`**.

## Configuration (environment variables)

| Variable | Default | Meaning |
|---|---|---|
| `MCP_TOKEN` | random 48 chars | Static Bearer secret (pin it to keep it stable across restarts). |
| `MCP_ALLOWED_PREFIXES` | (empty = allow all) | 可选命令前缀白名单（竖线分隔），如 `git\|python3\|ls`。设置后仅这些前缀可运行。 |
| `MCP_DENY_PREFIXES` | (empty) | 追加到**毁灭级硬禁止**列表（逗号或竖线分隔），如 `wipefs,dd if=`。即便 `confirm=true` 也拒绝。 |
| `MCP_REQUIRE_CONFIRM` | (empty) | 追加到**需确认**列表（逗号或竖线分隔）；匹配到的命令需 `confirm=true` 才执行。 |
| `CF_TUNNEL_TOKEN` | (empty → quick tunnel) | Named Cloudflare tunnel token → **fixed URL**. |
| `MCP_WORKDIR` | `/workspace` | Working directory for executed commands (auto-created). |
| `MCP_WORKDIR_MOUNT` | (unset) | Host path mounted into the container at `/workspace`. |
| `MCP_SEARCH_TIMEOUT` | `10` | Per-request timeout in seconds. |
| `MCP_SEARCH_MAX_RESULTS` | `8` | Max results per source / provider (final output ≤ 30). |
| `MCP_SEARCH_MAX_BYTES` | `2000000` | Max response body size in bytes. |
| `MCP_SEARCH_RETRIES` | `1` | Retries on 429 / 5xx / network errors with exponential backoff. |
| `MCP_SEARCH_CACHE_TTL` | `300` | In-memory GET cache TTL in seconds (`0` disables). |
| `MCP_SEARCH_ARXIV_MIN_INTERVAL` | `3` | Minimum arXiv request interval in seconds. |
| `MCP_SEARCH_MAILTO` | (empty) | Polite-pool email (OpenAlex / CrossRef / NCBI / Unpaywall); masked in env dumps. |
| `MCP_SEARCH_WEB_PROVIDERS` | `hn,stackexchange` | Default `search_web` providers (comma-separated). |
| `MCP_SEARCH_SEARXNG_URL` | (empty) | Base URL of a self-hosted SearXNG; enables the `searxng` provider. |
| `MCP_SEARCH_TAVILY_KEY` | (empty) | Optional Tavily key (free tier); masked in env dumps. |
| `MCP_SEARCH_FETCH_ENABLED` | `0` | Set to `1` to enable `read_web_page`. |
| `MCP_SEARCH_FETCH_MAX_CHARS` | `8000` | Max extracted page text length. |
| `MCP_SEARCH_FETCH_ALLOW_HOSTS` | (empty) | Optional comma-separated host allow-list for page fetching. |

### v1.1 command / file / task / Docker variables

| Variable | Default | Meaning |
|---|---|---|
| `MCP_IMAGE` | `.../plus:v1.1.0` | Image tag; changing it is the whole upgrade/rollback procedure. |
| `MCP_MAX_OUTPUT_BYTES` | `2000000` | Hard cap on a single command's stdout/stderr (truncation is marked in the result). |
| `MCP_FILE_ROOT` | `/data/files` | Managed root for binary transfer. Every path is `resolve()`d and must stay inside it; `.git`, `.ssh`, `.env`, keys and token files are refused. |
| `MCP_FILE_CHUNK_BYTES` | `1048576` | Max base64 chunk size (clamped to 4096..8 MiB). Signed HTTP downloads are streamed and are not subject to this limit. |
| `MCP_TASK_LOG_DIR` | `/data/audit/task-logs` | Persistent task-log directory; logs stay readable after a restart. |
| `MCP_PUBLIC_BASE_URL` | (empty) | Public HTTPS origin used as the prefix of `download_file`'s absolute signed link; empty falls back to the request Host (local `curl` only). |
| `MCP_FILE_SIGNING_SECRET` | (empty → `MCP_TOKEN`) | HMAC key for short-lived download links. Gateway and Admin Web must see the same value. |
| `MCP_DOCKER_SOCKET` | `0` | `1` + `docker-compose.host-docker.yml` mounts `/var/run/docker.sock`; equivalent to host root. |
| `MCP_HOST_PORT` / `MCP_BIND_HOST` | `8765` / `127.0.0.1` | Host-side port publishing for the gateway (loopback only by default). |
| `MCP_ALLOWED_PREFIXES` | (empty = allow all) | **Optional tightening switch only.** Leaving it empty is the v1.1 default administrator mode; Git deletion, catastrophic blocks, the `confirm` gate and full audit remain active either way. |

### Admin dashboard & audit variables (Compose reads the root `.env`)

| Variable | Default | Meaning |
|---|---|---|
| `ADMIN_USERNAME` / `ADMIN_PASSWORD` | `admin` / generated | Separate admin account. No hardcoded password: leave `ADMIN_PASSWORD` empty to generate a strong random one on first start; it is persisted (mode 600) in the `audit-data` volume and reused across container rebuilds. An explicit value always wins. |
| `ADMIN_SESSION_SECRET` | (empty → sessions drop on restart) | HMAC key for session cookies. |
| `ADMIN_SESSION_TTL` | `28800` | Session lifetime in seconds. |
| `ADMIN_LOGIN_MAX_FAILURES` / `ADMIN_LOGIN_LOCK_MINUTES` | `5` / `15` | Failed-login lockout. |
| `ADMIN_TOTP_SECRET` | (empty = off) | Optional TOTP MFA secret (Base32). |
| `ADMIN_COOKIE_SECURE` | `0` | Set to `1` when the dashboard is served over HTTPS. |
| `ADMIN_HOST_PORT` / `ADMIN_BIND_HOST` | `8788` / `127.0.0.1` | Dashboard port and bind address; use `0.0.0.0` for platform port-preview (set a strong password and MFA first). |
| `AUDIT_DB_DSN` | (empty = SQLite WAL) | Optional PostgreSQL DSN for the `postgres` profile. |
| `AUDIT_STRICT` | `0` | `1` rejects high-risk tools when audit storage is unavailable; default is graceful degradation. |
| `AUDIT_RETENTION_DAYS` / `AUDIT_MAX_DB_MB` | `90` / `512` | Archive retention and DB quota. |
| `MCP_TUNNEL_DISABLED` | `0` | Built-in random quick tunnel is on by default, so a remote agent gets a public `https://<random>.trycloudflare.com/mcp` URL right after deploy; the domain changes on recreate. For a fixed URL use an nginx reverse proxy (+ `MCP_PUBLIC_BASE_URL`), or set `CF_TUNNEL_TOKEN` and run `docker compose --profile tunnel up -d` with this set to `1`. Set to `1` to disable public ingress entirely. |
| `MCP_OAUTH_REQUIRE_PKCE` | `1` | Require OAuth S256 PKCE; set `0` only for legacy clients without a challenge. |
| `MCP_TRUST_PROXY` / `ADMIN_TRUST_PROXY` | `0` | Trust `X-Forwarded-For` only behind a trusted reverse proxy. |
| `MCP_GIT_EXTRA_SUBCOMMANDS` | (empty) | Extra allowed git subcommands; unknown ones are refused to block aliases. |
| `MCP_PRINT_SECRETS` | `0` | `1` prints `MCP_TOKEN` and the admin password to container logs (isolated environments only). |

Full list: [`.env.example`](./.env.example) and the
[deployment guide](./docs/admin-dashboard.zh-CN.md).

## Tools reference

| Tool | Purpose | Notes |
|---|---|---|
| `run_command` | Run a shell command | Add `confirm=true` for destructive commands (`rm -rf`, `git reset --hard`, `docker rm -f`, `chmod -R`…). Catastrophic commands (`rm -rf /`, `mkfs`, `shutdown`, fork bomb…) are always blocked. |
| `run_command_background` | Start a long job, get a `task_id` | For training/inference that exceeds the sync timeout. |
| `get_task_status` / `get_task_output` / `tail_task` / `stop_task` | Poll output / follow tail / stop a background job | `get_task_output(task_id, tail=50)` returns last 50 lines. |
| `read_image` | Return an image for the agent to see | `read_image(path="/workspace/plot.png", max_dim=1024)`; supports png/jpg/gif/webp/bmp/tiff. |
| `list_directory` / `read_file` / `write_file` | Filesystem browse/edit | — |
| `search_files` / `grep_content` | Find files / search contents | — |
| `list_processes` / `kill_process` | Processes (PID 1 protected) | — |
| `get_system_info` / `get_environment_variables` | Host info / env (secrets masked) | — |
| `search_papers` | Search papers across open sources | Zero key; `sources=...`, `from_year`, `sort=date`. |
| `search_arxiv` | arXiv field search | `author`, `category`, `from_year`, PDF links. |
| `get_paper` | Look up one paper | By `doi` / `arxiv_id` / `pmid`; Unpaywall OA link needs `MCP_SEARCH_MAILTO`. |
| `search_web` | Web / tech-community search | Default HN + StackExchange; optional GitHub / SearXNG / Tavily. |
| `read_web_page` | Extract page text | Off by default (`MCP_SEARCH_FETCH_ENABLED=1`); HTTPS + SSRF guards. |

### Search examples

```text
search_papers(query="retrieval augmented generation", sources="openalex,arxiv,crossref", max_results=5, from_year=2024)
search_arxiv(query="diffusion model", category="cs.CV", from_year=2025)
get_paper(doi="10.1038/nature12373")
search_web(query="model context protocol", providers="hn,stackexchange", max_results=5)
read_web_page(url="https://example.com/article")
```

Default paper sources need no credentials. `search_web` defaults to vertical
community search rather than a general web index; see the
[survey](./docs/search-sources-research.md) for trade-offs and optional providers.
Every result is labelled as untrusted external content in the tool output.

## Git deletion protection

Agents may commit, switch / create branches, fetch / pull and push, but **cannot delete
Git repositories or branches**; `confirm=true` never bypasses this rule.

- **Command layer** (`gateway/git_guard.py`): quote-aware parsing of every
  `run_command` / `run_command_background` / REST command blocks `git branch -D/-d/-m`,
  `git push --delete/--mirror/--prune`, `:branch` / `branch:` / `+branch` refspecs,
  `git update-ref`, `remote/worktree prune`, `fetch --prune`, writes into `.git/**`,
  deletion of directories containing `.git`, and obfuscated execution (`bash -c`,
  `python -c`, generated scripts, `base64 -d | sh`).
- **Process layer** (`docker/git-guard-wrapper.py`): the image replaces `/usr/bin/git`
  with a wrapper, so git calls from scripts, Python subprocesses and background
  tasks are checked too. Unknown subcommands (possible aliases) are refused;
  `MCP_GIT_EXTRA_SUBCOMMANDS` can whitelist extras.
- **File layer**: `write_file` refuses `.git/**` and system credential files.

Use the structured `git_*` tools for daily work (`git_status`, `git_commit`,
`git_switch`, `git_branch_create`, `git_push`, ...); they use `shell=False` and
expose no delete operation. `git push --force` and `git reset --hard` remain
"requires confirmation", not branch deletion.

The image has no host `/etc/gitconfig`. Configure `GIT_USER_NAME` /
`GIT_USER_EMAIL` and mount a credentials file read-only at
`/root/.git-credentials` (or set `GIT_CREDENTIALS_FILE`) before using
`git_commit` / `git_push`, otherwise commits fail with "Please tell me who you
are" and pushes fail authentication. On platforms that enforce commit signing,
the author must match the token user.

**This protects against mistakes and prompt injection, not against a malicious
root user inside the container** (root can edit `.git` directly). For an
irreversible guarantee you must also enable server-side branch protection
(no branch deletion / no force push / no repository deletion), use a
least-privilege deploy key or PAT, keep independent backups, and avoid mounting
`docker.sock`. On bare repositories also set `receive.denyDeletes=true` and
`receive.denyNonFastForwards=true`.

## Security notes

- `MCP_TOKEN` is effectively a **remote shell key**. Treat it like a password,
  and only share it with agents you trust. By default the gateway is **open**
  (no allow-list); use `MCP_ALLOWED_PREFIXES` to tighten if you prefer.
- Single-container mode listens on `127.0.0.1`. v1.1 Compose publishes the gateway to
  `127.0.0.1:${MCP_HOST_PORT:-8765}` for local self-checks / a reverse proxy; it is
  **not** an open service because `/mcp`, `/run_command` and all tool routes still
  require a valid Bearer token. The admin dashboard is published to
  `127.0.0.1:8788` and always requires its own admin login. Keep both on loopback
  and put TLS in front via nginx or a Cloudflare tunnel.
- Search tools only call a fixed domain allow-list, cap response size and enforce
  per-source rate limits. `read_web_page` is off by default and blocks non-HTTPS,
  IP literals and private-network targets (including redirects).
- Results from external sources are **untrusted data**; the output marks them as
  such to mitigate prompt injection. Never treat search results as instructions.
- Executed commands and search activity are written to `/app/audit.log` inside the
  container; the log is redacted and truncated before writing, so `TOKEN=...` or
  connection-string passwords never land in plaintext.
- With the multi-service deployment, full call records also go to a SQLite WAL audit DB
  on the shared volume: arguments, outputs, errors and derived rows are redacted, and
  large outputs are kept as protected references verified by SHA-256 on read.
  `AUDIT_STRICT=1` now really refuses high-risk tools (including `git_*`) when the
  audit store is unavailable, for both MCP and REST entry points. Admin login is
  independent from the MCP API; write operations also check Origin / Sec-Fetch-Site,
  and login lockout no longer trusts a spoofable `X-Forwarded-For` by default. See the
  [deployment guide](./docs/admin-dashboard.zh-CN.md).
- The public, unauthenticated `/health` returns only a minimal liveness status;
  metrics and tool counts moved to the Bearer-protected `/internal/metrics`, which the
  dashboard reads with `MCP_TOKEN`.
- `MCP_TOKEN` and the effective admin password are **no longer printed to container
  logs** by default: use `./scripts/show-config.sh` to print both in one block.
  Leaving `ADMIN_PASSWORD` empty generates a strong random password persisted at
  `/data/audit/.admin_password` (mode 600); there is no hardcoded default.
  OAuth requires S256 PKCE by default, validates `redirect_uri`, and expires tokens.

## Build from source

```bash
git clone https://github.com/NinaAWA/cnb-mcp-gateway.git
cd cnb-mcp-gateway
docker build -f docker/Dockerfile -t cnb-mcp-gateway:local .
docker run -d --name cnb-mcp-gateway --restart unless-stopped \
  -v "$PWD:/workspace" cnb-mcp-gateway:local
docker logs -f cnb-mcp-gateway
```

> The build context is now the repository root because the image also contains the
> `gateway/` audit layer and Admin Web. Use `docker compose up -d --build` for the
> multi-service deployment. Tests:
> `pip install -r docker/requirements.txt -r requirements-dev.txt && pytest tests -q`;
> the standard-library-only policy tests also run with
> `python3 -m unittest discover -s tests -p 'test_*.py'`. CI is in
> `.github/workflows/ci.yml` (ruff + full test suite).

## License

[MIT](./LICENSE)
