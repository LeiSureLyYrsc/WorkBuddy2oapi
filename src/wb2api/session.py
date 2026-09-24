"""会话粘性路由：同一会话尽量绑定同一账号。

纯内存管理 + 双段分配策略（优先空闲账号，其次全池散列）。
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass


@dataclass
class _Entry:
    uid: str
    last_active: float


def fnv1a_32(key: str) -> int:
    """32-bit FNV-1a 哈希。"""
    h = 2166136261
    for b in key.encode("utf-8"):
        h ^= b
        h = (h * 16777619) & 0xFFFFFFFF
    return h


def hash_index(key: str, n: int) -> int:
    """FNV-1a 哈希取模。"""
    if n <= 0:
        return 0
    return fnv1a_32(key) % n


def extract_key(body: bytes | str) -> str:
    """从请求体提取会话键。

    尝试优先级：
    1. metadata.conversation_id
    2. metadata.conversationId
    3. metadata.user_id
    4. conversation_id
    5. conversationId
    """
    if not body:
        return ""
    if isinstance(body, str):
        raw = body
    else:
        try:
            raw = body.decode("utf-8")
        except UnicodeDecodeError:
            return ""

    if not raw.strip():
        return ""

    try:
        obj = json.loads(raw)
    except Exception:
        return ""

    if not isinstance(obj, dict):
        return ""

    meta = obj.get("metadata")
    if isinstance(meta, dict):
        v = meta.get("conversation_id")
        if isinstance(v, str) and v:
            return v
        v = meta.get("conversationId")
        if isinstance(v, str) and v:
            return v
        v = meta.get("user_id")
        if isinstance(v, str) and v:
            return v

    v = obj.get("conversation_id")
    if isinstance(v, str) and v:
        return v

    v = obj.get("conversationId")
    if isinstance(v, str) and v:
        return v

    return ""


class StickyRouter:
    """会话粘性路由器。线程安全。"""

    def __init__(
        self,
        ttl_seconds: float = 1800.0,
        gc_interval_seconds: float = 300.0,
        available: Callable[[], list[str]] | None = None,
    ) -> None:
        self.ttl_seconds = float(ttl_seconds if ttl_seconds > 0 else 1800.0)
        self.gc_interval_seconds = float(gc_interval_seconds if gc_interval_seconds > 0 else 300.0)
        self._available = available
        self._entries: dict[str, _Entry] = {}
        self._lock = threading.RLock()
        self._gc_task: asyncio.Task[None] | None = None

    def resolve(self, key: str) -> str | None:
        """返回会话 key 应绑定的账号 uid，None 表示当前无可用账号。"""
        if not key:
            return None

        now = time.time()
        available_uids = self._available() if self._available else []
        available_set = set(available_uids)

        with self._lock:
            # 1. 检查已有绑定（有效且仍在可用列表中）
            e = self._entries.get(key)
            if e is not None and (now - e.last_active <= self.ttl_seconds):
                if e.uid in available_set:
                    e.last_active = now
                    return e.uid
                # 绑定号已不可用/已冷却，清掉该绑定
                self._entries.pop(key, None)
            elif e is not None:
                # 已过期
                self._entries.pop(key, None)

            if not available_uids:
                return None

            # 2. 双段分配策略：优先"未绑定任何会话的可用号"，其次全池
            bound_uids = {
                entry.uid
                for entry in self._entries.values()
                if (now - entry.last_active <= self.ttl_seconds)
            }
            idle_uids = [u for u in available_uids if u not in bound_uids]
            candidate_pool = idle_uids if idle_uids else available_uids

            selected_uid = candidate_pool[hash_index(key, len(candidate_pool))]
            self._entries[key] = _Entry(uid=selected_uid, last_active=now)
            return selected_uid

    def bind(self, key: str, uid: str) -> None:
        """显式绑定会话 key 到 uid。"""
        if not key or not uid:
            return
        with self._lock:
            self._entries[key] = _Entry(uid=uid, last_active=time.time())

    def unbind(self, key: str) -> bool:
        """解除会话绑定，返回此前是否存在。"""
        if not key:
            return False
        with self._lock:
            return self._entries.pop(key, None) is not None

    def count(self) -> int:
        """返回当前活跃绑定数。"""
        with self._lock:
            return len(self._entries)

    def gc_once(self, now: float | None = None) -> int:
        """清理 TTL 过期的绑定，返回清理数量。"""
        t = now if now is not None else time.time()
        with self._lock:
            expired_keys = [
                k for k, e in self._entries.items() if (t - e.last_active > self.ttl_seconds)
            ]
            for k in expired_keys:
                del self._entries[k]
            return len(expired_keys)

    async def _gc_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(self.gc_interval_seconds)
                self.gc_once()
        except asyncio.CancelledError:
            pass

    def start_gc(self) -> None:
        """启动后台 GC 任务（幂等）。"""
        with self._lock:
            if self._gc_task is not None and not self._gc_task.done():
                return
            try:
                loop = asyncio.get_running_loop()
                self._gc_task = loop.create_task(self._gc_loop())
            except RuntimeError:
                pass

    def stop_gc(self) -> None:
        """停止后台 GC 任务（幂等）。"""
        with self._lock:
            if self._gc_task is not None:
                self._gc_task.cancel()
                self._gc_task = None

    def load_from_store(self, binds: dict[str, str]) -> None:
        """从备份恢复绑定（不覆盖本地已有记录）。"""
        if not binds:
            return
        now = time.time()
        with self._lock:
            for k, uid in binds.items():
                if k not in self._entries:
                    self._entries[k] = _Entry(uid=uid, last_active=now)
