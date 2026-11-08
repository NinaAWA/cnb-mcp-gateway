> 📖 中文 | [English](./README.md)

# CNB MCP Gateway（中文文档）

一个**自托管的 MCP（模型上下文协议）服务器**：把你自己机器/服务器上的能力，通过一个 Cloudflare 快速隧道，暴露给**任意** MCP 客户端（Claude Desktop、Cursor、VS Code、Codex、ChatGPT 连接器，或你自己写的 agent），让 AI 能像操作「远程桌面」一样在你的服务器上**跑实验、配环境、看数据、看图片、检索网页与论文**。

```
 你的服务器  ──►  命令 / 文件 / 进程 / 图片 / 后台任务 / 网页与论文检索   ◄──  任意 MCP 客户端
   │
   └─ cloudflared 快速隧道（随机 https 地址） ◄── 鉴权：Bearer 令牌 / OAuth2 PKCE
```

## v1.1 — 通用、自托管的 Linux 云服务器远程控制

两种部署方式、同一套工具能力。不绑定任何特定研究项目、目录、仓库或依赖栈；不引入任务编排、多租户体系，也不依赖 Redis / Celery / Kubernetes。

- **默认管理员模式**：`MCP_ALLOWED_PREFIXES` 现在只是「可选的收紧开关」，留空即放行全部日常管理命令（`apt`/`dpkg`、`pip`/`uv`/`conda`、`bash`、Python、`gcc`/`cmake`、Git/Git LFS、Docker/Compose）。始终保留：Bearer/OAuth 鉴权、全量审计、Git 分支/仓库删除硬禁（含 shell、Python、编码绕过）、毁灭级黑名单、破坏性指令 `confirm=true` 确认门。同步 `run_command`、后台 `run_command_background`、REST `/run_command` 共用同一个 `_authorize()` 闸门，规则不会漂移。
- **真实命令结果**：`run_command` 返回 `exit_code`、`duration_ms`、`timed_out`、`stdout`、`stderr`，并支持 `env` 与 `stdin`；输出受 `MCP_MAX_OUTPUT_BYTES` 上限保护。超时返回约定码 `124` 并明确说明「并非进程真实退出码」。环境变量值、标准输入、分片内容一律不写审计。
- **任意二进制文件**：`upload_file` / `download_file` / `file_info` / `file_checksum`：base64 分片（默认 1 MiB）、`.part` 暂存、流式 SHA-256、`os.replace()` 原子落盘、覆盖检测、失败删除 `.part` 并保留原文件。`download_file` 同时返回 HMAC 签名、短时效的绝对链接（`GET /files/download`），浏览器或 `curl` 可直接打开并支持 HTTP `Range`。所有路径必须位于 `MCP_FILE_ROOT` 内；`.git`、`.ssh`、`.env`、私钥与令牌文件一律拒绝。
- **Git**：19 个 `shell=False` 结构化工具。`git_add` 改为显式路径数组（拒绝 `.` 与通配符）；`git_push` 用 `git rev-parse HEAD` + `git ls-remote` 核验远端 HEAD 后才报成功。新增 `git_merge`、`git_rebase`（安全形式）、`git_stash`、`git_show`、`git_cherry_pick`、`git_checkout`。删除本地/远端分支、强制 refspec 重写、`git lfs prune`/`uninstall`/`migrate` 始终拒绝。已修复 `sh -c 'git lfs pre-push ... repo.git'` 的 `.git` URL 误杀。
- **长任务**：元数据落在 SQLite WAL（`mcp_tasks`），日志落在 `MCP_TASK_LOG_DIR` 持久卷，重启后仍可读。无法确认存活（pid + `/proc` 启动时间）的 `running` 任务一律标为 `unknown`、退出码保持 NULL，**绝不**写成 `done` 或成功。取消任务同样校验 pid 身份，避免误杀被复用的 pid。
- **Docker**：常规 Docker 操作由 `run_command` 覆盖，另提供轻量 `docker_status` 状态视图。容器化部署默认**不**挂载宿主机 Docker socket。
- **管理网站**：首页仪表盘、完整调用记录、后台任务、Git 记录、文件传输记录五个页面；首页补受管文件根目录的磁盘指标。所有页面读取同一份已脱敏审计库：不记录口令、私钥、完整令牌、`env` 值、`stdin`、base64 分片与签名材料。

### 部署 v1.1 —— 一条命令，无需预先填任何秘密

> **该用哪个 main？** 只有 GitHub `main` 已合并 v1.1；CNB `main` 仍是旧提交
> `9266f9f`，没有 `.env.example` / `scripts/`，直接克隆会报
> `cp: 对 '.env.example' 调用 stat 失败: 没有那个文件或目录`。请使用下方 GitHub 地址；
> 只能用 CNB 时改用
> `git clone -b feat/mcp-v1.1 https://cnb.cool/webgamer-2026/cnb-mcp-gateway.git`。
> 当前已是 root 时无需 `sudo`；Docker 路径不依赖 systemd。

**Docker / Compose（无 systemd 的主机推荐）** —— 整块粘贴，部署、等就绪、打印配置一次完成：

```bash
(
set -e
DEST=/workspace/mcp-gateway-v11

# 先删除之前的部署：compose 里容器名写死，来自其他项目目录的旧实例也会冲突
for name in cnb-mcp-gateway cnb-mcp-admin-web cnb-audit-storage cnb-mcp-cloudflared cnb-mcp-postgres; do
  docker rm -f "$name" >/dev/null 2>&1 || true
done

if [ -d "$DEST/.git" ]; then git -C "$DEST" pull --ff-only; else git clone https://github.com/NinaAWA/cnb-mcp-gateway.git "$DEST"; fi
cd "$DEST"
for f in .env.example docker-compose.yml; do [ -f "$f" ] || { echo "缺少 $f：请确认代码来源包含 v1.1"; exit 1; }; done
# 需要连数据卷一起清空时加 PURGE_DATA=1（会丢失审计记录、文件与自动生成的凭据）
if [ "${PURGE_DATA:-0}" = "1" ]; then docker compose down -v --remove-orphans; fi
[ -f .env ] || cp .env.example .env && docker compose up -d --build
for i in $(seq 1 60); do curl -fsS http://127.0.0.1:8765/health >/dev/null 2>&1 && curl -fsS http://127.0.0.1:8788/healthz >/dev/null 2>&1 && break; sleep 2; done
if [ -f scripts/show-config.sh ]; then
  bash scripts/show-config.sh
else
  # 该 main 尚未合并 show-config.sh 时，用最小内联方式打印同一批信息（默认端口）
  TOKEN="$(docker exec cnb-mcp-gateway sed -n 's/^MCP_TOKEN=//p' /app/.env | head -1)"
  PASS="$(docker exec cnb-mcp-admin-web cat /data/audit/.admin_password)"
  printf 'MCP_URL    http://127.0.0.1:8765/mcp\nBearer     %s\n客户端配置:\n{\n  "mcpServers": {\n    "cnb-gateway": {\n      "url": "http://127.0.0.1:8765/mcp",\n      "headers": {\n        "Authorization": "Bearer %s"\n      }\n    }\n  }\n}\nAdmin Web  http://127.0.0.1:8788/\n账号       admin\n口令       %s\n' "$TOKEN" "$TOKEN" "$PASS"
fi
)
```

终端会直接打印：**可远程连接的公网 MCP 地址**（默认随机 `https://<域名>.trycloudflare.com/mcp`）、
Bearer Token、分行缩进、可直接复制的 `mcpServers` JSON、管理网站地址、管理员账号与口令、
两个健康检查地址。重复执行会复用同一目录且不会覆盖 `.env`。

重复执行会**先删除之前的部署**（按固定的 `cnb-*` 容器名清理，不论旧实例在哪个目录），
再重新部署，因此不会再出现容器名冲突。默认保留命名卷；需要连审计记录、文件与
自动生成的凭据一起清空时，在命令前加 `PURGE_DATA=1`。

**原生 Linux + systemd**（能力最完整：复用宿主机软件、GPU、驱动与 Docker；
需要真实 systemd 与 root，不是 `sudo`）：

```bash
git clone https://github.com/NinaAWA/cnb-mcp-gateway.git && cd cnb-mcp-gateway && ./scripts/install-native.sh
```

安装完成后同样会打印连接信息。Compose 部署可用 `python3 scripts/selfcheck_v11.py`
执行 T01–T12 真实自检（逐项 PASS/FAIL/SKIP）；该脚本硬编码 Compose 路径，
不是原生部署的通用自检。

### 随时重新打印连接信息（只读）

```bash
./scripts/show-config.sh                    # 自动探测原生 / Compose
./scripts/show-config.sh --mode docker     # 强制 Compose
./scripts/install-native.sh --show-config  # 原生部署
```

默认开启内置 quick tunnel（`MCP_TUNNEL_DISABLED=0`），部署完成后 `./scripts/show-config.sh`
会直接打印可远程连接的 `https://<随机域名>.trycloudflare.com/mcp` 与 Bearer 令牌，
域名在容器重建后会变化。在 `.env` 设置 `MCP_PUBLIC_BASE_URL`（可选 `ADMIN_PUBLIC_BASE_URL`）
可升级为固定公网 HTTPS 地址；脚本按「固定 / 随机隧道 / 仅本机」如实标注，**不伪造公网网址**。

需要**固定公网 HTTPS 入口**时设置 `MCP_PUBLIC_BASE_URL=https://mcp.example.com`，
并在前面放 nginx 或 Cloudflare 命名隧道（见 `deploy/reverse-proxy.example.conf`），
或填 `CF_TUNNEL_TOKEN` 并设 `MCP_TUNNEL_DISABLED=1` 后执行
`docker compose --profile tunnel up -d`。网关端口默认只发布到 `127.0.0.1`，公网流量由隧道/反代进入。

两种方式都保留 Bearer + OAuth 2.1 PKCE/DCR、内置 Git LFS、SQLite WAL 审计，以及文件与任务日志持久卷。升级/回滚见下节；宿主机 Docker 授权需按后文显式叠加 override 文件。

### 运行态 override：真实宿主工作区、宿主 Docker、两条公网隧道

主 compose 的 `./workspace:/workspace` 指向部署目录下的子目录，端口也只发布到回环地址。需要让网关操作真实宿主仓库时，显式叠加 `docker-compose.runtime.yml`：

```bash
# 在 .env 中设置（保留已有的 MCP_TOKEN / ADMIN_PASSWORD）
MCP_HOST_WORKSPACE=/workspace
MCP_DOCKER_SOCKET=1
MCP_PUBLIC_BASE_URL=https://<网关隧道域名>.trycloudflare.com
ADMIN_PUBLIC_BASE_URL=https://<后台隧道域名>.trycloudflare.com

docker compose -p <项目名> -f docker-compose.yml -f docker-compose.runtime.yml \
  up -d --force-recreate
# 从两个隧道容器日志里解析随机 HTTPS 域名，再回写以上两项并重建业务容器
docker logs <gateway-tunnel 容器名>
docker logs <admin-tunnel 容器名>
```

override 把 `MCP_HOST_WORKSPACE` 同时挂到 `/workspace`（让 `MCP_WORKDIR` 与结构化 `git_*` 工具看到真实仓库）和 `/data/files/host-workspace`（文件工具根仍是 `/data/files`，经 `host-workspace/...` 访问同一批文件）。`MCP_FILE_ROOT` 不变，因此既有 `files-data` 卷与后台共用的 HMAC 签名密钥保持一致。文件工具仍硬禁 `.git`、`.env`、私钥与凭据目录；`run_command` 保持完整管理员权限。

override 同时挂载 `/var/run/docker.sock` 与宿主静态 `docker` CLI，权限等价宿主 root（见后文）。两条独立 quick tunnel 分别转发 `mcp-gateway:8765` 与 `mcp-admin-web:8788`：把隧道拆成独立容器后，重建业务容器不会改变已分发的公网地址。`show-config.sh` 优先读取两个固定基址，其次解析 `admin-tunnel` 容器日志中的域名，最后才回落本机地址。

随机域名只在隧道容器自身重建时变化：重新读取两条 `docker logs` 并回写 `.env` 的两项，再重建业务容器即可。重建务必不带 `-v`，以保留 `audit-data`（审计库、`task-logs/`、自动生成的凭据）与 `files-data`。

### 升级与回滚

- 原生：`./scripts/install-native.sh --upgrade` 会把旧代码备份到 `/opt/mcp-gateway/app.previous`，`--rollback` 恢复备份并重启两个单元；数据与 `/etc/mcp-gateway/env` 从不动。
- Compose：修改 `.env` 中的 `MCP_IMAGE` 标签后 `docker compose up -d`；回滚把标签改回上一个可用值（如 `ghcr.io/ninaawa/cnb-mcp-gateway-plus:v3.1.1`）再 `up -d`。升级前先记录当前镜像 digest 与 `docker inspect` 启动参数作为回滚锚点。
- 有意不覆盖 `v3.1.0` 标签与旧 package `cnb-mcp-gateway`。

### Docker socket 权限（务必阅读）

容器内 root **不等于**宿主机 root。只有挂载 `/var/run/docker.sock` 才实质获得宿主机 root 能力（可启动特权容器、挂载宿主根目录、读取任意文件）。默认关闭，且必须同时设置 `MCP_DOCKER_SOCKET=1` 并叠加 override 文件。该 override 还会只读挂载宿主机的静态 `docker` CLI（路径不同时用 `MCP_HOST_DOCKER_CLI` 指定 `command -v docker` 的结果），因此授权后 `run_command` 里的 `docker ps` / `docker build` 确实可执行；即使不挂 CLI，`docker_status` 也会用标准库直接读 socket 的 Engine API 返回真实容器状态。原生 systemd 部署直接复用宿主机 Docker，无需该授权。

### 验证

`python3 scripts/selfcheck_v11.py` 会对真实运行的服务执行 T01–T12，逐项打印真实命令、输出摘录与 PASS/FAIL/SKIP；任何失败或跳过都明确说明，绝不虚构。凭据优先取 `MCP_TOKEN` / `ADMIN_PASSWORD` 环境变量，未设置时自动从容器内的 `/app/.env` 与 `/data/audit/.admin_password` 读取首次启动生成的值。`--skip-restart` 会跳过 T10 的真实容器重启并标记为 SKIP。单元测试：`pytest -q`。

## 为什么做这个

你想让 AI agent 能**从任意位置在你的服务器上执行命令**，但又不想直接给一个开放 shell。这个网关把一组受控工具（带令牌门、确认门与危险黑名单安全网、完整审计日志）暴露出来，而不是裸 shell。

## 功能特性

- **通用 MCP**：支持所有会说 MCP 的客户端：
  - **Bearer 直连（最简单）**：向 `/mcp` 发送 `Authorization: Bearer <MCP_TOKEN>`，无需 OAuth 往返。适用于 Claude Desktop / Cursor / VS Code / 各类 SDK。
  - **OAuth 2.1（PKCE + DCR）**：给只支持 OAuth 的客户端（如 ChatGPT 连接器）用。默认强制 S256 PKCE；`redirect_uri` 必须与 DCR 注册值精确一致，未注册客户端只接受 HTTPS 或本机回环 HTTP（自定义 scheme 需先 `/register` 注册），避免授权码被重定向到任意地址。
- **远程管理工具集（灵感来自 Remote Desktop Commander，但适配 Linux）**——真正像 RDC 一样跑实验、配环境、看数据与图片：
  - `run_command` —— 执行 shell 命令（默认**开放**；破坏性命令需要 `confirm=true`；毁灭级命令被安全网**硬禁止**）。
  - `run_command_background` + `get_task_status` / `get_task_output` / `tail_task` / `stop_task` —— 跑**长实验**（训练/推理），不被超时掐断。
  - `list_directory` / `read_file` / `write_file` —— 浏览与编辑文件系统。
  - `search_files` / `grep_content` —— 按文件名查找或搜索文件内容。
  - `list_processes` / `kill_process` —— 查看与终止进程（PID 1 受保护）。
  - `read_image` —— 返回一张图片（图表、截图），让多模态 agent 能**看见**它。
  - `get_system_info` —— 内核、运行时间、CPU、内存、磁盘。
  - `get_environment_variables` —— 当前环境变量（密钥值已脱敏）。
  - `git_status` / `git_log` / `git_diff` / `git_branch_list` / `git_branch_create` / `git_switch` / `git_add` / `git_commit` / `git_fetch` / `git_pull` / `git_push` / `git_remote_list` —— **结构化 Git 工具**（参数化执行、无删除接口），用于远端提交、切换/创建分支、推送与查看状态；详见 [Git 删除保护](#git-删除保护硬约束)。
- **网页搜索 + 论文检索（零 API Key）**，方案调研见 [免费/开源数据源选型](./docs/search-sources-research.md)：
  - `search_papers` —— 聚合 OpenAlex、arXiv、CrossRef、PubMed、Europe PMC（另可选 OpenReview / dblp / Semantic Scholar）；多源结果轮流交错，带 DOI、引用数、摘要与开放全文链接。
  - `search_arxiv` / `get_paper` —— 按作者/分类查 arXiv；按 DOI / arXiv ID / PMID 查单篇论文。
  - `search_web` —— 可插拔 provider，默认 Hacker News + StackExchange（免密钥）；可选 GitHub、自建 SearXNG、Tavily。
  - `read_web_page` —— 可选抓取网页正文（默认关闭），带 SSRF 防护与不可信内容标注。
- **安全模型**：默认开放（无白名单）+ 对破坏性命令的**确认门** + **毁灭级黑名单**（`rm -rf /`、`mkfs`、`shutdown` 等）即便确认也无法绕过 + **Git 删除硬保护**（删除分支/仓库、删除型 refspec、别名与混淆执行一律硬禁止）。每次操作都有完整审计日志。
- **可调旋钮（Sandbox knobs）**：可选的 `MCP_ALLOWED_PREFIXES` 白名单、`MCP_DENY_PREFIXES`、`MCP_REQUIRE_CONFIRM`、工作目录、审计日志。
- **内置 Git LFS**：制品已安装并系统级初始化 `git-lfs`，带 LFS `pre-push` 钩子的仓库可直接推送，无需向 agent 开放系统包安装权限或放宽命令白名单。
- **零配置入口**：cloudflared 快速隧道自动给你一个公网 HTTPS 地址（每次重启地址会变；设置 `CF_TUNNEL_TOKEN` 可固定地址）。
- **自包含镜像**：服务器 + 隧道在同一个容器里。
- **MCP Admin Dashboard（多服务 Compose 部署，默认 3 容器 + 2 个可选 profile）**：独立管理员登录、实时调用监控、完整调用审计、
  Git/实验监控、容器状态、图表与日志导出；SQLite WAL 持久化，默认脱敏、大输出只存引用。
  详见 [中文部署与运维文档](./docs/admin-dashboard.zh-CN.md)。

## 快速开始（Docker）

```bash
# 1)（一次性）如果拉取时报鉴权错误，先登录 GitHub 容器仓库
#    docker login ghcr.io   # 公开镜像通常不需要

# 2) 启动（默认全开放；如需收紧可加 -e MCP_ALLOWED_PREFIXES="git|python3|ls"）
docker run -d --name cnb-mcp-gateway --restart unless-stopped \
  -v "$PWD:/workspace" \
  ghcr.io/ninaawa/cnb-mcp-gateway:latest

docker logs -f cnb-mcp-gateway              # 打印 BASE_URL、/mcp、openapi（默认不打印密钥）
docker exec cnb-mcp-gateway cat /app/.env   # 读取 MCP_TOKEN
```

或用自带的辅助脚本（所有参数可用环境变量覆盖）：

```bash
./run.sh
```

### 带审计后台的多服务部署

直接用部署章节的一键命令即可。在已有的 v1.1 目录里，最简形式是：

```bash
cp .env.example .env && docker compose up -d --build && ./scripts/show-config.sh
```

`./scripts/show-config.sh` 是唯一汇总入口：自动读取首次启动生成的 `MCP_TOKEN` 与管理员口令，
打印 MCP 地址、分行缩进的 `mcpServers` JSON 配置、后台地址与账号口令。后台默认只在宿主机
`http://127.0.0.1:8788` 可达，使用**独立管理员账号**登录（与 `MCP_TOKEN` 无关），
两者访问控制互相独立。配置、可选 HTTPS 入口、PostgreSQL、备份与验收清单见
[部署与运维文档](./docs/admin-dashboard.zh-CN.md)。

默认开启内置 quick tunnel（`MCP_TUNNEL_DISABLED=0`）；`show-config.sh` 会等待隧道域名出现，
然后直接打印可远程连接的 `https://<随机域名>.trycloudflare.com/mcp`。域名在容器重建后会变化；
需要固定公网 HTTPS 时按上文设置 `MCP_PUBLIC_BASE_URL` 并配置反代/命名隧道，脚本改为打印固定地址。
生成的凭据默认不进容器日志，统一用 `./scripts/show-config.sh` 读取
（仅在隔离环境可设 `MCP_PRINT_SECRETS=1` 恢复日志打印）。
设 `MCP_TUNNEL_DISABLED=1` 并重建容器即可关闭公网入口。

## 连接你的 agent

### A. Bearer 直连（推荐，任何能设请求头的客户端）

把 MCP 客户端指向 `https://<BASE_URL>/mcp`，并带上请求头：

```
Authorization: Bearer <MCP_TOKEN>
```

`claude_desktop_config.json` 示例：

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

### B. OAuth（ChatGPT 连接器 / 只支持 OAuth 的客户端）

- 服务地址 Service URL：`https://<BASE_URL>/mcp`
- 授权地址 Authorize URL：`https://<BASE_URL>/authorize`
- 令牌地址 Token URL：`https://<BASE_URL>/token`
- 注册地址 Registration URL：`https://<BASE_URL>/register`
- 在同意页面：用户名 = 任意，**密码 = `MCP_TOKEN`**。
- 客户端需启用 PKCE S256（默认强制；确需兼容不支持的客户端时设 `MCP_OAUTH_REQUIRE_PKCE=0`），
  并先用 `/register` 注册 `redirect_uri`（自定义 scheme 必须预注册）。

## 配置（环境变量）

| 变量 | 默认值 | 含义 |
|---|---|---|
| `MCP_TOKEN` | 随机 48 位 | 静态 Bearer 密钥（固定它可让重启后令牌不变）。 |
| `MCP_ALLOWED_PREFIXES` | （空 = 全部允许） | 可选命令前缀白名单（竖线分隔），如 `git\|python3\|ls`。设置后仅这些前缀可运行。 |
| `MCP_DENY_PREFIXES` | （空） | 追加到**毁灭级硬禁止**列表（逗号或竖线分隔），如 `wipefs,dd if=`。即便 `confirm=true` 也拒绝。 |
| `MCP_REQUIRE_CONFIRM` | （空） | 追加到**需确认**列表（逗号或竖线分隔）；匹配到的命令需 `confirm=true` 才执行。 |
| `CF_TUNNEL_TOKEN` | （空 → 快速隧道） | 命名 Cloudflare 隧道令牌 → **固定地址**。 |
| `MCP_WORKDIR` | `/workspace` | 命令执行的工作目录（自动创建）。 |
| `MCP_WORKDIR_MOUNT` | （未设置） | 宿主机挂载到容器内 `/workspace` 的路径。 |
| `MCP_SEARCH_TIMEOUT` | `10` | 单次外部请求超时（秒）。 |
| `MCP_SEARCH_MAX_RESULTS` | `8` | 每个来源 / provider 的条数上限（最终输出最多 30 条）。 |
| `MCP_SEARCH_MAX_BYTES` | `2000000` | 响应体体积上限（字节）。 |
| `MCP_SEARCH_RETRIES` | `1` | 429 / 5xx / 网络错误重试次数（指数退避）。 |
| `MCP_SEARCH_CACHE_TTL` | `300` | 内存 GET 缓存有效期（秒），`0` 关闭。 |
| `MCP_SEARCH_ARXIV_MIN_INTERVAL` | `3` | arXiv 最小请求间隔（秒）。 |
| `MCP_SEARCH_MAILTO` | （空） | 礼貌池邮箱（OpenAlex / CrossRef / NCBI / Unpaywall），环境变量转储中自动脱敏。 |
| `MCP_SEARCH_WEB_PROVIDERS` | `hn,stackexchange` | `search_web` 默认 provider（逗号分隔）。 |
| `MCP_SEARCH_SEARXNG_URL` | （空） | 自建 SearXNG 地址，配置后启用 `searxng` provider。 |
| `MCP_SEARCH_TAVILY_KEY` | （空） | 可选 Tavily 免费额度密钥，自动脱敏。 |
| `MCP_SEARCH_FETCH_ENABLED` | `0` | 设为 `1` 才启用 `read_web_page`。 |
| `MCP_SEARCH_FETCH_MAX_CHARS` | `8000` | 抓取正文最大字符数。 |
| `MCP_SEARCH_FETCH_ALLOW_HOSTS` | （空） | 页面抓取主机白名单（逗号分隔），留空允许任意公网 HTTPS 主机。 |
| `MCP_OAUTH_REQUIRE_PKCE` | `1` | 强制 OAuth S256 PKCE；老客户端不发 `code_challenge` 时才设 `0`（不建议）。 |
| `MCP_TRUST_PROXY` / `ADMIN_TRUST_PROXY` | `0` | 仅在可信反向代理之后设 `1`，否则客户端可伪造 `X-Forwarded-For` 绕过登录锁定 / 伪造审计来源 IP。 |
| `MCP_GIT_EXTRA_SUBCOMMANDS` | （空） | 额外放行的 git 子命令（逗号分隔）；未知子命令默认拒绝，防止 alias 隐藏删除操作。 |
| `MCP_PRINT_SECRETS` | `0` | `1` = 在容器日志打印 `MCP_TOKEN` 与管理员口令（仅隔离环境使用）；默认只写文件。 |

### v1.1 命令 / 文件 / 长任务 / Docker 变量

| 变量 | 默认值 | 含义 |
|---|---|---|
| `MCP_IMAGE` | `.../plus:v1.1.0` | 镜像标签；升级/回滚只需切换该标签再 `up -d`。 |
| `MCP_MAX_OUTPUT_BYTES` | `2000000` | 单次命令 stdout/stderr 上限（超出会在结果里明确标注截断）。 |
| `MCP_FILE_ROOT` | `/data/files` | 二进制传输的受管根目录：所有路径 `resolve()` 后必须位于其中；`.git`、`.ssh`、`.env`、私钥与令牌文件一律拒绝。 |
| `MCP_FILE_CHUNK_BYTES` | `1048576` | base64 分片上限（钳制在 4096..8 MiB）；签名 HTTP 下载为流式读取，不受此限制。 |
| `MCP_TASK_LOG_DIR` | `/data/audit/task-logs` | 长任务日志持久目录，重启后仍可读。 |
| `MCP_PUBLIC_BASE_URL` | （空） | `download_file` 绝对签名链接的公网 HTTPS 前缀；留空回落到请求 Host，仅适合本机 `curl` 验证。 |
| `MCP_FILE_SIGNING_SECRET` | （空 → `MCP_TOKEN`） | 短时效下载链接的 HMAC 密钥；网关与管理后台必须看到同一个值。 |
| `MCP_DOCKER_SOCKET` | `0` | `1` 且叠加 `docker-compose.host-docker.yml` 才挂载 `/var/run/docker.sock`；该权限等价宿主机 root。 |
| `MCP_HOST_PORT` / `MCP_BIND_HOST` | `8765` / `127.0.0.1` | 网关发布到宿主机的端口与绑定地址（默认仅回环）。 |
| `MCP_ALLOWED_PREFIXES` | （空 = 全部允许） | **仅是可选取紧开关**：留空即 v1.1 默认管理员模式；无论是否设置，Git 删除硬禁、毁灭级黑名单、`confirm` 确认门与全量审计都始终生效。 |

### Admin Dashboard 与审计变量（Compose 读取项目根目录 `.env`）

| 变量 | 默认值 | 含义 |
|---|---|---|
| `ADMIN_USERNAME` / `ADMIN_PASSWORD` | `admin` / 随机生成 | 后台独立管理员账号；不再内置弱口令。留空即首次启动生成强随机口令，以 600 权限持久化在 `audit-data` 卷并在容器重建时复用；显式指定优先。 |
| `ADMIN_SESSION_SECRET` | （空 = 重启后旧会话失效） | 会话 Cookie 的 HMAC 签名密钥。 |
| `ADMIN_SESSION_TTL` | `28800` | 会话有效期（秒）。 |
| `ADMIN_LOGIN_MAX_FAILURES` / `ADMIN_LOGIN_LOCK_MINUTES` | `5` / `15` | 失败次数上限与锁定窗口。 |
| `ADMIN_TOTP_SECRET` | （空 = 关闭） | 可选 TOTP 双因素密钥（Base32）。 |
| `ADMIN_COOKIE_SECURE` | `0` | 通过 HTTPS 访问后台时设为 `1`。 |
| `ADMIN_HOST_PORT` / `ADMIN_BIND_HOST` | `8788` / `127.0.0.1` | 后台监听端口与绑定地址；CNB 等端口预览需设为 `0.0.0.0`，开放前请配置强口令与 MFA。 |
| `AUDIT_DB_DSN` | （空 = SQLite WAL） | 可选 PostgreSQL DSN，配合 `postgres` profile。 |
| `AUDIT_STRICT` | `0` | `1` = 审计存储不可用时拒绝高风险工具；默认降级放行，不中断实验。 |
| `AUDIT_RETENTION_DAYS` / `AUDIT_MAX_DB_MB` | `90` / `512` | 归档保留天数与数据库配额。 |
| `AUDIT_BACKUP_KEEP` / `AUDIT_MAINTENANCE_INTERVAL` | `7` / `300` | 备份保留数量与维护间隔（秒）。 |
| `MCP_TUNNEL_DISABLED` | `0` | 内置随机 quick tunnel 默认开启，部署完即可用 `https://<随机域名>.trycloudflare.com/mcp` 远程连接（域名每次重建变化）。固定入口请用 nginx 反代并设 `MCP_PUBLIC_BASE_URL`，或填 `CF_TUNNEL_TOKEN` 并把本项设为 `1` 后 `docker compose --profile tunnel up -d`；完全关闭公网入口也设为 `1` 并重建。 |

完整变量见 [`.env.example`](./.env.example)，部署细节见
[部署与运维文档](./docs/admin-dashboard.zh-CN.md)。

## Git 删除保护（硬约束）

> 目标：agent 可以**提交、切换/创建分支、拉取、推送**，但**绝不能删除 git 仓库与分支**；
> 这条规则属于硬禁止，`confirm=true` 也不可绕过。

### 三层本地防护

1. **命令层**（`gateway/git_guard.py`）：所有 `run_command`、`run_command_background`、
   REST `/run_command` 在执行前先做引号感知的分段解析，识别 `git branch -D/-d/-m`、
   `git push --delete/--mirror/--prune`、`:branch` / `branch:` / `+branch` 删除型 refspec、
   `git update-ref`、`git remote/worktree prune`、`git fetch --prune`、对 `.git` 的重定向写入，
   以及删除包含 `.git` 的仓库目录（运行时 `stat` 判断）。
   `bash -c` / `sh -c` / `python -c` 内嵌删除命令、写脚本再执行、`base64 -d | sh`
   等混淆写法同样被拒绝。
2. **进程层**（`docker/git-guard-wrapper.py`）：镜像把真实 git 移到
   `/usr/lib/git-core/git.real`，`/usr/bin/git` 换成防护 wrapper。因此**脚本里、
   Python subprocess 里、后台任务里、显式写 `/usr/bin/git` 的调用**都经过
   `check_argv`；未知子命令（可能是 alias）、`git -c alias.*`、`GIT_EXEC_PATH`
   注入一律拒绝。可用 `MCP_GIT_EXTRA_SUBCOMMANDS` 放行你确需的额外子命令。
3. **文件层**：`write_file` 禁止改写 `.git/**` 与系统凭据文件；
   `CATASTROPHIC_PATTERNS` 对常见字面写法再做一次硬禁止。

`git push --force`、`git reset --hard`、`git clean -fd` 仍属「需确认」的破坏性命令
（它们不删除分支），`git status` / `commit` / `switch` / `branch -a` 等完全不受影响。

### 正规通道：结构化 `git_*` 工具

用参数化（`shell=False`）工具完成日常 Git 操作，它们没有删除接口：

```text
git_status(cwd="/workspace/repo")
git_commit(cwd="/workspace/repo", message="exp: tune lr")
git_switch(cwd="/workspace/repo", branch="exp", create=true)
git_push(cwd="/workspace/repo", remote="origin", branch="exp")
```

`git_push` 参数不接受 `:`、`+`、以 `-` 开头的 ref，也不提供 `--force` / `--delete`。

### 容器内 Git 身份与凭据

镜像里没有宿主机的 `/etc/gitconfig`，首次使用 `git_commit` / `git_push` 前需要显式配置，
否则会报 `Please tell me who you are` 或远端认证失败：

```bash
# Compose：在项目根目录 .env 中设置
GIT_USER_NAME=your-name
GIT_USER_EMAIL=you@example.com
# 凭据文件以只读卷挂载（留空 GIT_CREDENTIALS_FILE 时自动探测 /root/.git-credentials）
docker run -v "$HOME/.git-credentials:/root/.git-credentials:ro" ...
```

凭据文件绝不要放进环境变量、命令参数或日志。CNB 等平台会强制提交签名
（`commit.gpgsign=true` + `cnb-gpgsign`），此时**提交作者必须与令牌用户一致**，
否则提交会被签名程序拒绝——这在端到端自检中已复现。

### 必须配合的远端措施（重要）

本地防护防的是**误操作、以及被网页/论文内容诱导的提示注入**，不是对抗持有容器
root 的攻击者（root 可直接改 `.git` 文件或替换二进制）。任何允许任意 shell 的
系统都无法在客户端给出数学意义上的保证。要真正不可逆，请务必同时：

1. 在 CNB / GitHub / GitLab 开启**分支保护**：禁止删除分支、禁止 force push，
   删除仓库要求二次审批（服务端 bare 仓库可设 `receive.denyDeletes=true`、
   `receive.denyNonFastForwards=true`）。
2. 给 agent 使用**最小权限 PAT / 部署密钥**：只允许推送到指定分支，无仓库管理权限；
   不要把管理员 token 放进容器。
3. 配置**独立备份**并定期校验；仓库与分支删除的恢复能力比任何黑名单都可靠。
4. 生产环境可让网关以非 root 用户运行（配合 UID 映射的绑定挂载），
   不要挂载 `/var/run/docker.sock`。

## 权限模型：开放 + 确认门 + 黑名单

为了让 agent 能自由干活，同时防止作死，命令分三类：

1. **普通（normal）**：默认直接执行（如 `ls`、`pip install`、`apt-get`、`python train.py`）。
2. **破坏性（destructive）**：可恢复/可逆，但危险，执行前**必须显式确认**。例如：
   - `rm -rf ./build`、`git reset --hard`、`git clean -fd`
   - `docker rm -f`、`docker rmi -f`
   - `chmod -R`、`chown -R`、`mv` 到系统目录、`git push --force`
   - 数据库 `drop/truncate table`、`:> /etc/xxx` 清空系统文件
   - 不传 `confirm=true` 时返回「⚠️ 需确认」提示**且不执行**；传 `confirm=true` 才放行。
3. **毁灭级（catastrophic）**：硬禁止，连确认都不行（防自杀安全网）。例如：
   - `rm -rf /`、`rm -rf /*`、`mkfs*`、`shutdown`/`halt`/`reboot`/`poweroff`
   - `dd ... of=/dev/*`、fork bomb（`:(){ :|:& };`）、`> /dev/sd*`、`chmod -R` 到根目录
   - `kill_process` 终止 PID 1 / init / systemd 也走硬禁止。
4. **Git 删除（git-guard，优先级最高）**：删除分支/仓库、删除型 refspec、别名与混淆执行，
   由 `gateway.git_guard` 和镜像内 `git` wrapper 硬禁止；详见上一章。

> 所有拒绝、待确认、放行、后台启动/停止都会写进审计日志（容器内 `/app/audit.log`，写入前脱敏）。

## 工具参考

| 工具 | 用途 | 说明 |
|---|---|---|
| `run_command` | 执行 shell 命令 | 破坏性命令加 `confirm=true`（如 `rm -rf`、`git reset --hard`、`docker rm -f`、`chmod -R`…）。毁灭级命令（`rm -rf /`、`mkfs`、`shutdown`、fork bomb…）始终被拦截。 |
| `run_command_background` | 后台启动长任务，拿到 `task_id` | 用于超过同步超时的训练/推理。 |
| `get_task_status` / `get_task_output` / `tail_task` / `stop_task` | 查状态 / 取输出 / 跟随尾部 / 停止后台任务 | `get_task_output(task_id, tail=50)` 返回最后 50 行。 |
| `read_image` | 返回一张图片给 agent 看 | `read_image(path="/workspace/plot.png", max_dim=1024)`；支持 png/jpg/gif/webp/bmp/tiff。 |
| `list_directory` / `read_file` / `write_file` | 文件系统浏览/编辑 | — |
| `search_files` / `grep_content` | 按名查找 / 搜内容 | — |
| `list_processes` / `kill_process` | 进程（PID 1 受保护） | — |
| `get_system_info` / `get_environment_variables` | 主机信息 / 环境变量（密钥脱敏） | — |
| `search_papers` | 跨开放数据源检索论文 | 零密钥；`sources=...`、`from_year`、`sort=date`。 |
| `search_arxiv` | arXiv 字段检索 | `author`、`category`、`from_year`，返回 PDF 链接。 |
| `get_paper` | 查单篇论文 | 按 `doi` / `arxiv_id` / `pmid`；Unpaywall 开放全文需 `MCP_SEARCH_MAILTO`。 |
| `search_web` | 网页 / 技术社区检索 | 默认 HN + StackExchange；可选 GitHub / SearXNG / Tavily。 |
| `read_web_page` | 抓取网页正文 | 默认关闭（`MCP_SEARCH_FETCH_ENABLED=1`）；仅 HTTPS + SSRF 防护。 |
| `git_status` / `git_commit` / `git_switch` / `git_branch_create` / `git_push` | 结构化 Git 操作 | 参数化执行，无删除接口；另有 `git_current_branch` / `git_log` / `git_diff` / `git_branch_list` / `git_add` / `git_fetch` / `git_pull` / `git_remote_list`。 |

### 检索示例

```text
search_papers(query="retrieval augmented generation", sources="openalex,arxiv,crossref", max_results=5, from_year=2024)
search_arxiv(query="diffusion model", category="cs.CV", from_year=2025)
get_paper(doi="10.1038/nature12373")
search_web(query="model context protocol", providers="hn,stackexchange", max_results=5)
read_web_page(url="https://example.com/article")
```

论文数据源默认零密钥即可使用。`search_web` 默认是**技术社区垂直检索**而非全网通用搜索，
三条网页搜索路线的取舍与可选 provider 见 [调研文档](./docs/search-sources-research.md)。
所有外部结果在返回文本中都会标注为不可信内容。

### 后台长任务示例

```text
# 1) 启动一个会跑很久的训练
run_command_background(command="python train.py --epochs 100", cwd="/workspace")

# 返回：task_id=abcd1234...
# 2) 跟进度
tail_task(task_id="abcd1234...")          # 看最后 50 行
get_task_status(task_id="abcd1234...")    # running / done / killed-timeout

# 3) 跑太久了想停
stop_task(task_id="abcd1234...")
```

### 看图片示例

```text
# agent 生成了一张结果图，直接“看”它
read_image(path="/workspace/results/loss_curve.png", max_dim=1024)
```

## 安全说明

- `MCP_TOKEN` 本质上是一把**远程 shell 钥匙**。像密码一样对待它，只分享给你信任的 agent。默认网关是**开放**的（无白名单）；如果你更保守，可以用 `MCP_ALLOWED_PREFIXES` 收紧。
- 单容器模式只监听 `127.0.0.1`。v1.1 Compose 为了方便本机自检与反向代理，把网关发布到
  `127.0.0.1:${MCP_HOST_PORT:-8765}`；这**不是**开放服务——`/mcp`、`/run_command` 及所有工具路由
  仍强制校验 Bearer 令牌。管理后台默认只发布到宿主 `127.0.0.1:8788`，且始终要求独立管理员登录。
  两者都保持回环绑定，公网固定 HTTPS 入口请用 nginx 或 Cloudflare 隧道。
- 检索工具只访问固定域名白名单，限制响应体积并按来源限速；`read_web_page` 默认关闭，开启后也会拦截非 HTTPS、IP 字面量与私网地址（含跳转目标）。
- 外部数据源返回的标题、摘要与正文都是**不可信数据**，工具输出会明确标注，避免提示注入；不要把它们当作指令执行。
- 被执行过的命令，以及检索活动（查询词、来源、耗时、结果条数）都会写入容器内的 `/app/audit.log`；写入前统一脱敏并截断，命令里的 `TOKEN=...` / 连接串口令不会以明文落盘。
- 多服务部署（默认 3 个容器 + 2 个可选 profile）还会把完整调用记录写入共享卷的 SQLite WAL 审计库：参数、输出、错误与派生子表统一脱敏，大输出只保存受保护引用（读取时校验 SHA-256）；
  `AUDIT_STRICT=1` 在写库失败时会真正拒绝高风险工具（含 `git_*`），REST 入口同样受保护。
  后台与 MCP API 的认证相互独立，未登录不能查询任何调用记录，也不能用 `MCP_TOKEN` 登录后台；
  后台写操作额外校验 Origin / Sec-Fetch-Site，登录锁定默认不信任可伪造的 `X-Forwarded-For`。
  详见 [部署与运维文档](./docs/admin-dashboard.zh-CN.md)。
- 公网免鉴权的 `/health` 只返回最小存活状态；系统指标与工具数量改由需 Bearer 的 `/internal/metrics` 提供，后台用 `MCP_TOKEN` 读取。
- `MCP_TOKEN` 与生效管理员口令**默认不再打印到容器日志**，统一用 `./scripts/show-config.sh` 一并打印（底层分别从 `/app/.env` 与 `/data/audit/.admin_password` 读取）；后台不再内置弱口令，留空时首次启动生成强随机值、以 600 权限持久化并在容器重建时复用。OAuth 默认强制 S256 PKCE、校验 `redirect_uri`、签发的 token 有过期并会回收。
- ⚠️ **密钥提醒**：如果你用的是共享/明文出现的 PAT 或令牌，建议用完后立即在对应平台撤销并轮换；agent 用的 Git 凭据务必最小权限，并在远端开启分支保护（见 [Git 删除保护](#git-删除保护硬约束)）。

## 从源码构建

```bash
git clone https://github.com/NinaAWA/cnb-mcp-gateway.git
cd cnb-mcp-gateway
docker build -f docker/Dockerfile -t cnb-mcp-gateway:local .
docker run -d --name cnb-mcp-gateway --restart unless-stopped \
  -v "$PWD:/workspace" cnb-mcp-gateway:local
docker logs -f cnb-mcp-gateway
```

> 构建上下文已改为仓库根目录，因为镜像现在还包含 `gateway/` 统一采集层与 Admin Web。
> 多服务部署用 `docker compose up -d --build`；测试用
> `pip install -r docker/requirements.txt -r requirements-dev.txt && pytest tests -q`。
> 只依赖标准库的策略回归测试可直接运行 `python3 -m unittest discover -s tests -p 'test_*.py'`；
> CI（`.github/workflows/ci.yml`）会执行 ruff 检查与完整测试。

## 许可证

[MIT](./LICENSE)
