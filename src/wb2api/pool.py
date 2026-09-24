"""账号池：单一状态机（健康/冷却/熔断）+ 在途租约 + 三因子加权挑选 + state.json 持久化。

从 Go 实现 workbuddy2api/internal/pool/pool.go 严格对齐移植：
1. 健康维度（唯一权威）：healthy = not disabled and not (until 生效) and not (breaker_until 生效)
2. 并发维度：in_flight（在途租约）
3. 统计维度：success_count / err_total / last_used / last_success / last_err
"""

from __future__ import annotations

import json
import logging
import math
import os
import random
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable

from wb2api.models import Account, AccountStatus, iso

logger = logging.getLogger(__name__)

# 默认常量（与 Go 版本保持一致）
DEFAULT_BREAKER_THRESHOLD = 3
DEFAULT_BREAKER_COOLDOWN = 30 * 60.0  # 30 分钟
DEFAULT_BREAKER_COOLDOWN_MAX = 6 * 3600.0  # 6 小时
DEFAULT_SOFT_RATE_MAX = 2 * 3600.0  # 2 小时
DEFAULT_IDLE_WEIGHT_PER_HOUR = 0.5
DEFAULT_IDLE_WEIGHT_MAX = 5.0
DEFAULT_MIN_PICK_GAP = 0.1  # 100ms
SESSION_DEAD_THRESHOLD = 3
SESSION_DEAD_REASON = "12153 session dead"
SOFT_STREAK_SHIFT_MAX = 16
PERSIST_LOG_EVERY = 12
SCALE = 1_000_000


def _parse_ts(val: Any) -> float:
    """解析时间戳：支持 float/int 秒数以及 ISO8601 字符串（包含 Z）。"""
    if not val:
        return 0.0
    if isinstance(val, (int, float)):
        return float(val)
    if isinstance(val, str):
        val = val.strip()
        if not val:
            return 0.0
        try:
            if val.endswith("Z"):
                val = val[:-1] + "+00:00"
            return datetime.fromisoformat(val).timestamp()
        except Exception:
            return 0.0
    return 0.0


@dataclass
class _Entry:
    """池内单账号运行时状态实体。"""

    account: Account
    credits: int = 0
    success_count: int = 0
    err_total: int = 0
    last_err: float = 0.0
    last_success: float = 0.0
    cool_kind: str = ""  # "hard_credit" | "soft_rate" | ""
    until: float = 0.0  # epoch
    disabled: bool = False
    reason: str = ""
    last_used: float = 0.0  # epoch
    breaker_until: float = 0.0  # epoch
    fails: int = 0
    retry_count: int = 0
    soft_streak: int = 0
    session_dead_fails: int = 0
    soft_rate_model: str = ""
    in_flight: int = 0


class AccountPool:
    """线程安全账号池，移植自 Go internal/pool/pool.go。"""

    def __init__(
        self,
        state_file: str = "",
        *,
        breaker_threshold: int = DEFAULT_BREAKER_THRESHOLD,
        breaker_cooldown: float = DEFAULT_BREAKER_COOLDOWN,
        breaker_cooldown_max: float = DEFAULT_BREAKER_COOLDOWN_MAX,
        soft_rate_max: float = DEFAULT_SOFT_RATE_MAX,
        idle_weight_per_hour: float = DEFAULT_IDLE_WEIGHT_PER_HOUR,
        idle_weight_max: float = DEFAULT_IDLE_WEIGHT_MAX,
        max_in_flight: int = 0,
        min_pick_gap: float = DEFAULT_MIN_PICK_GAP,
        now_fn: Callable[[], float] = time.time,
        rand_fn: Callable[[int], int] = random.randrange,
    ) -> None:
        self._lock = threading.RLock()
        self._by_uid: dict[str, _Entry] = {}
        self.state_file = state_file
        self._dirty = False
        self._persist_fails = 0

        self.breaker_threshold = breaker_threshold
        self.breaker_cooldown = float(breaker_cooldown)
        self.breaker_cooldown_max = float(breaker_cooldown_max)
        self.soft_rate_max = float(soft_rate_max)
        self.idle_weight_per_hour = float(idle_weight_per_hour)
        self.idle_weight_max = float(idle_weight_max)
        self.max_in_flight = max_in_flight
        self.min_pick_gap = float(min_pick_gap)

        self.now_fn = now_fn
        self.rand_fn = rand_fn

        self._flusher_thread: threading.Thread | None = None
        self._flusher_stop = threading.Event()

        if self.state_file:
            self.load()
            self.start_flusher()

    # ---------------------------------------------------------------------------
    # 配置注入 / 运行时调优
    # ---------------------------------------------------------------------------

    def set_breaker(
        self,
        threshold: int = 0,
        cooldown: float = 0.0,
        cooldown_max: float = 0.0,
    ) -> None:
        """注入熔断器参数。非正值保留原值。"""
        with self._lock:
            if threshold > 0:
                self.breaker_threshold = threshold
            if cooldown > 0:
                self.breaker_cooldown = float(cooldown)
            if cooldown_max > 0:
                self.breaker_cooldown_max = float(cooldown_max)

    def set_soft_rate_max(self, seconds: float) -> None:
        """注入软冷却指数退避的封顶时长。非正值保留原值。"""
        with self._lock:
            if seconds > 0:
                self.soft_rate_max = float(seconds)

    def set_weights(self, idle_per_hour: float = 0.0, idle_max: float = 0.0) -> None:
        """注入三因子加权的闲置补偿参数。非正值保留原值。"""
        with self._lock:
            if idle_per_hour > 0:
                self.idle_weight_per_hour = float(idle_per_hour)
            if idle_max > 0:
                self.idle_weight_max = float(idle_max)

    def set_max_in_flight(self, n: int) -> None:
        """注入单账号最大在途请求数；0 = 不限。负值保留原值。"""
        with self._lock:
            if n >= 0:
                self.max_in_flight = n

    def set_random_source(self, fn: Callable[[int], int] | None) -> None:
        """注入随机数发生器（测试用确定性源）。None 则回退 random.randrange。"""
        with self._lock:
            self.rand_fn = fn if fn is not None else random.randrange

    # ---------------------------------------------------------------------------
    # 健康判定谓词 (与 Go 语义严格对齐)
    # ---------------------------------------------------------------------------

    def _healthy(self, e: _Entry, now: float) -> bool:
        """报告账号当前是否可选（未禁用、未处于任一冷却/熔断期）。"""
        if e.disabled:
            return False
        if e.until > 0 and now < e.until:
            return False
        if e.breaker_until > 0 and now < e.breaker_until:
            return False
        return True

    def _healthy_for_model(self, e: _Entry, now: float, req_model: str) -> bool:
        """报告账号对指定 model 是否可选（含模型级软冷却豁免）。"""
        if (
            not self._healthy(e, now)
            and req_model
            and e.soft_rate_model
            and e.cool_kind == "soft_rate"
            and e.soft_rate_model != req_model
        ):
            if e.disabled:
                return False
            return e.breaker_until <= 0 or not (now < e.breaker_until)
        return self._healthy(e, now)

    def _expiry(self, e: _Entry, now: float) -> float:
        """返回账号当前仍在生效的最近冷却/熔断截止时间（取较早者）；不在冷却期返回 0。"""
        t = 0.0
        if e.until > 0 and now < e.until:
            t = e.until
        if e.breaker_until > 0 and now < e.breaker_until:
            if t <= 0 or e.breaker_until < t:
                t = e.breaker_until
        return t

    def _fallback_kind(self, e: _Entry, now: float) -> str:
        """报告兜底账号属于哪一类冷却（soft：即时软冷却；breaker：熔断期）。"""
        if e.breaker_until > 0 and now < e.breaker_until:
            if e.until <= 0 or not (now < e.until) or e.breaker_until < e.until:
                return "breaker"
        return "soft"

    def _in_flight_full(self, e: _Entry) -> bool:
        """报告账号是否已占满在途名额（max_in_flight<=0 不限 → 恒 False）。"""
        if self.max_in_flight <= 0:
            return False
        return e.in_flight >= self.max_in_flight

    def _soft_rate_max_or(self) -> float:
        """返回软冷却退避封顶；未注入（<=0）时按默认值。"""
        if self.soft_rate_max <= 0:
            return DEFAULT_SOFT_RATE_MAX
        return self.soft_rate_max

    # ---------------------------------------------------------------------------
    # 账号管理与同步
    # ---------------------------------------------------------------------------

    def add(self, account: Account) -> None:
        """加入账号；已存在则保留原状态、更新凭证。"""
        with self._lock:
            self._upsert_locked(account)

    def _upsert_locked(self, account: Account) -> None:
        if account.uid in self._by_uid:
            self._by_uid[account.uid].account = account
            return
        self._by_uid[account.uid] = _Entry(account=account)

    def sync_from_dir(self, accounts: list[Account]) -> None:
        """对齐池账号列表：新账号加入、消失的账号剔除并立即落盘。"""
        with self._lock:
            seen: set[str] = set()
            for a in accounts:
                seen.add(a.uid)
                self._upsert_locked(a)
            changed = False
            to_del = [uid for uid in self._by_uid if uid not in seen]
            for uid in to_del:
                del self._by_uid[uid]
                changed = True
            if changed:
                self._dirty = True
                self._save_locked()

    # ---------------------------------------------------------------------------
    # 在途租约 (Acquire / Release)
    # ---------------------------------------------------------------------------

    def acquire(self, uid: str) -> bool:
        """为账号占一个在途名额；False 表示账号不存在或已达上限。"""
        with self._lock:
            e = self._by_uid.get(uid)
            if not e:
                return False
            if self.max_in_flight <= 0:
                e.in_flight += 1
                return True
            if e.in_flight >= self.max_in_flight:
                return False
            e.in_flight += 1
            return True

    def release(self, uid: str) -> None:
        """释放一个在途名额，底限为 0。"""
        with self._lock:
            e = self._by_uid.get(uid)
            if not e:
                return
            if e.in_flight > 0:
                e.in_flight -= 1

    # ---------------------------------------------------------------------------
    # 挑号算法 (Pick)
    # ---------------------------------------------------------------------------

    def pick(self) -> Account | None:
        """无排除、无模型感知选号。"""
        return self.pick_excluding_for_model(None, "")

    def pick_excluding(self, tried: set[str] | None = None) -> Account | None:
        """跳过 tried 集合的选号。"""
        return self.pick_excluding_for_model(tried, "")

    def pick_excluding_for_model(
        self,
        tried: set[str] | None = None,
        req_model: str = "",
    ) -> Account | None:
        """三因子加权随机选号（Top5 截断 + 防撞号 LRU 兜底 + 全冷却兜底）。"""
        with self._lock:
            now = self.now_fn()

            def healthy_of(e: _Entry) -> bool:
                if req_model:
                    return self._healthy_for_model(e, now, req_model)
                return self._healthy(e, now)

            cands: list[_Entry] = []
            for uid, e in self._by_uid.items():
                if tried and uid in tried:
                    continue
                if not healthy_of(e):
                    continue
                if self._in_flight_full(e):
                    continue
                cands.append(e)

            if not cands:
                return self._pick_earliest_expiry_locked(tried, now)

            max_credits = max((e.credits for e in cands), default=0)

            # 预计算权重并排序：权重降序，uid 升序
            weighted_list = [
                (self._weight_of(e, max_credits, now), e.account.uid, e)
                for e in cands
            ]
            weighted_list.sort(key=lambda x: (-x[0], x[1]))
            top5 = [x[2] for x in weighted_list[:5]]

            # 防并发撞号过滤：最近 min_pick_gap 秒内被用过的暂不复选
            eligible = [e for e in top5 if (now - e.last_used) >= self.min_pick_gap]
            if not eligible:
                # Top5 全部刚被用过：LRU 兜底
                selected = top5[0]
                for c in top5[1:]:
                    if c.last_used < selected.last_used:
                        selected = c
            else:
                selected = self._pick_weighted(eligible, now)

            selected.last_used = self.now_fn()
            return selected.account

    def _pick_earliest_expiry_locked(
        self,
        tried: set[str] | None,
        now: float,
    ) -> Account | None:
        """全冷却兜底：在非禁用的软冷却/熔断账号中选截止最早的一个。"""
        best: _Entry | None = None
        best_exp = 0.0
        for uid in sorted(self._by_uid.keys()):
            e = self._by_uid[uid]
            if tried and uid in tried:
                continue
            if e.disabled:
                continue
            if e.cool_kind == "hard_credit" and e.until > 0 and now < e.until:
                continue
            if self._in_flight_full(e):
                continue
            exp = self._expiry(e, now)
            if exp <= 0:
                continue
            if best is None or exp < best_exp:
                best = e
                best_exp = exp

        if best is None:
            return None

        logger.info(
            "pool: fallback_earliest_expiry uid=%s until=%s kind=%s",
            best.account.uid,
            iso(best_exp),
            self._fallback_kind(best, now),
        )
        best.last_used = self.now_fn()
        return best.account

    def _weight_of(self, e: _Entry, max_credits: int, now: float) -> float:
        """计算单账号三因子权重：基础 1.0 + 积分占比×10 + 闲置补偿 + 成功率×3。"""
        w = 1.0
        if max_credits > 0:
            w += (float(e.credits) / float(max_credits)) * 10.0

        if e.last_used <= 0:
            w += self.idle_weight_max
        else:
            hours = (now - e.last_used) / 3600.0
            idle_w = hours * self.idle_weight_per_hour
            if idle_w > self.idle_weight_max:
                idle_w = self.idle_weight_max
            elif idle_w < 0:
                idle_w = 0.0
            w += idle_w

        total_req = e.success_count + e.err_total
        if total_req > 0:
            w += (float(e.success_count) / float(total_req)) * 3.0
        else:
            w += 1.5
        return w

    def _pick_weighted(self, cands: list[_Entry], now: float) -> _Entry:
        """定点放大累加加权抽签。"""
        max_credits = max((e.credits for e in cands), default=0)
        weights: list[int] = []
        total = 0
        for e in cands:
            w = self._weight_of(e, max_credits, now)
            int_w = int(w * SCALE)
            weights.append(int_w)
            total += int_w

        if total <= 0:
            idx = self.rand_fn(len(cands))
            return cands[idx]

        r = self.rand_fn(total)
        acc = 0
        for i, e in enumerate(cands):
            acc += weights[i]
            if r < acc:
                return e
        return cands[-1]

    # ---------------------------------------------------------------------------
    # 状态更新 / 冷却 / 熔断
    # ---------------------------------------------------------------------------

    def set_credits(self, uid: str, credits: int) -> None:
        """更新账号积分。"""
        with self._lock:
            e = self._by_uid.get(uid)
            if e:
                e.credits = credits
                self._dirty = True

    def cooldown(self, uid: str, kind: str, seconds: float, reason: str) -> None:
        """即时冷却账号至 now + seconds。"""
        with self._lock:
            e = self._by_uid.get(uid)
            if not e:
                return
            d = seconds
            if kind == "soft_rate":
                e.soft_streak += 1
                d = self._soft_duration_locked(d, e.soft_streak)
            now = self.now_fn()
            e.until = now + d
            e.cool_kind = kind
            e.reason = reason
            e.soft_rate_model = ""
            self._record_breaker_failure_locked(e)
            self._dirty = True

    def cooldown_soft_for_model(
        self,
        uid: str,
        base_seconds: float,
        reset_at_epoch: float,
        model: str,
        reason: str,
    ) -> None:
        """模型级软冷却入口（429 code=6004 带解析恢复时间）。"""
        with self._lock:
            e = self._by_uid.get(uid)
            if not e:
                return
            e.soft_streak += 1
            has_reset = reset_at_epoch > 0
            d = self._soft_duration_locked(base_seconds, e.soft_streak)
            now = self.now_fn()
            if has_reset:
                cap = now + self._soft_rate_max_or()
                if reset_at_epoch <= now:
                    d = 0.001
                elif reset_at_epoch > cap:
                    d = cap - now
                else:
                    d = reset_at_epoch - now
            e.until = now + d
            e.cool_kind = "soft_rate"
            e.reason = reason
            if has_reset:
                e.soft_rate_model = model
            else:
                e.soft_rate_model = ""
            self._record_breaker_failure_locked(e)
            self._dirty = True

    def _soft_duration_locked(self, d: float, streak: int) -> float:
        """软冷却连续退避：d << (streak-1)，封顶 soft_rate_max。"""
        if streak <= 1:
            return d
        shift = min(streak - 1, SOFT_STREAK_SHIFT_MAX)
        d = d * (1 << shift)
        max_d = self._soft_rate_max_or()
        if d > max_d or d <= 0:
            d = max_d
        return d

    def _record_breaker_failure_locked(self, e: _Entry) -> None:
        """累计一次熔断失败，达到阈值触发指数退避熔断。"""
        e.fails += 1
        if e.fails < self.breaker_threshold:
            return
        d = self.breaker_cooldown
        for _ in range(e.retry_count):
            d *= 2.0
            if d >= self.breaker_cooldown_max:
                d = self.breaker_cooldown_max
                break
        e.fails = 0
        e.retry_count += 1
        now = self.now_fn()
        e.breaker_until = now + d

    @staticmethod
    def _next_4am(now: float) -> float:
        """返回本地时区下一个 04:00 的 epoch。"""
        dt = datetime.fromtimestamp(now)
        if dt.hour < 4:
            target = dt.replace(hour=4, minute=0, second=0, microsecond=0)
        else:
            target = (dt + timedelta(days=1)).replace(hour=4, minute=0, second=0, microsecond=0)
        return target.timestamp()

    def cooldown_until_tomorrow_4am(self, uid: str, reason: str) -> None:
        """硬冷却到下一个 04:00（本地时区）。"""
        now = self.now_fn()
        next_4am_ts = self._next_4am(now)
        seconds = max(0.0, next_4am_ts - now)
        self.cooldown(uid, "hard_credit", seconds, reason)

    def disable(self, uid: str, reason: str) -> None:
        """永久禁用账号。"""
        with self._lock:
            e = self._by_uid.get(uid)
            if e:
                e.disabled = True
                e.reason = reason
                self._dirty = True

    def note_session_dead(self, uid: str) -> bool:
        """记录一次 12153 错误，连续 3 次才判死禁用。"""
        with self._lock:
            e = self._by_uid.get(uid)
            if not e:
                return False
            e.session_dead_fails += 1
            if e.session_dead_fails < SESSION_DEAD_THRESHOLD:
                return False
            e.disabled = True
            e.reason = SESSION_DEAD_REASON
            e.session_dead_fails = 0
            self._dirty = True
            return True

    def clear_session_dead(self, uid: str) -> None:
        """清除连续 12153 计数。"""
        with self._lock:
            e = self._by_uid.get(uid)
            if e:
                e.session_dead_fails = 0

    def revive_disabled(self, uid: str) -> None:
        """手工或接口复活禁用账号。"""
        with self._lock:
            e = self._by_uid.get(uid)
            if e and e.disabled:
                e.disabled = False
                e.reason = ""
                e.session_dead_fails = 0
                self._dirty = True

    def reenable_if_credits(self, uid: str, remain: int) -> None:
        """签到成功后解冻：remain > 0 且未禁用时清除冷却域（不动熔断器）。"""
        with self._lock:
            e = self._by_uid.get(uid)
            if e:
                if remain > 0 and not e.disabled:
                    e.credits = remain
                    e.until = 0.0
                    e.cool_kind = ""
                    e.reason = ""
                    e.soft_streak = 0
                    e.soft_rate_model = ""
                else:
                    e.credits = remain
                self._dirty = True

    def note_error(self, uid: str) -> None:
        """记录一次普通错误：递增 err_total，喂入熔断器。"""
        with self._lock:
            e = self._by_uid.get(uid)
            if e:
                e.err_total += 1
                e.last_err = self.now_fn()
                self._record_breaker_failure_locked(e)
                self._dirty = True

    def note_success(self, uid: str) -> None:
        """请求成功：刷新计数并清空熔断状态、软退避指数和 12153 计数。"""
        with self._lock:
            e = self._by_uid.get(uid)
            if e:
                e.success_count += 1
                e.last_success = self.now_fn()
                e.fails = 0
                e.retry_count = 0
                e.breaker_until = 0.0
                e.soft_streak = 0
                e.session_dead_fails = 0
                self._dirty = True

    # ---------------------------------------------------------------------------
    # 查询与观测
    # ---------------------------------------------------------------------------

    def status(self, uid: str) -> AccountStatus | None:
        """查询单账号状态。"""
        with self._lock:
            e = self._by_uid.get(uid)
            if not e:
                return None
            return self._status_of(uid, e)

    def auth_by_uid(self, uid: str) -> Account | None:
        """获取账号完整凭证。"""
        with self._lock:
            e = self._by_uid.get(uid)
            return e.account if e else None

    def peek_by_uid(self, uid: str) -> Account | None:
        """只读获取凭证（不记录使用时刻、不检查健康）。"""
        with self._lock:
            e = self._by_uid.get(uid)
            return e.account if e else None

    def pick_by_uid(self, uid: str) -> Account | None:
        """精准选择指定 uid 账号（健康且未满在途则记录 last_used 并返回）。"""
        with self._lock:
            e = self._by_uid.get(uid)
            if not e:
                return None
            now = self.now_fn()
            if not self._healthy(e, now):
                return None
            if self._in_flight_full(e):
                return None
            e.last_used = now
            return e.account

    def available_uids(self) -> list[str]:
        """返回当前 healthy 且未占满在途名额的 UID 列表（按 UID 升序）。"""
        with self._lock:
            now = self.now_fn()
            uids = [
                uid
                for uid, e in self._by_uid.items()
                if self._healthy(e, now) and not self._in_flight_full(e)
            ]
            uids.sort()
            return uids

    def counts_detailed(self) -> tuple[int, int, int, int, int]:
        """返回 (total, healthy, cooling, disabled, in_flight_full) 详细计数。"""
        with self._lock:
            now = self.now_fn()
            total = len(self._by_uid)
            healthy = 0
            cooling = 0
            disabled = 0
            in_flight_full = 0
            for e in self._by_uid.values():
                if e.disabled:
                    disabled += 1
                elif not self._healthy(e, now):
                    cooling += 1
                else:
                    healthy += 1
                    if self._in_flight_full(e):
                        in_flight_full += 1
            return total, healthy, cooling, disabled, in_flight_full

    def servable_now(self) -> bool:
        """报告池当前是否可服务：至少存在一个 healthy 且未占满在途名额的账号。"""
        with self._lock:
            now = self.now_fn()
            for e in self._by_uid.values():
                if self._healthy(e, now) and not self._in_flight_full(e):
                    return True
            return False

    def list(self) -> list[AccountStatus]:
        """返回所有账号状态（按 UID 升序）。"""
        with self._lock:
            uids = sorted(self._by_uid.keys())
            return [self._status_of(uid, self._by_uid[uid]) for uid in uids]

    def model_soft_cooldown(self, uid: str) -> tuple[str, bool]:
        """查询指定账号当前的模型级冷却信息 (触发模型, 是否生效)。"""
        with self._lock:
            e = self._by_uid.get(uid)
            if not e:
                return "", False
            return e.soft_rate_model, bool(e.soft_rate_model)

    def _status_of(self, uid: str, e: _Entry) -> AccountStatus:
        now = self.now_fn()
        cooling = (e.until > 0 and now < e.until) or (e.breaker_until > 0 and now < e.breaker_until)
        cool_rem = 0
        cool_kind = ""
        if cooling:
            if e.until > now:
                cool_rem = math.ceil(e.until - now)
            cool_kind = e.cool_kind

        disabled_reason = e.reason if e.disabled else ""
        return AccountStatus(
            uid=uid,
            nickname=e.account.nickname,
            credits=e.credits,
            cooling=cooling,
            cool_kind=cool_kind,
            cool_remaining_sec=max(0, cool_rem),
            until=iso(e.until),
            reason=e.reason,
            soft_streak=e.soft_streak,
            soft_rate_model=e.soft_rate_model,
            disabled=e.disabled,
            disabled_reason=disabled_reason,
            success_count=e.success_count,
            err_total=e.err_total,
            last_success=iso(e.last_success),
            last_err=iso(e.last_err),
            in_flight=e.in_flight,
            breaker_fails=e.fails,
            breaker_until=iso(e.breaker_until),
        )

    # ---------------------------------------------------------------------------
    # 持久化与后台刷新
    # ---------------------------------------------------------------------------

    def load(self) -> None:
        """从磁盘加载 state.json。"""
        with self._lock:
            if not self.state_file or not os.path.exists(self.state_file):
                return
            try:
                with open(self.state_file, "r", encoding="utf-8") as f:
                    raw = f.read()
                if not raw.strip():
                    return
                doc = json.loads(raw)
                accounts = doc.get("accounts", {})
                self._apply_accounts_locked(accounts)
            except Exception as exc:
                logger.warning("pool: 加载 state.json 失败: %s", exc)

    def _apply_accounts_locked(self, accounts: dict[str, Any]) -> None:
        for uid, s in accounts.items():
            if not isinstance(s, dict):
                continue
            err_total = int(s.get("err_total", 0))
            err_count = int(s.get("err_count", 0))
            if err_count > err_total:
                err_total = err_count

            cool_kind_raw = s.get("cool_kind", "")
            if cool_kind_raw == 0 or cool_kind_raw == "0":
                cool_kind = "hard_credit"
            elif cool_kind_raw == 1 or cool_kind_raw == "1":
                cool_kind = "soft_rate"
            elif isinstance(cool_kind_raw, str):
                cool_kind = cool_kind_raw
            else:
                cool_kind = ""

            acc = self._by_uid[uid].account if uid in self._by_uid else Account(uid=uid)
            e = _Entry(
                account=acc,
                credits=int(s.get("credits", 0)),
                disabled=bool(s.get("disabled", False)),
                reason=str(s.get("reason", "")),
                until=_parse_ts(s.get("until")),
                cool_kind=cool_kind,
                success_count=int(s.get("success_count", 0)),
                err_total=err_total,
                last_err=_parse_ts(s.get("last_err")),
                last_success=_parse_ts(s.get("last_success")),
                soft_streak=int(s.get("soft_streak", 0)),
            )
            self._by_uid[uid] = e

    def flush(self) -> None:
        """同步落盘（仅脏数据落盘）。"""
        with self._lock:
            if self._dirty:
                self._dirty = False
                self._save_locked()

    def _save_locked(self) -> None:
        if not self.state_file:
            return
        sf = self._state_overview_locked()
        try:
            raw = json.dumps(sf, ensure_ascii=False, indent=2) + "\n"
            dir_name = os.path.dirname(self.state_file)
            if dir_name:
                os.makedirs(dir_name, exist_ok=True)
            tmp = self.state_file + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(raw)
            try:
                os.chmod(tmp, 0o600)
            except OSError:
                pass
            os.replace(tmp, self.state_file)
            if self._persist_fails > 0:
                logger.info("pool: state.json 落盘恢复（此前连续失败 %d 次）", self._persist_fails)
                self._persist_fails = 0
        except Exception as exc:
            self._note_persist_fail(exc)

    def _note_persist_fail(self, exc: Exception) -> None:
        if self._persist_fails == 0:
            logger.warning("pool: state.json 落盘失败: %s", exc)
        elif self._persist_fails % PERSIST_LOG_EVERY == 0:
            logger.warning("pool: state.json 连续落盘失败 %d 次: %s", self._persist_fails, exc)
        self._persist_fails += 1

    def _state_overview_locked(self) -> dict[str, Any]:
        accounts: dict[str, Any] = {}
        for uid, e in self._by_uid.items():
            acc_dict: dict[str, Any] = {
                "credits": e.credits,
                "disabled": e.disabled,
                "cool_kind": e.cool_kind,
                "success_count": e.success_count,
                "err_total": e.err_total,
                "soft_streak": e.soft_streak,
            }
            if e.reason:
                acc_dict["reason"] = e.reason
            if e.until > 0:
                acc_dict["until"] = iso(e.until)
            if e.last_success > 0:
                acc_dict["last_success"] = iso(e.last_success)
            if e.last_err > 0:
                acc_dict["last_err"] = iso(e.last_err)
            accounts[uid] = acc_dict
        return {"accounts": accounts}

    def start_flusher(self, interval: float = 5.0) -> None:
        """启动后台周期落盘线程。"""
        with self._lock:
            if self._flusher_thread is not None and self._flusher_thread.is_alive():
                return
            self._flusher_stop.clear()
            self._flusher_thread = threading.Thread(
                target=self._flusher_loop,
                args=(interval,),
                daemon=True,
                name="wb2api-pool-flusher",
            )
            self._flusher_thread.start()

    def _flusher_loop(self, interval: float) -> None:
        while not self._flusher_stop.wait(interval):
            self.flush()

    def stop_flusher(self) -> None:
        """停止后台刷新线程。"""
        self._flusher_stop.set()
        with self._lock:
            thread = self._flusher_thread
            self._flusher_thread = None
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=2.0)

    def __del__(self) -> None:
        try:
            self.stop_flusher()
        except Exception:
            pass
