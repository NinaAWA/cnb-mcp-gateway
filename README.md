# CNB MCP Gateway

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
- **One tool**: `run_command { "command": "echo hi" }` → `{ "exit_code", "stdout", "stderr" }`.
- **Sandbox knobs**: `MCP_ALLOWED_PREFIXES` allow-list, working dir, audit log.
- **Zero config ingress**: cloudflared quick tunnel gives you a public HTTPS URL
  automatically (URL rotates each restart; set `CF_TUNNEL_TOKEN` for a fixed URL).
- **Self-contained Docker image**: server + tunnel in one container.

## Quick start (Docker)

```bash
# 1) (one time) login to GitHub Container Registry if you hit a pull auth error
#    docker login ghcr.io   # public images usually don't need this

# 2) run it
docker run -d --name cnb-mcp-gateway --restart unless-stopped \
  -e MCP_ALLOWED_PREFIXES="git status|ls|python3 /workspace" \
  -v "$PWD:/workspace" \
  ghcr.io/NinaAWA/cnb-mcp-gateway:latest

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
| `MCP_ALLOWED_PREFIXES` | (empty = allow all) | Pipe-separated command prefixes, e.g. `git status\|ls\|python3 /workspace`. |
| `CF_TUNNEL_TOKEN` | (empty → quick tunnel) | Named Cloudflare tunnel token → **fixed URL**. |
| `MCP_WORKDIR` | `/workspace` | Working directory for executed commands (auto-created). |
| `MCP_WORKDIR_MOUNT` | (unset) | Host path mounted into the container at `/workspace`. |

## Security notes

- `MCP_TOKEN` is effectively a **remote shell key**. Treat it like a password,
  set `MCP_ALLOWED_PREFIXES`, and only share it with agents you trust.
- The container only listens on `127.0.0.1`; Cloudflare terminates TLS and
  exposes the public endpoint. No host port is published.
- All executed commands are written to `/app/audit.log` inside the container.

## Build from source

```bash
git clone https://github.com/<your-github-username>/cnb-mcp-gateway.git
cd cnb-mcp-gateway
docker build -t cnb-mcp-gateway:local ./docker
docker run -d --name cnb-mcp-gateway --restart unless-stopped \
  -v "$PWD:/workspace" cnb-mcp-gateway:local
docker logs -f cnb-mcp-gateway
```

## License

[MIT](./LICENSE)
