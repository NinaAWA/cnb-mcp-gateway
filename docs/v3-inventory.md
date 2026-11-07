# V3 升级：已实现 / 缺陷 / 缺失功能清单

基线：GitHub `main` = `3cf1ab57a100399704cfe702bfdabb3f739aece8`（PR #1 合并 `1812023`），tree `e353ce31d50ebe5cc7ee9e1820db4f20f5e76176`。本地 38 个文件与远端 `main` 逐一比对哈希，差异为 0。工作分支 `upgrade/v3-full-admin-research` 由该提交创建，`main` / `origin/main` 未改动。

本清单用于「不重复开发已实现功能」。所有结论均经只读源码复核（2026-11-07）。

## 1. 已实现（保留，不重做）

| 能力 | 证据 |
| --- | --- |
| 21 个 MCP 工具 | `docker/server.py:605,689,769,794,816,822,850,881,909,937,963,1006,1021,1052,1077,1089,1777,1817,1922,2138,2173` |
| 统一审计分发 | `docker/server.py:499-503` 一处 `mcp.add_middleware(AuditMiddleware())` 覆盖全部工具 |
| 三层采集（MCP / REST / 任务） | `gateway/audit/middleware.py:97-207`（`on_call_tool`）、`:282-314`（`trace_call`）、`:317-342`（`task_lifecycle`） |
| 写库单点脱敏 | `gateway/audit/storage.py:243-277`（`_execute` → `redact_params`）；`gateway/audit/redaction.py:49,62,84,91,104,109,113` |
| 大输出 artifact（SHA-256） | `gateway/audit/redaction.py:113-151` |
| 先归档后按主键精确删 | `gateway/audit/storage.py:741-761`（`_write_archive` 0600 + fsync + 回读行数）、`:786-852`（`archive_and_prune`） |
| 三态持久化判定 | `gateway/audit/storage.py:218-234`（`persistence_state` / `confirm_persisted`） |
| 10 张历史表纳入保留 | `gateway/audit/storage.py:36-47`（`ARCHIVE_TABLES`） |
| OAuth DCR / PKCE / token 过期 | `docker/server.py:205-215`、`:335-373`、`:390-450`、`:453-488` |
| `/health` 最小化 + 需鉴权的 `/health/details` | `docker/server.py:2328-2359,2470-2471`；`OAUTH_PATHS` `:279-293` |
| 8 个学术来源 | `docker/server.py:1436,1475,1523,1558,1585,1622,1648,1675`；注册表 `:1698-1708` |
| 5 个网页 provider + 垂直/通用区分 | `docker/server.py:1972,1993,2022,2042,2061`、`:2083-2089`、`:1965-1969`、`:2128-2129` |
| 统一 HTTP 客户端 / 退避 / 限速 / 缓存 | `docker/server.py:1262-1319`（429/5xx 指数退避）、`:1164-1170`、`:1241-1259` |
| SSRF / 私网 / 白名单 / 重定向重校验 | `docker/server.py:1139-1145,1176-1196,1199-1224,1227-1238,1296-1298,2205-2206` |
| 后台 PBKDF2 / TOTP / 登录锁定 / 会话过期撤销 | `gateway/admin/auth.py:79-189`；`gateway/admin/api.py:97-105,155-158,450-470` |
| 导出 HMAC 签名 + 流式导出 | `gateway/admin/api.py:560-594,597-643` |
| 容器/后台指标来源标注 | `gateway/monitoring/system.py:20-24,90-161`；`gateway/admin/static/app.js:201,218,287,558` |
| GPU 可选透传 / 增量任务日志 | `gateway/monitoring/system.py:38-60`；`docker/server.py:752-766,795-813` |
| 5 服务 Compose 与 44 个测试 | `docker-compose.yml:5-160`；`tests/` 5 文件 44 用例 |

## 2. 已实现但有缺陷（阶段 A 修复目标）

| 缺陷 | 证据 |
| --- | --- |
| REST `/run_command` 无严格审计门 | `gateway/audit/middleware.py:282-314` 直接以 `RUNNING` 落库（`:298`）；`AUDIT_STRICT`/`confirm_persisted` 仅存在于 `on_call_tool` `:128-136`；`HIGH_RISK_TOOLS` 仅 6 项（`storage.py:50-53`） |
| 审计模块导入失败 fail-open | `docker/server.py:86-103`：`AUDIT_ENABLED=False` + `_NullRecorder`，仅 stderr 告警与健康状态降级，仍正常服务 |
| 降级缓冲纯内存 | `gateway/audit/storage.py:163` `deque(maxlen=MAX_BUFFER)`、`:243-277` 故障路径；重启丢事件、满则丢最旧 |
| 归档无校验值、无恢复查询 | `gateway/audit/storage.py:741-761` 无 SHA-256 sidecar；`restore()` `:854-881` 只处理 `VACUUM INTO` 的 DB 备份 |
| OAuth 状态全内存、令牌明文 | `docker/server.py:205-208` 三个 dict；`:481-488` 令牌明文为 key、只存过期 epoch；无哈希/撤销端点/持久化 |
| `client_sessions.token_expires` 未写入 | `gateway/audit/middleware.py:124-125` 调用 `upsert_session` 未传令牌过期 |
| 任务 stdout/stderr 落盘未脱敏 | `docker/server.py:716-723` 直写 `task-logs/*.log`，仅读取时脱敏（`:752-766`） |
| 后台无 CSRF、无并发会话上限 | 全仓 `csrf/xsrf` 0 命中；`gateway/admin/api.py:155-158` 仅 `SameSite=Lax`；`gateway/admin/auth.py` 无并发限制 |
| legacy `audit.log` 不在持久卷 | `docker/server.py:179-183` 位于容器 `/app/audit.log` |

## 3. 已实现但有缺陷（阶段 B 修复目标）

| 缺陷 | 证据 |
| --- | --- |
| `from_year` 五源语义为「单年精确」 | `docker/server.py:1559`（Europe PMC `FIRST_PDATE:[y-01-01 TO y-12-31]`）、`:1586`（PubMed `{year}[pdat]`）、`:1635`（OpenReview `startswith`）、`:1661`（dblp `==`）、`:1679`（S2 `year=`）；仅 OpenAlex `:1444`、arXiv `:1503`、Crossref `:1531` 为「≥年」 |
| 无跨源去重 / 元数据合并 | 仅 `_interleave` `:1720-1727`、`:1763-1764` |
| `sort=date` 先截断再排序 | `docker/server.py:1764` 截断 → `:1765-1766` 排序 |
| 引用数缺失与真实零引用不可区分 | `docker/server.py:1468,1551,1577,1692` 用 `or 0`；`:1412-1415` 过滤 0 |
| 无 DNS rebinding 防护 / 无 Content-Type 校验 | `docker/server.py:1185-1196` `getaddrinfo` 与 urllib 实际连接分离；`:1296-1298` 仅魔数与大小 |
| SearXNG 私网放行过宽、无 compose profile | `docker/server.py:2042-2048` `allow_private=True`；`docker-compose.yml:131,147` 仅 `tunnel`/`postgres` profile |
| 无检索层 mock 与搜索工具测试 | `tests/` 仅 `test_dashboard.py:200-231` 本地 HTTP stub；无 `search_papers`/`search_web` 用例 |

## 4. 确认缺失（阶段 B–E 新增目标）

- **PDF 全链路**：`find_paper_fulltext` / `download_paper` / `batch_download_papers` / `read_paper_pdf` / `search_paper_fulltext` / `list_downloaded_papers` / `get_paper_download_status` / `export_bibliography` 全仓 0 命中；`docker/requirements.txt` 无 PDF 依赖；仅有 PDF 链接字符串（`server.py:1509-1518,1890-1898`）与 Unpaywall `url_for_pdf`（`:1872`）。
- **权限模型**：`MCP_ACCESS_MODE` / `full_admin` 0 命中；现状为「默认全开放 + 同次调用 `confirm` bool + 毁灭级硬禁」（`server.py:509-556,577-580,606,690`）；`approval_events` 表与 `add_approval()`（`storage.py:106-110,369-371`）无调用方。
- **Host Backend / Host Agent**：仅容器内 `subprocess.run`/`Popen`（`server.py:582-589,721-723`）；`gateway/audit/events.py:47` 的 `host-agent` 只是注释枚举。
- **结构化 Git 工具**：8 个全无；`gateway/audit/enrich.py:3-6` 明言依赖 `run_command` 正则派生。
- **任务持久化与进程组**：`_TASKS` 纯内存（`server.py:627`）；`Popen` 无 `start_new_session`、`terminate()` 只杀单进程（`:721-723,831`）；`experiment_runs.git_commit/output_path`（`storage.py:99-105`）从不填充。
- **通用文件传输**：无上传 / `Range` / 分块续传 / 传输级 SHA-256（0 命中）；仅审计导出下载（`api.py:560-643`）。
- **Dashboard 新页面**：`templates/index.html:17-25` 9 项导航、`app.js:677-688` 10 视图，无 Search Monitoring / Paper Library；非工具 HTTP 端点不进 `tool_calls`。

## 5. 复用约束（集成点，禁止绕过）

1. `mcp.add_middleware(AuditMiddleware())`（`server.py:500`）——新工具一律靠分发层覆盖审计，禁止工具体内自写审计。
2. `trace_call(...)` + `CallRecorder.set/ok/fail`（`middleware.py:282-279`）——REST/新入口唯一补审计方式。
3. `get_storage()._execute/_insert/insert_call/update_call/add_event`（`storage.py:243,336,306,315,324`）——不得直连 sqlite3/psycopg 写业务表。
4. `redact_text/redact_structure/redact_json/redact_params`（`redaction.py:49-101`）——所有落库、输出、导出、日志的唯一脱敏入口。
5. `_PG_SCHEMA` 由 `_SQLITE_SCHEMA.replace(...)` 生成（`storage.py:145`）——新表 DDL 必须方言中性、用 `?` 占位符。
6. `ARCHIVE_TABLES`（`storage.py:36-47`）——所有新历史表必须纳入保留策略，`admin_settings` 仍跳过。
7. `OAUTH_PATHS`（`server.py:279-293`）与 `Route` 清单（`:2448-2471`）——新增路由两处同步；`/health/details` 刻意不在白名单。
8. `tests/conftest.py:16-33` 固定/清空环境变量，`:159-161` 清空 OAuth 字典——新增模块级状态必须同步夹具，避免串状态。
9. `entrypoint.sh` `SEARCH_VARS`（`:79-101`）仅非空才 export、`server.py:108-177` 默认值、`docker-compose.yml:12-51` environment 三处闭环。
10. Cookie 签名 `_sign/_unsign` HMAC-SHA256 前 32 位 hex（`auth.py:88-99`）；下载签名同一密钥、前 24 位 + `f"job:expiry"` + 600s（`api.py:560-584`）——新增 CSRF 不得改变这两处格式。

## 6. 阶段 A 实施结果（2026-11-07，已实际运行测试）

| 原缺陷 | 修复位置 | 验证 |
| --- | --- | --- |
| REST 无严格审计门 | `gateway/audit/middleware.py::strict_gate`（MCP `on_call_tool` 与 REST `trace_call` 共用）、`docker/server.py::run_command_rest` 捕获 `StrictAuditDenied` 返回 503 | `tests/test_phase_a_integrity.py::test_rest_entry_uses_shared_strict_gate` / `test_mcp_entry_uses_same_strict_gate` |
| 导入失败静默 fail-open | `docker/server.py:80-135` 醒目 `[AUDIT ALERT]` + `AUDIT_REQUIRED=1` 拒绝启动 + `AUDIT_ALLOW_UNAUDITED=1` 显式放行；`/health/details` 报告原因 | `test_audit_import_failure_can_refuse_startup` |
| 缓冲纯内存 | 新增 `gateway/audit/spool.py`（0600 JSONL + fsync + 有界轮转 + event_id 幂等）；`storage.py::_execute/_flush_pending/_startup_replay` | `test_spool_replays_events_after_process_restart` / `test_spool_files_are_private_and_bounded` |
| 归档无校验/恢复 | `storage.py::_write_archive` 流式 SHA-256 + `.manifest.json`；`verify_archive` / `list_archives` / `query_archive` / `restore_archive`；`archive_and_prune` 删前二次校验；后台 `/api/archives*` | `test_archive_manifest_verify_restore_and_corruption` / `test_archive_failure_keeps_rows_and_is_reported` |
| OAuth 全内存明文 | 新增 `gateway/oauth/store.py`（clients/codes/tokens 三表、令牌与授权码只存 SHA-256、按哈希惰性水合、`/revoke`）；`CLIENTS`/`AUTH_CODES`/`ISSUED_TOKENS` 保持兼容 | `test_oauth_tokens_are_hashed_persisted_hydrated_and_revoked` / `test_oauth_clients_and_codes_survive_inmemory_reset` |
| `token_expires` 未写入 | `middleware.py::_token_expiry` + `upsert_session(..., token_expires=...)` | 既有 `test_sessions_expire_by_ttl_not_forever_active` |
| 任务日志落盘未脱敏 | `server.py::_spawn_redacting_logger` 用管道 + `redact_stream` 逐行脱敏后 0600 落盘 | `test_task_log_is_redacted_before_hitting_disk` |
| 后台无 CSRF / 会话上限 | `api.py::AdminCsrfMiddleware`（纯 ASGI 双提交 + Origin，登录/healthz 豁免）、`auth.py::csrf_valid` / `ADMIN_MAX_SESSIONS`、`app.js::api` 自动带 token | `test_csrf_blocks_state_changes_and_exempts_login_healthz` / `test_admin_max_sessions_revokes_oldest` |
| 新表与监控 | `storage.py` 追加 10 张方言中性表与索引；`events.py` 新事件常量；`middleware.py` 结构化事件入口；`maintenance.tick` 消费 spool/归档校验失败告警 | `test_structured_event_helpers_use_redaction_boundary` |

**有意偏离计划的判断**：`oauth_*`、`paper_library`、`spool_applied` 属于「当前有效状态 / 幂等标记」，按时间归档删除会误删仍有效的授权或导致重放，故与 `admin_settings` 一样不纳入 `ARCHIVE_TABLES`；其余新增历史表（`search_events`、`paper_downloads`、`paper_text_cache`、`file_transfers`、`task_state`、`archive_manifest`）全部纳入保留策略。`spool_applied` 标记在 spool 清空后仍保留，用于崩溃窗口的幂等保护。

**配置闭环**：`docker/.env.template`、`docker/entrypoint.sh` 的 `SEARCH_VARS`、`docker-compose.yml` 三处已同步 `AUDIT_REQUIRED` / `AUDIT_ALLOW_UNAUDITED` / `AUDIT_SPOOL_*` / `OAUTH_PERSIST_ENABLED` / `ADMIN_MAX_SESSIONS`；`AUDIT_STRICT=0`、兼容开关、`ADMIN_BIND_HOST=127.0.0.1`、`/health` healthcheck 均保持原默认值。

**实际测试**：`.venv/bin/python -m pytest -q` → `57 passed in 7.39s`（既有 44 + 阶段 A 新增 13）；`bash -n docker/entrypoint.sh` 通过。未运行 Docker 构建或真实网络测试，本阶段不声称其通过。

## 7. 真实网络探测（只读，2026-11-07）

OpenAlex、arXiv、Crossref、PubMed、Europe PMC、HN Algolia、StackExchange、GitHub API 在当前出口均返回 HTTP 200。**仅代表本环境出口可达**；不得据此推定所有 CNB / 云服务器可达，也不得把免费 API 描述为无限调用。Unpaywall 需真实邮箱，SearXNG 自建软件免费但上游引擎可达性需运行时探测。

## 8. 阶段 B 实施结果（检索 + 论文，2026-11-07）

| 原缺陷 / 缺失 | 实现位置 | 验证 |
| --- | --- | --- |
| `from_year` 五源语义不一致 | `docker/server.py` 各 `_src_*` 统一为「该年及之后」；Europe PMC 改 `FIRST_PDATE:[y-01-01 TO *]`，PubMed 用日期区间，OpenReview/dblp/S2 客户端 `>=` 过滤；新增 `to_year` | `tests/test_paper_search.py` |
| 先截断后排序 | `_run_paper_sources` 改为「先跨源合并去重 → 再全局排序 → 最后截断」；`gateway/research/metadata.py::merge_papers/rank_papers/canonical_key` | 同上 |
| 引用数缺失与真实零引用混淆 | `Paper.citation_count: int \| None`，`None` = unavailable、`0` = 真实零引用；`format="json"` 结构化输出（默认仍为文本） | 同上 |
| DNS rebinding / Content-Type / 私网过宽 | 新增 `gateway/net/client.py::SafeHttpClient`：一次解析校验全部 IP → 固定 IP 连接并保留 TLS SNI/Host，重定向逐跳重校验，非文本 Content-Type 检测，私网放行收窄到配置主机 | `tests/test_search_web.py` |
| 网页检索能力不足 | 新增 `search_web_advanced` / `fetch_web_page` / `extract_web_content` / `search_website`；`searxng/settings.yml` + Compose `search` profile | 同上 |
| PDF 全链路缺失 | `gateway/research/{fulltext,download,pdf,library,batch}.py` + `gateway/transfer/core.py`；合法 OA 发现顺序 arXiv → PMC/Europe PMC → OpenAlex → Unpaywall（缺邮箱显式 `SOURCE_UNAVAILABLE`）→ 机构/出版社 OA，主机白名单，禁止盗版镜像 | `tests/test_pdf_download.py`、`test_pdf_read.py`、`test_library.py` |
| 假 PDF / 假报成功 | 流式临时文件 + 增量 SHA-256 + `%PDF-` 头 / Content-Type / 最终 URL / 大小 / EOF 校验 + 原子改名 + Range 续传 + 同哈希去重；`paper_downloads` 记录创建/下载中/校验/成功/失败全过程 | 同上 |
| 按页阅读缺失 | `pypdf` 按页文本、页码引用、章节/关键词/参考文献检索、结果缓存；扫描版明确提示需 OCR（默认不安装、不调用） | `tests/test_pdf_read.py` |
| 无本地文献库 | 项目/标签/阅读状态、去重、BibTeX/RIS/CSV/JSON 导出、有界并发批量下载与逐篇失败原因 | `tests/test_library.py` |

**MarkItDown 交叉校验（真实执行，2026-11-07）**：用 `tests/mock_server.py::make_test_pdf` 生成 2 页测试 PDF，经 `download_paper` 下载并校验（`status=success`，1031 bytes，SHA-256 `05f2eaedd6e7dc21…`）；`read_paper_pdf` 提取 2 页 / 195 字符 / 章节 `Abstract, Methods, Results, References`；`markitdown.MarkItDown().convert()` 得到 242 字符且含分页符 `\x0c`。关键词 `transformer`、`Accuracy improved`、`Vaswani` 在两条链路中均命中；MarkItDown 转换过程出现 `onnxruntime pthread_setaffinity_np` 警告（不影响结果）。该校验使用本地模拟 PDF，**不是真实网络下载的论文**，不能据此声称真实 OA PDF 下载已验收。

**测试**：阶段 B 结束时 `.venv/bin/python -m pytest -q` → `89 passed`。

## 9. 阶段 C 实施结果（权限 / 双后端 / Git，2026-11-07）

| 缺失 | 实现位置 | 验证 |
| --- | --- | --- |
| 默认完整管理员权限模型 | `gateway/access/policy.py`：默认 `full_admin`（无路径 / shell 白名单），`restricted` 为兼容模式；灾难级正则与确认门明确标注「辅助保护，非安全边界」 | `tests/test_access_mode.py` |
| 可选独立确认 | `MCP_INDEPENDENT_CONFIRM` 经 `approval_events` 发放一次性审批，审批绑定命令摘要且仅可消费一次 | 同上 |
| Host Agent / 双后端 | `gateway/host/{protocol,agent,client}.py`（0600 Unix Socket + HMAC + 时间窗防重放 + 目录白名单，无 TCP），`gateway/exec/backends.py`（容器包装 `_exec`、宿主代理不静默降级） | `tests/test_host_agent.py` |
| 8 个结构化 Git 工具 | `gateway/gitops/tools.py`：`shell=False` 参数列表、按仓库锁、高风险默认 `dry_run`、URL 内嵌凭据拒绝（只走 `GIT_ASKPASS`）、`safe_repo_label` + 脱敏写 `git_operations` | `tests/test_gitops.py` |
| 部署接线 | `entrypoint.sh` 新增 `host-agent` 角色（缺密钥拒绝启动）、Compose `host` profile（不发布端口、显式挂载）、`SOURCE_LABELS` 增加容器/宿主标识 | `tests/test_startup.py` |

**测试**：阶段 C 专项 20 用例通过；全量 `103 passed`。真实宿主权限**未验收**——只在本地 Unix Socket / 容器 profile 上验证协议、HMAC、目录隔离与 0600 权限。

## 10. 阶段 D 实施结果（任务 / 文件传输，2026-11-07）

| 缺失 | 实现位置 | 验证 |
| --- | --- | --- |
| 任务状态纯内存、只杀单进程 | `gateway/tasks/manager.py`：`task_state` 持久化、`start_new_session=True` + `os.killpg`、`/proc/<pid>/environ` 的 `MCP_TASK_ID` 标记防 PID 复用、启动重关联（不匹配如实 `interrupted`）、`experiment_runs` 填充 git commit / output_path、`experiment_manifest` 导出复现清单 | `tests/test_tasks_v3.py` |
| 通用分块 / Range / 签名下载 | `gateway/transfer/http_api.py` + `server.py` 的 `/files/upload`、`/files/download`：`.part` + fsync + 原子改名、`X-Upload-Offset` 续传（偏移不符 409）、超限 413 清理、SHA-256 不符 422、`Range`/206 `Content-Range`、短期 HMAC 签名 | `tests/test_transfer.py` |
| 非工具 HTTP 端点不入审计 | 两端点走 `trace_call(..., source="http")` 进入 `tool_calls`；`HIGH_RISK_TOOLS` 纳入 `files.*`，严格模式拒绝返回 503 | 同上 |
| 配置项 | `MCP_TRANSFER_DIR` / `MCP_TRANSFER_SECRET` / `MCP_TRANSFER_ALLOW_PRIVATE` / `TRANSFER_MAX_MB` / `MCP_TASK_LOG_DIR` 同步到模板、入口、Compose、`.gitignore`、`CONFIG_WHITELIST` | `tests/test_startup.py` |

**测试**：全量 `122 passed`。重关联只在同机 `/proc` 标记匹配时算 `reattached`；容器重建后仅恢复元数据，不声称进程复活。

## 11. 阶段 E 实施结果（后台，2026-11-07）

| 缺失 | 实现位置 | 验证 |
| --- | --- | --- |
| 两处新视图 | `templates/index.html` 导航 + `app.js` 的 `renderSearch` / `renderPapers`，扩展仪表盘 / 调用 / 实验 / 服务器四页；`app.css` 来源徽标、状态色、进度条、论文卡片与响应式 | `tests/test_dashboard_v3.py` |
| 监控 API | `/api/search/events`（单条聚合查询的逐源矩阵）、`/api/papers*` `/api/transfers*`、`/api/host/status`、`/api/v3/overview` | 同上 |
| 「任务创建成功」与「文件校验成功」混淆 | 下载任务面板用分段流程条 + 进度条区分 `created / downloading / verifying / success / failed`，逐条展示失败原因；页脚标注来源与更新时间 | 同上 |
| 来源与后端标识 | `sourceChip()` + `SOURCE_LABELS` + `/api/host/status`；`tool_calls.source` 筛选与结果摘要列 | 同上 |
| 签名下载鉴权 | `/api/papers/file`、`/api/transfers/file` 不挂 `require_admin`（否则纯签名链接被提前 401），改为「有效会话或有效 HMAC 签名二选一」；未配置 `MCP_TRANSFER_SECRET` 时后台拒绝签发 | 同上 |

**测试**：阶段 E 专项 19 用例通过；全量 `141 passed`（阶段结束快照；文档/配置同步后为 `142 passed`）。**未在真实浏览器中渲染页面**：验证覆盖 API、模板/静态资源接线、`node --check` 语法与端到端 HTTP 冒烟，浏览器视觉验收尚未执行。后台「上游连通性」固定显示「未检测」，不自动发外部请求、不虚构可达性。

## 12. 当前规模（阶段 F 实测）

- `docker/server.py` 注册 `@mcp.tool()` **49 个**（基线 21 + 新增 28）；网关 `Route` 追加 11 条；后台 API 路由 41 条。
- 离线套件：`tests/test_*.py` **17 个文件**、`def test_*` **133 个**（含参数化共 **143 个用例**）。
- opt-in 集成套件：`tests/integration/test_*.py` **2 个文件 / 14 个用例**，由 `pytest.ini` 的
  `-m "not integration"` 默认排除；`-m integration` 显式启用。
- 基线默认值未变：`AUDIT_STRICT=0`、`OAUTH_ALLOW_*=0`、`ADMIN_BIND_HOST=127.0.0.1`、`/health` 路径与 Compose healthcheck 不变。

## 13. 阶段 F 复核与修复（2026-11-07）

阶段 F 前用 code-explorer 做了 very thorough 只读复核，发现并修复以下**事实性错误 / 真实缺陷**：

| 问题 | 处理 |
| --- | --- |
| 文档引用不存在的 `tests/test_spool_archives.py` / `test_csrf_oauth_persist.py` / `tests/integration/` | 前两处改为真实文件；新建 `tests/integration/{test_real_network,test_workflows}.py` 与 `pytest.ini` |
| `integration` marker 未注册、`-m integration tests/integration/` 必然报错 | `pytest.ini` 注册 marker 并默认排除；`-m integration` 可覆盖 |
| README 仍称「四服务部署」（实际 7 service）与 `MCP_WORKDIR_MOUNT` 误导 | 两处中英文 README 已改正并注明该变量仅 `run.sh` 读取 |
| `.gitignore` 漏掉根目录 `transfers/`、`.signed-download-*` 无对应实现 | 补 `transfers/`；移除空转规则并说明签名走 HMAC 查询参数 |
| `MCP_TASK_LOG_DIR` / `MCP_UPSTREAM_URL` 被 Compose 硬编码覆盖；`admin-web` 缺 `AUDIT_STRICT` / `AUDIT_SPOOL_*` / `AUDIT_ARCHIVE_BATCH` 等 | 改用 `${...}` 插值、补齐 admin-web 环境段与 `CONFIG_WHITELIST`；`.env.template` 如实列出只能由 Compose 设置的内部变量 |
| `tests/conftest.py` 未复位 `_POLICY` / `_STORE` / `_BACKENDS` / `_MANAGER` / `_REPO_LOCKS` 等模块级单例 | 新增 autouse 复位夹具；默认套件仍全绿 |
| `docs/v3-upgrade.zh-CN.md` 称 8 个 Git 工具「全部高风险默认 dry_run」 | 改为逐项说明：clone/commit/sync/recover 默认 `True`，branch/stage/worktree 默认 `False`，inspect 为只读 |
| **真实产品缺陷**：`batch_download_papers(project=...)` 的项目参数不写入 `paper_library`，且重复下载会用空值覆盖已有项目 / 标签 / 阅读状态 | `gateway/research/download.py::_upsert_library` 接收显式 `project`，重复下载保留用户元数据，写入失败改为显式告警；新增 `tests/test_library.py` 离线回归 |

阶段 F 真实执行结果见 [v3-acceptance-report.md](./v3-acceptance-report.md)。
