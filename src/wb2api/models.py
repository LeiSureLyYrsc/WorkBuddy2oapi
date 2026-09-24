"""共享数据模型：账号凭证、池状态、任务、登录会话等。"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Literal

# ---------------------------------------------------------------------------
# 账号凭证
# ---------------------------------------------------------------------------


@dataclass
class Account:
    """归一化账号凭证（来源：插件 OAuth 嵌套形 / 手写扁平形）。"""

    access_token: str = ""
    refresh_token: str = ""
    expires_at: int = 0  # Unix 秒
    domain: str = ""
    uid: str = ""
    enterprise_id: str = ""
    nickname: str = ""
    file_path: str = ""  # 来源文件绝对路径（不序列化）

    def expires_at_time(self) -> float:
        return float(self.expires_at)

    def needs_refresh(self, within: float = 600) -> bool:
        if self.expires_at <= 0:
            return True
        return time.time() + within >= self.expires_at

    def expired(self) -> bool:
        return self.expires_at > 0 and time.time() >= self.expires_at

    def is_global(self) -> bool:
        d = (self.domain or "").strip().lower()
        d = d.removeprefix("https://").removeprefix("http://")
        return d == "workbuddy.ai" or d.endswith(".workbuddy.ai")


# ---------------------------------------------------------------------------
# 池状态
# ---------------------------------------------------------------------------

CoolKind = Literal["hard_credit", "soft_rate"]


@dataclass
class AccountStatus:
    """单个账号对外暴露的状态（脱敏）。"""

    uid: str
    nickname: str = ""
    credits: int = 0
    cooling: bool = False
    cool_kind: str = ""
    cool_remaining_sec: int = 0
    until: str = ""  # ISO8601
    reason: str = ""
    soft_streak: int = 0
    soft_rate_model: str = ""
    disabled: bool = False
    disabled_reason: str = ""
    success_count: int = 0
    err_total: int = 0
    last_success: str = ""
    last_err: str = ""
    in_flight: int = 0
    breaker_fails: int = 0
    breaker_until: str = ""

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "uid": self.uid,
            "credits": self.credits,
            "cooling": self.cooling,
            "disabled": self.disabled,
            "in_flight": self.in_flight,
            "breaker_fails": self.breaker_fails,
        }
        # 可选字段：沿用旧网关的 omitempty 语义，避免前端出现空字段噪音。
        optional = {
            "nickname": self.nickname,
            "cool_kind": self.cool_kind,
            "reason": self.reason,
            "until": self.until,
            "soft_rate_model": self.soft_rate_model,
            "disabled_reason": self.disabled_reason,
            "last_success": self.last_success,
            "last_err": self.last_err,
            "breaker_until": self.breaker_until,
        }
        for k, v in optional.items():
            if v:
                out[k] = v
        if self.cool_remaining_sec:
            out["cool_remaining_sec"] = self.cool_remaining_sec
        if self.soft_streak:
            out["soft_streak"] = self.soft_streak
        if self.success_count:
            out["success_count"] = self.success_count
        if self.err_total:
            out["err_total"] = self.err_total
        return out


# ---------------------------------------------------------------------------
# 任务
# ---------------------------------------------------------------------------


@dataclass
class TaskItem:
    uid: str
    nickname: str
    action: str
    ok: bool
    message: str
    started_at: str = ""
    ended_at: str = ""
    reward: int = 0

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "uid": self.uid,
            "nickname": self.nickname,
            "action": self.action,
            "ok": self.ok,
            "message": self.message,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
        }
        if self.reward:
            d["reward"] = self.reward
        return d


@dataclass
class TaskView:
    id: str
    kind: str
    title: str
    running: bool = True
    error: str = ""
    started_at: str = ""
    finished_at: str = ""
    total: int = 0
    done: int = 0
    ok: int = 0
    failed: int = 0
    items: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "id": self.id,
            "kind": self.kind,
            "title": self.title,
            "running": self.running,
            "started_at": self.started_at,
            "total": self.total,
            "done": self.done,
            "ok": self.ok,
            "failed": self.failed,
            "items": self.items,
        }
        if self.error:
            d["error"] = self.error
        if self.finished_at:
            d["finished_at"] = self.finished_at
        return d


# ---------------------------------------------------------------------------
# 登录会话
# ---------------------------------------------------------------------------

LoginState = Literal["pending", "success", "error", "expired", "cancelled"]


@dataclass
class LoginSession:
    id: str
    region: str
    auth_url: str
    status: LoginState = "pending"
    message: str = ""
    created_at: str = ""
    updated_at: str = ""
    uid: str = ""
    nickname: str = ""
    saved: bool = False
    file: str = ""
    restart: str = ""

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "id": self.id,
            "region": self.region,
            "auth_url": self.auth_url,
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "saved": self.saved,
        }
        if self.message:
            d["message"] = self.message
        if self.uid:
            d["uid"] = self.uid
        if self.nickname:
            d["nickname"] = self.nickname
        if self.file:
            d["file"] = self.file
        if self.restart:
            d["restart"] = self.restart
        return d


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------


def iso(ts: float | None) -> str:
    """Unix 秒 → ISO8601（UTC，带 Z）。零/负值返回空串。"""
    if not ts or ts <= 0:
        return ""
    from datetime import datetime, timezone

    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def now_iso() -> str:
    return iso(time.time())


def split_region_prefix(model: str) -> tuple[str, str]:
    """解析模型名称中的区域前缀。

    返回 (region, bare)：
    - 若以 "cn:" 开头，返回 ("cn", bare)
    - 若以 "global:" 开头，返回 ("global", bare)
    - 其余返回 ("", model)
    即使 bare 内部包含冒号也完整保留。
    """
    if not model:
        return "", ""
    lower = model.lower()
    if lower.startswith("cn:"):
        return "cn", model[3:]
    if lower.startswith("global:"):
        return "global", model[7:]
    return "", model

