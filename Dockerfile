# syntax=docker/dockerfile:1

# ── 阶段 1：构建前端（React + Vite → web/dist）────────────────────────
FROM node:22-alpine AS web
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY web/ ./
RUN npm run build

# ── 阶段 2：Python 运行时 ────────────────────────────────────────────
FROM python:3.12-slim AS runtime

# 时区 + 证书（上游 HTTPS 必需）。
ENV TZ=Asia/Shanghai \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    PATH="/app/.venv/bin:$PATH"

RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates tzdata \
 && rm -rf /var/lib/apt/lists/*

# uv 包管理器（官方静态二进制）。
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src/ ./src/
RUN uv sync --no-dev --no-cache

# 前端构建产物（由 SPA 路由托管）。
COPY --from=web /web/dist ./web/dist

# 运行数据目录（auths / data），以非 root 用户运行。
RUN useradd -m -u 10001 app \
 && mkdir -p /app/auths /app/data \
 && chown -R app:app /app
USER app

COPY config.example.json ./config.json

EXPOSE 7863
ENV WB2API_CONFIG=/app/config.json

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request,sys; \
r=urllib.request.urlopen('http://127.0.0.1:7863/healthz', timeout=4); \
sys.exit(0 if r.status in (200,503) else 1)" || exit 1

CMD ["uv", "run", "uvicorn", "wb2api.main:app", "--host", "0.0.0.0", "--port", "7863"]
