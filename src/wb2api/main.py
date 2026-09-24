"""wb2api 服务入口：单端口同时提供 OpenAI 兼容 API + Web 控制台 + OAuth 登录。

启动：
    uv run wb2api            # 或
    uv run uvicorn wb2api.main:app --host 0.0.0.0 --port 7863

设计要点：账号与配置在**进程内热加载**，新增账号 / 改配置均无需重启。
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI

from . import __version__
from .app_state import build_state
from .deps import SessionStore
from .middleware import NormalizeJSONContentType
from .routers import admin_api, openai_api, spa

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("wb2api.main")


def create_app(config_path: str = "config.json") -> FastAPI:
    """构建 FastAPI 应用（依赖 app.state.wb 共享状态）。"""
    state = build_state(config_path, __version__)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.wb = state
        app.state.sessions = SessionStore(ttl=state.cfg.console.session_ttl_seconds)
        logger.info("wb2api %s 启动", state.version)
        logger.info("  单端口监听: %s", state.cfg.listen)
        logger.info("  控制台账号: %s", state.cfg.console.username)
        logger.info("  凭证目录:   %s", state.store.dir)
        logger.info("  配置文件:   %s", config_path)
        total, healthy, cooling, disabled, _ = state.pool.counts_detailed()
        logger.info("  账号池:     总 %d / 可用 %d / 冷却 %d / 禁用 %d", total, healthy, cooling, disabled)
        if state.cfg.using_default_password():
            logger.warning("  正在使用默认口令（admin/workbuddy），请尽快修改 console.password")

        # 后台调度器（签到/保活/旅行）。
        sched_task = asyncio.create_task(state.scheduler.run())
        try:
            yield
        finally:
            sched_task.cancel()
            try:
                await sched_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            state.sticky.stop_gc()
            state.pool.flush()
            state.metrics.flush()
            state.pool.stop_flusher()
            await state.upstream.aclose()
            logger.info("wb2api 已退出")

    app = FastAPI(
        title="WorkBuddy2API",
        version=__version__,
        description="CodeBuddy 账号 → OpenAI 兼容 API 网关 + Web 控制台（单端口 · 热加载）",
        lifespan=lifespan,
    )
    app.state.wb = state

    # 容忍「JSON 请求体但 Content-Type 非 application/json」的客户端（浏览器 fetch 默认 text/plain）。
    app.add_middleware(NormalizeJSONContentType)

    # 路由挂载顺序：API 在前，SPA 兜底最后。
    app.include_router(openai_api.router)
    app.include_router(admin_api.router)
    app.include_router(spa.router)
    return app


def _load_env_file(path: str = ".env") -> None:
    """极简 .env 加载（KEY=VALUE，已存在的环境变量不覆盖）。"""
    if not os.path.exists(path):
        return
    try:
        for line in open(path, encoding="utf-8"):
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            k, v = k.strip(), v.strip().strip('"').strip("'")
            if k and k not in os.environ:
                os.environ[k] = v
    except OSError:
        pass


def main() -> None:
    """命令行入口。

    用法：``uv run wb2api``（配置默认读 ./config.json，可用环境变量 ``WB2API_CONFIG`` 覆盖）。
    ``-c/--config`` 为可选参数，项目自身已有合理默认，无需每次指定。
    """
    _load_env_file()
    parser = argparse.ArgumentParser(description="WorkBuddy2API 统一服务")
    parser.add_argument(
        "-c",
        "--config",
        default=os.environ.get("WB2API_CONFIG", "config.json"),
        help="配置文件路径（默认 config.json，可用 WB2API_CONFIG 覆盖）",
    )
    parser.add_argument("--host", default="", help="监听地址（默认取自配置 listen）")
    parser.add_argument("--port", type=int, default=0, help="监听端口（默认取自配置 listen）")
    parser.add_argument("--version", action="store_true", help="打印版本后退出")
    args = parser.parse_args()

    if args.version:
        print(f"wb2api {__version__}")
        return

    app = create_app(args.config)
    host, port = app.state.wb.cfg.host_port
    if args.host:
        host = args.host
    if args.port:
        port = args.port

    import uvicorn

    uvicorn.run(app, host=host, port=port, log_level="info")


# 模块级 ASGI 应用：供 `uvicorn wb2api.main:app` 加载。
# 用惰性 __getattr__ 暴露，避免 import 时即创建状态（否则 `main()` 会重复建池）。
_app: FastAPI | None = None


def __getattr__(name: str) -> object:
    global _app
    if name == "app":
        if _app is None:
            _app = create_app(os.environ.get("WB2API_CONFIG", "config.json"))
        return _app
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


if __name__ == "__main__":
    main()
