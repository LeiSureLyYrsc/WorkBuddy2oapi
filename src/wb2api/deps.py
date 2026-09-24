"""鉴权依赖：网关 API Key 校验 + 控制台会话（HttpOnly Cookie / Bearer）+ CSRF。

单端口下两套鉴权并存：
* ``/v1/*``、``/status``、``/v1/quota`` 等网关端点 → ``require_api_key``（api_key 为空则放行）
* ``/api/*`` 控制台端点 → ``require_session``（强制登录）
* ``/healthz`` → 恒无鉴权
"""

from __future__ import annotations

import hmac
import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request, status

from .app_state import AppState
from .config import ConsoleConfig

SESSION_COOKIE = "wb2api_session"
MAX_LOGIN_FAILURES = 10
LOGIN_LOCK_WINDOW = 600.0  # 10 分钟


def get_state(request: Request) -> AppState:
    """从 app.state 取得共享状态。"""
    return request.app.state.wb  # type: ignore[no-any-return]


StateDep = Annotated[AppState, Depends(get_state)]


# ---------------------------------------------------------------------------
# 控制台会话表
# ---------------------------------------------------------------------------


@dataclass
class _Session:
    token: str
    username: str
    expires_at: float


@dataclass
class _Attempt:
    failures: int = 0
    first_at: float = 0.0


class SessionStore:
    """内存会话表 + 登录防爆破。线程安全。"""

    def __init__(self, ttl: float = 12 * 3600) -> None:
        self.ttl = float(ttl if ttl > 0 else 12 * 3600)
        self._sessions: dict[str, _Session] = {}
        self._attempts: dict[str, _Attempt] = {}
        self._lock = threading.RLock()

    def create(
        self, username: str, password: str, console: ConsoleConfig, source: str
    ) -> tuple[str | None, str]:
        """校验口令并创建会话；返回 (token, 错误信息)。"""
        now = time.time()
        with self._lock:
            att = self._attempts.get(source)
            if att and att.failures >= MAX_LOGIN_FAILURES and now - att.first_at < LOGIN_LOCK_WINDOW:
                remain = int(LOGIN_LOCK_WINDOW - (now - att.first_at))
                return None, f"失败次数过多，请 {remain} 秒后再试"
            if att and now - att.first_at >= LOGIN_LOCK_WINDOW:
                self._attempts.pop(source, None)
                att = None

            user_ok = hmac.compare_digest(username or "", console.username or "")
            pass_ok = hmac.compare_digest(password or "", console.password or "")
            if not (user_ok and pass_ok):
                if att is None or now - att.first_at >= LOGIN_LOCK_WINDOW:
                    att = _Attempt(first_at=now)
                att.failures += 1
                self._attempts[source] = att
                return None, "用户名或密码错误"

            self._attempts.pop(source, None)
            token = secrets.token_hex(32)
            self._sessions[token] = _Session(token=token, username=username, expires_at=now + self.ttl)
            self._gc_locked(now)
            return token, ""

    def validate(self, token: str) -> str | None:
        if not token:
            return None
        now = time.time()
        with self._lock:
            sess = self._sessions.get(token)
            if sess is None:
                return None
            if now > sess.expires_at:
                self._sessions.pop(token, None)
                return None
            return sess.username

    def revoke(self, token: str) -> None:
        with self._lock:
            self._sessions.pop(token, None)

    def revoke_all(self) -> None:
        with self._lock:
            self._sessions.clear()

    def _gc_locked(self, now: float) -> None:
        for t in [k for k, s in self._sessions.items() if now > s.expires_at]:
            self._sessions.pop(t, None)


# ---------------------------------------------------------------------------
# 依赖
# ---------------------------------------------------------------------------


def _bearer(authorization: str | None) -> str:
    if authorization and authorization.startswith("Bearer "):
        return authorization[len("Bearer ") :]
    return ""


async def require_api_key(
    state: StateDep,
    authorization: Annotated[str | None, Header()] = None,
) -> None:
    """网关端点鉴权：api_key 为空则放行，否则校验 Bearer。"""
    key = state.cfg.api_key
    if not key:
        return
    if not hmac.compare_digest(_bearer(authorization), key):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "error": {
                    "message": "missing or invalid API key",
                    "type": "api_error",
                    "code": "invalid_api_key",
                }
            },
        )


def same_origin(origin: str, host: str) -> bool:
    if not origin:
        return True
    o = origin.rstrip("/")
    for scheme in ("http://", "https://"):
        if o.startswith(scheme):
            return o[len(scheme) :].lower() == host.lower()
    return False


async def require_session(
    request: Request,
    state: StateDep,
    authorization: Annotated[str | None, Header()] = None,
    origin: Annotated[str | None, Header()] = None,
) -> str:
    """控制台端点鉴权：Cookie 或 Bearer 会话；写操作校验同源。"""
    store: SessionStore = request.app.state.sessions

    token = ""
    cookie = request.cookies.get(SESSION_COOKIE)
    if cookie:
        token = cookie
    elif authorization and authorization.startswith("Bearer "):
        token = authorization[len("Bearer ") :]

    user = store.validate(token)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": "未登录或会话已过期", "code": "unauthorized"},
        )

    if request.method not in ("GET", "HEAD", "OPTIONS") and origin:
        if not same_origin(origin, request.headers.get("host", "")):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={"error": "跨站请求被拒绝", "code": "csrf"},
            )
    return user
