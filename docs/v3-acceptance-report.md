# CNB MCP Gateway V3 验收报告

> 本报告只记录**真实执行过**的检查。未运行的测试一律标注「未执行」，不得据此声称通过。
> 最近更新：2026-11-07（阶段 A–E 完成；阶段 F 的测试、重建与 8 条工作流已执行；推送见第 7 节）。
> 配套：[v3-inventory.md](./v3-inventory.md) · [v3-upgrade.zh-CN.md](./v3-upgrade.zh-CN.md)
> 机器可读结果：`tests/integration/conftest.py` 在集成运行结束时写出
> `/tmp/mcp-v3-e2e-results.json`，每条工作流状态由 pytest `makereport` 钩子按真实结论定稿。

---

## 0. 基线与分支

| 项 | 值 |
| --- | --- |
| GitHub `main`（阶段 F 前重新核对） | `3cf1ab57a100399704cfe702bfdabb3f739aece8`（`git ls-remote github refs/heads/main`） |
| 源码树 | `e353ce31d50ebe5cc7ee9e1820db4f20f5e76176` |
| 本地 `main` / `origin/main` | 旧 `6c71287`，**未改动**（`git ls-remote origin refs/heads/main` 复核一致） |
| 工作分支 | `upgrade/v3-full-admin-research`（由 `3cf1ab57` 创建） |
| 回退点 | `183ee29` 保留 |
| 基线差异 | 本地 38 文件与远端 `main` 逐一比对哈希，差异 0 |
| 默认分支 / 历史 | 未改写、未 force-push |
| 提交 / 推送 | 见第 7 节 |

---

## 1. 离线单测（真实执行）

统一命令：`.venv/bin/python -m pytest -q`（全局 `python -m pytest` 会报 `No module named pytest`）。
`pytest.ini` 默认带 `-m "not integration"`，真实网络测试默认取消选中。

| 阶段 | 全量结果 | 专项结果 | 备注 |
| --- | --- | --- | --- |
| 基线 | 44 passed | — | 既有 5 个测试文件 |
| A 审计完整性 | **57 passed** | 新增 13 用例 | 严格门、spool 重启回放、归档 SHA-256/恢复、OAuth 哈希/撤销、CSRF、会话上限、任务日志脱敏 |
| B 检索 + 论文 | **89 passed** | 新增检索/论文模拟测试 | 本地 `ThreadingHTTPServer` mock，不依赖外部限流 |
| C 权限 / 双后端 / Git | **103 passed** | 专项 20 passed | `test_access_mode` / `test_host_agent` / `test_gitops` / `test_startup` |
| D 任务 / 文件传输 | **122 passed** | 传输 9 + 任务 6 passed | 分块续传、Range、签名下载、进程组终止、重关联 |
| E 后台 | **141 passed** | 专项 19 passed | 搜索监控 / 论文库 API、导出、签名 Range 下载、来源标识、权限 |
| 文档与配置同步 | **142 passed** | 新增 1 条一致性用例 | env 模板、依赖、4 profile、SearXNG、49 个工具名与三份文档 |
| 阶段 F 缺陷修复后 | **143 passed, 14 deselected** | 新增文献库项目字段回归用例 | `test_upsert_library_records_project_and_preserves_user_metadata` |
| **当前** | **143 passed, 14 deselected**（24.74s） | 离线 17 文件 / `def test_*` 133 个；opt-in 集成 2 文件 / 14 用例 | 最后一次真实执行于 2026-11-07 |

阶段专项命令与结果（均真实执行）：

```
.venv/bin/python -m pytest -q tests/test_access_mode.py tests/test_host_agent.py tests/test_gitops.py tests/test_startup.py  → 20 passed
.venv/bin/python -m pytest -q tests/test_transfer.py tests/test_tasks_v3.py                                              → 15 passed
.venv/bin/python -m pytest -q tests/test_dashboard_v3.py                                                               → 19 passed
```

### 语法 / 配置校验（真实执行）

| 命令 | 结果 |
| --- | --- |
| `bash -n docker/entrypoint.sh` | 通过 |
| `node --check gateway/admin/static/app.js` | 通过 |
| `.venv/bin/python -m compileall -q docker/server.py gateway/ tests/` | 通过 |
| `docker compose config -q` | 通过（默认 profile） |
| `--profile search / host / tunnel / postgres config -q` | 4 个 profile 全部通过（改动后复验） |
| `read_lints`（本轮全部改动文件） | 0 error / 0 warning |
| `docker compose config --format json` 校验 admin-web 环境闭环 | `AUDIT_STRICT=0` / `AUDIT_SPOOL_ENABLED=1` / `AUDIT_ARCHIVE_BATCH=1000` / `AUDIT_MAX_FIELD_CHARS=32768` 均已注入 |

---

## 2. 独立真实网络集成（opt-in，真实执行）

命令：`.venv/bin/python -m pytest -q -m integration tests/integration/`
最终一轮结果：**13 passed, 1 skipped in 416.91s（0:06:56）**。

> 首轮结果为 `3 failed, 10 passed, 1 skipped`。3 条失败经定位后如实修复并复跑通过，
> 未把失败隐藏成通过：WF4 断言按 `search_paper_fulltext` 的真实 JSON 返回格式修正；
> WF6 查询用了错误列名（应为 `operation`）；WF5 暴露真实产品缺陷（见第 6 节第 7 条）。

| 项 | 状态 | 真实结果 |
| --- | --- | --- |
| 逐源只读连通性 | **已执行** | 9/9 端点 HTTP 200：OpenAlex、Crossref、arXiv API、PubMed、Europe PMC、StackExchange、GitHub API、HN Algolia、arXiv OA PDF。**仅代表本次运行出口**，不代表所有 CNB / 云环境可达，也不代表无限调用 |
| 五源真实学术检索 + 失败隔离 | **已执行** | `result_count=15`、`sources=[OpenAlex, CrossRef, arXiv, PubMed, Europe PMC]`、`source_failures=[]`、2.881s；`citation_count=null` 与 `0` 语义在 JSON 中明确区分 |
| Unpaywall 邮箱门 | **已执行** | 未配置 `MCP_SEARCH_MAILTO` 时显式返回 `SOURCE_UNAVAILABLE` 及原因，未静默跳过；同时如实返回 OpenAlex 找到的合法 OA 候选 |
| 真实 arXiv OA PDF 下载并校验 | **已执行** | `status=success`、2,215,244 字节、`%PDF-` / `%%EOF` / Content-Type / 最终 URL / SHA-256 全部校验通过；SHA-256 `bdfaa68d8984f0dc02beaca527b76f207d99b666d31d1da728ee0728182df697`，并用 Python 独立重算哈希比对 |
| 真实 PDF 按页阅读 / 全文检索 | **已执行** | `pypdf` 提取 15 页 / 39,597 字符，第 1 页含 `Attention Is All You Need`；`transformer` 全文检索命中 3 处 |
| MarkItDown 独立交叉校验 | **已执行** | 对**真实下载的 arXiv PDF**（非模拟 PDF）：MarkItDown 40,174 字符，`Attention Is All You Need` / `Vaswani` / `transformer` 在两条链路均命中 |
| 免费 API 真实 429 / 限额降级观测 | **未执行** | 仅本地 mock 模拟 429 / 5xx 退避；未主动打满免费额度，不声称观测到真实限流 |
| 自建 SearXNG 上游引擎可达性 | **已实测（不可达）** | 集成用例中未配置 `MCP_SEARCH_SEARXNG_URL` → `SKIPPED`（不虚构）；另在部署验收中真实启动 `search` profile 并探测：JSON API HTTP 200，但 5 个上游引擎全部 `timeout`、`results=[]`。详见第 4 节 |
| PMC OA PDF 真实下载 | **未执行** | 真实链路只覆盖 arXiv 官方 OA；不声称 PMC 已验收 |

---

## 3. 八条端到端工作流（真实 MCP 客户端，全部执行）

客户端为 `mcp.ClientSession` 经 **streamable HTTP `/mcp`** 连接真实 uvicorn + ASGI 网关
（同一份 `docker/server.py`），不是进程内直接调函数；数据源为真实免费服务。

| # | 工作流 | 状态 | 真实输出摘要 |
| --- | --- | --- | --- |
| 1 | OAuth 授权 / MCP 连接 / 工具列举 | **PASS** | DCR `/register` 201 → PKCE S256 `/authorize` → `/token` → 用签发令牌 `initialize` 并列举 **49 个工具** → `/revoke` 后令牌确实失效 |
| 2 | 免费网页搜索 → 正文抓取 | **PASS** | `search_web_advanced` 用 GitHub + StackExchange 真实返回 6 条（`provider_failures=[]`）；`fetch_web_page(https://example.com)` 393 字符含不可信标注；`extract_web_content` 返回 16 个结构化字段含分块 |
| 3 | 学术检索（多源 + JSON）→ OA 发现 | **PASS** | 五源真实检索 24 条、跨源合并去重、`sort=date`、`source_failures=[]`；`find_paper_fulltext` → `状态: FOUND`，候选 `https://arxiv.org/pdf/1706.03762` |
| 4 | 单篇 PDF 下载 → 按页阅读 → 全文检索 | **PASS** | 真实 arXiv OA PDF `success` / 2,215,244 字节 / SHA-256 独立复算一致；15 页，第 1 页标题命中，`transformer` 命中 3 处 |
| 5 | 批量下载 → 文献库 → 四种导出 | **PASS** | 3 个标识去重为 2、`partial`（成功 1 / 失败 1，失败项如实报告）；文献库 `count=1`；BibTeX 162 / RIS 95 / CSV 297 / JSON 837 字符 |
| 6 | 结构化 Git 工具链 → 审计记录 | **PASS** | 临时真实仓库执行 inspect / stage / commit / branch / worktree；HEAD = `v3 acceptance commit`；`git_operations` 写入 5 类 op |
| 7 | 后台任务 → 进程组终止 → 复现 manifest | **PASS** | `sleep 25 & wait` 真实启动；`stop_task` 返回「已终止整个进程组」；`experiment_manifest` 导出 13 个字段含 `git_commit` / `output_path` / `env_key_fingerprint` |
| 8 | 分块上传 / Range / 签名下载 / 后台页 | **PASS** | `.part` 断点续传 1,714 字节且 SHA-256 一致；`Range: bytes=100-199` → 206 `Content-Range: bytes 100-199/1714`；HMAC 签名链接 200、无签名 401；后台 `/api/v3/overview`、`/api/transfers`、`/api/search/events`、`/api/papers` 均 200 |

> WF8 的后台部分是 **HTTP 层（TestClient + CSRF）验证**，不是浏览器视觉验收。
> 工作流结果由 `pytest_runtest_makereport` 钩子按 pytest 真实结论定稿；失败用例不会被记成 PASS。

---

## 4. 部署环境验收（真实执行）

| 项 | 状态 | 真实结果 |
| --- | --- | --- |
| 镜像重建 `docker compose up -d --build` | **已执行** | 构建成功，产出 `ghcr.io/ninaawa/cnb-mcp-gateway:latest`；3 个常驻容器 Recreated → Started |
| 重建时凭据保护 | **已执行** | 用内存态 Python 进程环境继承现有容器的 44 个变量后调用 Compose；未写 `.env`、`.git/config`、凭据文件或任何仓库文件；`MCP_TOKEN` / `ADMIN_PASSWORD` 保持非空 |
| `/health` / `/healthz` | **已执行** | 网关 `{"status":"ok","audit":{"healthy":true}}`；后台 `{"status":"ok","audit":{"healthy":true,"backend":"sqlite-wal","spool_pending":0,...}}` |
| `docker compose ps` | **已执行** | `mcp-gateway` / `mcp-admin-web` / `audit-storage` 全部 `Up (healthy)`（集成用例 `compose-health` PASS） |
| 持久化卷 / 实验数据不丢失 | **已执行** | `workspace_audit-data` 卷保留；`audit.db` 由 188,416 → 290,816 字节（新表建立，旧数据未删），`.metrics_token` / `artifacts/` / `backup/` / `task-logs/` 时间戳保持；新建 `spool/`（0700）。未执行 `down -v` |
| 预览容器短暂中断与恢复 | **已执行** | 重建期间 3 容器 Recreate，恢复后全部 healthy |
| `search` profile 实际启动 | **已执行** | `docker compose --profile search up -d searxng`（`SEARXNG_SECRET` 仅内存态）成功；容器内 `/healthz` 返回 `OK`；从网关容器经内部网络请求 `/search?q=open+access&format=json` → HTTP 200。**但 `results=[]`**，`unresponsive_engines` 为 brave / duckduckgo / google cse / wikidata / wikipedia 全部 `timeout` —— 实证「软件免费 ≠ 上游可达」，不声称通用搜索可用 |
| `host` profile 实际启动 | **已执行（容器内）** | `docker compose --profile host up -d host-agent` 成功；socket `srw-------`（0600）；未发布任何宿主端口（`docker ps` 只有 EXPOSE，无 `->` 映射）。用随机内存态 HMAC 令牌 `ping` 成功（`role=host-agent, no_tcp=True, roots=['/workspace']`），`system_info` 返回 `cpu_percent/gpu/scope_note/source`；错误令牌被拒 `AUTH_FAILED` |
| 可选 profile 收尾 | **已执行** | 验证后 `docker compose --profile search --profile host stop` 停掉两个可选容器，恢复为 3 常驻服务，三者仍 healthy；`workspace_audit-data` / `workspace_host-socket` 卷均保留 |
| `tunnel` / `postgres` profile 实际启动 | **未执行** | 需外部隧道令牌 / 独立 PG 实例；只做了 `config -q` 语法校验 |
| 真实宿主权限（Host Agent 部署到宿主机） | **未验收** | 上面是**容器内** Host Agent，只能访问显式挂载目录，不等于真实宿主 root 权限；真实授权需部署时由用户挂载 |
| 浏览器视觉验收（两处新视图） | **未执行** | 仅验证 API、模板/静态资源接线、`node --check` 与 HTTP 冒烟 |

---

## 5. 阶段 F 前复核发现并修复的事实性错误

由 code-explorer 以 very thorough 级别只读复核，逐条修复（详见
[v3-inventory.md 第 13 节](./v3-inventory.md)）：

1. 文档引用 3 个不存在的测试路径 → 前两处改为真实文件，第三处新建 `tests/integration/`。
2. `integration` marker 未注册、`-m integration tests/integration/` 必然失败 → 新建 `pytest.ini` 注册并默认排除。
3. README 仍称「四服务部署」（实际 7 service）、`MCP_WORKDIR_MOUNT` 被误列为 Compose 变量 → 中英文 README 均已改正。
4. `.gitignore` 漏掉根目录 `transfers/`、`.signed-download-*` 无对应实现 → 补规则、删空转规则。
5. Compose 配置闭环断点（`MCP_TASK_LOG_DIR` / `MCP_UPSTREAM_URL` 硬编码覆盖、admin-web 缺审计变量、`MCP_HOST_AGENT_ROOTS` 未注入网关）→ 全部补齐并复验 5 种 `config -q`。
6. `tests/conftest.py` 未复位 V3 模块级单例 → 新增 autouse 复位夹具；默认套件仍全绿。
7. 文档称 8 个 Git 工具「全部默认 dry_run」与实际不符 → 改为逐项说明。
8. `CONFIG_WHITELIST` 缺 `AUDIT_MAX_FIELD_CHARS` 等 → 补齐。
9. **真实产品缺陷**：`batch_download_papers(project=...)` 的项目参数不写入 `paper_library`，重复下载还会用空值覆盖已有项目 / 标签 / 阅读状态 → `gateway/research/download.py::_upsert_library` 修复，写入失败改为显式告警，新增离线回归用例。

---

## 6. 已修复的实现缺陷（完整回归清单）

1. `TaskManager.terminate` 曾可能误杀同进程组无关进程 —— 现以 `/proc/<pid>/environ` 的 `MCP_TASK_ID` 标记为唯一依据。
2. `get_task_status` 发现终态后未同步 `task_state` —— 现写入终态。
3. `/files/download` 签名链接被 `BearerAuthMiddleware` 提前 401 —— 现仅在 HMAC 签名有效时豁免 Bearer。
4. `/api/papers/file`、`/api/transfers/file` 挂 `require_admin` 会让纯签名链接提前 401 —— 改为「有效会话或有效 HMAC 签名二选一」。
5. 分块上传偏移与临时分片大小不一致会产生损坏文件 —— 现 409；超限 413 清理 `.part`；SHA-256 不符 422 删除临时文件。
6. 临时 Git 测试仓库提交被全局 gpg 签名配置拒绝 —— 测试仓库级显式关闭。
7. **阶段 F 新增**：批量下载丢失 `project` 且重复下载清空用户元数据 —— 见第 5 节第 9 条；离线回归 + WF5 真实工作流双重验证。

---

## 7. 事实性错误评审与交付记录

### 7.1 `[skill:cnb-code-review]` 评审结论：approve（LGTM，无行级意见）

按「只看本次改动 `+` 行、只提事实性错误」的口径复核全量 diff：
文档与代码的工具数（49）、测试数（离线 143 / 集成 14）、环境变量闭环、`@mcp.tool`
签名与调用方、验收命令引用的每个测试路径，均由 `test_docs_and_config_stay_in_sync_with_v3_code`
及真实运行结果交叉验证；`git diff --check`、语法检查、5 种 Compose 配置校验均通过；
改动中不含真实 PAT（扫描到的 `ghp_ABCDEF...` 均为测试用假令牌）。
评审前发现并已修复的 9 类事实性错误逐条列于 [v3-inventory.md 第 13 节](./v3-inventory.md)，
其中批量下载丢失 `project` 属于真实产品缺陷，已同时补离线回归与 WF5 真实工作流验证。
**结论：无遗留事实性错误，放行。**

### 7.2 提交 / PR / 推送

| 项 | 状态 |
| --- | --- |
| `[skill:cnb-code-commit]` 规范提交与功能分支 PR | 执行结果见本节末尾「交付回填」 |
| 双远端推送（内存态 `credential.helper`） | 执行结果见本节末尾「交付回填」 |
| 推送前后 `.git/config` / `.git/.git-credentials` / remote URL 哈希核对 | 执行结果见本节末尾「交付回填」 |
| PAT 用后吊销轮换提醒 | 推送完成后提醒用户 |

推送前基线哈希（2026-11-07 实测，文件内容未打印）：
`.git/config` = `cad813acb05d18ca122d5359e8430a816a04ae2dc52272d1f1a9f0b9212fec59`；
`.git/.git-credentials` = `39d24ccff4d19aa5fe7f6036ac8353afa24f1ef4c322748ea4611149e90b2608`
（该文件在接手前已存在，仅覆盖 `cnb.cool`，本次不写入、不修改）；
两个 remote URL 均为无凭据的纯 HTTPS 地址。

### 7.3 交付回填

> 本节在提交与双远端推送完成后回填；若推送未完成，保留「未执行」标注，不声称成功。

- 规范提交：见回填
- 功能分支 PR：见回填
- `origin` / `github` 双远端推送：见回填
- 推送后文件哈希复核：见回填

---

## 8. 如实声明

- 本报告中所有 `passed` 均来自本地 `.venv/bin/python -m pytest` 的真实执行；没有把未运行的测试写成通过。
- 首轮集成的 3 条失败已如实记录，修复后复跑通过；没有改写历史结果。
- 真实网络结果仅为 2026-11-07 当前出口的实测，不等于服务条款合规、额度充足或所有环境可达。
- 垂直社区搜索不是全网通用搜索；免费 API 不是无限调用；SearXNG 软件免费不等于上游可达；OCR 为可选能力、默认未启用。
- 「任务创建成功」与「文件最终校验成功」在代码、API 与页面上均已区分，不存在把仅返回标题 / PDF 链接当作实际下载成功的路径。
- 真实宿主 root 权限、浏览器视觉验收、免费 API 真实 429 观测、PMC OA PDF 真实下载、
  `tunnel` / `postgres` profile 实际启动均**未执行**，不声称通过。
- `search` / `host` profile 虽已真实启动并验证，**上游搜索引擎在当前出口全部超时**，
  且 Host Agent 为容器内实例；不声称通用全网搜索可用或真实宿主权限已验收。
