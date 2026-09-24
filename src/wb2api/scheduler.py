"""定时任务调度器：每日签到、token 保活与猫猫旅行。

对齐 Go 实现：workbuddy2api/internal/scheduler/scheduler.go 与 travel.go。
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import logging
import threading
from typing import TYPE_CHECKING, Any, Callable

from .upstream import ErrKind, UpstreamClient, UpstreamError, supports_checkin

if TYPE_CHECKING:
    from .config import Config
    from .models import Account

logger = logging.getLogger("wb2api.scheduler")

CST_TZ = timezone(timedelta(hours=8))


def cst_day(dt: datetime | None = None) -> str:
    """返回 dt 所属的上游自然日（CST，UTC+8），格式 YYYY-MM-DD。"""
    if dt is None:
        dt = datetime.now(timezone.utc)
    return dt.astimezone(CST_TZ).strftime("%Y-%m-%d")


def next_fire(now: datetime, hours: list[int]) -> datetime | None:
    """返回 now 之后最近的一个整点触发时间；hours 为本地小时列表（0-23）。"""
    if not hours:
        return None
    earliest: datetime | None = None
    for h in hours:
        t = now.replace(hour=h, minute=0, second=0, microsecond=0)
        if t <= now:
            t += timedelta(days=1)
        if earliest is None or t < earliest:
            earliest = t
    return earliest


class Scheduler:
    """定时调度器：按配置小时触发签到与保活，支持运行时配置热重载。"""

    def __init__(
        self,
        pool: Any,
        upstream: UpstreamClient,
        config_provider: Callable[[], Config],
        save_account: Callable[[Account], None] | None = None,
    ) -> None:
        self.pool = pool
        self.upstream = upstream
        self.config_provider = config_provider
        self.save_account = save_account

        self._lock = threading.Lock()
        self._adopt_tried: dict[str, str] = {}
        self._running = False

    def adopt_tried_today(self, uid: str) -> bool:
        """检查该账号今日是否已尝试领养但门槛未达。"""
        with self._lock:
            return self._adopt_tried.get(uid) == cst_day()

    def mark_adopt_tried(self, uid: str) -> None:
        """标记该账号今日领养门槛未达，当日不再重试。"""
        with self._lock:
            self._adopt_tried[uid] = cst_day()

    def next_wake(self, now: datetime) -> tuple[datetime | None, list[str]]:
        """计算下一次唤醒时刻及该时刻需执行的任务列表。"""
        cfg = self.config_provider()
        sched = cfg.schedule
        slots: list[tuple[datetime, str]] = []

        if sched.checkin_enabled and sched.checkin_hours:
            t = next_fire(now, sched.checkin_hours)
            if t is not None:
                slots.append((t, "checkin"))

        if sched.keepalive_enabled and sched.keepalive_hours:
            t = next_fire(now, sched.keepalive_hours)
            if t is not None:
                slots.append((t, "keepalive"))

        if not slots:
            return None, []

        earliest = min(sl[0] for sl in slots)
        kinds = [sl[1] for sl in slots if sl[0] == earliest]
        return earliest, kinds

    async def run(self) -> None:
        """调度主循环，阻塞直到被取消。"""
        self._running = True
        try:
            while self._running:
                now = datetime.now().astimezone()
                earliest, kinds = self.next_wake(now)
                if earliest is None:
                    try:
                        await asyncio.sleep(5.0)
                    except asyncio.CancelledError:
                        break
                    continue

                diff = (earliest - now).total_seconds()
                if diff <= 0.5:
                    for kind in kinds:
                        if kind == "checkin":
                            await self.run_checkin_now()
                        elif kind == "keepalive":
                            await self.run_keepalive_now()
                    try:
                        await asyncio.sleep(1.0)
                    except asyncio.CancelledError:
                        break
                else:
                    try:
                        # 每次至多睡 10s，以便及时响应配置热重载
                        await asyncio.sleep(min(diff, 10.0))
                    except asyncio.CancelledError:
                        break
        finally:
            self._running = False

    def stop(self) -> None:
        """停止调度主循环。"""
        self._running = False

    async def run_checkin_now(self) -> None:
        """立即对所有可用账号执行签到 + 刷新余额 + 解冻，收尾推进猫猫旅行。"""
        accounts_status = self.pool.list()
        for st in accounts_status:
            if st.disabled:
                continue
            a = self.pool.auth_by_uid(st.uid)
            if a is None or not a.refresh_token:
                continue
            # 国际版无签到接口：跳过，避免发出必然失败的请求。
            if not supports_checkin(a):
                continue
            try:
                cr = await self.upstream.daily_checkin(a)
            except Exception as e:
                logger.warning("daily_checkin failed uid=%s: %s", st.uid, e)
                cr = None
            # 国际版 unsupported：跳过余额查询（其 billing 路径仍需探测，见下）。
            if cr is not None and cr.unsupported:
                continue

            try:
                credits_res = await self.upstream.user_resource(a)
                remain = credits_res.remain
                self.pool.reenable_if_credits(st.uid, remain)
            except Exception as e:
                logger.warning("user_resource failed uid=%s: %s", st.uid, e)
                continue

        await self.run_travel_now()

    async def run_keepalive_now(self) -> None:
        """立即对所有可用账号刷新 token 并持久化；识别失效 session。"""
        accounts_status = self.pool.list()
        for st in accounts_status:
            if st.disabled:
                continue
            a = self.pool.auth_by_uid(st.uid)
            if a is None or not a.refresh_token:
                continue
            try:
                await self.upstream.refresh_token(a)
                self.pool.clear_session_dead(st.uid)
                if self.save_account:
                    try:
                        self.save_account(a)
                    except Exception as e:
                        logger.warning("save_account failed uid=%s: %s", st.uid, e)
            except UpstreamError as ue:
                logger.warning("keepalive failed uid=%s: %s", st.uid, ue)
                if ue.kind == ErrKind.session_dead:
                    self.pool.note_session_dead(st.uid)
            except Exception as e:
                logger.warning("keepalive failed uid=%s: %s", st.uid, e)

    async def run_travel_now(self) -> None:
        """对所有可用账号推进一趟旅行状态机（限速 0.8s 避免风控）。"""
        accounts_status = self.pool.list()
        first = True
        for st in accounts_status:
            if st.disabled:
                continue
            a = self.pool.auth_by_uid(st.uid)
            if a is None or not a.refresh_token:
                continue
            # 国际版无成长中心接口：跳过，避免发出必然失败的请求。
            if not supports_checkin(a):
                continue
            if self.adopt_tried_today(a.uid):
                continue
            if not first:
                await asyncio.sleep(0.8)
            first = False
            try:
                res = await self.upstream.travel_once(a)
                if res.action == "skip" and "门槛" in res.message:
                    self.mark_adopt_tried(a.uid)
            except Exception as e:
                logger.warning("travel_once failed uid=%s: %s", a.uid, e)
