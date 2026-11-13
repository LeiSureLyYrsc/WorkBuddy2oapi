"""上游请求头构造（CN / GLOBAL 双区域）。

登录 / 刷新 / 聊天对齐本机 CodeBuddy CLI 2.184（账号作用域外链登录）。
billing / growth 仍用网页控制台指纹 —— 否则 httpx 会发送 ``python-httpx/...``，
被上游网关判定为「请求不合法」（业务 code ``10085``，即客户端指纹拦截）。

三类请求头：
* ``common_headers``   —— OAuth / refresh（CLI 指纹）
* ``chat_headers``     —— 聊天接口（CLI 指纹 + 账号头；**绝不携带 X-Refresh-Token**）
* ``billing_headers``  —— billing / growth 接口（**网页控制台指纹**）
"""

from __future__ import annotations

import uuid

from .models import Account

# CodeBuddy CLI 2.184 客户端指纹（OAuth、refresh、chat 用）。
# 产品名 / 版本与 ``@tencent-ai/codebuddy-code`` 的 CliClientInfoProvider 一致。
CLI_VERSION = "2.184.0"
CLI_PRODUCT = "CodeBuddy"
CLIENT_UA = f"{CLI_PRODUCT}/{CLI_VERSION}"

# 网页控制台指纹（billing / growth 用）。
# 桌面端与官网套餐页均为浏览器客户端，必须带真实 UA，否则被网关指纹拦截（10085）。
WEB_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36"
)

ORIGIN_CN = "https://www.codebuddy.cn"
ORIGIN_GLOBAL = "https://www.workbuddy.ai"


def origin_for(account: Account | None) -> str:
    """按账号区域返回 Origin/Referer 基址（Global 必须带 workbuddy.ai）。"""
    if account is not None and account.is_global():
        return ORIGIN_GLOBAL
    return ORIGIN_CN


def common_headers(account: Account | None) -> dict[str, str]:
    origin = origin_for(account)
    return {
        "Content-Type": "application/json",
        "Accept": "application/json, text/plain, */*",
        "X-Requested-With": "XMLHttpRequest",
        "Origin": origin,
        "Referer": origin + "/",
        "User-Agent": CLIENT_UA,
    }


def anonymous_headers(account: Account | None) -> dict[str, str]:
    """未登录请求（申请 state / 轮询 token）的占位头。

    CLI 2.184 在这些请求上显式声明没有账号上下文，缺 ``X-No-Tenant-Id``
    会被网关当成未知客户端。
    """
    h = common_headers(account)
    h["X-No-Authorization"] = "true"
    h["X-No-User-Id"] = "true"
    h["X-No-Enterprise-Id"] = "true"
    h["X-No-Department-Info"] = "true"
    h["X-No-Tenant-Id"] = "true"
    return h


def chat_headers(account: Account, *, conversation_id: str = "") -> dict[str, str]:
    """chat 专属账号头。安全红线：绝不携带 X-Refresh-Token。"""
    h = common_headers(account)
    if account.access_token:
        h["Authorization"] = "Bearer " + account.access_token
    else:
        h["X-No-Authorization"] = "true"
    if account.uid:
        h["X-User-Id"] = account.uid
    else:
        h["X-No-User-Id"] = "true"
    if account.enterprise_id:
        h["X-Enterprise-Id"] = account.enterprise_id
    else:
        h["X-No-Enterprise-Id"] = "true"
    if account.domain:
        h["X-Domain"] = account.domain
    else:
        h["X-No-Department-Info"] = "true"
    h["X-Product"] = "SaaS"
    h["X-IDE-Type"] = "CLI"
    h["X-IDE-Name"] = CLI_PRODUCT
    h["X-IDE-Version"] = CLI_VERSION
    h["X-Agent-Intent"] = "craft"
    message_id = uuid.uuid4().hex
    h["X-Conversation-ID"] = conversation_id or message_id
    h["X-Conversation-Request-ID"] = message_id
    h["X-Conversation-Message-ID"] = message_id
    h["X-Request-ID"] = message_id
    return h


def billing_headers(account: Account) -> dict[str, str]:
    """billing / growth 域请求头（网页控制台指纹）。

    关键：必须显式设置 ``User-Agent``。缺失时 httpx 会发送 ``python-httpx/...``，
    上游网关会以 ``10085 请求不合法`` 拒绝（客户端指纹拦截）。
    """
    origin = origin_for(account)
    h = {
        "Authorization": "Bearer " + account.access_token,
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
        "User-Agent": WEB_UA,
        # 网页用户中心（Axios 拦截器）固定携带的客户端标识；桌面端调同一组
        # billing 接口时也保持一致，避免网关把请求当成未知客户端。
        "X-Client-Platform": "web",
        "Origin": origin,
        "Referer": origin + "/profile/plans-usage",
    }
    if account.uid:
        h["X-User-Id"] = account.uid
    if account.enterprise_id:
        h["X-Enterprise-Id"] = account.enterprise_id
        h["X-Tenant-Id"] = account.enterprise_id
    if account.domain:
        h["X-Domain"] = account.domain
    return h


def refresh_headers(account: Account) -> dict[str, str]:
    """refresh 端点专属头（X-Refresh-Token 只允许出现在这里）。"""
    h = common_headers(account)
    h["X-Refresh-Token"] = account.refresh_token
    if account.enterprise_id:
        h["X-Enterprise-Id"] = account.enterprise_id
    h["X-Auth-Refresh-Source"] = "plugin"
    return h
