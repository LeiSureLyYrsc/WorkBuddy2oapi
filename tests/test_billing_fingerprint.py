"""回归测试：billing/growth 请求修复（10085 指纹拦截 / 国际版短路 / 404-only 回退）。

背景：Python 移植版 ``billing_headers`` 未设置 User-Agent，httpx 发送
``python-httpx/...`` 被上游网关以业务 code 10085「请求不合法」拦截（客户端指纹）。
参考实现（workbuddy-switch）明确：401/403 是鉴权、10085 是网关指纹拦截、
不能被当成路径问题而回退。
"""

from __future__ import annotations

import httpx
import pytest

from wb2api.headers import (
    CLIENT_UA,
    WEB_UA,
    billing_headers,
    chat_headers,
)
from wb2api.models import Account
from wb2api.upstream import (
    BILLING_PREFIX_CN,
    BILLING_PREFIX_WEB,
    ErrKind,
    UpstreamClient,
    UpstreamError,
    billing_path_candidates,
    classify,
    is_fingerprint_rejected,
    supports_checkin,
)

CN = Account(
    access_token="tok-cn",
    refresh_token="r",
    uid="uid-cn",
    domain="copilot.tencent.com",
)
GLOBAL = Account(
    access_token="tok-global",
    refresh_token="r",
    uid="uid-global",
    domain="www.workbuddy.ai",
)


# ---------------------------------------------------------------------------
# 1. 请求指纹（核心修复）
# ---------------------------------------------------------------------------


def test_billing_headers_carry_web_fingerprint() -> None:
    h = billing_headers(CN)
    # 必须显式带 UA，否则 httpx 会发 python-httpx/... 被拦截。
    assert h["User-Agent"] == WEB_UA
    assert h["User-Agent"] != CLIENT_UA
    assert h["X-Client-Platform"] == "web"
    assert h["Origin"] == "https://www.codebuddy.cn"
    assert h["Referer"].endswith("/profile/plans-usage")
    assert h["Authorization"] == "Bearer tok-cn"


def test_billing_headers_origin_follows_region() -> None:
    assert billing_headers(GLOBAL)["Origin"] == "https://www.workbuddy.ai"
    assert billing_headers(GLOBAL)["Referer"].startswith("https://www.workbuddy.ai/")


def test_chat_headers_keep_cli_fingerprint() -> None:
    # 聊天接口不受影响：仍用 CLI UA，且带 X-Product。
    h = chat_headers(CN)
    assert h["User-Agent"] == CLIENT_UA
    assert h["X-Product"] == "SaaS"
    assert "X-Refresh-Token" not in h


# ---------------------------------------------------------------------------
# 2. 10085 分类与识别
# ---------------------------------------------------------------------------


def test_classify_fingerprint() -> None:
    body = '{"code":10085,"msg":"请求不合法，如有疑问请联系客服","requestId":"x"}'
    assert classify(403, body) == ErrKind.fingerprint
    assert is_fingerprint_rejected(body) is True
    assert is_fingerprint_rejected('{"code":0}') is False


def test_classify_priority_untouched() -> None:
    # 既有优先级不受影响。
    assert classify(402, "x") == ErrKind.hard_credit
    assert classify(401, "Offline user session not found") == ErrKind.session_dead
    assert classify(404, "not found") == ErrKind.not_found
    assert classify(500, "boom") == ErrKind.server


# ---------------------------------------------------------------------------
# 3. 区域感知路径候选
# ---------------------------------------------------------------------------


def test_billing_path_candidates() -> None:
    assert billing_path_candidates(CN, "/get-user-resource") == [
        f"{BILLING_PREFIX_CN}/get-user-resource"
    ]
    assert billing_path_candidates(GLOBAL, "/get-user-resource") == [
        f"{BILLING_PREFIX_WEB}/get-user-resource",
        f"{BILLING_PREFIX_CN}/get-user-resource",
    ]


# ---------------------------------------------------------------------------
# 4. supports_checkin（国际版短路）
# ---------------------------------------------------------------------------


def test_supports_checkin() -> None:
    assert supports_checkin(CN) is True
    assert supports_checkin(GLOBAL) is False


async def test_daily_checkin_skips_global_without_request() -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, json={"code": 0, "data": {}})

    up = UpstreamClient(transport=httpx.MockTransport(handler))
    res = await up.daily_checkin(GLOBAL)
    assert res.unsupported is True
    assert calls == []  # 未发起任何请求
    await up.aclose()


async def test_travel_once_skips_global_without_request() -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, json={"code": 0, "data": {}})

    up = UpstreamClient(transport=httpx.MockTransport(handler))
    res = await up.travel_once(GLOBAL)
    assert res.action == "skip"
    assert calls == []  # 未发起任何请求
    await up.aclose()


# ---------------------------------------------------------------------------
# 5. 404-only 路径回退（回退不得掩盖 10085 / 401）
# ---------------------------------------------------------------------------


async def test_billing_falls_back_only_on_404() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.url.path.startswith(BILLING_PREFIX_WEB):
            return httpx.Response(404, json={"code": 404, "msg": "not found"})
        return httpx.Response(200, json={"code": 0, "data": {"Response": {"Data": {"Accounts": []}}}})

    up = UpstreamClient(transport=httpx.MockTransport(handler))
    credits = await up.user_resource(GLOBAL)
    assert credits.packages == 0
    # 先网页路径（404）→ 回退到 /v2 路径成功。
    assert seen[0].startswith(BILLING_PREFIX_WEB)
    assert seen[1].startswith(BILLING_PREFIX_CN)
    await up.aclose()


async def test_billing_does_not_fall_back_on_fingerprint() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return httpx.Response(
            403,
            json={"code": 10085, "msg": "请求不合法，如有疑问请联系客服", "requestId": "x"},
        )

    up = UpstreamClient(transport=httpx.MockTransport(handler))
    with pytest.raises(UpstreamError) as ei:
        await up.user_resource(GLOBAL)
    assert ei.value.kind == ErrKind.fingerprint
    # 10085 不得触发路径回退：只请求了一次。
    assert len(seen) == 1
    await up.aclose()


async def test_billing_does_not_fall_back_on_unauthorized() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return httpx.Response(401, json={"code": 12153, "msg": "offline user session not found"})

    up = UpstreamClient(transport=httpx.MockTransport(handler))
    with pytest.raises(UpstreamError) as ei:
        await up.user_resource(GLOBAL)
    assert ei.value.kind == ErrKind.session_dead
    assert len(seen) == 1
    await up.aclose()


# ---------------------------------------------------------------------------
# 6. 请求头确实随 billing 请求发出（端到端断言）
# ---------------------------------------------------------------------------


async def test_billing_request_carries_user_agent_header() -> None:
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update({k.lower(): v for k, v in request.headers.items()})
        return httpx.Response(200, json={"code": 0, "data": {"Response": {"Data": {"Accounts": []}}}})

    up = UpstreamClient(transport=httpx.MockTransport(handler))
    await up.user_resource(CN)
    assert captured.get("user-agent") == WEB_UA
    assert captured.get("x-client-platform") == "web"
    assert "origin" in captured and "referer" in captured
    await up.aclose()
