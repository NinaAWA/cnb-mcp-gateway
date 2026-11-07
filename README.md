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
    connectors). `redirect_uri` accepts any valid absolute URI (`https://`,
    `http://`, custom schemes) so non-ChatGPT agents are not blocked.
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
- **Zero config ingress**: cloudflared quick tunnel gives you a public HTTPS URL
  automatically (URL rotates each restart; set `CF_TUNNEL_TOKEN` for a fixed URL).
- **Self-contained Docker image**: server + tunnel in one container.
- **MCP Admin Dashboard (four-service Compose deployment)**: separate admin login,
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

docker logs -f cnb-mcp-gateway   # prints BASE_URL, /mcp, openapi, MCP_TOKEN
```

Or use the bundled helper (env-overridable):

```bash
./run.sh
```

### Four-service deployment with the audit dashboard (recommended for server management)

```bash
cp docker/.env.template .env
# Pin MCP_TOKEN and ADMIN_PASSWORD in .env for production
docker compose up -d --build
docker logs cnb-mcp-admin-web       # prints the generated admin password if unset
```

The dashboard listens on `http://127.0.0.1:8788` only and uses a **separate admin account**
(unrelated to `MCP_TOKEN`); the MCP API is still reached through the tunnel URL printed by the
gateway container. The two entry points have independent access control. Configuration, optional
HTTPS ingress, PostgreSQL, backup and the acceptance checklist are in the
[deployment guide](./docs/admin-dashboard.zh-CN.md).

For single-container mode, the startup banner prints everything you need:

```
 公网根地址 (BASE_URL):   https://xxxx.trycloudflare.com
 MCP 连接器地址 (MCP_URL): https://xxxx.trycloudflare.com/mcp
 ChatGPT 插件 OpenAPI 清单: https://xxxx.trycloudflare.com/openapi.yaml
 密钥 (MCP_TOKEN):        <random-48-char-token>
```

> Every container restart → a **new random URL** + **new random token** (unless
> you pin them, see below). Re-check `docker logs` after each restart.

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

OAuth hardening in this release: `redirect_uri` must **exactly** match a URI registered
at `/register` (fragments, wildcards and embedded credentials are rejected), PKCE
`S256` is mandatory and the `code_verifier` is verified at `/token`. Issued access
tokens expire after `MCP_OAUTH_TOKEN_TTL` (default 30 days); expired entries are
removed by the auth middleware. Legacy clients may opt out with
`OAUTH_ALLOW_ANY_REDIRECT=1` / `OAUTH_ALLOW_NO_PKCE=1` — both default to `0` and
enabling them weakens the authorization-code flow.

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

### Admin dashboard & audit variables (Compose reads the root `.env`)

| Variable | Default | Meaning |
|---|---|---|
| `ADMIN_USERNAME` / `ADMIN_PASSWORD` | `admin` / generated | Separate admin account; the generated password is persisted in the `audit-data` volume. |
| `ADMIN_SESSION_SECRET` | (empty → sessions drop on restart) | HMAC key for session cookies. |
| `ADMIN_SESSION_TTL` | `28800` | Session lifetime in seconds. |
| `ADMIN_LOGIN_MAX_FAILURES` / `ADMIN_LOGIN_LOCK_MINUTES` | `5` / `15` | Failed-login lockout. |
| `ADMIN_TOTP_SECRET` | (empty = off) | Optional TOTP MFA secret (Base32). |
| `ADMIN_COOKIE_SECURE` | `0` | Set to `1` when the dashboard is served over HTTPS. |
| `ADMIN_HOST_PORT` / `ADMIN_BIND_HOST` | `8788` / `127.0.0.1` | Dashboard port and bind address; use `0.0.0.0` for platform port-preview (set a strong password and MFA first). |
| `AUDIT_DB_DSN` | (empty = SQLite WAL) | Optional PostgreSQL DSN for the `postgres` profile. |
| `AUDIT_STRICT` | `0` | `1` denies high-risk tools unless the `RECEIVED` row is confirmed persisted (healthy DB, empty buffer, read-back); default is graceful degradation with a bounded buffer. |
| `AUDIT_RETENTION_DAYS` / `AUDIT_MAX_DB_MB` | `90` / `512` | Retention and DB quota. Every history table (calls, events, tasks, Git, experiments, approvals, logins, jobs, expired sessions) is archived in batches before exact-ID deletion; `admin_settings` is kept. |
| `AUDIT_ARCHIVE_BATCH` / `AUDIT_BUFFER_MAX` | `1000` / `1000` | Archive batch size (100..5000) and write-failure buffer cap. |
| `MCP_SESSION_TTL` | `3600` | Client-session idle/token TTL used for active-client statistics. |
| `OAUTH_ALLOW_ANY_REDIRECT` / `OAUTH_ALLOW_NO_PKCE` | `0` / `0` | Legacy OAuth compatibility switches; keep off in production. |
| `MCP_OAUTH_TOKEN_TTL` | `2592000` | Lifetime (seconds) of issued OAuth access tokens. |
| `GATEWAY_METRICS_TOKEN` | auto-generated | Internal read-only token for `/health/details`, stored as `/data/audit/.metrics_token` (mode 0600). Never hand it to agents. |
| `MCP_TUNNEL_DISABLED` | `0` | Set to `1` when using the standalone `cloudflared` service. |

Full list: [`docker/.env.template`](./docker/.env.template) and the
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

## Security notes

- `MCP_TOKEN` is effectively a **remote shell key**. Treat it like a password,
  and only share it with agents you trust. By default the gateway is **open**
  (no allow-list); use `MCP_ALLOWED_PREFIXES` to tighten if you prefer.
- Single-container mode listens on `127.0.0.1`; in Compose mode the gateway listens
  on `0.0.0.0` **inside the container network only** (no host port published).
  Cloudflare terminates TLS. The admin dashboard is published to `127.0.0.1:8788`
  and always requires its own admin login.
- Search tools only call a fixed domain allow-list, cap response size and enforce
  per-source rate limits. `read_web_page` is off by default and blocks non-HTTPS,
  IP literals and private-network targets (including redirects).
- Results from external sources are **untrusted data**; the output marks them as
  such to mitigate prompt injection. Never treat search results as instructions.
- Executed commands and search activity (query, source, duration, result count)
  are written to `/app/audit.log` inside the container; keys and response bodies
  are never logged.
- With the four-service deployment, full call records also go to a SQLite WAL audit DB
  on the shared volume: arguments, outputs and errors are redacted, and large outputs
  are kept as protected references. Admin login is fully independent from the MCP API;
  unauthenticated users cannot read any call record. See the
  [deployment guide](./docs/admin-dashboard.zh-CN.md).
- **Health endpoints are split.** Public `/health` returns only `status`, `service`,
  `uptime_seconds` and `audit.healthy`. Tool count, detailed audit state and
  process/container metrics require authenticated `/health/details` (Bearer or the
  internal read-only metrics token). Compose healthchecks still use `/health`.
- **One redaction boundary.** Every SQL write parameter passes through `redact_params`;
  derived Git/experiment fields, the legacy `audit.log`, task-log readback, archives
  and all exports are redacted too. Archive files are created 0600, flushed + fsynced
  and line-count checked before only those exact primary keys are deleted — a failed
  batch keeps all data and raises `audit_storage_warning`.
- **Strict audit is a three-state decision.** `available()` only means "can buffer".
  With `AUDIT_STRICT=1`, high-risk tools additionally require
  `confirm_persisted(request_id)`; a failed terminal write is surfaced as an
  `audit_strict_violation` event and in `health()["strict_violations"]`.

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
> four-service deployment; run tests with `.venv/bin/python -m pytest tests -q`.

## License

[MIT](./LICENSE)
