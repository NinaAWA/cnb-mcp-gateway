# 网页搜索 / 论文检索：开源免费方案调研与选型

> 调研日期：2026-11-07
> 适用项目：CNB MCP Gateway（`/workspace`，FastMCP 4.1.6 单容器 HTTP + Bearer/OAuth 网关）
> 本文是新增检索工具的唯一选型依据；宿主机与容器内出网均已实测。

## 1. 结论速览

- **论文检索**：存在一批完全开放、零 API Key、无需自建服务的官方 API，且在本机与容器内**全部实测可达**。直接调用即可，这是本次默认落地的能力。
- **通用网页搜索**：这台服务器是**白名单式出网**。DuckDuckGo / Google / Wikipedia / Brave / Jina Reader / 公共 SearXNG 实例均连接超时，Mojeek 对自动化查询返回 403，Bing RSS 返回空结果且版权声明限定「仅个人非商业用途在 RSS 聚合器中呈现」。因此不存在「零 Key + 开源 + 通用网页搜索」三者同时成立、且在本机可用的方案。
- **默认取舍**：论文侧走官方开放 API；网页侧默认启用本机实测可达的**垂直开放检索源**（HN Algolia、StackExchange、GitHub 仓库等），并把「自建 SearXNG」和「免费额度 Key 源」做成可选 provider，未配置时优雅降级。
- **不引入新依赖**：全部用 Python 标准库 `urllib.request` / `json` / `xml.etree.ElementTree` 实现，`docker/requirements.txt` 与 `docker/Dockerfile` 保持不变。

## 2. 论文 / 学术数据源（零 Key、开放）

| 数据源 | 端点 | 覆盖与返回 | 限流 / 条款 | 实测 |
|---|---|---|---|---|
| OpenAlex | `https://api.openalex.org/works` | 5 亿+ 全学科作品、作者、机构、引用数；数据 CC0；JSON | 无 Key 10 req/s，免费 Key 30 req/s 且日额度 ×10；**无 Key 只能取第一页**（cursor paging 需免费 Key）；`per_page` ≤100，建议 `select` 裁字段；semantic search 1 req/s | 宿主机 200 / 容器 200 |
| arXiv | `https://export.arxiv.org/api/query` | 理/工/CS 预印本，Atom 1.0；标题、作者、摘要、分类、PDF/DOI 链接 | 官方 API User's Manual：建议请求间隔 ≥3 秒并缓存；单次 `max_results` ≤2000、总量 ≤30000；须遵守 arXiv API Terms of Use | 200 / 200 |
| CrossRef | `https://api.crossref.org/works` | 1.4 亿+ DOI 注册元数据，JSON，可按 DOI 精确查 | 无需注册、无 Key；建议带 `mailto` 进 polite pool；绝大多数元数据无版权限制（少数摘要有出版方版权） | 200 / 200 |
| Europe PMC | `https://www.ebi.ac.uk/europepmc/webservices/rest/search` | 生命科学 4000 万+，含预印本与 PMC 全文，`format=json` | 无 Key；宽松限流 | 200 / 200 |
| PubMed / NCBI | `https://eutils.ncbi.nlm.nih.gov/entrez/eutils/` | 生物医学文献；`esearch` + `esummary`/`efetch` | 无 Key 3 req/s，免费 Key 10 req/s | 200 / 200 |
| dblp | `https://dblp.org/search/publ/api` | CS 书目 650 万+，`format=json` | 无 Key；实测返回 202 + 「Making sure you're not a bot!」HTML 反自动化页，**当前出口 IP 实际不可用**；仅作可选源并显式报错 | 202（反自动化页）/ 202（反自动化页） |
| OpenReview | `https://api.openreview.net/notes/search` | 会议投稿与公开评审 | 无 Key | 200 / 200 |
| Unpaywall | `https://api.unpaywall.org/v2/{doi}?email=` | 按 DOI 返回合法 OA 全文链接 | 无 Key，但**必须真实 email**；10 万次/天；占位邮箱返回 422 | 422（占位邮箱）/ 200 |
| Semantic Scholar | `https://api.semanticscholar.org/graph/v1/paper/search` | 2 亿+ 论文、引用图、TL;DR | 免费但无 Key 限流严格，本机与容器均 **429**（共享 IP）；仅作可选源并做退避 | 429 / 429 |

其他可扩展但本阶段不启用：Zenodo、HAL、bioRxiv/medRxiv、CORE（需 Key）、OpenAlex 免费 Key 解锁深翻页、NCBI 免费 Key 提额。

## 3. 网页搜索方案

### 3.1 开源自建（零 Key）

| 方案 | 许可 / 活跃度 | 说明 | 本机可用性 |
|---|---|---|---|
| SearXNG | AGPL-3.0，39k stars | 聚合 261 个引擎、无追踪、可自建；内置 arXiv / OpenAlex / Crossref / PubMed / Europe PMC / Semantic Scholar 引擎。JSON 输出默认关闭，需在 `settings.yml` 开启；自建需额外容器与运维 | 公共实例 `searx.be`、`searx.tiekoetter.com`、`search.bus-hit.me`、`searxng.site` **全部超时**；自建时上游 Google/DDG 亦被封，仅 Bing 可达 |
| YaCy / Marginalia / Mwmbl | 各自开源许可 | 自建索引，覆盖面和新鲜度远不如聚合引擎，运维成本高 | 未纳入 |
| Whoogle | MIT | 本质是 DuckDuckGo 代理，DDG 被封即不可用 | 不可用 |

### 3.2 现成开源 MCP Server（可借鉴，不建议直接挂载）

| 项目 | 许可 | 能力 |
|---|---|---|
| `ihor-sokoliuk/mcp-searxng` | MIT，1.3k stars | SearXNG 搜索、多实例故障转移、页面转 Markdown |
| `modelcontextprotocol/servers` | 参考实现 | `fetch`（页面转 Markdown）、`brave-search`（需 Key） |
| `blazickjp/arxiv-mcp-server` | Apache-2.0，3.2k stars | arXiv 搜索 / 下载 / 读全文 |
| `cyanheads/openalex-mcp-server` | Apache-2.0 | OpenAlex 检索 |
| `oksure/openalex-research-mcp` | MIT | OpenAlex 研究检索 |
| `synapticore-io/paper-search-mcp` | MIT | 11 个学术平台聚合 |
| `n24q02m/wet-mcp` | PyPI MIT | 内嵌 SearXNG 的搜索/抓取/学术调研 |

这些多为独立 stdio 服务。挂到现有单容器 HTTP + OAuth 网关需要额外进程、鉴权穿透与生命周期管理，与当前自包含架构冲突，故只借鉴其解析与归一化思路。

### 3.3 免费额度但需 Key（非开源，可选）

| 服务 | 免费额度 | 端点可达性 |
|---|---|---|
| Tavily | 1000 credits/月 | `api.tavily.com` 实测 401（域名可达，仅缺 Key） |
| Brave Search API | 2000 次/月 | `api.search.brave.com` **超时** |
| Exa / Serper / AnySearch | 各有免费额度 | 未逐一验证 |

### 3.4 零 Key 的垂直开放 API（默认网页检索来源）

| 来源 | 端点 | 实测 |
|---|---|---|
| Hacker News (Algolia) | `https://hn.algolia.com/api/v1/search` | 宿主机 200 / 容器 200 |
| StackExchange | `https://api.stackexchange.com/2.3/search/advanced` | 200 / 200 |
| GitHub 仓库检索 | `https://api.github.com/search/repositories` | 宿主机偶发超时，列为可选 |
| Bing HTML / RSS | `www.bing.com` | HTML 302→200；RSS 返回空 channel 且版权声明禁止非聚合器用途，**不采用** |
| Mojeek | `www.mojeek.com` | 普通查询 200，自动化/JSON 查询 403，**不采用** |

## 4. 出网实测汇总

测试方式：`curl -sS --max-time 8~20 -o /dev/null -w '%{http_code}'`，分别在宿主机与 `docker run --rm cnb-mcp-gateway:local` 容器内执行；`000` = 连接超时。

| 域名 | 宿主机 | 容器 | 用途 |
|---|---|---|---|
| `api.openalex.org` | 200 | 200 | 论文 |
| `export.arxiv.org` | 200 | 200 | 论文 |
| `api.crossref.org` | 200 | 200 | 论文 |
| `www.ebi.ac.uk` | 200 | 200 | 论文 |
| `eutils.ncbi.nlm.nih.gov` | 200 | 200 | 论文 |
| `dblp.org` | 202（反自动化页） | 202（反自动化页） | 论文（不可用） |
| `api.openreview.net` | 200 | 200 | 论文 |
| `api.unpaywall.org` | 422* | 200 | 论文 |
| `api.semanticscholar.org` | 429 | 429 | 论文（可选） |
| `hn.algolia.com` | 200 | 200 | 网页（默认） |
| `api.stackexchange.com` | 200 | 200 | 网页（默认） |
| `api.tavily.com` | 401 | 401 | 网页（可选 Key） |
| `www.bing.com` | 302 | 302 | 不采用 |
| `www.mojeek.com` | 200/403 | 200 | 不采用 |
| `html.duckduckgo.com` | 000 | 000 | 不可用 |
| `api.duckduckgo.com` | 000 | 000 | 不可用 |
| `www.google.com` | 000 | — | 不可用 |
| `en.wikipedia.org` | 000 | — | 不可用 |
| `api.search.brave.com` | 000 | — | 不可用 |
| `r.jina.ai` | 000 | — | 不可用 |
| `searx.be` 等公共实例 | 000 | — | 不可用 |

\* 宿主机用了占位邮箱 `test@example.com`，Unpaywall 按预期返回 422；容器内用普通邮箱返回 200。

**结论**：容器内出网与宿主机一致，默认 provider 可直接在容器里工作。

## 5. 选型与路线取舍

### 默认方案（零 Key、单容器、不改依赖）

1. **论文检索**：OpenAlex + CrossRef + arXiv + Europe PMC + PubMed 五个来源，一源一解析函数 + 一个聚合工具；dblp / OpenReview / Unpaywall 作为补充工具或聚合可选项；Semantic Scholar 默认关闭。
2. **网页检索**：provider 注册表，默认启用 HN Algolia + StackExchange 两个实测可达的零 Key 来源；可选 provider 为「自建 SearXNG」与「Tavily（需 Key）」，未配置时返回明确提示而不是报错。
3. **正文抓取**：可选能力，默认关闭；只允许 HTTPS、固定域名白名单、拒绝私网/回环地址，返回内容明确标注为「不可信外部内容」。

### 三条网页搜索路线的取舍

| 路线 | 优点 | 代价 | 结论 |
|---|---|---|---|
| 自建 SearXNG | 最合规可控、零 Key、开源 | 需额外容器与运维；本机多数上游引擎不可达，只有 Bing 可用；JSON API 需手工开启 | **可选扩展**，不阻塞首版 |
| 免费额度 Key（Tavily 等） | 结果质量好、域名可达、接入简单 | 非开源、需注册、额度有限、条款可能变动 | **可选扩展**，用户自行申请 |
| 垂直开放 API | 零 Key、稳定、合法、可达 | 不是通用网页搜索，覆盖面窄 | **首版默认**，并明确告知 Agent 覆盖边界 |

### 待用户后续决定（不阻塞首版）

- 是否申请 OpenAlex / NCBI / Semantic Scholar / Tavily 免费 Key 以解锁深翻页与更高质量结果。
- 是否新增自建 SearXNG 容器并只启用可达上游。
- 是否为新工具补齐 REST 端点与 `_OPENAPI_TMPL` 接口清单。
- 是否允许抓取页面正文（涉及 SSRF 与提示注入风险）。

## 6. 安全与稳定性约束（实现必须遵守）

- **来源白名单**：只允许固定域名，拒绝 `file:`、非 HTTP(S)、私网/回环/链路本地地址，规避 SSRF（服务自身监听 `127.0.0.1`）。
- **统一网络封装**：一次实现超时、重试退避、响应体积上限、状态码处理、可读错误文本，所有工具共用。
- **限速与缓存**：arXiv 最小 3 秒间隔并缓存结果；OpenAlex 用 `select` / `per_page` 裁剪；聚合调用固定小常数个请求，禁止 N+1 逐条取详情。
- **审计**：只记录查询词、来源、耗时、状态与结果条数，不记录密钥与响应体；复用现有 `audit()`。
- **不可信内容**：外部标题/摘要/正文在返回文本中显式标注来源与「不可信外部内容」，缓解提示注入。
- **配置脱敏**：新增变量名若含密钥，命中现有 `_SECRET_KEYS`（含 `KEY`/`TOKEN`/`SECRET`…）；礼貌池邮箱等需补充脱敏规则或采用中性命名。
- **不改架构**：保留单容器、Bearer/OAuth 鉴权、命令三态安全模型、后台任务逻辑；不启动公网隧道、不推送镜像。

## 7. 实现落地记录（本轮）

- **零新增依赖**：网络、Atom、HTML 与并发全部用标准库（`urllib.request`、`xml.etree.ElementTree`、`html.parser`、`concurrent.futures`），`docker/requirements.txt` 与 `Dockerfile` 未改。
- **统一网络层** `_http_request`：固定域名白名单、私网/回环/链路本地拦截、跳转目标复检、超时、429/5xx 退避重试、响应体积上限、内存 GET 缓存；`_json_get` / `_xml_get` 共用。
- **本环境 DNS 特点**：`eutils.ncbi.nlm.nih.gov`、`api.github.com` 等合法域名被解析到 `198.18.x.x` 透明代理段。因此固定白名单来源豁免私网检查（主机名由代码拼接、跳转仍受白名单约束）；`read_web_page` 保留严格检查：仅 HTTPS、禁止 IP 字面量、禁止非标准端口、拦截 RFC1918 / 回环 / 链路本地 / IPv6 ULA，跳转目标同样校验。
- **论文检索**：8 个一源一解析函数 + 注册表，默认 5 源（OpenAlex / arXiv / CrossRef / PubMed / Europe PMC）；`ThreadPoolExecutor` 最多 4 并发，总耗时约等于最慢来源；结果按来源轮流交错（最多 30 条），单源失败只追加错误说明。
- **arXiv**：服务端 `submittedDate` 区间语法实测返回 500，改为按提交日期排序后客户端过滤年份；保留 3 秒最小请求间隔与缓存。
- **网页检索**：5 个 provider（`hn` / `stackexchange` / `github` / `searxng` / `tavily`），默认前两个零 Key；Tavily 用 POST 且不缓存（不落密钥），自建 SearXNG 由运维配置故允许私网地址；未配置可选 provider 时明确提示而非抛异常。
- **新增 5 个 MCP 工具**：`search_papers`、`search_arxiv`、`get_paper`、`search_web`、`read_web_page`（工具总数 21）。
- **配置链路**：`_SECRET_KEYS` 增加 `MAIL` 以脱敏礼貌池邮箱；`entrypoint.sh` 只透传/写入非空的可选变量，避免空字符串覆盖代码默认值；`.env.template` 去掉会被 `load_env()` 误读的行内注释；Compose 与中英文 README 同步。
- **未做（待用户决定）**：新工具的 REST 端点与 `_OPENAPI_TMPL` 接口清单、自建 SearXNG 容器、免费额度 Key 的申请与启用。

## 8. 验收清单

- [ ] 宿主机与容器内各来源可达性已记录（本文第 4 节）。
- [ ] `tools/list` 能看到全部新增工具且参数说明清晰。
- [ ] 每个论文来源至少一次真实查询返回非空结果；聚合工具在部分来源失败时仍返回其余结果并标注失败原因。
- [ ] 网页搜索在零 Key 默认配置下返回结果；可选 provider 未配置时返回明确提示而非异常。
- [ ] 私网地址、超大响应、超时、429/5xx 均有可读错误与审计记录。
- [ ] 环境变量经 `docker/.env.template` → `docker-compose.yml` → `entrypoint.sh` → `server.py` 全链路透传，且敏感值在 `get_environment_variables` 中被脱敏。
- [ ] `docker build` 成功；本地 `.venv` 下 `import server`、`pip check` 通过；中英文 README 与工具表一致。
