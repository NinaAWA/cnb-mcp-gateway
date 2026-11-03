FROM python:3.12-slim

WORKDIR /app

RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates procps \
 && rm -rf /var/lib/apt/lists/*

# cloudflared（固定版本，保证构建可复现）
ARG CLOUDFLARED_VERSION=2026.10.7
RUN curl -sSL -o /usr/local/bin/cloudflared \
      "https://github.com/cloudflare/cloudflared/releases/download/${CLOUDFLARED_VERSION}/cloudflared-linux-amd64" \
 && chmod +x /usr/local/bin/cloudflared

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY server.py entrypoint.sh ./
RUN chmod +x entrypoint.sh

EXPOSE 8765
CMD ["/app/entrypoint.sh"]
