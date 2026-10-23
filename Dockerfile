# syntax=docker/dockerfile:1

# ── 阶段 1：构建前端（React + Vite → web/dist）────────────────────────
FROM node:22-alpine AS web
WORKDIR /web
COPY web/package.json web/package-lock.json ./
# cache mount：复用 npm 下载缓存，源码改动时不必重新下载整个 node_modules。
RUN --mount=type=cache,target=/root/.npm \
    npm ci --no-audit --no-fund
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
# 钉死版本而不是 :latest：tag 一旦移动，`COPY --from` 这一层的缓存就会失效，
# 导致后面整个 `uv sync` 层被迫重建（这正是「每次都要构建」的隐形来源之一）。
# 升级方式：改版本号后本地 `uv lock` 重新生成 uv.lock，保证两者一致。
COPY --from=ghcr.io/astral-sh/uv:0.11.14 /uv /usr/local/bin/uv

WORKDIR /app
# 必须把 uv.lock 一起拷进来：没有锁文件时每次构建都要重新解析依赖，
# 且运行时 uv 还会认为环境「不确定」而再解析一次。
# --locked：锁文件与 pyproject.toml 不一致时直接构建失败（而不是悄悄改锁）。
# cache mount：复用下载缓存；--no-cache 会让每次重建都重新下载全部 wheel。
# UV_LINK_MODE=copy：缓存目录与目标不在同一文件系统时消除 hardlink 警告。
# UV_COMPILE_BYTECODE=1：安装时就把 .py 编译成 .pyc。启动阶段设置了
# PYTHONDONTWRITEBYTECODE=1（运行时不再写盘），所以这里预编译能明显降低冷启动耗时。
ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1
COPY pyproject.toml uv.lock README.md ./
COPY src/ ./src/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev

# 前端构建产物（由 SPA 路由托管）。
COPY --from=web /web/dist ./web/dist

# 运行数据目录（auths / data），以非 root 用户运行。
# 配置模板也放在 chown 之前：否则 config.json 属 root，容器内（uid 10001）保存配置会失败。
COPY config.example.json ./config.json

RUN useradd -m -u 10001 app \
 && mkdir -p /app/auths /app/data \
 && chown -R app:app /app
USER app

EXPOSE 7863
ENV WB2API_CONFIG=/app/config.json

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD /app/.venv/bin/python -c "import urllib.request,sys; r=urllib.request.urlopen('http://127.0.0.1:7863/healthz', timeout=4); sys.exit(0 if r.status in (200,503) else 1)"

# ── 关键：启动不要再走 `uv run` ─────────────────────────────────────
# `uv run` 在项目里会「先同步环境、再执行命令」。镜像构建时用的是
# `uv sync --no-dev`，而 `uv run` 默认会带上 dev 依赖组（uv 把 dev 组
# 当作默认组），于是每创建一个新容器都会：
#   Building wb2api @ file:///app → Downloading pygments (1.2MiB) → Installed 7 packages
# 也就是每次 `docker compose up` 都白做一遍依赖安装。
# 这里直接执行 venv 里的 python（PATH 已包含 /app/.venv/bin），启动零同步。
# 同时把 UV_NO_DEV 固定为 1，保证即便在容器里手动 `uv run`，也不会又装回 dev 依赖。
ENV UV_NO_DEV=1

# 用绝对路径，避免 PATH 被覆盖时跑到系统 python（系统 python 没有 fastapi）。
CMD ["/app/.venv/bin/python", "-m", "uvicorn", "wb2api.main:app", "--host", "0.0.0.0", "--port", "7863"]
