FROM python:3.12-slim

WORKDIR /app

RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates procps git \
 && rm -rf /var/lib/apt/lists/*

# Git 删除硬防护：把真实 git 藏到 libexec 目录，/usr/bin/git 换成防护 wrapper。
# 显式写 /usr/bin/git、脚本内调用、Python subprocess 调用都会经过 check_argv。
RUN GIT_REAL="$(readlink -f /usr/bin/git)" \
 && mv "$GIT_REAL" /usr/lib/git-core/git.real \
 && rm -f /usr/bin/git

# cloudflared（固定版本；设置 CLOUDFLARED_SHA256 可校验下载完整性）
ARG CLOUDFLARED_VERSION=2026.10.7
ARG CLOUDFLARED_SHA256=""
RUN curl -sSL -o /usr/local/bin/cloudflared \
      "https://github.com/cloudflare/cloudflared/releases/download/${CLOUDFLARED_VERSION}/cloudflared-linux-amd64" \
 && if [ -n "$CLOUDFLARED_SHA256" ]; then \
      echo "$CLOUDFLARED_SHA256  /usr/local/bin/cloudflared" | sha256sum -c -; \
    fi \
 && chmod +x /usr/local/bin/cloudflared

# 构建上下文为仓库根目录：docker build -f docker/Dockerfile -t <image> .
COPY docker/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY docker/server.py docker/entrypoint.sh ./
COPY gateway/ ./gateway/
COPY docker/git-guard-wrapper.py /usr/bin/git
RUN chmod 755 /usr/bin/git /app/entrypoint.sh

# 8765 = MCP API（仅容器网络 / 隧道可达），8788 = Admin Web（默认仅绑定宿主 127.0.0.1）
EXPOSE 8765 8788
CMD ["/app/entrypoint.sh"]
