"""单页应用（SPA）静态文件与路由兜底服务模块。

负责托管 web/dist 构建产物，提供静态资源访问以及前端深链接路由兜底。
当前端产物尚未构建时，优雅返回 503 提示页面。
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response

from wb2api.staticfiles import NOT_BUILT_HTML, find_dist_dir

__all__ = ["router"]

router = APIRouter(tags=["spa"])

# index.html 响应头：禁止缓存，确保前端发版后即时生效
INDEX_HEADERS: dict[str, str] = {
    "Cache-Control": "no-cache, no-store, must-revalidate",
}

# Vite 构建带哈希的资源响应头：安全开启长效不可变缓存
ASSET_HEADERS: dict[str, str] = {
    "Cache-Control": "public, max-age=31536000, immutable",
}

# 明确排除的后端 API / 探针 / 文档前缀，避免被 SPA 兜底吞掉
API_PREFIXES: tuple[str, ...] = (
    "/api",
    "/v1",
    "/status",
    "/healthz",
    "/docs",
    "/redoc",
)


def _is_api_route(path: str) -> bool:
    """判断路径是否属于后端 API、探针或文档路由。"""
    normalized = "/" + path.strip("/")
    if normalized == "/openapi.json":
        return True
    for prefix in API_PREFIXES:
        if normalized == prefix or normalized.startswith(prefix + "/"):
            return True
    return False


def _safe_resolve_file(
    base_dir: Path,
    subpath: str,
    root_limit: Path | None = None,
) -> Path | None:
    """安全解析子路径并验证文件存在，防止路径穿越逃逸。

    :param base_dir: 基础定位目录
    :param subpath: 相对子路径
    :param root_limit: 边界限制目录（默认为 base_dir，必须保持在此目录树内）
    """
    try:
        clean = subpath.lstrip("/\\")
        limit = (root_limit or base_dir).resolve()
        target = (base_dir / clean).resolve()
        if target.is_relative_to(limit) and target.is_file():
            return target
    except (ValueError, OSError):
        return None
    return None


def _serve_index(dist: Path | None) -> Response:
    """返回 SPA index.html，若未构建则返回 503 提示页。"""
    if dist is None:
        return HTMLResponse(
            content=NOT_BUILT_HTML,
            status_code=503,
            headers=INDEX_HEADERS,
        )

    index_path = dist / "index.html"
    if not index_path.is_file():
        return HTMLResponse(
            content=NOT_BUILT_HTML,
            status_code=503,
            headers=INDEX_HEADERS,
        )

    return FileResponse(
        path=index_path,
        media_type="text/html",
        headers=INDEX_HEADERS,
    )


@router.get("/", summary="前端单页入口")
async def get_root() -> Response:
    """提供 SPA 首页 index.html。"""
    return _serve_index(find_dist_dir())


@router.get("/assets/{file_path:path}", summary="前端静态资源")
async def get_asset(file_path: str) -> Response:
    """提供 Vite 构建的静态资源（带内容哈希，长缓存）。"""
    dist = find_dist_dir()
    if dist is None:
        return HTMLResponse(
            content=NOT_BUILT_HTML,
            status_code=503,
            headers=INDEX_HEADERS,
        )

    asset_dir = dist / "assets"
    target = _safe_resolve_file(asset_dir, file_path, dist)
    if target is None:
        return JSONResponse(status_code=404, content={"detail": "Asset not found"})

    return FileResponse(
        path=target,
        headers=ASSET_HEADERS,
    )


@router.get("/{full_path:path}", summary="前端深链接兜底与静态文件")
async def get_spa_fallback(full_path: str) -> Response:
    """处理前端深链接与根目录静态文件，未命中 API 时回退至 index.html。"""
    if _is_api_route(full_path):
        return JSONResponse(status_code=404, content={"detail": "Not Found"})

    dist = find_dist_dir()
    if dist is not None:
        target = _safe_resolve_file(dist, full_path, dist)
        if target is not None:
            clean = full_path.lstrip("/\\")
            if clean.startswith("assets/"):
                return FileResponse(path=target, headers=ASSET_HEADERS)
            if target == (dist / "index.html").resolve():
                return _serve_index(dist)
            return FileResponse(path=target)

    return _serve_index(dist)
