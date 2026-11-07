# MCP Admin Dashboard 部署与运维（中文）

> 适用分支：`feature/admin-dashboard`。目标是在不削弱原有 Shell / 文件 / Git / 实验能力的前提下，
> 完整记录、查询、审计与监控 AI Agent 对服务器发起的每一次操作。

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

## 9. 故障排查

| 现象 | 检查 |
|---|---|
| 后台打不开 | `docker logs cnb-mcp-admin-web`；确认 `ADMIN_HOST_PORT` 与 `ADMIN_BIND_HOST` |
| 端口预览报 `dial tcp <工作区IP>:8788: connect: connection refused` | 容器已停止或端口只绑了 `127.0.0.1`；`docker compose up -d` 并临时设置 `ADMIN_BIND_HOST=0.0.0.0` 后重建后台服务 |
| 忘记密码 | `docker compose exec mcp-admin-web cat /data/audit/.admin_password` |
| 登录被锁 | 等待 `ADMIN_LOGIN_LOCK_MINUTES`，或审查 `login_attempts` 事件 |
| 仪表盘显示网关离线 | 检查 `GATEWAY_URL=http://mcp-gateway:8765` 与网关 `MCP_BIND=0.0.0.0`；后台仍可查历史 |
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
