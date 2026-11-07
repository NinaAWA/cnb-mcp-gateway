FROM python:3.12-slim

WORKDIR /app

RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates procps git \
 && rm -rf /var/lib/apt/lists/*

# cloudflared（固定版本，保证构建可复现）
ARG CLOUDFLARED_VERSION=2026.10.7
RUN curl -sSL -o /usr/local/bin/cloudflared \
      "https://github.com/cloudflare/cloudflared/releases/download/${CLOUDFLARED_VERSION}/cloudflared-linux-amd64" \
 && chmod +x /usr/local/bin/cloudflared

# 构建上下文为仓库根目录：docker build -f docker/Dockerfile -t <image> .
COPY docker/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY docker/server.py docker/entrypoint.sh ./
COPY gateway/ ./gateway/
RUN chmod +x entrypoint.sh

# 8765 = MCP API（仅容器网络 / 隧道可达），8788 = Admin Web（默认仅绑定宿主 127.0.0.1）
EXPOSE 8765 8788
CMD ["/app/entrypoint.sh"]
