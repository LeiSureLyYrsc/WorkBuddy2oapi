"""应用共享状态：把所有子系统装配在一起，并提供**热加载**能力。

这是"消除重启"的核心：新增/删除账号或改配置后，直接调用本模块的方法原地更新，
无需重启进程。
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .auth_store import AccountStore
from .config import Config, ConfigManager
from .metrics import Collector
from .models import Account
from .oauth import LoginManager
from .pool import AccountPool
from .pricing import PricingTable
from .scheduler import Scheduler
from .session import StickyRouter
from .tasks import TaskManager
from .upstream import UpstreamClient

logger = logging.getLogger("wb2api.state")


@dataclass
class AppState:
    """进程级共享状态容器。

    所有 router 通过 ``request.app.state.wb`` 取得本对象。
    """

    config_manager: ConfigManager
    store: AccountStore
    pool: AccountPool
    upstream: UpstreamClient
    sticky: StickyRouter
    metrics: Collector
    pricing: PricingTable
    tasks: TaskManager
    logins: LoginManager
    scheduler: Scheduler

    _lock: threading.RLock = field(default_factory=threading.RLock, init=False)
    started_at: float = field(default=0.0, init=False)
    version: str = "1.0.0"

    # ------------------------------------------------------------------
    # 便捷访问
    # ------------------------------------------------------------------

    @property
    def cfg(self) -> Config:
        return self.config_manager.current

    # ------------------------------------------------------------------
    # 账号热加载
    # ------------------------------------------------------------------

    def sync_accounts(self) -> tuple[list[Account], list[str]]:
        """从磁盘重新扫描凭证并原地同步到内存池（新增/移除），不重启进程。"""
        accounts, warnings = self.store.list()
        self.pool.sync_from_dir(accounts)
        logger.info("账号热同步：池内 %d 个（磁盘 %d 个）", len(self.pool.list()), len(accounts))
        return accounts, warnings

    def add_account_live(self, account: Account) -> None:
        """把单个账号原地加入内存池（OAuth/导入后立即生效）。"""
        self.pool.add(account)

    def remove_account_live(self, uid: str) -> None:
        """删除单个账号的磁盘凭证并原地移出内存池。"""
        self.store.delete(uid)
        self.pool.sync_from_dir(self.store.list()[0])

    # ------------------------------------------------------------------
    # 配置热加载
    # ------------------------------------------------------------------

    def reload_config(self) -> tuple[Config, bool]:
        """重新读盘并热应用配置；返回 (新配置, 是否 listen 变更需重启)。"""
        old = self.cfg
        new = self.config_manager.reload()
        self.apply_config(new, old)
        listen_changed = old.listen != new.listen
        if listen_changed:
            logger.warning("listen 由 %s 改为 %s：监听地址变更需重启进程才生效", old.listen, new.listen)
        return new, listen_changed

    def write_config(self, doc: dict[str, Any]) -> tuple[Config, bool]:
        """校验并写回配置，随后热应用。"""
        old = self.cfg
        self.config_manager.write(doc)
        new = self.cfg
        self.apply_config(new, old)
        return new, old.listen != new.listen

    def apply_config(self, new: Config, old: Config | None = None) -> None:
        """把配置热应用到各子系统（不重建进程）。"""
        with self._lock:
            # 1. 池参数
            self.pool.set_breaker(
                new.pool.breaker_threshold,
                new.pool.breaker_cooldown_seconds,
                new.pool.breaker_cooldown_max_seconds,
            )
            self.pool.set_soft_rate_max(new.cooldown.soft_rate_max_seconds)
            self.pool.set_weights(new.pool.idle_weight_per_hour, new.pool.idle_weight_max)
            self.pool.set_max_in_flight(new.pool.max_in_flight)

            # 2. 会话粘性参数（available 闭包不变）
            if old is not None and new.session_sticky.enabled != old.session_sticky.enabled:
                if new.session_sticky.enabled:
                    self.sticky.start_gc()
                else:
                    self.sticky.stop_gc()
            self.sticky.ttl_seconds = new.session_sticky.ttl_seconds
            self.sticky.gc_interval_seconds = new.session_sticky.gc_interval_seconds

            # 3. 上游超时 / 脱敏：仅在相关字段变化时重建（避免打断在途流）
            if old is not None:
                changed = (
                    new.upstream.timeout_seconds != old.upstream.timeout_seconds
                    or new.upstream.header_timeout_seconds != old.upstream.header_timeout_seconds
                    or new.upstream.idle_timeout_seconds != old.upstream.idle_timeout_seconds
                    or new.features.sanitize_blacklist_fingerprints
                    != old.features.sanitize_blacklist_fingerprints
                )
                if changed:
                    self.rebuild_upstream(new)

            # 4. 统计保留期
            self.metrics.set_retention(new.server.metrics_retention_days * 86400)

    def rebuild_upstream(self, cfg: Config | None = None) -> None:
        """按最新配置重建上游客户端（异步关闭旧连接）。"""
        cfg = cfg or self.cfg
        old_client = self.upstream
        self.upstream = UpstreamClient(
            timeout_seconds=cfg.upstream.timeout_seconds,
            header_timeout_seconds=cfg.upstream.header_timeout_seconds,
            idle_timeout_seconds=cfg.upstream.idle_timeout_seconds,
            sanitize=cfg.features.sanitize_blacklist_fingerprints,
        )
        # 让依赖旧引用的子系统改用新客户端
        self.logins.upstream = self.upstream
        self.scheduler.upstream = self.upstream
        import asyncio

        try:
            loop = asyncio.get_running_loop()
            loop.create_task(old_client.aclose())
        except RuntimeError:
            pass


# ---------------------------------------------------------------------------
# 工厂
# ---------------------------------------------------------------------------


def build_pool(cfg: Config) -> AccountPool:
    return AccountPool(
        state_file=cfg.state_file,
        breaker_threshold=cfg.pool.breaker_threshold,
        breaker_cooldown=cfg.pool.breaker_cooldown_seconds,
        breaker_cooldown_max=cfg.pool.breaker_cooldown_max_seconds,
        soft_rate_max=cfg.cooldown.soft_rate_max_seconds,
        idle_weight_per_hour=cfg.pool.idle_weight_per_hour,
        idle_weight_max=cfg.pool.idle_weight_max,
        max_in_flight=cfg.pool.max_in_flight,
    )


def build_upstream(cfg: Config) -> UpstreamClient:
    return UpstreamClient(
        timeout_seconds=cfg.upstream.timeout_seconds,
        header_timeout_seconds=cfg.upstream.header_timeout_seconds,
        idle_timeout_seconds=cfg.upstream.idle_timeout_seconds,
        sanitize=cfg.features.sanitize_blacklist_fingerprints,
    )


def build_state(config_path: str, version: str, web_dist: str = "") -> AppState:
    """装配全部子系统。"""
    cm = ConfigManager(config_path)
    cfg = cm.current

    # 凭证目录：不存在则创建（首次部署常见）。
    store = AccountStore(cfg.auth_dir)
    Path(store.dir).mkdir(parents=True, exist_ok=True)

    pool = build_pool(cfg)
    accounts, warnings = store.list()
    if warnings:
        for w in warnings:
            logger.warning("凭证文件问题: %s", w)
    pool.sync_from_dir(accounts)
    logger.info("启动加载 %d 个账号（来自 %s）", len(accounts), cfg.auth_dir)

    upstream = build_upstream(cfg)
    metrics = Collector(cfg.server.metrics_file if cfg.server.metrics_enabled else "")
    metrics.set_retention(cfg.server.metrics_retention_days * 86400)
    pricing = PricingTable(cfg.pricing_file)
    tasks = TaskManager()
    logins = LoginManager(upstream)

    sticky = StickyRouter(
        ttl_seconds=cfg.session_sticky.ttl_seconds,
        gc_interval_seconds=cfg.session_sticky.gc_interval_seconds,
        available=pool.available_uids,
    )
    if cfg.session_sticky.enabled:
        sticky.start_gc()

    scheduler = Scheduler(
        pool=pool,
        upstream=upstream,
        config_provider=lambda: cm.current,
        save_account=store.save,
    )

    state = AppState(
        config_manager=cm,
        store=store,
        pool=pool,
        upstream=upstream,
        sticky=sticky,
        metrics=metrics,
        pricing=pricing,
        tasks=tasks,
        logins=logins,
        scheduler=scheduler,
        version=version,
    )
    import time

    state.started_at = time.time()
    return state
