# MCP Admin Dashboard 部署与运维（中文）

> 适用分支：`upgrade/v3-full-admin-research`（基线 GitHub `main` = `3cf1ab57`）。
> 目标是在不削弱原有 Shell / 文件 / Git / 实验能力的前提下，完整记录、查询、审计与监控
> AI Agent 对服务器发起的每一次操作。第 1–10 节为后台既有能力说明，第 11 节为 V3 扩展；
> 若两处描述冲突，以第 11 节与 [v3-upgrade.zh-CN.md](./v3-upgrade.zh-CN.md) 为准。

## 1. 架构与数据流

| 服务 | 角色（`MCP_SERVICE_ROLE`） | 暴露方式 | 认证 |
|---|---|---|---|
| `mcp-gateway` | `gateway`（默认） | 仅容器网络 `8765`，无宿主端口 | `MCP_TOKEN` / OAuth |
| `mcp-admin-web` | `admin-web` | 宿主机 `127.0.0.1:8788` | 独立管理员账号 + 会话 Cookie |
| `audit-storage` | `maintenance` | 无端口，定时维护共享卷 | — |
| `cloudflared` | `cloudflared`（profile `tunnel`） | 可选 HTTPS 入口 | 由 Cloudflare 终结 TLS |
| `postgres` | profile `postgres` | 可选审计后端 | `AUDIT_DB_DSN` |

```
MCP tools/call ─┐
REST /run_command ─┼─> 统一采集层 ─> SQLite WAL (/data/audit/audit.db) ─> Admin Web
后台任务生命周期 ─┘        │                    │
                    脱敏 / 大输出引用      SSE / 图表 / 详情 / 导出
```

采集发生在 FastMCP 工具分发中间件与 REST/任务入口，**不修改任何现有工具内部**，原有 21 个工具保持兼容。
管理后台与网关是不同进程：后台停止不会影响已启动的训练任务；网关离线时后台仍可查询历史。

## 2. 快速开始

```bash
cp docker/.env.template .env
# 至少固定 MCP_TOKEN；生产环境建议同时设置 ADMIN_PASSWORD
docker compose up -d --build
docker logs cnb-mcp-admin-web | grep -A4 "自动生成"   # 未设置 ADMIN_PASSWORD 时查看初始口令
# 浏览器打开 http://127.0.0.1:8788
```

首次启动若未设置 `ADMIN_PASSWORD`，入口脚本会生成随机口令、以 `600` 权限写入共享卷
`/data/audit/.admin_password`，并打印到容器日志；**重启后口令不变**。`.env` 含密钥，不要提交到仓库。

单容器快速启动（`./run.sh` / `docker run`）仍保持原有行为：一个容器里运行 MCP 网关与内置 quick tunnel，
但不会启动管理后台；如需审计后台请使用 Compose 部署。

## 3. 认证与安全边界

- 后台使用**独立管理员账号**，绝不接受 `MCP_TOKEN` 登录；口令以 PBKDF2-HMAC-SHA256（200,000 轮）校验。
- 会话 Cookie 为 HMAC 签名、`HttpOnly`、`SameSite=Lax`，默认 8 小时过期。
  未配置 `ADMIN_SESSION_SECRET` 时密钥随容器重启变化，旧会话自动失效（更安全）。
- 连续 5 次失败登录后锁定 15 分钟（可配置）；设置 `ADMIN_TOTP_SECRET` 后启用 TOTP 双因素。
- 未登录时：页面 `/` 重定向 `/login`，所有 `/api/*` 返回 `401`；`/healthz` 只返回后台与审计存储状态，
  不含任何调用数据。管理员可在「客户端与会话」中强制下线任意会话。
- 导出文件通过 10 分钟有效的 HMAC 签名链接下载；实验日志只能读取任务日志目录内的文件。
- **默认不把后台暴露到公网**：Compose 只把后台发布到 `127.0.0.1`，MCP API 不发布宿主端口。
  需要 HTTPS 公网访问时，请使用固定域名隧道，并设置 `ADMIN_PASSWORD`（强口令）、
  `ADMIN_COOKIE_SECURE=1`、`ADMIN_TOTP_SECRET`；不要以「地址隐藏」代替认证。
- 在 CNB / 云平台用「端口预览」查看后台时，代理会连工作区容器 IP 的端口，需要临时设置
  `ADMIN_BIND_HOST=0.0.0.0` 并重启后台服务（默认值仍为 `127.0.0.1`）。这会让后台随预览 URL 可达，
  因此务必先设置强口令、开启 MFA，看完后恢复默认绑定。
- 通过公网访问后台时建议再加一层来源 IP 限制 / SSO；后台的`/api/config` 不返回任何敏感值明文。
- **健康接口拆分**：网关公开 `/health` 只返回 `status`、`service`、`uptime_seconds`
  与 `audit.healthy`；工具数、审计健康详情和进程 / 磁盘 / 容器指标移到 `/health/details`，
  必须携带有效 Bearer 或内部只读指标令牌。Compose healthcheck 仍用 `/health`。
- **内部指标令牌**：网关入口在共享卷幂等生成 `/data/audit/.metrics_token`（权限 `600`，
  重启不变），也可通过 `GATEWAY_METRICS_TOKEN` 显式指定。后台用它请求 `/health/details`，
  失败自动降级到最小 `/health`；该令牌**只允许访问详情接口**，不是 `MCP_TOKEN`，
  不能用于任何业务路径，也绝不交给 agent。
- **OAuth 加固**：`redirect_uri` 与 DCR 注册值精确匹配（拒绝片段 / 通配符 / 内嵌凭据），
  默认强制 PKCE `S256` 并核对 `code_verifier`；令牌按 `MCP_OAUTH_TOKEN_TTL` 过期并清理。
  `OAUTH_ALLOW_ANY_REDIRECT=1` / `OAUTH_ALLOW_NO_PKCE=1` 是仅供旧客户端的显式降级开关，
  默认 `0`，开启会降低安全性。
- **REST `confirm` 严格解析**：只接受布尔真或 `true/1/yes/on`；`"false"`、`"0"` 等字符串
  不再被误判为「已确认」而放行破坏性命令。

## 4. 审计数据模型

每次调用记录包含：`request_id`、`trace_id`、起止时间、耗时、工具名、参数、结果、状态、错误类型/信息、
退出码、客户端、操作者、会话、来源 IP、`task_id`、`approval_id`、`git_repo`、`experiment_id`、`file_path`。
状态为 `RECEIVED / RUNNING / SUCCESS / FAILED / DENIED / TIMEOUT / CANCELLED / INTERRUPTED`。

- **身份边界**：`client_id` / `actor_id` 来自已验证 Bearer Token 的 SHA-256 指纹；
  `declared_client`（`X-MCP-Client`、User-Agent、clientInfo）只是自报名称，**仅用于展示，绝不用于鉴权**。
- **单一脱敏入口**：所有 SQL 写参数在 `AuditStorage._execute()` 统一过 `redact_params()`
  （`redact_text`）；派生 Git / 实验字段、旧式 `audit.log`、任务日志回读、事件详情、
  归档 JSONL、三种导出与后台读取接口都复用同一规则。令牌、密码、API Key、私钥 PEM、
  Bearer、URL basic auth、环境变量转储统一替换为 `***REDACTED***`；修复前落库的存量明文行
  在读取 / 归档 / 导出时同样被脱敏。
- **大输出保护**：超过 `AUDIT_MAX_FIELD_CHARS`（默认 32KB）的文本写入权限 `600` 的 artifact 文件，
  数据库只保存 `result_ref`、大小、SHA-256 与预览；详情接口可从引用读回完整内容。
- **后台任务独立生命周期**：`run_command_background` 的工具调用成功与训练任务真实运行结果分开记录；
  `_task_reaper()` 与 `get_task_status()` 按退出码补写 `done`（0）或 `failed`（非 0），
  超时/停止分别记录 `killed-timeout` / `killed`。
- **Git / 实验**：由统一采集层解析 `run_command` 中的 git 命令与后台任务，不侵入工具实现。
- **严格审计三态**：`available()` 只表示「还能缓冲写入」；`persistence_state()` 给出
  healthy / bufferable / persisted，`confirm_persisted(request_id)` 还要求数据库健康、
  缓冲为空且能回读刚写入的 `RECEIVED` 行。`AUDIT_STRICT=1` 时高风险工具（如
  `run_command`、`write_file`）用它决定放行；终态写入失败会记录 `audit_strict_violation`
  并体现在 `health()["strict_violations"]`。默认 `AUDIT_STRICT=0` 仍是降级缓冲不中断任务。
- **完整归档与导出**：归档以 `AUDIT_ARCHIVE_BATCH`（默认 1000，上限 5000）分批查询，
  写 JSONL（权限 600）→ flush / fsync → 回读核验行数 → 只按该批主键精确删除 → 循环至无残留；
  任一步失败都保留原记录并写 `audit_storage_warning`，绝不按时间范围整批删除。配额裁剪同样先归档。
  导出用主键游标分页并固定时间上界，`/api/export/stream` 为流式生成器，内存 O(批)。
- **保留策略**：覆盖 `tool_calls`、`audit_events`、`background_tasks`、`git_operations`、
  `experiment_runs`、`approval_events`、`login_attempts`、`export_jobs`、过期管理员会话与
  过期客户端会话；`admin_settings` 是配置，不清理。`audit-storage` 每 300 秒按
  `AUDIT_RETENTION_DAYS`（90 天）归档、按 512MB 配额裁剪、保留 7 份 `VACUUM INTO` 备份
  并写心跳文件 `/data/audit/.maintenance.ok`。
- **统计准确性**：活跃客户端按 `last_seen` / `token_expires` 与 `MCP_SESSION_TTL` 实时判定；
  趋势与耗时分布走 SQLite 时间函数或 PostgreSQL `EXTRACT` 的 SQL 聚合，失败时回退到
  有界（2 万行）Python 聚合。

## 5. 后台功能

首页仪表盘（KPI、调用趋势折线图、状态/工具/客户端/耗时分布、最近异常、网关与审计状态、
两个容器各自的 CPU/内存/磁盘/进程指标）、实时调用监控（SSE）、完整调用记录（组合筛选、排序、
分页、详情、时间线、关联任务/Git/实验、JSON 格式化与折叠复制）、Git 操作审计、科研实验监控与日志、
服务器状态、客户端与会话、服务配置、CSV/JSON/JSONL 导出（直接下载或后台任务 + 签名链接）。
界面中文优先，支持深浅色与窄屏布局，零外部 CDN 依赖。

> 监控指标来自**容器自身**，不是宿主机全机指标：后台容器与 MCP 网关容器分开标注，
> 命令实际在网关容器内执行。GPU 只有在容器能调用 `nvidia-smi` 时显示；
> Docker 容器列表只有在显式挂载 `/var/run/docker.sock` 后才可用（挂载前请评估该 socket 等同于宿主机 root 的风险）。

## 6. 可选组件

### HTTPS 入口（profile `tunnel`）

```bash
# .env
MCP_TUNNEL_DISABLED=1        # 关闭网关内置 quick tunnel，避免两个公网入口
CF_TUNNEL_TOKEN=<命名隧道令牌>
docker compose --profile tunnel up -d
```

quick tunnel（未设令牌）会生成随机 `https://*.trycloudflare.com` 地址。若也要把后台暴露到公网，
请在 Cloudflare 命名隧道的 ingress 中把后台域名指向 `http://mcp-admin-web:8788`，并先完成第 3 节的安全配置。

### PostgreSQL 审计后端（profile `postgres`）

```bash
# .env
AUDIT_DB_DSN=postgresql://mcp_audit:change-me@postgres:5432/mcp_audit
docker compose --profile postgres up -d
```

镜像已包含 `psycopg[binary]`；三个服务共用同一 DSN。PostgreSQL 模式请使用 `pg_dump` 备份
（内置 `backup()` 仅适用于 SQLite）。多实例 / 高并发写入场景推荐此模式。

### GPU 与 Docker 状态

在 Compose 中为对应服务透传 NVIDIA 设备（`deploy.resources.reservations.devices`）后，`/api/monitoring`
才会显示 GPU；`docker.sock` 默认不挂载。

## 7. 持久化与备份

共享卷 `audit-data` 挂载到三个服务的 `/data/audit`，包含：

```
audit.db / audit.db-wal / audit.db-shm   审计主库
artifacts/                               大输出引用文件（权限 600）
task-logs/                               后台任务日志
exports/                                 导出文件（权限 600）
archive/  backup/                        归档与备份
.admin_password                          自动生成的管理员口令（权限 600）
.metrics_token                           内部只读指标令牌（权限 600，重启不变）
.maintenance.ok                          维护心跳
```

手动备份 / 恢复（SQLite，恢复前先停止写入方进程）：

```bash
docker compose exec audit-storage python -c \
  "from gateway.audit.storage import get_storage; print(get_storage().backup(''))"
docker compose stop mcp-gateway mcp-admin-web
docker compose exec audit-storage python -c \
  "from gateway.audit.storage import get_storage; print(get_storage().restore('/data/audit/backup/audit-YYYYMMDD-HHMMSS.db'))"
docker compose start mcp-gateway mcp-admin-web
```

忘记管理员口令：

```bash
docker compose exec mcp-admin-web cat /data/audit/.admin_password
```

## 8. 测试与验收

```bash
python -m venv .venv
.venv/bin/pip install -r docker/requirements.txt pytest httpx
.venv/bin/python -m pytest tests -q
```

验收清单：

- [ ] `docker compose config --quiet`、镜像构建成功；21 个原有工具仍可调用。
- [ ] 未登录访问 `/`、`/api/calls` 分别重定向 / 返回 401；登录后页面与数据可用。
- [ ] MCP `tools/call` 与 REST `/run_command` 都产生记录，含参数、输出、状态、耗时与 Git 关联。
- [ ] 同步命令、后台任务、失败/拒绝/超时/确认门均有记录；任务启动与真实运行结果分开。
- [ ] 明文 Token / 密码 / 私钥不出现在数据库、详情、导出与 artifact 中。
- [ ] 大输出只保存引用；容器重启后历史记录仍在；SSE 实时收到新调用事件。
- [ ] 组合筛选、排序、分页、CSV/JSON/JSONL 导出与签名下载可用。
- [ ] 停止后台容器不影响正在运行的实验；网关 `/health` 不可达时后台仍显示历史与「离线」状态。
- [ ] 监控页面明确区分两个容器来源，不冒充宿主机指标。

本轮修复新增的必验项：

- [ ] **空 `MCP_TOKEN` 启动**：入口表达式可执行，自动生成 48 位字母数字口令，
  `bash -n docker/entrypoint.sh` 通过，`/health` 返回 200。
- [ ] **>5000 条归档不丢数据**：造 6000 条过期记录，归档文件行数 = 6000、
  库中过期行 = 0、未过期记录保留、`errors` 为空；归档目录失败时 1200 条原记录全部保留。
- [ ] **>500 条导出完整**：`/api/export/stream` 与后台导出任务都返回全部 1200 行，
  CSV / JSON / JSONL 均为单一合法文档，不出现重复表头或截断。
- [ ] **严格审计持久化确认**：`AUDIT_STRICT=1` 下模拟写库失败，高风险工具被拒绝；
  终态写入失败产生 `audit_strict_violation` 与健康状态计数。
- [ ] **脱敏全链路**：所有子表、事件详情、旧式 `audit.log`、归档 JSONL 与三种导出
  都不含明文凭据（含修复前落库的存量行）。
- [ ] **健康接口隔离**：公开 `/health` 不含工具数 / 进程 / 磁盘 / 审计路径；
  `/health/details` 匿名 401，Bearer 与内部只读令牌 200，且内部令牌不能访问业务路径。
- [ ] **OAuth 加固**：`redirect_uri` 精确匹配、强制 PKCE S256、令牌过期被拒并清理；
  两个兼容开关默认关闭时旧流程被拒。
- [ ] **重启持久化**：重建容器后历史记录、管理员口令与内部指标令牌均不变；
  原有 21 个工具与 `/run_command` 行为不变。

## 9. 故障排查

| 现象 | 检查 |
|---|---|
| 后台打不开 | `docker logs cnb-mcp-admin-web`；确认 `ADMIN_HOST_PORT` 与 `ADMIN_BIND_HOST` |
| 端口预览报 `dial tcp <工作区IP>:8788: connect: connection refused` | 容器已停止或端口只绑了 `127.0.0.1`；`docker compose up -d` 并临时设置 `ADMIN_BIND_HOST=0.0.0.0` 后重建后台服务 |
| 忘记密码 | `docker compose exec mcp-admin-web cat /data/audit/.admin_password` |
| 登录被锁 | 等待 `ADMIN_LOGIN_LOCK_MINUTES`，或审查 `login_attempts` 事件 |
| 仪表盘显示网关离线 | 检查 `GATEWAY_URL=http://mcp-gateway:8765` 与网关 `MCP_BIND=0.0.0.0`；后台仍可查历史 |
| 监控页缺少网关容器指标 | 后台已降级到公开 `/health`；检查共享卷 `/data/audit/.metrics_token` 是否存在且权限 600，两个服务的 `GATEWAY_METRICS_TOKEN_FILE` 是否一致 |
| `/health/details` 返回 401 | 该接口需有效 Bearer 或内部只读指标令牌；匿名访问属预期行为，存活检查请用 `/health` |
| 归档告警但记录未减少 | 归档失败会刻意保留全部数据；查看 `audit_storage_warning` 事件与归档目录权限 / 磁盘空间，修复后维护任务会重试 |
| 审计状态「降级」 | 检查卷权限与磁盘；恢复后缓冲会自动补写；查看 `audit_storage_warning` 事件 |
| SSE 无新事件 | 确认调用确实发生；SSE 只推送 `tool_call` / `task_lifecycle` / `admin_auth` 三类已落库事件 |
| 看不到 GPU / 容器列表 | 未透传 `nvidia-smi` 或未挂载 `docker.sock`（默认如此） |
| 导出文件下载 401 | 签名链接 10 分钟过期，请在导出页重新生成；或保持登录会话 |

## 10. 本轮实测记录

- 镜像构建成功；`docker compose up -d` 后三个服务均为 `healthy`；MCP `tools/list` = 21。
- 真实 MCP 会话（`initialize` / `tools/list` / `tools/call`）与 REST `/run_command` 都产生审计记录；
  `run_command_background` 成功启动并记录独立生命周期。
- 状态覆盖 `SUCCESS` / `FAILED` / `DENIED`：确认门拒绝曾被误记为 `SUCCESS`，已修复并补回归测试。
- 明文 GitHub-like token 未出现在数据库、调用详情与 CSV 导出；大输出以 artifact 引用保存。
- 强制重建容器后历史记录与引用仍在；SSE 收到 `RUNNING` / `SUCCESS` 事件；
  后台容器停机期间 MCP 调用仍成功，恢复后台后可查到该记录。
- 自动测试：`.venv/bin/python -m pytest tests -q` → 21 passed。
- 镜像补装 `git`，使 Git 命令与对应审计在容器内可用。

### 本轮修复记录（基线 `6c712870`，分支 `fix/audit-integrity-6c71287`）

- 启动：修正空 `MCP_TOKEN` 随机口令表达式的 `string.digits` 错误；网关角色幂等生成
  `/data/audit/.metrics_token`（600），角色分流与 `MCP_TUNNEL_DISABLED` 行为不变。
- 完整性：归档改为「分批写盘核验 → 按主键精确删除 → 循环至无残留」，覆盖所有历史表；
  导出改为主键游标 + 固定时间上界，`/api/export/stream` 流式写出。
- 严格审计：新增 `persistence_state()` / `confirm_persisted()` / `note_strict_violation()`，
  区分可缓冲与已确认持久化；`AUDIT_STRICT` 默认仍为 0。
- 脱敏：写库参数统一走 `redact_params()`；派生字段、旧式日志、任务日志回读、
  归档、导出与后台读取接口都复用同一规则。
- 鉴权：`/health` 最小化，新增需鉴权的 `/health/details`；OAuth 令牌带过期并清理，
  `redirect_uri` 精确匹配、默认强制 PKCE S256；REST `confirm` 严格解析。
- 统计：会话按 TTL 判活，后台任务按退出码区分 `done` / `failed`，
  趋势与耗时分布改为 SQL 聚合（失败回退有界 Python 聚合）。
- 回归：新增 `tests/test_oauth.py`、`tests/test_startup.py`，并扩充 3 个既有测试文件；
  `.venv/bin/python -m pytest tests -q` → 44 passed（原有 21 个全部保留）。
- 部署验收与双远端同步见本节上方清单与 README 的「本轮修复的验证命令」。

> 说明：第 10 节是**历史实测记录**（旧分支 `fix/audit-integrity-6c71287`、21 个工具、21 passed），
> 不代表 V3 当前状态。V3 当前为 49 个工具、全量离线 `143 passed`，见第 11 节与验收报告。

---

## 11. V3 扩展（搜索监控 / 论文库 / 来源标识 / 严格审计与归档恢复）

### 11.1 页面

导航由 9 项扩为 11 项，新增「搜索监控」「论文库与下载」；仪表盘总览、完整调用记录、
科研实验监控、服务器与执行后端四页同步扩展。沿用深色 GitHub 变量体系、渐变强调边、
状态色徽标、玻璃质感卡片与克制的淡入 / hover 微动效；支持 `prefers-reduced-motion`，
1280px / 900px 逐级折叠为堆叠卡片，表格横向滚动。

**搜索监控**
- 筛选：时间范围、搜索类型、客户端、脱敏关键词、数据源、状态；条件变化即刷新。
- 来源矩阵：每个来源的请求数、成功/失败、平均耗时、返回条数、API 限流与缓存命中；
  失败行标红并展开原因。数据来自单条聚合查询（`/api/search/events`），避免 N+1。
- 标题区明确「垂直学术源 / 社区源与可选通用搜索的覆盖范围」；垂直社区搜索**不是全网通用搜索**。
- 页脚：上游连通性固定显示「**未检测**」（后台不自动发外部请求、不虚构可达性）、限额提示与采样说明。

**论文库与下载**
- 筛选：关键词、DOI/arXiv ID、OA 状态、年份、项目、标签、阅读状态；BibTeX/RIS/CSV/JSON 导出。
- 论文卡片：标题、作者、年份、期刊、DOI/arXiv ID、来源、OA 状态、SHA-256 校验结果、PDF 预览入口。
- 下载任务面板：进度条 + `created → downloading → verifying → success` 分段流程条、失败原因、
  文件大小、保存路径、关联 MCP 请求 ID。
- **口径（必须遵守）**：`created` 仅表示「任务创建成功」，`success` / `duplicate` 才表示
  「文件最终校验成功」（`%PDF-` 头、Content-Type、最终 URL、大小、EOF、SHA-256、fsync + 原子改名）。
  页面不得把仅返回标题或 PDF 链接当作实际下载成功。
- 页脚：索引持久化说明、默认保存目录、OCR 未启用说明与最近同步时间。

**完整调用记录**：新增 `source` 筛选（mcp / rest / admin / http / task / host-agent / …）与
「结果摘要」列（脱敏后的结果预览 + `result_ref` 引用），详情抽屉可查看调用时间线与关联事件。

**服务器与执行后端**：新增「容器 / 宿主」来源切换与 `scope_note`。Host Agent 未连接 / 未授权时
明确显示原因，不展示任何宿主指标，也不把容器指标误报为宿主指标；GPU 未配置采集器时如实
返回 `unavailable`，不虚构数据。

### 11.2 数据来源标注

所有数据块都用 `source-chip` 标注来源：

| 徽标 | 含义 |
| --- | --- |
| 容器 | 网关容器 / 后台容器自身的指标与审计库数据 |
| 宿主机 | 仅在用户显式挂载授权目录并启用 Host Agent 后可用 |
| 外部来源 | 免费学术 / 网页 API 的调用结果，不是本机指标 |
| 离线 | 未检测 / 未连接 / 不可用，附具体原因 |

`/api/host/status` 返回 `SOURCE_LABELS`、默认后端、容器与 Host Agent 可用性、Unix Socket 路径
与「真实宿主权限需部署时由用户授予」的显式说明。

### 11.3 新增 API

全部走 `require_admin`（签名下载端点除外，见下）并在返回前经 `_safe_rows()` / `redact_structure()` 脱敏。

| 端点 | 用途 |
| --- | --- |
| `GET /api/search/events` | 逐源矩阵 + 最近事件；`by_provider` 为单条聚合查询 |
| `GET /api/papers` | 文献库筛选 + `total/verified/bytes/verified_rate` 统计 |
| `GET /api/papers/downloads` | 下载任务全过程与按状态计数 |
| `GET /api/papers/export?fmt=bibtex\|ris\|csv\|json` | 文献导出（非法格式 400） |
| `GET /api/papers/link` / `GET /api/papers/file` | 已校验 PDF 的短期 HMAC 签名链接与 Range 下载 |
| `GET /api/transfers` / `/api/transfers/link` / `/api/transfers/file` | 传输监控、签名链接与 Range 下载 |
| `GET /api/host/status` | 执行后端与来源标识 |
| `GET /api/v3/overview` | 仪表盘单条聚合总览（搜索 / 论文 / 下载 / 传输 / 工具来源 / 最近失败下载） |
| `GET /api/archives*` | 归档列表、校验、查询与幂等恢复（阶段 A） |

**签名下载鉴权**：`/api/papers/file` 与 `/api/transfers/file` 刻意不挂 `require_admin`
（否则纯签名链接会在进入函数前被 401），改为「有效后台会话**或**有效短期 HMAC 签名，二选一」；
二者皆无时返回 401。后台未配置 `MCP_TRANSFER_SECRET` 时 `/api/transfers/link` 直接拒绝签发，
避免用弱默认密钥。签名绑定路径 / 论文 ID 与过期时间（600 秒）。

### 11.4 严格审计与归档恢复的口径更新

第 4 节的「严格审计三态」仍然成立，V3 补充：

- **统一严格门**：MCP 中间件、REST `/run_command`、任务与后台操作、非工具 HTTP 传输端点共用
  `strict_gate(tool_name, request_id, source)`；`HIGH_RISK_TOOLS` 已扩展至 Git / 论文 / 传输 /
  宿主工具及 `files.*`。严格模式下失败返回可操作错误（REST / HTTP 为 503）并记 `DENIED`。
- **导入失败不再静默 fail-open**：`AUDIT_REQUIRED=1` 时醒目 `[AUDIT ALERT]` + 拒绝启动，
  仅在 `AUDIT_ALLOW_UNAUDITED=1`（运维明确承担风险）时放行；`/health/details` 报告降级原因。
  默认 `AUDIT_REQUIRED=0` 保持既有行为。
- **磁盘持久化缓冲**：写库失败路径改为 `spool/` 下 0600 JSONL + fsync + 有界轮转
  （`AUDIT_SPOOL_MAX_MB=64` / `AUDIT_SPOOL_MAX_FILES=4`），启动时回放；
  `confirm_persisted()` 追加「spool 为空」条件。
- **归档校验与恢复**：`_write_archive` 增加流式 SHA-256 + `.manifest.json` 清单
  （表名 / 批次 / 行数 / 摘要 / 时间），删除前二次读回比对行数与摘要；
  提供 `list_archives` / `verify_archive` / `query_archive` / `restore_archive`
  （按 `request_id` 幂等 `INSERT OR IGNORE`）；维护任务消费校验失败原因并告警。
  **失败保留原记录、绝不按时间整批删**的语义不变。
- **不纳入 `ARCHIVE_TABLES` 的例外**：`oauth_*`、`paper_library`、`spool_applied`
  属于「当前有效状态 / 幂等标记」，按时间删除会误删仍有效的授权或导致重放，与
  `admin_settings` 一样跳过；其余新增历史表全部纳入保留策略。

### 11.5 逐阶段验收清单（V3）

命令统一用 `.venv/bin/python -m pytest -q`（全局 `python` 无 pytest）。

- [x] **A**：`tests/test_phase_a_integrity.py`、`tests/test_audit_security.py`；
  全量 `57 passed`。
- [x] **B-检索**：`tests/test_search_web.py`、`test_paper_search.py`；全量 `89 passed`。
- [x] **B-论文**：`tests/test_pdf_download.py`、`test_pdf_read.py`、`test_library.py`；
  MarkItDown 与 `read_paper_pdf` 在本地模拟 2 页 PDF 上交叉比对通过（见验收报告第 2 节）。
- [x] **C**：`tests/test_access_mode.py`、`test_host_agent.py`、`test_gitops.py`、`test_startup.py`；
  专项 20 passed、全量 `103 passed`。真实宿主权限**未验收**。
- [x] **D**：`tests/test_tasks_v3.py`、`test_transfer.py`；全量 `122 passed`。
- [x] **E**：`tests/test_dashboard_v3.py`；专项 19 passed、全量 `141 passed`（阶段结束时的快照）。
  浏览器视觉验收**未执行**。
- [x] **文档与配置**：新增 `test_docs_and_config_stay_in_sync_with_v3_code`，
  全量 `142 passed`（后续新增文献库回归用例为 `143 passed`）；4 个 Compose profile 的 `config -q` 全部通过。
- [x] **F（验收部分）**：镜像重建、3 个常驻服务健康、独立真实网络集成
  （`-m integration`，13 passed / 1 skipped）、8 条真实 MCP 客户端工作流全部通过。
  浏览器视觉验收、真实宿主权限与双远端推送**未执行**，见 [v3-acceptance-report.md](./v3-acceptance-report.md)。

配置校验已通过：`bash -n docker/entrypoint.sh`、`node --check gateway/admin/static/app.js`、
`docker compose config -q`，以及 `--profile search` / `--profile host` /
`--profile tunnel` / `--profile postgres` 四个 profile 的 `config -q`。
注意：四个 profile 的**配置语法**通过不等于全部 profile 服务都已实际启动。阶段 F 已真实启动
`search` 与 `host` 两个 profile（SearXNG JSON API HTTP 200 但 5 个上游引擎全部超时；Host Agent
socket 0600、HMAC 认证正常），`tunnel` / `postgres` 仍未启动。后台「上游连通性」固定显示
「未检测」，不自动发外部请求、不虚构可达性。

### 11.6 故障排查补充

| 现象 | 检查 |
| --- | --- |
| 搜索监控显示「上游连通性：未检测」 | 预期行为，后台不自动探测；用独立集成测试记录真实可达性 |
| 论文卡片无「PDF 预览」按钮 | 该条目 `file_path` 为空或文件未通过校验；先看下载任务面板的失败原因 |
| 下载任务停在 `created` | 只代表任务已创建，不代表文件已下载；检查 `error_message` 与 OA 状态 |
| 签名下载 401 | 签名过期（600 秒）或缺失，且未登录；重新生成链接或保持后台会话 |
| `/api/transfers/link` 返回 `ok=false` | `MCP_TRANSFER_SECRET` 未配置或文件不在授权传输目录内 |
| 服务器页宿主来源为空 | Host Agent 未连接 / 未授权；查 `/api/host/status` 的 `reason`，不要误填容器指标 |
| 审计状态「降级」但数据不丢 | 检查 `spool/` 与 `health()`；恢复后自动回放，严格模式会拒绝高风险工具 |
