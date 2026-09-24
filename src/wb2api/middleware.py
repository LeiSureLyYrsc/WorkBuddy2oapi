"""ASGI 中间件：容忍「JSON 请求体但 Content-Type 不是 application/json」的客户端。

背景：浏览器 ``fetch`` 对字符串 body 默认发送 ``Content-Type: text/plain;charset=UTF-8``
（前端若未显式设置 JSON 头就会如此）；部分 HTTP 客户端/工具默认 ``application/x-www-form-urlencoded``
或干脆不带 Content-Type。FastAPI 只在 ``application/json``（或 ``+json``）下解析请求体，
其余情况把原始字节交给 Pydantic，导致 ``422 Unprocessable Entity``。

本中间件在路由之前嗅探：若方法带 body、Content-Type 非 JSON，但 body 首字符是 ``{`` / ``[``
（即确为 JSON），则把 Content-Type 改写为 ``application/json`` 并重放 body，使任何客户端
（浏览器 / curl / 脚本）都能正常调用。Content-Type 已是 JSON 或 body 非 JSON 形状时行为不变。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any

_JSON_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
_JSON_PREFIXES = (b"{", b"[")


class NormalizeJSONContentType:
    """把「形似 JSON 的请求体」的 Content-Type 归一为 application/json。"""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(
        self,
        scope: MutableMapping[str, Any],
        receive: Callable[[], Awaitable[dict[str, Any]]],
        send: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> None:
        if scope.get("type") != "http" or scope.get("method") not in _JSON_METHODS:
            await self.app(scope, receive, send)
            return

        headers = scope.get("headers") or []
        content_type = ""
        for key, value in headers:
            if key.lower() == b"content-type":
                content_type = value.decode("latin-1").lower()
                break

        # 已是 JSON（application/json / application/*+json）：直通，不缓冲 body。
        if "application/json" in content_type or content_type.endswith("+json"):
            await self.app(scope, receive, send)
            return

        # multipart/form-data 是二进制/多段结构，绝不可能以 '{'/'[' 开头，直通不缓冲。
        if content_type.startswith("multipart/form-data"):
            await self.app(scope, receive, send)
            return

        body = b""
        while True:
            message = await receive()
            if message.get("type") == "http.disconnect":
                await self.app(scope, receive, send)
                return
            body += message.get("body", b"") or b""
            if not message.get("more_body", False):
                break

        # 非 JSON 形状：原样重放，不做改写。
        if not body.lstrip().startswith(_JSON_PREFIXES):
            await self.app(scope, self._replayer(body, receive), send)
            return

        new_scope = dict(scope)
        new_headers = [(k, v) for (k, v) in headers if k.lower() != b"content-type"]
        new_headers.append((b"content-type", b"application/json"))
        new_scope["headers"] = new_headers
        await self.app(new_scope, self._replayer(body, receive), send)

    @staticmethod
    def _replayer(
        body: bytes,
        original: Callable[[], Awaitable[dict[str, Any]]],
    ) -> Callable[[], Awaitable[dict[str, Any]]]:
        """返回一个把已读 body 重放一次、之后委托原始 receive 的接收函数。"""
        sent = False

        async def replay() -> dict[str, Any]:
            nonlocal sent
            if not sent:
                sent = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await original()

        return replay
