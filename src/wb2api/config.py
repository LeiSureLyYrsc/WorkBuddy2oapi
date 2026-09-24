"""统一配置：单一 config.json + WB2API_* 环境变量覆盖 + 运行时热重载。

设计要点
--------
* 网关与控制台合并为**一份**配置（旧项目里是两份，容易混淆、也是"改完要重启"的根源）。
* 所有时长字段以人类可读字符串书写（"600s" / "2h" / "30m"），解析成秒存储。
* 支持**热重载**：``ConfigManager.reload()`` 重新读盘并原子替换当前配置；
  各子系统通过 ``manager.current`` 读取，不持有旧引用。仅 ``listen`` 变更需进程重启。
"""

from __future__ import annotations

import json
import os
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, field_validator

# ---------------------------------------------------------------------------
# 时长解析
# ---------------------------------------------------------------------------

_DURATION_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(ms|s|m|h|d)?\s*$", re.IGNORECASE)
_UNIT_SECONDS = {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0, "d": 86400.0}


def parse_duration(value: Any, default: float) -> float:
    """把 "600s" / "2h" / 600 / 30 解析成秒。

    无单位纯数字按秒处理（沿用旧配置直觉）；非法值回落 default。
    """
    if value is None or value == "":
        return default
    if isinstance(value, (int, float)):
        return float(value)
    m = _DURATION_RE.match(str(value))
    if not m:
        return default
    n = float(m.group(1))
    unit = (m.group(2) or "s").lower()
    return n * _UNIT_SECONDS.get(unit, 1.0)


# ---------------------------------------------------------------------------
# 子配置
# ---------------------------------------------------------------------------


class ConsoleConfig(BaseModel):
    """Web 控制台自身的鉴权与安全开关。"""

    username: str = "admin"
    password: str = "workbuddy"
    session_ttl: str = "12h"
    credentials_file: str = "./data/credentials.json"
    read_only: bool = False
    dangerous_ops: bool = False

    @property
    def session_ttl_seconds(self) -> float:
        return parse_duration(self.session_ttl, 12 * 3600)


class CooldownConfig(BaseModel):
    soft_rate: str = "600s"
    soft_rate_max: str = "2h"

    @property
    def soft_rate_seconds(self) -> float:
        return parse_duration(self.soft_rate, 600)

    @property
    def soft_rate_max_seconds(self) -> float:
        return parse_duration(self.soft_rate_max, 2 * 3600)


class ScheduleConfig(BaseModel):
    checkin_hours: list[int] = Field(default_factory=lambda: [9, 21])
    keepalive_hours: list[int] = Field(default_factory=lambda: [22])
    checkin_enabled: bool = True
    keepalive_enabled: bool = True

    @field_validator("checkin_hours", "keepalive_hours")
    @classmethod
    def _valid_hours(cls, v: list[int]) -> list[int]:
        for h in v:
            if h < 0 or h > 23:
                raise ValueError(f"{h} 不是合法小时（0-23）")
        return v


class UpstreamConfig(BaseModel):
    timeout_seconds: int = 120
    header_timeout_seconds: int = 0  # <=0 回落 timeout_seconds
    idle_timeout_seconds: int = 0  # <=0 回落 300

    @property
    def header_timeout(self) -> float:
        return float(self.header_timeout_seconds or self.timeout_seconds or 120)

    @property
    def idle_timeout(self) -> float:
        return float(self.idle_timeout_seconds or 300)


class FeaturesConfig(BaseModel):
    sanitize_blacklist_fingerprints: bool = True


class PoolConfig(BaseModel):
    max_in_flight: int = 3
    breaker_threshold: int = 3
    breaker_cooldown: str = "30m"
    breaker_cooldown_max: str = "6h"
    idle_weight_per_hour: float = 0.5
    idle_weight_max: float = 5.0

    @property
    def breaker_cooldown_seconds(self) -> float:
        return parse_duration(self.breaker_cooldown, 30 * 60)

    @property
    def breaker_cooldown_max_seconds(self) -> float:
        return parse_duration(self.breaker_cooldown_max, 6 * 3600)


class SessionStickyConfig(BaseModel):
    enabled: bool = True
    ttl: str = "30m"
    gc_interval: str = "5m"

    @property
    def ttl_seconds(self) -> float:
        return parse_duration(self.ttl, 30 * 60)

    @property
    def gc_interval_seconds(self) -> float:
        return parse_duration(self.gc_interval, 5 * 60)


class ServerConfig(BaseModel):
    max_body_mb: int = 8
    metrics_enabled: bool = True
    metrics_file: str = "./data/metrics.json"
    metrics_retention_days: int = 30


# ---------------------------------------------------------------------------
# 顶层配置
# ---------------------------------------------------------------------------


class Config(BaseModel):
    """完整运行时配置。"""

    listen: str = ":7863"
    api_key: str = ""
    auth_dir: str = "./auths"
    state_file: str = "./data/state.json"
    pricing_file: str = "./data/pricing.json"

    console: ConsoleConfig = Field(default_factory=ConsoleConfig)
    cooldown: CooldownConfig = Field(default_factory=CooldownConfig)
    schedule: ScheduleConfig = Field(default_factory=ScheduleConfig)
    upstream: UpstreamConfig = Field(default_factory=UpstreamConfig)
    features: FeaturesConfig = Field(default_factory=FeaturesConfig)
    pool: PoolConfig = Field(default_factory=PoolConfig)
    session_sticky: SessionStickyConfig = Field(default_factory=SessionStickyConfig)
    server: ServerConfig = Field(default_factory=ServerConfig)

    # 运行时派生字段（不参与序列化）
    source_path: str = ""

    # ------------------------------------------------------------------
    # 派生属性
    # ------------------------------------------------------------------

    @property
    def host_port(self) -> tuple[str, int]:
        """把 ":7863" / "0.0.0.0:7863" / "7863" 解析成 (host, port)。"""
        raw = (self.listen or ":7863").strip()
        if raw.startswith(":"):
            return "0.0.0.0", int(raw[1:])
        if raw.isdigit():
            return "0.0.0.0", int(raw)
        host, _, port = raw.rpartition(":")
        return (host or "0.0.0.0"), int(port or "7863")

    @property
    def max_body_bytes(self) -> int:
        mb = self.server.max_body_mb if self.server.max_body_mb > 0 else 8
        return mb << 20

    def public_dict(self) -> dict[str, Any]:
        """用于 GET /api/config 的可编辑 JSON（不含派生字段）。"""
        data = self.model_dump(exclude={"source_path"})
        return data

    def using_default_password(self) -> bool:
        return self.console.username == "admin" and self.console.password == "workbuddy"


# ---------------------------------------------------------------------------
# 环境变量覆盖（非空才覆盖，沿用旧项目语义）
# ---------------------------------------------------------------------------

_ENV_MAP: dict[str, tuple[str, str]] = {
    # env 变量名: (顶层字段, 子字段)  —— 子字段为空表示顶层标量
    "WB2API_LISTEN": ("listen", ""),
    "WB2API_API_KEY": ("api_key", ""),
    "WB2API_AUTH_DIR": ("auth_dir", ""),
    "WB2API_STATE_FILE": ("state_file", ""),
    "WB2API_PRICING_FILE": ("pricing_file", ""),
    "WB2API_CONSOLE_USERNAME": ("console", "username"),
    "WB2API_CONSOLE_PASSWORD": ("console", "password"),
    "WB2API_SESSION_TTL": ("console", "session_ttl"),
    "WB2API_CREDENTIALS_FILE": ("console", "credentials_file"),
    "WB2API_SOFT_RATE": ("cooldown", "soft_rate"),
    "WB2API_SOFT_RATE_MAX": ("cooldown", "soft_rate_max"),
    "WB2API_TIMEOUT_SECONDS": ("upstream", "timeout_seconds"),
    "WB2API_HEADER_TIMEOUT_SECONDS": ("upstream", "header_timeout_seconds"),
    "WB2API_IDLE_TIMEOUT_SECONDS": ("upstream", "idle_timeout_seconds"),
    "WB2API_BREAKER_COOLDOWN": ("pool", "breaker_cooldown"),
    "WB2API_BREAKER_COOLDOWN_MAX": ("pool", "breaker_cooldown_max"),
    "WB2API_MAX_BODY_MB": ("server", "max_body_mb"),
    "WB2API_METRICS_FILE": ("server", "metrics_file"),
    "WB2API_METRICS_RETENTION_DAYS": ("server", "metrics_retention_days"),
}

_ENV_BOOL: dict[str, tuple[str, str]] = {
    "WB2API_SANITIZE_FINGERPRINTS": ("features", "sanitize_blacklist_fingerprints"),
    "WB2API_READ_ONLY": ("console", "read_only"),
    "WB2API_DANGEROUS_OPS": ("console", "dangerous_ops"),
    "WB2API_METRICS_ENABLED": ("server", "metrics_enabled"),
    "WB2API_CHECKIN_ENABLED": ("schedule", "checkin_enabled"),
    "WB2API_KEEPALIVE_ENABLED": ("schedule", "keepalive_enabled"),
    "WB2API_SESSION_STICKY": ("session_sticky", "enabled"),
}


def _apply_env(data: dict[str, Any]) -> None:
    for env_key, (top, sub) in _ENV_MAP.items():
        raw = os.environ.get(env_key)
        if raw is None or raw == "":
            continue
        target = data.setdefault(top, {}) if sub else data
        if sub:
            # 数值型字段尝试转 int
            if isinstance(target.get(sub), int) and not isinstance(target.get(sub), bool):
                try:
                    target[sub] = int(raw)
                    continue
                except ValueError:
                    pass
            target[sub] = raw
        else:
            data[top] = raw

    for env_key, (top, sub) in _ENV_BOOL.items():
        raw = os.environ.get(env_key)
        if raw is None or raw == "":
            continue
        data.setdefault(top, {})[sub] = raw.strip().lower() in {"1", "true", "yes", "on"}


# ---------------------------------------------------------------------------
# 加载
# ---------------------------------------------------------------------------


def load_config(path: str | os.PathLike[str] | None = None) -> Config:
    """读取配置：默认值 → JSON 文件 → 环境变量。文件不存在不报错。"""
    data: dict[str, Any] = {}
    resolved = ""
    if path:
        p = Path(path)
        resolved = str(p)
        if p.exists():
            # utf-8-sig：容忍 Windows 编辑器写入的 UTF-8 BOM（否则 json.loads 会报错）。
            raw = p.read_text(encoding="utf-8-sig")
            if raw.strip():
                data = json.loads(raw)

    _apply_env(data)
    cfg = Config.model_validate(data)
    cfg.source_path = resolved
    return cfg


# ---------------------------------------------------------------------------
# 热重载管理器
# ---------------------------------------------------------------------------


@dataclass
class ConfigManager:
    """持有当前配置，支持运行时原子热重载。

    所有子系统都应通过 ``manager.current`` 读取，而非缓存 Config 实例，
    这样 ``reload()`` 后各处立即看到新值（无需重启进程）。
    """

    path: str
    _current: Config = field(init=False)
    _lock: threading.RLock = field(default_factory=threading.RLock, init=False)
    # 监听地址变更回调（listen 变更无法热应用，由 main 决定是否提示）
    _listeners: list = field(default_factory=list, init=False)

    def __post_init__(self) -> None:
        self._current = load_config(self.path)

    @property
    def current(self) -> Config:
        with self._lock:
            return self._current

    def reload(self) -> Config:
        """重新读盘并原子替换当前配置，返回新配置。"""
        new_cfg = load_config(self.path)
        with self._lock:
            self._current = new_cfg
        return new_cfg

    def write(self, doc: dict[str, Any]) -> None:
        """把完整配置对象写回磁盘（先校验再原子替换）。"""
        cfg = Config.model_validate(doc)
        payload = json.dumps(cfg.model_dump(exclude={"source_path"}), ensure_ascii=False, indent=2)
        payload += "\n"
        target = Path(self.path)
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_text(payload, encoding="utf-8")
        os.replace(tmp, target)
        with self._lock:
            cfg.source_path = str(target)
            self._current = cfg
