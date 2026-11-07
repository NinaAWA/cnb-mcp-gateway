> 📖 中文 | [English](./README.md)

# CNB MCP Gateway（中文文档）

一个**自托管的 MCP（模型上下文协议）服务器**：把你自己机器/服务器上的能力，通过一个 Cloudflare 快速隧道，暴露给**任意** MCP 客户端（Claude Desktop、Cursor、VS Code、Codex、ChatGPT 连接器，或你自己写的 agent），让 AI 能像操作「远程桌面」一样在你的服务器上**跑实验、配环境、看数据、看图片、检索网页与论文**。

```
 你的服务器  ──►  命令 / 文件 / 进程 / 图片 / 后台任务 / 网页与论文检索   ◄──  任意 MCP 客户端
   │
   └─ cloudflared 快速隧道（随机 https 地址） ◄── 鉴权：Bearer 令牌 / OAuth2 PKCE
```

## 为什么做这个

你想让 AI agent 能**从任意位置在你的服务器上执行命令**，但又不想直接给一个开放 shell。这个网关把一组受控工具（带令牌门、确认门与危险黑名单安全网、完整审计日志）暴露出来，而不是裸 shell。

## 功能特性

- **通用 MCP**：支持所有会说 MCP 的客户端：
  - **Bearer 直连（最简单）**：向 `/mcp` 发送 `Authorization: Bearer <MCP_TOKEN>`，无需 OAuth 往返。适用于 Claude Desktop / Cursor / VS Code / 各类 SDK。
  - **OAuth 2.1（PKCE + DCR）**：给只支持 OAuth 的客户端（如 ChatGPT 连接器）用。`redirect_uri` 接受任意合法的绝对 URI（`https://`、`http://`、自定义 scheme），不会把非 ChatGPT 的 agent 挡在门外。
- **远程管理工具集（灵感来自 Remote Desktop Commander，但适配 Linux）**——真正像 RDC 一样跑实验、配环境、看数据与图片：
  - `run_command` —— 执行 shell 命令（默认**开放**；破坏性命令需要 `confirm=true`；毁灭级命令被安全网**硬禁止**）。
  - `run_command_background` + `get_task_status` / `get_task_output` / `tail_task` / `stop_task` —— 跑**长实验**（训练/推理），不被超时掐断。
  - `list_directory` / `read_file` / `write_file` —— 浏览与编辑文件系统。
  - `search_files` / `grep_content` —— 按文件名查找或搜索文件内容。
  - `list_processes` / `kill_process` —— 查看与终止进程（PID 1 受保护）。
  - `read_image` —— 返回一张图片（图表、截图），让多模态 agent 能**看见**它。
  - `get_system_info` —— 内核、运行时间、CPU、内存、磁盘。
  - `get_environment_variables` —— 当前环境变量（密钥值已脱敏）。
- **网页搜索 + 论文检索（零 API Key）**，方案调研见 [免费/开源数据源选型](./docs/search-sources-research.md)：
  - `search_papers` —— 聚合 OpenAlex、arXiv、CrossRef、PubMed、Europe PMC（另可选 OpenReview / dblp / Semantic Scholar）；多源结果轮流交错，带 DOI、引用数、摘要与开放全文链接。
  - `search_arxiv` / `get_paper` —— 按作者/分类查 arXiv；按 DOI / arXiv ID / PMID 查单篇论文。
  - `search_web` —— 可插拔 provider，默认 Hacker News + StackExchange（免密钥）；可选 GitHub、自建 SearXNG、Tavily。
  - `read_web_page` —— 可选抓取网页正文（默认关闭），带 SSRF 防护与不可信内容标注。
- **安全模型**：默认开放（无白名单）+ 对破坏性命令的**确认门** + 一份**毁灭级黑名单**（`rm -rf /`、`mkfs`、`shutdown` 等）即便确认也无法绕过。每次操作都有完整审计日志。
- **可调旋钮（Sandbox knobs）**：可选的 `MCP_ALLOWED_PREFIXES` 白名单、`MCP_DENY_PREFIXES`、`MCP_REQUIRE_CONFIRM`、工作目录、审计日志。
- **零配置入口**：cloudflared 快速隧道自动给你一个公网 HTTPS 地址（每次重启地址会变；设置 `CF_TUNNEL_TOKEN` 可固定地址）。
- **自包含镜像**：服务器 + 隧道在同一个容器里。
- **MCP Admin Dashboard（四服务 Compose 部署）**：独立管理员登录、实时调用监控、完整调用审计、
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

docker logs -f cnb-mcp-gateway   # 会打印 BASE_URL、/mcp、openapi、MCP_TOKEN
```

或用自带的辅助脚本（所有参数可用环境变量覆盖）：

```bash
./run.sh
```

### 带审计后台的四服务部署（推荐用于服务器管理与完整调用审计）

```bash
cp docker/.env.template .env
# 建议在 .env 中固定 MCP_TOKEN 与 ADMIN_PASSWORD
docker compose up -d --build
docker logs cnb-mcp-admin-web       # 未设置 ADMIN_PASSWORD 时这里打印初始口令
```

后台默认只在宿主机 `http://127.0.0.1:8788` 可达，使用**独立管理员账号**登录（与 `MCP_TOKEN` 无关）；
MCP API 仍通过网关容器日志里的隧道地址访问，两者访问控制互相独立。
配置、可选 HTTPS 入口、PostgreSQL、备份与验收清单见
[部署与运维文档](./docs/admin-dashboard.zh-CN.md)。

单容器模式的启动横幅会打印你需要的一切：

```
 公网根地址 (BASE_URL):   https://xxxx.trycloudflare.com
 MCP 连接器地址 (MCP_URL): https://xxxx.trycloudflare.com/mcp
 ChatGPT 插件 OpenAPI 清单: https://xxxx.trycloudflare.com/openapi.yaml
 密钥 (MCP_TOKEN):        <随机 48 位令牌>
```

> 每次重启容器 → **新的随机地址** + **新的随机令牌**（除非你固定它们，见下）。每次重启后重新看一眼 `docker logs`。

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

### Admin Dashboard 与审计变量（Compose 读取项目根目录 `.env`）

| 变量 | 默认值 | 含义 |
|---|---|---|
| `ADMIN_USERNAME` / `ADMIN_PASSWORD` | `admin` / 随机生成 | 后台独立管理员账号；随机口令持久化在 `audit-data` 卷，重启不变。 |
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
| `MCP_TUNNEL_DISABLED` | `0` | 启用独立 `cloudflared` 服务时设为 `1`。 |

完整变量见 [`docker/.env.template`](./docker/.env.template)，部署细节见
[部署与运维文档](./docs/admin-dashboard.zh-CN.md)。

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

> 所有拒绝、待确认、放行、后台启动/停止都会写进审计日志（容器内 `/app/audit.log`）。

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
- 单容器模式只监听 `127.0.0.1`；Compose 模式下网关监听容器网络 `0.0.0.0` 但**不发布宿主端口**，
  由 Cloudflare 终结 TLS 并暴露公网端点。管理后台默认只发布到宿主 `127.0.0.1:8788`，
  且始终要求独立管理员登录。
- 检索工具只访问固定域名白名单，限制响应体积并按来源限速；`read_web_page` 默认关闭，开启后也会拦截非 HTTPS、IP 字面量与私网地址（含跳转目标）。
- 外部数据源返回的标题、摘要与正文都是**不可信数据**，工具输出会明确标注，避免提示注入；不要把它们当作指令执行。
- 被执行过的命令，以及检索活动（查询词、来源、耗时、结果条数）都会写入容器内的 `/app/audit.log`；密钥与响应体不会写入日志。
- 四服务部署还会把完整调用记录写入共享卷的 SQLite WAL 审计库：参数、输出与错误统一脱敏，
  大输出只保存受保护引用；后台与 MCP API 的认证相互独立，未登录不能查询任何调用记录，
  也不能用 `MCP_TOKEN` 登录后台。详见 [部署与运维文档](./docs/admin-dashboard.zh-CN.md)。
- ⚠️ **密钥提醒**：如果你用的是共享/明文出现的 PAT 或令牌，建议用完后立即在对应平台撤销并轮换。

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
> 四服务部署用 `docker compose up -d --build`；测试用 `.venv/bin/python -m pytest tests -q`。

## 许可证

[MIT](./LICENSE)
