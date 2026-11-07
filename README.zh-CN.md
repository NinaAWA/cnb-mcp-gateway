> 📖 中文 | [English](./README.md)

# CNB MCP Gateway（中文文档）

一个**自托管的 MCP（模型上下文协议）服务器**：把你自己机器/服务器上的能力，通过一个 Cloudflare 快速隧道，暴露给**任意** MCP 客户端（Claude Desktop、Cursor、VS Code、Codex、ChatGPT 连接器，或你自己写的 agent），让 AI 能像操作「远程桌面」一样在你的服务器上**跑实验、配环境、看数据、看图片、检索网页与论文**。

```
 你的服务器  ──►  命令 / 文件 / 进程 / 图片 / 后台任务 / 网页与论文检索   ◄──  任意 MCP 客户端
   │
   └─ cloudflared 快速隧道（随机 https 地址） ◄── 鉴权：Bearer 令牌 / OAuth2 PKCE
```

## V3 升级（当前分支 `upgrade/v3-full-admin-research`）

本分支把网关升级为面向**可信管理员**的全权限科研服务器：**49 个 `@mcp.tool()`**（基线 21 个全部保留），
覆盖服务器管理、结构化 Git、科研实验、免费网页搜索、免费学术检索、合法开放获取 PDF 下载与阅读、
本地文献库、分块 / Range 文件传输、Web 管理后台与完整操作审计。

- 中文部署与升级：[docs/v3-upgrade.zh-CN.md](./docs/v3-upgrade.zh-CN.md) ·
  基线清单：[docs/v3-inventory.md](./docs/v3-inventory.md) ·
  验收报告：[docs/v3-acceptance-report.md](./docs/v3-acceptance-report.md)
- **默认 `MCP_ACCESS_MODE=full_admin`**：不启用路径 / Shell 白名单。灾难级正则与
  `confirm` 确认门是**辅助保护，不是安全边界**；真正边界是只把 `MCP_TOKEN` 交给可信管理员。
- **双执行后端**：`container`（默认）或经 0600 Unix Socket + HMAC 授权的 `host`；
  Compose `host` profile 不发布任何端口，真实宿主权限需部署时由用户显式授予，网关不自动提权。
- **免费检索**：8 个零 Key 学术源 + 垂直社区源 + 可选通用搜索（`search` profile 自建 SearXNG）。
  **垂直社区搜索不等于全网通用搜索**；免费 API 不是无限调用；SearXNG 软件免费不等于上游可达。
- **合法 OA PDF**：仅供开放获取来源，严格校验（`%PDF-` 头 / Content-Type / 最终 URL / 大小 /
  EOF / SHA-256 / 原子改名）与断点续传；状态明确区分「任务创建成功」与「文件最终校验成功」。
  `pypdf` 是唯一新增纯 Python 依赖；**OCR 为可选能力，默认不安装、不调用**。
- **后台新增两页**：搜索监控、论文库与下载；所有监控数据标注来自容器 / 宿主机 / 外部来源。

```bash
# 离线全量当前 143 passed（14 条 opt-in 集成测试默认取消选中）
.venv/bin/python -m pytest -q
# 独立真实网络集成 + 8 条真实 MCP 客户端工作流：13 passed / 1 skipped
# （SearXNG 上游未配置）；浏览器视觉验收未执行
.venv/bin/python -m pytest -q -m integration tests/integration/
bash -n docker/entrypoint.sh && node --check gateway/admin/static/app.js && docker compose config -q
```

> 第「功能特性」及后续章节为基线说明（21 个工具）。若与本节冲突，以本节和 V3 文档为准。

## 为什么做这个

你想让 AI agent 能**从任意位置在你的服务器上执行命令**，但又不想直接给一个开放 shell。这个网关把一组受控工具（带令牌门、确认门与危险黑名单安全网、完整审计日志）暴露出来，而不是裸 shell。

## 功能特性

- **通用 MCP**：支持所有会说 MCP 的客户端：
  - **Bearer 直连（最简单）**：向 `/mcp` 发送 `Authorization: Bearer <MCP_TOKEN>`，无需 OAuth 往返。适用于 Claude Desktop / Cursor / VS Code / 各类 SDK。
  - **OAuth 2.1（PKCE + DCR）**：给只支持 OAuth 的客户端（如 ChatGPT 连接器）用。`redirect_uri` 默认要求与 DCR 注册值**精确匹配**并强制 PKCE `S256`；`OAUTH_ALLOW_ANY_REDIRECT=1` / `OAUTH_ALLOW_NO_PKCE=1` 是仅供旧客户端的显式降级开关，默认 `0`。
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
  - `search_papers` —— 聚合 OpenAlex、arXiv、CrossRef、PubMed、Europe PMC（另可选 OpenReview / dblp / Semantic Scholar）；V3 起先跨源合并去重、再全局排序、最后截断，带 DOI、引用数（`None` = 不可用，`0` = 真实零引用）、摘要与开放全文链接，可选 `format="json"`。
  - `search_arxiv` / `get_paper` —— 按作者/分类查 arXiv；按 DOI / arXiv ID / PMID 查单篇论文。
  - `search_web` —— 可插拔 provider，默认 Hacker News + StackExchange（免密钥）；可选 GitHub、自建 SearXNG、Tavily。
  - `read_web_page` —— 可选抓取网页正文（默认关闭），带 SSRF 防护与不可信内容标注。
- **安全模型**：默认开放（无白名单）+ 对破坏性命令的**确认门** + 一份**毁灭级黑名单**（`rm -rf /`、`mkfs`、`shutdown` 等）即便确认也无法绕过。每次操作都有完整审计日志。
- **可调旋钮（Sandbox knobs）**：可选的 `MCP_ALLOWED_PREFIXES` 白名单、`MCP_DENY_PREFIXES`、`MCP_REQUIRE_CONFIRM`、工作目录、审计日志。
- **零配置入口**：cloudflared 快速隧道自动给你一个公网 HTTPS 地址（每次重启地址会变；设置 `CF_TUNNEL_TOKEN` 可固定地址）。
- **自包含镜像**：服务器 + 隧道在同一个容器里。
- **MCP Admin Dashboard（Compose：3 个常驻服务 + 4 个可选 profile）**：独立管理员登录、实时调用监控、完整调用审计、
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

### 带审计后台的 Compose 部署（推荐用于服务器管理与完整调用审计）

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

本轮加固：`redirect_uri` 必须与 `/register` 注册值**精确匹配**（拒绝片段、通配符、内嵌凭据），
GET / POST 都强制 PKCE `S256`，`/token` 会核对 `code_verifier`；签发的令牌按
`MCP_OAUTH_TOKEN_TTL`（默认 30 天）过期并由中间件清理。老客户端可显式设置
`OAUTH_ALLOW_ANY_REDIRECT=1` / `OAUTH_ALLOW_NO_PKCE=1` 降级兼容，默认均为 `0`，
开启会削弱授权码流程，生产环境请勿启用。

## 配置（环境变量）

| 变量 | 默认值 | 含义 |
|---|---|---|
| `MCP_TOKEN` | 随机 48 位 | 静态 Bearer 密钥（固定它可让重启后令牌不变）。 |
| `MCP_ALLOWED_PREFIXES` | （空 = 全部允许） | 可选命令前缀白名单（竖线分隔），如 `git\|python3\|ls`。设置后仅这些前缀可运行。 |
| `MCP_DENY_PREFIXES` | （空） | 追加到**毁灭级硬禁止**列表（逗号或竖线分隔），如 `wipefs,dd if=`。即便 `confirm=true` 也拒绝。 |
| `MCP_REQUIRE_CONFIRM` | （空） | 追加到**需确认**列表（逗号或竖线分隔）；匹配到的命令需 `confirm=true` 才执行。 |
| `CF_TUNNEL_TOKEN` | （空 → 快速隧道） | 命名 Cloudflare 隧道令牌 → **固定地址**。 |
| `MCP_WORKDIR` | `/workspace` | 命令执行的工作目录（自动创建）。 |
| `MCP_WORKDIR_MOUNT` | （未设置） | **仅 `run.sh` 单容器脚本读取**；Compose 部署使用 `docker-compose.yml` 中固定的 `./workspace` 绑定挂载，此变量不生效。 |
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
| `AUDIT_STRICT` | `0` | `1` = 只有确认 `RECEIVED` 行已持久化（数据库健康、缓冲为空、可回读）才放行高风险工具；默认降级缓冲，不中断实验。 |
| `AUDIT_RETENTION_DAYS` / `AUDIT_MAX_DB_MB` | `90` / `512` | 保留天数与数据库配额；覆盖调用、事件、任务、Git、实验、审批、登录、导出与过期会话等所有历史表，`admin_settings` 不清理。 |
| `AUDIT_ARCHIVE_BATCH` / `AUDIT_BUFFER_MAX` | `1000` / `1000` | 归档分批大小（100..5000）与写失败缓冲上限。 |
| `MCP_SESSION_TTL` | `3600` | 客户端会话空闲 / 令牌有效期（秒），用于活跃客户端统计。 |
| `OAUTH_ALLOW_ANY_REDIRECT` / `OAUTH_ALLOW_NO_PKCE` | `0` / `0` | 旧客户端兼容开关，默认关闭；开启有授权码被劫持风险。 |
| `MCP_OAUTH_TOKEN_TTL` | `2592000` | OAuth 令牌有效期（秒）。 |
| `GATEWAY_METRICS_TOKEN` | 自动生成 | `/health/details` 的内部只读令牌，落在共享卷 `/data/audit/.metrics_token`（600）；绝不能交给 agent。 |
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

### V3 新增工具（28 个）

| 分组 | 工具 | 要点 |
|---|---|--- |
| 网页 | `search_web_advanced` / `fetch_web_page` / `extract_web_content` / `search_website` | 站点 / 时间 / 语言筛选、去重、来源与抓取时间、逐源失败原因；垂直源不冒充全网搜索 |
| 论文 | `find_paper_fulltext` / `download_paper` / `batch_download_papers` / `read_paper_pdf` / `search_paper_fulltext` / `list_downloaded_papers` / `get_paper_download_status` / `export_bibliography` | 合法 OA 发现、严格校验与续传、按页阅读、文献库与 BibTeX/RIS/CSV/JSON 导出；`created` ≠ `success` |
| 权限 / 后端 | `request_approval` / `host_status` | 可选独立审批；容器 / 宿主来源与授权状态如实报告 |
| Git | `git_inspect` / `git_clone` / `git_branch` / `git_stage` / `git_commit` / `git_sync` / `git_recover` / `git_worktree` | `shell=False`、按仓库锁、高风险默认 `dry_run`、凭据只走 `GIT_ASKPASS` |
| 传输 / 任务 | `upload_file` / `download_file` / `signed_download_link` / `file_transfer_status` / `list_tasks` / `experiment_manifest` | 分块 / Range / 短期签名链接；任务重关联、进程组终止与复现元数据 |

`run_command` 追加可选 `backend`（默认 `container`）与 `approval_id`；`search_papers` / `get_paper`
追加可选 `format="json"`（默认仍为文本）。所有工具由一处 `AuditMiddleware` 统一审计与脱敏。

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
- Compose 部署还会把完整调用记录写入共享卷的 SQLite WAL 审计库：参数、输出与错误统一脱敏，
  大输出只保存受保护引用；后台与 MCP API 的认证相互独立，未登录不能查询任何调用记录，
  也不能用 `MCP_TOKEN` 登录后台。详见 [部署与运维文档](./docs/admin-dashboard.zh-CN.md)。
- ⚠️ **密钥提醒**：如果你用的是共享/明文出现的 PAT 或令牌，建议用完后立即在对应平台撤销并轮换。
- **健康接口拆分**：公开 `/health` 只返回 `status` / `service` / `uptime_seconds` /
  `audit.healthy`；工具数、审计详情与进程 / 容器指标移到需鉴权的 `/health/details`
  （Bearer 或内部只读指标令牌）。Compose healthcheck 仍指向 `/health`。
- **单一脱敏入口**：所有 SQL 写参数统一经过 `redact_params`；派生 Git / 实验字段、
  旧式 `audit.log`、任务日志回读、归档文件与三种导出也都脱敏。归档按批写 JSONL（600）、
  flush + fsync 并核验行数后才按主键精确删除；任一步失败保留全部数据并写
  `audit_storage_warning`。
- **严格审计是三态判定**：`available()` 只表示「还能缓冲」。`AUDIT_STRICT=1` 时高风险
  工具额外要求 `confirm_persisted(request_id)`；终态写入失败会记录
  `audit_strict_violation` 并体现在 `health()["strict_violations"]`。

## 本轮修复的验证命令

```bash
# 静态与回归
bash -n docker/entrypoint.sh
docker compose config --quiet
.venv/bin/python -m pytest tests -q          # 覆盖 6000 条归档、1200 条导出、OAuth、启动

# 空 MCP_TOKEN 启动（入口会自动生成 48 位随机口令并打印到容器日志）
unset MCP_TOKEN; docker compose up -d --build mcp-gateway

# 健康接口隔离（网关在容器网络内，本机可用 docker compose exec 调用）
docker compose exec mcp-gateway curl -fsS http://127.0.0.1:8765/health
docker compose exec mcp-gateway curl -fsS http://127.0.0.1:8765/health/details   # 期望 401
TOKEN=$(docker compose exec mcp-gateway cat /data/audit/.metrics_token)
docker compose exec mcp-gateway curl -fsS -H "Authorization: Bearer $TOKEN" \
  http://127.0.0.1:8765/health/details

# 重启持久化：重建后台容器后历史记录仍在
docker compose up -d --force-recreate mcp-admin-web
docker compose exec mcp-admin-web python -c \
  "from gateway.audit.storage import get_storage; print(get_storage().query_calls({})[1])"
```

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
> Compose 部署用 `docker compose up -d --build`；测试用 `.venv/bin/python -m pytest tests -q`。

## 许可证

[MIT](./LICENSE)
