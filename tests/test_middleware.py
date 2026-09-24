"""回归测试：JSON 请求体但 Content-Type 非 application/json 时不应 422。

背景：浏览器 fetch 对字符串 body 默认带 ``text/plain``；前端若漏设 JSON 头，
FastAPI 会因 body 无法解析为对象而返回 422。middleware.NormalizeJSONContentType
负责把形似 JSON 的请求体 Content-Type 归一为 application/json。
"""

from __future__ import annotations

import json

import httpx
import pytest


@pytest.fixture()
def app(tmp_path):
    from wb2api.main import create_app

    cfg = tmp_path / "config.json"
    cfg.write_text(
        json.dumps(
            {
                "listen": ":0",
                "api_key": "k",
                "auth_dir": str(tmp_path / "auths"),
                "state_file": str(tmp_path / "state.json"),
                "pricing_file": str(tmp_path / "pricing.json"),
                "console": {
                    "username": "admin",
                    "password": "workbuddy",
                    "credentials_file": str(tmp_path / "credentials.json"),
                },
                "schedule": {"checkin_enabled": False, "keepalive_enabled": False},
            }
        ),
        encoding="utf-8",
    )
    return create_app(str(cfg))


@pytest.mark.parametrize(
    "content_type",
    [
        "application/json",
        "text/plain;charset=UTF-8",
        "application/x-www-form-urlencoded",
        None,
    ],
)
async def test_login_accepts_various_content_types(app, content_type: str | None) -> None:
    body = json.dumps({"username": "admin", "password": "workbuddy"}).encode()
    headers = {"Content-Type": content_type} if content_type else {}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t"
    ) as c:
        r = await c.post("/api/login", content=body, headers=headers)
    assert r.status_code == 200, f"content-type={content_type!r} -> {r.status_code}: {r.text[:200]}"
    assert r.json()["username"] == "admin"


async def test_non_json_body_not_rewritten(app) -> None:
    """非 JSON 形状的 body 不应被改写 Content-Type（保持原样交给路由处理）。"""
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t"
    ) as c:
        r = await c.post("/api/login", content=b"username=admin&password=workbuddy")
    # 不是 JSON 对象 → 仍应 422（我们只修正"确为 JSON"的 body）。
    assert r.status_code == 422
