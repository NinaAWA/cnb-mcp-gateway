# CNB MCP Gateway V3 升级与部署指南（中文）

> 适用基线：GitHub `main` = `3cf1ab57`（tree `e353ce3`）。工作分支 `upgrade/v3-full-admin-research`。
> 本文描述的是**已落地的代码行为**，未执行的验收项统一标注「未执行」。
> 配套清单：[v3-inventory.md](./v3-inventory.md)；验收记录：[v3-acceptance-report.md](./v3-acceptance-report.md)。

---

## 1. 升级目标与阶段

| 阶段 | 内容 | 主要新增模块 |
| --- | --- | --- |
| A | 统一严格审计门、导入失败显式告警、磁盘 spool、归档 SHA-256 清单与恢复、OAuth 持久化与撤销、全链路脱敏、后台 CSRF 与会话上限 | `gateway/audit/spool.py`、`gateway/oauth/store.py` |
| B-检索 | `from_year` 语义统一、跨源去重合并、全局排序、结构化元数据与 JSON 输出、可选通用搜索、抓取安全加固 | `gateway/net/client.py`、`gateway/research/metadata.py`、`gateway/web/tools.py`、`searxng/settings.yml` |
| B-论文 | 合法 OA 全文发现、单篇/批量 PDF 下载、严格校验与断点续传、按页阅读与全文检索、本地文献库与导出 | `gateway/research/{fulltext,download,pdf,library,batch}.py`、`gateway/transfer/core.py` |
| C | 默认完整管理员权限、可配置辅助保护与可选独立确认、容器/宿主双后端、8 个结构化 Git 工具与凭据保护 | `gateway/access/policy.py`、`gateway/exec/backends.py`、`gateway/host/`、`gateway/gitops/tools.py` |
| D | 任务持久化与重启重关联、进程组终止、实验复现元数据、通用分块与 Range 传输、认证下载入口 | `gateway/tasks/manager.py`、`gateway/transfer/http_api.py` |
| E | 搜索监控、论文库/下载监控、后端来源标识、全量调用审计与结果摘要 | `gateway/admin/api.py`、`templates/index.html`、`static/app.{js,css}` |
| F | 离线单测、独立真实网络集成、8 条端到端工作流、中文文档、验收报告与推送 | `tests/integration/`、本文与验收报告 |

**顺序约束**：阶段 B（检索与论文）排在 Git 与科研实验能力（C/D）之前，与升级方案一致。

---

## 2. 快速升级（不中断既有数据）

```bash
# 0) 备份当前配置；不要执行 docker compose down -v
cp .env .env.bak 2>/dev/null || true
git fetch github main
git log -1 --oneline github/main

# 1) 同步可选变量（只追加，不覆盖已有值）
diff -u docker/.env.template .env || true      # 人工比对后按需合并

# 2) 重建必要服务（保留 audit-data 卷、workspace 挂载与实验数据）
docker compose up -d --build mcp-gateway mcp-admin-web audit-storage
docker compose ps
docker compose exec mcp-gateway curl -fsS http://127.0.0.1:8765/health

# 3) 后台：打开 http://127.0.0.1:8788/ 用独立管理员口令登录
```

> **禁止** `docker compose down -v`、`rm -rf workspace/`、`rm -rf audit-data`；论文目录 `/workspace/research/papers` 与传输目录 `/workspace/transfers` 位于 `./workspace` 绑定挂载内，属于实验数据，重建容器不会删除。回退点 `183ee29` 保留。

---

## 3. Compose profiles

| profile | 启动命令 | 作用 | 端口 |
| --- | --- | --- | --- |
| 默认 | `docker compose up -d` | 网关、后台、维护服务共 3 个常驻（全文件共 7 个 service：3 常驻 + 4 profile） | 网关 `8765`、后台 `8788`（默认 `127.0.0.1`） |
| `search` | `docker compose --profile search up -d` | 自建 SearXNG，启用 JSON API，仅在内部网络 | **不发布宿主端口** |
| `host` | `docker compose --profile host up -d` | 容器化 Host Agent，共享 `host-socket` 卷 | **不发布任何端口**（Unix Socket） |
| `tunnel` | `docker compose --profile tunnel up -d` | cloudflared HTTPS 入口 | 由隧道提供 |
| `postgres` | `docker compose --profile postgres up -d` | PostgreSQL 审计后端 | 仅内部网络 |

启用 SearXNG 后还需在 `.env` 中设置：

```env
MCP_SEARCH_SEARXNG_URL=http://searxng:8080
MCP_SEARCH_WEB_PROVIDERS=hn,stackexchange,searxng
SEARXNG_SECRET=<随机长字符串>
```

**如实说明**：SearXNG 是**软件免费**，不等于其上游引擎在你的部署环境可达；上游连通性必须在部署环境实测。管理后台「搜索监控」页固定显示「未检测」，不会自动发起外部请求或虚构可达性。

`searxng/settings.yml` 启用 JSON API 与引擎级 `limiter`，实例不发布宿主端口、
`public_instance: false`。文件中的 `server.secret_key` 是**占位值**：请先设置
`SEARXNG_SECRET`；由于该文件以 `:ro` 挂载，若所用 SearXNG 镜像入口无法用环境变量覆盖
`secret_key`，请改为挂载写入真实密钥的 `searxng/settings.local.yml`（已在 `.gitignore` 中，
禁止提交）。不要保留占位密钥用于生产。

---

## 4. 权限模型（重要，务必先读）

默认 `MCP_ACCESS_MODE=full_admin`：**不启用路径白名单、不启用 Shell 白名单**，持有 `MCP_TOKEN` 的可信管理员可在执行环境内执行任意命令。

- 内置的灾难级正则拦截与「破坏性命令需 `confirm=true`」是**辅助保护，不是安全边界**：命令行拼接、别名、解释器、脚本文件都能绕过正则。真正的安全边界是「只把 `MCP_TOKEN` 交给可信管理员」与容器隔离。
- `MCP_DANGER_GUARD=1` 保持既有硬禁行为；`0` 显式关闭。
- `MCP_INDEPENDENT_CONFIRM=0` 默认关闭；设为 `1` 时，破坏性命令除 `confirm=true` 外还需先调用 `request_approval(command=...)` 取得一次性审批 ID 并传入 `approval_id`。审批绑定命令 SHA-256 摘要、300 秒有效、仅可消费一次。
- `restricted` 为兼容模式，启用 `MCP_ALLOWED_PREFIXES` 命令前缀白名单。
- 后台使用**独立管理员口令**，绝不能用 `MCP_TOKEN` 登录。CSRF 双提交 Cookie 始终启用，`ADMIN_MAX_SESSIONS=8` 超出时撤销最旧会话。

---

## 5. 双执行后端与 Host Agent

`MCP_EXEC_BACKEND=container`（默认）走容器内既有 `_exec` / `Popen`；`run_command(..., backend="host")` 或 `MCP_EXEC_BACKEND=host` 走宿主代理。

**没有隐式降级**：socket 或令牌缺失时明确返回 `HOST_AGENT_UNAVAILABLE` 及可操作提示，不会悄悄改用容器执行并声称是宿主机。

```env
MCP_HOST_AGENT_TOKEN=<与 Host Agent 共享的随机密钥>
MCP_HOST_AGENT_SOCKET=/var/run/mcp-host-agent/agent.sock
MCP_HOST_AGENT_ROOTS=/workspace
MCP_HOST_AGENT_HOST_DIR=./workspace
```

- 协议：JSON-lines 单帧 ≤ 1MB，HMAC-SHA256 签名 + 60 秒时间窗防重放，操作白名单 `ping/system_info/run/spawn/kill/git/list_dir/docker_ps/processes`，socket 权限 `0600`，**无 TCP、无公网管理端口**。
- 路径必须落在 `MCP_HOST_AGENT_ROOTS` 内，否则返回 `BAD_ROOT`；`kill` 只允许终止本 Agent 自己启动的进程组；GPU 数据未配置采集器时如实返回 `unavailable`，不虚构指标。
- **重要**：Compose `host` profile 是「容器内 Host Agent」，只能访问显式挂载的目录，**不等于取得真实宿主 root 权限**。真实宿主权限必须由运维在宿主机上自行运行 `python -m gateway.host.agent`、注入共享密钥并把 socket 挂给网关；本仓库不自动提权。
- 风险：启用后 `run` 会在授权目录内以 `shell=True` 执行宿主命令，等价于把该目录的宿主权限交给 `MCP_TOKEN` 持有者。仅在可信网络、最小目录范围内启用。

`host_status` 工具与后台 `/api/host/status` 会标出容器/宿主来源与 `scope_note`；`/api/host/status` 在未授权时明确 `available=false` 并给出原因。

---

## 6. 免费检索与论文下载

### 免费数据源与限制

| 来源 | 用途 | 限额/注意事项 |
| --- | --- | --- |
| arXiv / Crossref / OpenAlex / PubMed / Europe PMC / Semantic Scholar / dblp / OpenReview | 学术元数据检索 | 全部零付费密钥；有各自速率限制与维护窗口，`429`/`5xx` 会指数退避（含 `Retry-After`）；`MCP_SEARCH_MAILTO` 可提升 OpenAlex / Crossref / NCBI / Unpaywall 的礼貌池额度 |
| HN Algolia / StackExchange / GitHub API | **垂直社区搜索** | 不是全网通用搜索，不得冒充 |
| DuckDuckGo / Wikipedia / 自建 SearXNG | 可选通用搜索 | 需显式启用；上游可达性须运行时探测 |
| Tavily | 可选 | 需要免费额度密钥 `MCP_SEARCH_TAVILY_KEY`；不配置时该来源明确报不可用 |
| Unpaywall | OA 位置发现 | **必须**配置 `MCP_SEARCH_MAILTO`；缺失时返回 `SOURCE_UNAVAILABLE`，不静默跳过 |

**不得**把垂直社区搜索描述为全网通用搜索；**不得**把免费 API 描述为无限调用或全网可达。

### 合法 OA 与下载状态

发现链：arXiv 官方 PDF → Europe PMC / PMC → OpenAlex OA 位置 → Unpaywall → 合法机构 / 出版社 OA。强制主机白名单，禁止盗版镜像与付费墙绕过；`MCP_PAPER_EXTRA_HOSTS` 可追加自建机构镜像。

下载状态**必须区分**：

| 状态 | 含义 |
| --- | --- |
| `created` | 任务已创建，**尚未拿到文件或尚未校验** |
| `downloading` / `verifying` | 流式下载中 / 校验 `%PDF-` 头、Content-Type、最终 URL、大小、EOF、SHA-256 中 |
| `success` | 文件最终校验通过、fsync + 原子改名落盘 |
| `duplicate` | 同 SHA-256 已存在，未重复下载 |
| `failed` / `no_oa` / `source_unavailable` | 明确失败原因，**绝不假报下载成功** |

`MCP_PAPER_DIR=/workspace/research/papers`、`MCP_PAPER_MAX_MB=64`、`MCP_DOWNLOAD_CONCURRENCY=3`。下载全程流式，不把正文写入审计库；大 PDF 只返回路径 / 签名链接，不转 Base64。`pypdf` 是唯一新增的纯 Python 依赖；**OCR 是可选能力，默认不安装、不调用**，扫描版 PDF 会明确提示。

### 文件传输

`MCP_TRANSFER_DIR=/workspace/transfers`、`TRANSFER_MAX_MB=256`、`MCP_TRANSFER_SECRET`（建议单独设置，不复用 `MCP_TOKEN`）。网关侧 `POST /files/upload`（`X-File-Path` / `X-Upload-Offset` / `X-File-Sha256`）与 `GET /files/download`（`Range` / 短期 HMAC 签名）；后台侧 `/api/transfers*` 提供监控与签名下载。两端点均进入 `tool_calls` 审计（`source="http"`），严格模式下同样受审计门约束。

---

## 7. 工具清单（49 个 `@mcp.tool()`）

- **既有 21 个**（保持不变）：`run_command`、`run_command_background`、`get_task_status`、`get_task_output`、`tail_task`、`stop_task`、`list_directory`、`read_file`、`write_file`、`search_files`、`grep_content`、`list_processes`、`kill_process`、`get_system_info`、`get_environment_variables`、`read_image`、`search_papers`、`search_arxiv`、`get_paper`、`search_web`、`read_web_page`。
- **检索新增 4 个**：`search_web_advanced`、`fetch_web_page`、`extract_web_content`、`search_website`。
- **论文新增 8 个**：`find_paper_fulltext`、`download_paper`、`batch_download_papers`、`read_paper_pdf`、`search_paper_fulltext`、`list_downloaded_papers`、`get_paper_download_status`、`export_bibliography`。
- **权限/后端新增 2 个**：`request_approval`、`host_status`。
- **Git 新增 8 个**：`git_inspect`、`git_clone`、`git_branch`、`git_stage`、`git_commit`、`git_sync`、`git_recover`、`git_worktree`（全部 `shell=False`、按仓库锁、凭据只走 `GIT_ASKPASS`）。高风险写操作 `git_clone` / `git_commit` / `git_sync` / `git_recover` 默认 `dry_run=True` 只预览；`git_inspect` 为只读无该参数，`git_branch` / `git_stage` / `git_worktree` 默认 `dry_run=False`，调用方需自行按需开启预览。
- **传输/任务新增 6 个**：`upload_file`、`download_file`、`signed_download_link`、`file_transfer_status`、`list_tasks`、`experiment_manifest`。

既有工具的默认文本输出与参数保持兼容；`search_papers` / `get_paper` 新增可选 `format="json"`，默认仍为文本；`run_command` 新增可选 `backend` / `approval_id`。

---

## 8. 管理后台

新增「搜索监控」「论文库与下载」两页，并扩展仪表盘总览、完整调用记录、科研实验监控、服务器与执行后端四页。全部数据块标注「容器 / 宿主机 / 外部来源」与更新时间。

- 搜索监控：逐源请求/成功率/耗时/返回条数/API 限流/缓存命中矩阵、失败原因展开、覆盖范围声明与「上游连通性：未检测」。
- 论文库：标题/作者/年份/期刊/DOI/OA 状态/SHA-256 校验、BibTeX/RIS/CSV/JSON 导出、PDF 预览与签名下载、下载任务进度条与分段状态。
- 调用记录：新增 `source` 筛选（mcp / rest / admin / http / host-agent / …）与结果摘要列。
- 服务器页：「容器 / 宿主」来源切换；未授权时明确显示原因，不展示任何宿主指标。

---

## 9. 审计与数据保护

- 所有 MCP 工具由 `mcp.add_middleware(AuditMiddleware())` 一处覆盖；REST、任务、后台操作与非工具 HTTP 端点通过 `trace_call` / `record_event` 补齐进入 `tool_calls`。
- `AUDIT_STRICT=0` 默认降级放行并缓冲；`AUDIT_REQUIRED=1` 时审计模块导入失败会拒绝启动，除非显式 `AUDIT_ALLOW_UNAUDITED=1`（须由运维承担风险）。
- DB 故障路径写 `spool/` 下 0600 JSONL + fsync 的磁盘缓冲，启动时回放；`AUDIT_SPOOL_MAX_MB=64` / `AUDIT_SPOOL_MAX_FILES=4` 有界轮转。
- 归档生成流式 SHA-256 + `.manifest.json` 清单，删前二次校验；`/api/archives*` 提供列表、校验、查询与幂等恢复（`INSERT OR IGNORE`）。
- OAuth 客户端 / 授权码 / 令牌写入穿透审计库，磁盘只存 SHA-256 摘要；提供 `/revoke` 撤销端点。
- 凭据保护：Git PAT 只走 `GIT_ASKPASS` / credential helper，绝不进入 argv 或日志；任务日志逐行脱敏后落盘；`MCP_TOKEN`、管理员口令、PAT、完整 PDF 正文不打印、不写审计库。

---

## 10. 验收命令

```bash
# 离线单测（必须用仓库 venv；全局 python 无 pytest）
.venv/bin/python -m pytest -q

# 阶段专项
.venv/bin/python -m pytest -q tests/test_phase_a_integrity.py tests/test_audit_security.py
.venv/bin/python -m pytest -q tests/test_search_web.py tests/test_paper_search.py
.venv/bin/python -m pytest -q tests/test_pdf_download.py tests/test_pdf_read.py tests/test_library.py
.venv/bin/python -m pytest -q tests/test_access_mode.py tests/test_host_agent.py tests/test_gitops.py
.venv/bin/python -m pytest -q tests/test_tasks_v3.py tests/test_transfer.py
.venv/bin/python -m pytest -q tests/test_dashboard_v3.py

# 语法 / 配置
bash -n docker/entrypoint.sh
node --check gateway/admin/static/app.js
docker compose config -q
docker compose --profile search config -q
docker compose --profile host config -q

# 独立真实网络集成（opt-in，默认不进常规套件）
.venv/bin/python -m pytest -q -m integration tests/integration/
```

**当前状态**：离线全量 `143 passed`（14 条 opt-in 集成测试默认取消选中）；
`-m integration tests/integration/` → `13 passed, 1 skipped`（SearXNG 上游未配置）。
`bash -n`、`node --check`、`compileall`、默认及 `search` / `host` / `tunnel` /
`postgres` 四个 profile 的 `docker compose config -q` 均已通过；
`docker compose up -d --build` 已真实重建，3 个常驻服务全部 healthy，审计卷数据保留。
8 条真实 MCP 客户端工作流全部通过（见验收报告）。

**已额外实测**：`search` profile 真实启动，SearXNG `/healthz=OK`、JSON API 从网关容器返回
HTTP 200，但 `results=[]` 且 brave / duckduckgo / google cse / wikidata / wikipedia 五个上游
引擎全部 `timeout` —— 实证「SearXNG 软件免费不等于上游可达」。`host` profile 真实启动，
socket 为 0600、无宿主端口发布，正确 HMAC 令牌 `ping` 成功、错误令牌被拒 `AUTH_FAILED`。
验证后两个可选容器已停止，恢复为 3 常驻服务。

**仍未执行**：浏览器视觉验收、真实宿主 root 权限（上面的 Host Agent 是容器内实例，
不等于真实宿主权限，需部署时由用户挂载授权）、免费 API 真实限额 / 429 降级观测、
PMC OA PDF 真实下载、`tunnel` / `postgres` profile 实际启动、双远端推送。
`config -q` 只证明配置语法合法，不等于服务已实际启动并验收。

---

## 11. 凭据与推送纪律

- 明文 PAT 视为**已泄露**：仅用于验收完成后单次内存态 `credential.helper` 推送，不写 `.git/config`、`.git/.git-credentials`、remote URL 或任何仓库文件。
- 推送前后核对上述文件哈希不变；推送完成后立即在平台侧**吊销并轮换**这两枚 PAT。
- 不改写 `main` / 历史，不 force-push，不删除实验数据、论文文件、模型权重或既有数据卷。
