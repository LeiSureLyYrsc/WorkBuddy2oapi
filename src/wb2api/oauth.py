"""网页版 OAuth 设备授权登录会话管理。

对齐 Go 实现：workbuddy2api-gui/internal/ops/login.go 与 loginflow.go。
"""

from __future__ import annotations

import dataclasses
import secrets
import threading
import time
from typing import TYPE_CHECKING

from .models import Account, LoginSession, now_iso

if TYPE_CHECKING:
    from .upstream import UpstreamClient


def normalize_region(s: str) -> str:
    """规范化区域字符串（大小写/空白容错），合法值为 'cn' 或 'global'。"""
    cleaned = (s or "").strip().lower()
    if cleaned in ("", "cn"):
        return "cn"
    if cleaned == "global":
        return "global"
    raise ValueError(f"未知区域 {s!r}（可选 cn | global）")


class LoginManager:
    """设备授权登录会话管理器（线程安全）。"""

    def __init__(self, upstream: UpstreamClient, ttl_seconds: float = 900) -> None:
        self.upstream = upstream
        self.ttl_seconds = ttl_seconds
        self._lock = threading.RLock()
        self._sessions: dict[str, LoginSession] = {}
        self._accounts: dict[str, Account] = {}
        self._states: dict[str, str] = {}
        self._created_times: dict[str, float] = {}
        self._updated_times: dict[str, float] = {}
        self._active_id: str = ""

    async def start(self, region: str) -> LoginSession:
        """发起一次登录：申请 state 与授权 URL，创建并激活会话。"""
        norm_region = normalize_region(region)
        state, auth_url = await self.upstream.start_login(norm_region)

        session_id = secrets.token_hex(16)
        now_f = time.time()
        now_str = now_iso()

        sess = LoginSession(
            id=session_id,
            region=norm_region,
            auth_url=auth_url,
            status="pending",
            message="请在浏览器中打开授权链接并完成登录，然后点击「我已完成登录」",
            created_at=now_str,
            updated_at=now_str,
        )

        with self._lock:
            self._gc_locked(now_f)
            self._sessions[session_id] = sess
            self._states[session_id] = state
            self._created_times[session_id] = now_f
            self._updated_times[session_id] = now_f
            self._active_id = session_id
            return dataclasses.replace(sess)

    async def poll(self, id: str) -> LoginSession:
        """轮询一次登录状态。"""
        with self._lock:
            sess = self._sessions.get(id)
            if not sess:
                raise ValueError("登录会话不存在或已过期")
            if sess.status in ("success", "error", "cancelled", "expired"):
                return dataclasses.replace(sess)

            created_t = self._created_times.get(id, 0.0)
            if time.time() - created_t > self.ttl_seconds:
                sess.status = "expired"
                sess.message = "登录会话已超时（15 分钟），请重新发起登录"
                sess.updated_at = now_iso()
                self._updated_times[id] = time.time()
                return dataclasses.replace(sess)

            state = self._states.get(id, "")
            region = sess.region

        try:
            acct = await self.upstream.poll_login(region, state)
        except Exception as e:
            with self._lock:
                sess = self._sessions.get(id)
                if sess:
                    sess.status = "error"
                    sess.message = str(e)
                    sess.updated_at = now_iso()
                    self._updated_times[id] = time.time()
                    return dataclasses.replace(sess)
                raise ValueError("登录会话不存在或已过期")

        with self._lock:
            sess = self._sessions.get(id)
            if not sess:
                raise ValueError("登录会话不存在或已过期")

            now_str = now_iso()
            sess.updated_at = now_str
            self._updated_times[id] = time.time()

            if acct is None:
                sess.message = "等待授权完成…（请在浏览器中完成登录）"
                return dataclasses.replace(sess)

            sess.uid = acct.uid
            sess.nickname = acct.nickname
            sess.status = "success"
            sess.message = "登录成功，正在保存凭证…"
            self._accounts[id] = acct
            return dataclasses.replace(sess)

    def take_account(self, id: str) -> Account | None:
        """取出并清除暂存的凭证（保证凭证仅被落盘一次）。"""
        with self._lock:
            return self._accounts.pop(id, None)

    def get(self, id: str) -> LoginSession:
        """获取指定会话的当前状态快照（支持惰性超时翻转）。"""
        with self._lock:
            sess = self._sessions.get(id)
            if not sess:
                raise ValueError("登录会话不存在或已过期")

            if sess.status == "pending" and time.time() - self._created_times.get(id, 0.0) > self.ttl_seconds:
                sess.status = "expired"
                sess.message = "登录会话已超时（15 分钟），请重新发起登录"
                sess.updated_at = now_iso()
                self._updated_times[id] = time.time()

            return dataclasses.replace(sess)

    def cancel(self, id: str) -> LoginSession:
        """取消指定登录会话。"""
        with self._lock:
            sess = self._sessions.get(id)
            if not sess:
                raise ValueError("登录会话不存在或已过期")

            if sess.status == "pending":
                sess.status = "cancelled"
                sess.message = "已取消登录"
                sess.updated_at = now_iso()
                self._updated_times[id] = time.time()

            if self._active_id == id:
                self._active_id = ""

            return dataclasses.replace(sess)

    def current(self) -> LoginSession | None:
        """返回当前进行中的活跃会话（没有或非 pending 返回 None）。"""
        with self._lock:
            if not self._active_id:
                return None
            sess = self._sessions.get(self._active_id)
            if not sess or sess.status != "pending":
                return None
            return dataclasses.replace(sess)

    def mark_saved(self, id: str, file: str, restart: str) -> None:
        """标记凭证已成功落盘与服务重启结果。"""
        with self._lock:
            sess = self._sessions.get(id)
            if sess:
                sess.saved = True
                sess.file = file
                sess.restart = restart
                sess.updated_at = now_iso()
                self._updated_times[id] = time.time()
            if self._active_id == id:
                self._active_id = ""

    def _gc_locked(self, now: float) -> None:
        """清理已终结且超时超过 TTL 的老会话（必须持锁调用）。"""
        to_delete: list[str] = []
        for sid, sess in self._sessions.items():
            if sess.status == "pending":
                continue
            updated_t = self._updated_times.get(sid, 0.0)
            if now - updated_t > self.ttl_seconds:
                to_delete.append(sid)

        for sid in to_delete:
            self._sessions.pop(sid, None)
            self._accounts.pop(sid, None)
            self._states.pop(sid, None)
            self._created_times.pop(sid, None)
            self._updated_times.pop(sid, None)
