# MCP Admin Dashboard 部署与运维（中文）

> 适用版本：v1.1（开发分支 `feat/mcp-v1.1`）。目标是在不削弱原有 Shell / 文件 / Git / 实验能力的前提下，
> 完整记录、查询、审计与监控 AI Agent 对服务器发起的每一次操作。
> 本文第 1–9 节为通用部署运维说明，第 10 节是初版历史实测数据，第 11 节为 v1.1 增量；
> v1.1 逐项真实验收结果以 `scripts/selfcheck_v11.py` 的 T01–T12 输出为准。

## 1. 架构与数据流

| 服务 | 角色（`MCP_SERVICE_ROLE`） | 暴露方式 | 认证 |
|---|---|---|---|
| `mcp-gateway` | `gateway`（默认） | 容器网络 `8765` + v1.1 宿主回环 `127.0.0.1:${MCP_HOST_PORT:-8765}`（本机自检 / 反代用） | `MCP_TOKEN` / OAuth |
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

采集发生在 FastMCP 工具分发中间件与 REST/任务入口，**不修改任何现有工具内部**。v1.1 共注册 45 个 MCP 工具
（`docker/server.py` 26 个 + `gateway/git_guard.py` 19 个结构化 `git_*`），原有工具签名保持兼容。
除调用记录外，v1.1 还有两条独立数据流：文件传输由 `gateway/files/service.py` 直接写 `file_transfers` 表，
后台任务状态由 `gateway/tasks/store.py` 写入同一 SQLite WAL 的 `mcp_tasks` 表、日志落持久卷。
管理后台与网关是不同进程：后台停止不会影响已启动的训练任务；网关离线时后台仍可查询历史。

## 2. 快速开始

```bash
# v1.1 推荐：使用全量占位配置示例（含文件根目录、任务日志、签名密钥、Docker 授权开关）
cp .env.example .env
# 全部留空即可：MCP_TOKEN / ADMIN_PASSWORD 首次启动自动生成
docker compose up -d --build
# 一条命令打印 MCP 地址、Bearer 令牌、客户端 JSON 配置、后台地址与管理员账号口令
./scripts/show-config.sh
# 浏览器打开 http://127.0.0.1:8788
```

> 请从已合并 v1.1 的 GitHub `main` 克隆；CNB `main` 仍是旧提交，没有 `.env.example` 与 `scripts/`。
> 旧模板 `docker/.env.template` 与 `.env.example` 变量集合并不一致，请勿再用于 v1.1 部署。
> 完整一键命令（含前置校验与就绪等待）与分支差异见 [README.zh-CN.md](../README.zh-CN.md)。

原生 Linux（systemd，直接复用宿主机软件、GPU、驱动与 Docker；需要真实 systemd 与 root）：

```bash
./scripts/install-native.sh                # --upgrade / --rollback / --show-config
# 安装完成后自动打印同一份连接信息；/etc/mcp-gateway/env（600）
# 单元 deploy/mcp-gateway.service 与 deploy/mcp-admin-web.service
# 固定 HTTPS 入口见 deploy/reverse-proxy.example.conf
```

未设置 `ADMIN_PASSWORD` 时首次启动自动生成强随机口令（`entrypoint.sh` 的 `generate_secret`），以 600 权限写入 `/data/audit/.admin_password`，容器重建时复用；不再有硬编码默认口令。
入口脚本会把当前生效口令以 `600` 权限重新写入共享卷 `/data/audit/.admin_password`；
默认**不打印到容器日志**，统一用 `./scripts/show-config.sh` 读取（或在隔离环境设
`MCP_PRINT_SECRETS=1` 恢复日志打印）。`MCP_TOKEN` 同理默认只写入网关容器 `/app/.env`。
Compose 模式下网关还会把令牌同步到共享卷 `/data/audit/.mcp_token`（权限 600），供后台读取需 Bearer 的
`/internal/metrics`；该文件等同 `MCP_TOKEN`，请控制共享卷的访问权限。`.env` 含密钥，不要提交到仓库。

固定公网 HTTPS 入口：默认已开启内置 quick tunnel，`show-config.sh` 会打印
`https://<随机域名>.trycloudflare.com/mcp`（域名在容器重建后变化）。要固定地址，在
`.env` 设置 `MCP_PUBLIC_BASE_URL`（可选 `ADMIN_PUBLIC_BASE_URL`），并按
`deploy/reverse-proxy.example.conf` 配置 nginx 或 Cloudflare 命名隧道；脚本会改为打印固定公网地址。
脚本按「固定 / 随机隧道 / 仅本机」如实标注，不伪造公网网址。管理网站默认只绑 `127.0.0.1`，
需要远程访问请按示例配置 `admin.example.com` 反代。

单容器快速启动（`./run.sh` / `docker run`）仍保持原有行为：一个容器里运行 MCP 网关，
但不会启动管理后台；如需审计后台请使用 Compose 部署。

## 3. 认证与安全边界

- 后台使用**独立管理员账号**，绝不接受 `MCP_TOKEN` 登录；口令以 PBKDF2-HMAC-SHA256（200,000 轮）校验。
- 会话 Cookie 为 HMAC 签名、`HttpOnly`、`SameSite=Lax`，默认 8 小时过期。
  未配置 `ADMIN_SESSION_SECRET` 时密钥随容器重启变化，旧会话自动失效（更安全）。
- 连续 5 次失败登录后锁定 15 分钟（可配置）；设置 `ADMIN_TOTP_SECRET` 后启用 TOTP 双因素。
  锁定按直连 IP 统计，默认**不信任客户端自报的 `X-Forwarded-For`**（仅在可信反代后设 `ADMIN_TRUST_PROXY=1`）。
- 所有写操作额外校验 `Origin` / `Sec-Fetch-Site`，作为 `SameSite=Lax` 之外的 CSRF 防护。
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

## 4. 审计数据模型

每次调用记录包含：`request_id`、`trace_id`、起止时间、耗时、工具名、参数、结果、状态、错误类型/信息、
退出码、客户端、操作者、会话、来源 IP、`task_id`、`approval_id`、`git_repo`、`experiment_id`、`file_path`。
状态为 `RECEIVED / RUNNING / SUCCESS / FAILED / DENIED / TIMEOUT / CANCELLED / INTERRUPTED`。

- **身份边界**：`client_id` / `actor_id` 来自已验证 Bearer Token 的 SHA-256 指纹；
  `declared_client`（`X-MCP-Client`、User-Agent、clientInfo）只是自报名称，**仅用于展示，绝不用于鉴权**。
- **默认脱敏**：令牌、密码、API Key、私钥 PEM、Bearer、URL basic auth、环境变量转储统一替换为
  `***REDACTED***`，覆盖参数、输出、错误信息与导出文件。
- **大输出保护**：超过 `AUDIT_MAX_FIELD_CHARS`（默认 32KB）的文本写入权限 `600` 的 artifact 文件，
  数据库只保存 `result_ref`、大小、SHA-256 与预览；详情接口可从引用读回完整内容。
- **后台任务独立生命周期**：`run_command_background` 的工具调用成功与训练任务真实运行结果分开记录；
  自然结束由 `_task_reaper()` 补写 `done`，超时/停止分别记录 `killed-timeout` / `killed`。
- **Git / 实验**：由统一采集层解析 `run_command` 中的 git 命令与后台任务，不侵入工具实现。
- **降级不中断任务**：写库失败时进入有界内存缓冲并标记降级，恢复后自动补写；
  `AUDIT_STRICT=1` 时才对高风险工具（如 `run_command`、`write_file`）拒绝执行。
- **保留策略**：`audit-storage` 每 300 秒归档超过 90 天的记录为 JSONL、按 512MB 配额裁剪、保留 7 份
  `VACUUM INTO` 备份并写心跳文件 `/data/audit/.maintenance.ok`。

## 5. 后台功能

首页仪表盘（KPI、调用趋势折线图、状态/工具/客户端/耗时分布、最近异常、网关与审计状态、
两个容器各自的 CPU/内存/磁盘/进程指标）、实时调用监控（SSE）、完整调用记录（组合筛选、排序、
分页、详情、时间线、关联任务/Git/实验、JSON 格式化与折叠复制）、Git 操作审计、科研实验监控与日志、
服务器状态、客户端与会话、服务配置、CSV/JSON/JSONL 导出（直接下载或后台任务 + 签名链接）。
v1.1 侧边栏固定为五个页面：首页仪表盘、完整调用记录、后台任务、Git 操作审计、文件传输记录。
首页新增受管文件根目录的磁盘容量/已用/可用与各挂载点用量、文件传输次数与累计字节，以及
`mcp_tasks` 持久任务状态卡片（重启后无法确认的任务显示 `unknown`，退出码不冒充 0）。
界面中文优先，支持深浅色与窄屏布局，零外部 CDN 依赖；五个页面的 v1.1 增量见第 11 节。

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
.admin_password                          当前生效的管理员口令（权限 600）
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

忘记管理员口令（未配置 `ADMIN_PASSWORD` 时为首次启动生成的强随机值）：

```bash
./scripts/show-config.sh                  # 同时打印 MCP 地址、Bearer 令牌、后台地址与账号口令
docker compose exec mcp-admin-web cat /data/audit/.admin_password   # 只取口令
```

## 8. 测试与验收

```bash
python -m venv .venv
.venv/bin/pip install -r docker/requirements.txt pytest httpx
.venv/bin/python -m pytest tests -q
```

v1.1 新增端到端自检（对真实运行的服务逐项输出 PASS/FAIL/SKIP，不模拟）：

```bash
MCP_TOKEN=<令牌> ADMIN_PASSWORD=<口令> python3 scripts/selfcheck_v11.py
# 本机无容器权限时可用 --skip-restart，T10 会被明确标记为 SKIP 而不是伪报成功
```

验收清单：

- [ ] `docker compose config --quiet`、镜像构建成功；v1.1 共 45 个 MCP 工具可调用（21 个原有 + 19 个 `git_*` + 5 个新增文件/Docker 工具），原有工具签名保持兼容。
- [ ] 单元测试 `pytest -q` 全通过；T01–T12 逐项有真实输出，任何 FAIL/SKIP 都在交付说明中列明。
- [ ] 未登录访问 `/`、`/api/calls` 分别重定向 / 返回 401；登录后页面与数据可用。
- [ ] MCP `tools/call` 与 REST `/run_command` 都产生记录，含参数、输出、状态、耗时与 Git 关联。
- [ ] 同步命令、后台任务、失败/拒绝/超时/确认门均有记录；任务启动与真实运行结果分开。
- [ ] 明文 Token / 密码 / 私钥不出现在数据库、详情、导出与 artifact 中。
- [ ] 大输出只保存引用；容器重启后历史记录仍在；SSE 实时收到新调用事件。
- [ ] 组合筛选、排序、分页、CSV/JSON/JSONL 导出与签名下载可用。
- [ ] 停止后台容器不影响正在运行的实验；网关 `/health` 不可达时后台仍显示历史与「离线」状态。
- [ ] 监控页面明确区分两个容器来源，不冒充宿主机指标。

## 9. 故障排查

| 现象 | 检查 |
|---|---|
| 后台打不开 | `docker logs cnb-mcp-admin-web`；确认 `ADMIN_HOST_PORT` 与 `ADMIN_BIND_HOST` |
| 端口预览报 `dial tcp <工作区IP>:8788: connect: connection refused` | 容器已停止或端口只绑了 `127.0.0.1`；`docker compose up -d` 并临时设置 `ADMIN_BIND_HOST=0.0.0.0` 后重建后台服务 |
| 忘记密码 | `./scripts/show-config.sh`，或 `docker compose exec mcp-admin-web cat /data/audit/.admin_password` |
| 登录被锁 | 等待 `ADMIN_LOGIN_LOCK_MINUTES`，或审查 `login_attempts` 事件 |
| 仪表盘显示网关离线 | 检查 `GATEWAY_URL=http://mcp-gateway:8765` 与网关 `MCP_BIND=0.0.0.0`；后台仍可查历史 |
| 审计状态「降级」 | 检查卷权限与磁盘；恢复后缓冲会自动补写；查看 `audit_storage_warning` 事件 |
| SSE 无新事件 | 确认调用确实发生；SSE 只推送 `tool_call` / `task_lifecycle` / `admin_auth` / `file_transfer` 四类已落库事件 |
| 看不到 GPU / 容器列表 | 未透传 `nvidia-smi` 或未挂载 `docker.sock`（默认如此） |
| 导出文件下载 401 | 签名链接 10 分钟过期，请在导出页重新生成；或保持登录会话 |
| `download_file` 链接打开报签名无效 | 网关与后台的 `MCP_FILE_SIGNING_SECRET`（或回落用的 `MCP_TOKEN`）必须一致；Compose 已把两者注入两个服务，原生部署请确认两个 systemd 单元的 EnvironmentFile 相同 |
| `download_file` 返回本机地址而非公网 HTTPS | 设置 `MCP_PUBLIC_BASE_URL=https://<固定域名>`；留空时只回落到请求 Host |
| 后台任务显示 `unknown` 且退出码为 `unknown` | 这是 v1.1 的正确行为：服务重启后无法确认进程存活与真实退出码，绝不误标成功；日志仍可从 `MCP_TASK_LOG_DIR` 读取 |
| Docker 命令在容器内查不到宿主容器 | 默认未挂载 `/var/run/docker.sock`；确认确实需要后叠加 `docker-compose.host-docker.yml` 并设 `MCP_DOCKER_SOCKET=1`（等价宿主机 root） |

## 10. 历史实测记录（Admin Dashboard 初版，非 v1.1 验收数据）

> 以下数字来自初版部署（当时 21 个原有工具 + 13 个 `git_*` = 34，测试 21 passed），仅作变更溯源。
> v1.1 为 45 个工具，逐项验收结果以 `scripts/selfcheck_v11.py` 的 T01–T12 真实输出为准。

- 镜像构建成功；`docker compose up -d` 后三个服务均为 `healthy`；当时 MCP `tools/list` = 34
  （原有 21 个 + 13 个结构化 `git_*` 工具）。网关 `/health` 只返回最小存活状态，
  后台通过需 Bearer 的 `/internal/metrics` 读取网关指标。
- 真实 MCP 会话（`initialize` / `tools/list` / `tools/call`）与 REST `/run_command` 都产生审计记录；
  `run_command_background` 成功启动并记录独立生命周期。
- 状态覆盖 `SUCCESS` / `FAILED` / `DENIED`：确认门拒绝曾被误记为 `SUCCESS`，已修复并补回归测试。
- 明文 GitHub-like token 未出现在数据库、调用详情与 CSV 导出；大输出以 artifact 引用保存。
- 强制重建容器后历史记录与引用仍在；SSE 收到 `RUNNING` / `SUCCESS` 事件；
  后台容器停机期间 MCP 调用仍成功，恢复后台后可查到该记录。
- 自动测试：`.venv/bin/python -m pytest tests -q` → 21 passed。
- 镜像补装 `git`，使 Git 命令与对应审计在容器内可用。

## 11. v1.1 增量（五个页面与文件传输记录）

v1.1 不重做监控平台，只在既有简洁页面上补齐必要区块：

| 页面 | 路由 | v1.1 变化 |
|---|---|---|
| 首页仪表盘 | `/api/dashboard` | 新增受管文件根目录的磁盘容量 / 已用 / 可用、各挂载点用量、文件传输次数与累计字节、SQLite 持久任务状态卡片 |
| 完整调用记录 | `/api/calls` | 保持原有筛选 / 排序 / 分页 / 详情；新增工具的 `env`、`stdin`、base64 分片只记录键名或是否提供 |
| 后台任务 | `/api/tasks` | 生命周期事件与 `mcp_tasks` 持久状态合并展示；`unknown` 明确标注「重启后无法确认真实退出码」，退出码显示 `unknown` 而非 0 |
| Git 操作审计 | `/api/git` | 新增 `recent_commits` / `recent_pushes` 摘要；结构化 `git_*` 工具也会写入 Git 记录 |
| 文件传输记录 | `/api/files` | 新增页面：时间、工具、方向、路径、大小、SHA-256、状态、耗时与失败原因；含上传/下载/失败/累计字节统计 |

**审计脱敏**：写库与读库双向脱敏。口令、私钥、完整令牌、`Authorization` 头、连接串凭据一律替换；`env`、`stdin`、`content_base64` / `chunk_base64`、`signature` 整值不入库；工具结果里整行的 base64 分片会被替换成 `***REDACTED***`；签名链接中的 `token=` 也会被脱敏。大输出只保存受保护引用、大小与 SHA-256。

**文件传输真实记录**：`file_transfers` 表由 `gateway/files/service.py` 直接写入，包含成功与失败（越权、覆盖拒绝、摘要不符）两类记录，只存路径、大小、摘要、方向与终态，绝不存分片内容。管理网站另提供登录后可用的 `POST /files/upload` 与 `GET /files/download`；公网可直开的免登录链接由网关 `GET /files/download` 的 HMAC 签名 token 提供，两者鉴权路径彼此独立。

**Docker 授权边界**：容器化部署默认不挂载 `/var/run/docker.sock`；只有显式设置 `MCP_DOCKER_SOCKET=1` 并叠加 `docker-compose.host-docker.yml` 才开放，该权限等价于宿主机 root。override 同时只读挂载宿主机静态 `docker` CLI（路径用 `MCP_HOST_DOCKER_CLI` 调整），使 `run_command` 的 `docker ps` / `docker build` 在授权后真实可用；`docker_status` 在没有 CLI 时也能通过标准库直读 socket 的 Engine API 返回真实容器状态。原生 systemd 部署直接复用宿主机 Docker，不需要该开关。

**验证**：`MCP_TOKEN=... ADMIN_PASSWORD=... python3 scripts/selfcheck_v11.py` 逐项输出 T01–T12 的真实命令、输出与 PASS/FAIL/SKIP；失败与跳过明确说明，不虚构成功。

## 12. v1.1 T01–T12 实测结果（本机真实服务）

执行环境：本机 `ghcr.io/ninaawa/cnb-mcp-gateway-plus:v1.1.0`（镜像 ID `31b2ac03f67b`），
网关发布 `127.0.0.1:8765`、后台 `127.0.0.1:8788`，共享卷 `mcp-audit-data` / `mcp-files-data`。
自检脚本 `scripts/selfcheck_v11.py` 通过真实 `fastmcp.Client`、REST、HTTP 与管理网站会话调用，
完整输出保存在 `/tmp/mcp_v11_selfcheck_final.log`。最终退出码 `0`，**PASS=12 / FAIL=0 / SKIP=0**。

| 项 | 结果 | 真实证据摘录 |
|---|---|---|
| T01 普通命令与结果语义 | PASS | `exit_code=0 duration_ms=4 timed_out=false`，stdout 含 `root / Linux / overlay 256G` |
| T02 软件安装能力 | PASS | 上传离线 wheel（836 B）后 `python3 -m venv` + `pip install --no-index`，输出 `SELFCHECK_PKG=1.0` |
| T03 env / stdin / 退出码 / 耗时 | PASS | `exit_code=7`，stdout 含 `from-stdin`、`ENV=from-env` |
| T04 后台任务 | PASS | `task_id=dae011c3c3890ed9` → `status=done exit_code=3`，日志含 `tick-1` / `DONE` |
| T05 二进制分片与签名链接 | PASS | 2,109,497 B 随机数据分片往返，SHA-256 一致，签名链接 HTTP `200` 与 `Range` `206` 均一致 |
| T06 Docker 授权边界 | PASS | 默认 `docker_available=false authorized=False socket_present=False`，`docker` 命令未被白名单误拦 |
| T07 Git 推送远端核验 | PASS | `remote_verification=PASS`，`git ls-remote` 得 `11b7a24b…` 与本地 HEAD 一致 |
| T08 分支删除硬禁 | PASS | 本地/远端删除、`base64 -d \| sh`、后台入口四种写法全部返回「硬禁止」 |
| T09 Git LFS 回归 | PASS | `git-lfs/3.6.1`，`sh -c '… repo.git'` 放行，`git lfs prune` 仍拒绝 |
| T10 重启后日志与未知态 | PASS | 真实 `docker restart` 后 `status=unknown exit_code=unknown source=sqlite`，日志含 `before-restart` |
| T11 管理网站五页面 | PASS | `/api/dashboard`、`/api/calls`、`/api/tasks`、`/api/git`、`/api/files` 全 200，含 `file_root_disk`，无明文令牌 |
| T12 未授权访问拒绝 | PASS | 无 Bearer / 错 Bearer 均 `401`，合法令牌下 `git branch -D` 仍硬禁 |

**额外真实核验（显式 Docker 授权）**：一次性容器挂载 `/var/run/docker.sock` 且
`MCP_DOCKER_SOCKET=1` 时，`docker_status` 经标准库 Engine API 返回真实版本
`29.9.2-2` 与宿主机容器列表；再只读挂载宿主机静态 `docker` CLI 时 CLI 分支同样返回真实列表；
不授权时明确返回 `docker_available=false`。

**首轮真实失败与修复（不隐瞒）**：首轮 `PASS=7 / FAIL=5`，逐项定位后修复并重新在真实服务上验证：
- T02：自检构造的 `pkg.whl` 不符合 PEP 427 命名 → 改为 `mcp_v11_selfcheck-1.0-py3-none-any.whl`。
- T05：`t05()` 返回值数量与 `check()` 的四元组约定不一致 → 改为显式四元组（并新增 AST 校验脚本）。
- T07：`CATASTROPHIC_PATTERNS` 的 `\.git(?:/|\b)` 把 bare 仓库名 `remote.git` 误判为 `.git` 目录删除 →
  服务端与 `git_guard._SCRIPT_DANGER_RE` 统一改为 `(?<![\w.-])\.git(?:/|\b)`，并补回归测试；
  同时自检改为写仓库级 `git config user.name/email`（结构化 `git_commit` 不接受 `env`）。
- T10 / T12：`docker restart` 重建端口映射瞬间的 TCP RST 让 `_http()` 抛出未捕获异常 →
  `_http` 对连接异常返回可判定结果、重启后等待真实 MCP 会话恢复、MCP 调用对连接类异常有限重试。

单元测试 `pytest -q` 共 85 项全部通过；`docker compose config --quiet`（含 override）、
Python/Shell 语法检查与 `read_lints` 均无错误。回滚锚点：旧镜像
`plus:v3.1.1` = `bb89f31c149c`、`plus:v3.1.0` = `7a33b2aa65ef`，两者及旧 package
`cnb-mcp-gateway` 均未被覆盖。
