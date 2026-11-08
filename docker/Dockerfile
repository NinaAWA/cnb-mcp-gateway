# Cloudflare publishes the same release as a signed registry image; copying its
# binary avoids treating a transient GitHub 504 HTML response as an executable.
# Keep the version pinned for reproducible builds.
FROM cloudflare/cloudflared:2026.10.7 AS cloudflared-bin

FROM python:3.12-slim

WORKDIR /app

RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates procps git git-lfs \
 && rm -rf /var/lib/apt/lists/*

# Git 删除硬防护：把真实 git 藏到 libexec 目录，/usr/bin/git 换成防护 wrapper。
# 显式写 /usr/bin/git、脚本内调用、Python subprocess 调用都会经过 check_argv。
RUN GIT_REAL="$(readlink -f /usr/bin/git)" \
 && mv "$GIT_REAL" /usr/lib/git-core/git.real \
 && rm -f /usr/bin/git

# Import a real ELF binary from Cloudflare's published image. Docker image pulls
# fail on bad HTTP responses instead of silently storing a 504 HTML file.
COPY --from=cloudflared-bin /usr/local/bin/cloudflared /usr/local/bin/cloudflared
RUN test "$(head -c 4 /usr/local/bin/cloudflared | od -An -tx1 | tr -d ' \n')" = "7f454c46" \
 && /usr/local/bin/cloudflared --version

# 构建上下文为仓库根目录：docker build -f docker/Dockerfile -t <image> .
COPY docker/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY docker/server.py docker/entrypoint.sh ./
COPY gateway/ ./gateway/
COPY docker/git-guard-wrapper.py /usr/bin/git
RUN chmod 755 /usr/bin/git /app/entrypoint.sh

# 仓库的 pre-push 钩子会调用 `git lfs pre-push`。镜像内置 git-lfs 并做系统级初始化，
# 运行时无需 agent 执行安装命令，也不必放宽 MCP_ALLOWED_PREFIXES 白名单。
# 必须放在 wrapper 与 gateway/ 复制完成之后：此前 /usr/bin/git 已被移走，
# 且 wrapper 需要 import /app/gateway/git_guard.py。
# `git lfs install` 已由 git_guard.LFS_SAFE_SUBCOMMANDS 放行（只装钩子、不删钩子）；
# 构建期仍用二进制全路径，仅为避免此刻依赖 wrapper 的 Python 导入链。
# --system 只写 /etc/gitconfig，不会删除任何仓库 Hook。
RUN /usr/bin/git-lfs install --system \
 && /usr/bin/git-lfs version

# v1.1 持久目录：二进制文件与长任务日志。Compose 用命名卷覆盖，
# 直接 docker run 时也应把这两个目录挂到宿主机，重建容器才不会丢数据。
RUN mkdir -p /data/audit/task-logs /data/files
ENV MCP_FILE_ROOT=/data/files \
    MCP_TASK_LOG_DIR=/data/audit/task-logs

# 管理后台不内置硬编码口令：首次启动由 entrypoint.sh 随机生成并写入
# /data/audit/.admin_password（umask 077、持久卷），显式 ADMIN_PASSWORD 优先。

# 生产依赖只来自 docker/requirements.txt；不把任何科研项目依赖硬编码进镜像。
# 8765 = MCP API（仅容器网络 / 隧道可达），8788 = Admin Web（默认仅绑定宿主 127.0.0.1）
EXPOSE 8765 8788
CMD ["/app/entrypoint.sh"]
