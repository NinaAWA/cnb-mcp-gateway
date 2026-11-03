> 📖 中文 | [English](./README.md)

# CNB MCP Gateway（中文文档）

一个**自托管的 MCP（模型上下文协议）服务器**：把你自己机器/服务器上的能力，通过一个 Cloudflare 快速隧道，暴露给**任意** MCP 客户端（Claude Desktop、Cursor、VS Code、Codex、ChatGPT 连接器，或你自己写的 agent），让 AI 能像操作「远程桌面」一样在你的服务器上**跑实验、配环境、看数据、看图片**。

```
 你的服务器  ──►  run_command / 文件 / 进程 / 图片 / 后台任务   ◄──  任意 MCP 客户端
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
- **安全模型**：默认开放（无白名单）+ 对破坏性命令的**确认门** + 一份**毁灭级黑名单**（`rm -rf /`、`mkfs`、`shutdown` 等）即便确认也无法绕过。每次操作都有完整审计日志。
- **可调旋钮（Sandbox knobs）**：可选的 `MCP_ALLOWED_PREFIXES` 白名单、`MCP_DENY_PREFIXES`、`MCP_REQUIRE_CONFIRM`、工作目录、审计日志。
- **零配置入口**：cloudflared 快速隧道自动给你一个公网 HTTPS 地址（每次重启地址会变；设置 `CF_TUNNEL_TOKEN` 可固定地址）。
- **自包含镜像**：服务器 + 隧道在同一个容器里。

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

启动横幅会打印你需要的一切：

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
- 容器只监听 `127.0.0.1`；由 Cloudflare 终结 TLS 并暴露公网端点。不会发布任何宿主机端口。
- 所有被执行过的命令都会写入容器内的 `/app/audit.log`。
- ⚠️ **密钥提醒**：如果你用的是共享/明文出现的 PAT 或令牌，建议用完后立即在对应平台撤销并轮换。

## 从源码构建

```bash
git clone https://github.com/NinaAWA/cnb-mcp-gateway.git
cd cnb-mcp-gateway
docker build -t cnb-mcp-gateway:local ./docker
docker run -d --name cnb-mcp-gateway --restart unless-stopped \
  -v "$PWD:/workspace" cnb-mcp-gateway:local
docker logs -f cnb-mcp-gateway
```

## 许可证

[MIT](./LICENSE)
