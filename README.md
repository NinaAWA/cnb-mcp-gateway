# CNB MCP Gateway

> 📖 English | [中文文档](./README.zh-CN.md)

A self-hosted **MCP (Model Context Protocol) server** that exposes a single,
sandboxed tool — `run_command` — on your own machine/server, reachable by **any**
MCP client (Claude Desktop, Cursor, VS Code, Codex, ChatGPT connectors, or your
own agents) over the public internet via a Cloudflare quick tunnel.

```
 your server  ──►  run_command (bash, sandboxed)   ◄──  any MCP client
   │
   └─ cloudflared quick tunnel (random https URL) ◄── auth: Bearer token / OAuth2 PKCE
```

## Why

You want to let an AI agent run commands **on your server** from anywhere, but
you don't want to hand out an open shell. This gateway exposes exactly one tool
with a token gate, an optional command-prefix allow-list, and a full audit log.

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
- **Safety model**: default open (no allow-list) + a **confirm gate** for destructive
  commands + a **catastrophic blacklist** (e.g. `rm -rf /`, `mkfs`, `shutdown`) that
  can't be bypassed even with confirmation. Full audit log of every action.
- **Sandbox knobs**: optional `MCP_ALLOWED_PREFIXES` allow-list, `MCP_DENY_PREFIXES`,
  `MCP_REQUIRE_CONFIRM`, working dir, audit log.
- **Zero config ingress**: cloudflared quick tunnel gives you a public HTTPS URL
  automatically (URL rotates each restart; set `CF_TUNNEL_TOKEN` for a fixed URL).
- **Self-contained Docker image**: server + tunnel in one container.

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

The startup banner prints everything you need:

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

## Security notes

- `MCP_TOKEN` is effectively a **remote shell key**. Treat it like a password,
  and only share it with agents you trust. By default the gateway is **open**
  (no allow-list); use `MCP_ALLOWED_PREFIXES` to tighten if you prefer.
- The container only listens on `127.0.0.1`; Cloudflare terminates TLS and
  exposes the public endpoint. No host port is published.
- All executed commands are written to `/app/audit.log` inside the container.

## Build from source

```bash
git clone https://github.com/NinaAWA/cnb-mcp-gateway.git
cd cnb-mcp-gateway
docker build -t cnb-mcp-gateway:local ./docker
docker run -d --name cnb-mcp-gateway --restart unless-stopped \
  -v "$PWD:/workspace" cnb-mcp-gateway:local
docker logs -f cnb-mcp-gateway
```

## License

[MIT](./LICENSE)
