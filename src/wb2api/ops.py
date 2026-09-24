"""业务操作层（ops）：聚合账号池状态、磁盘凭证、上游操作与异步任务。

对齐 Go 实现：
- workbuddy2api-gui/internal/ops/ops.go
- workbuddy2api-gui/internal/ops/loginflow.go
- workbuddy2api-gui/internal/ops/configfile.go
- workbuddy2api-gui/internal/ops/tasks.go
所有操作全面支持热加载，移除 Docker 与容器重启逻辑。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .app_state import AppState
from .auth_store import parse as parse_auth, valid_uid
from .models import Account, AccountStatus, LoginSession, TaskItem, TaskView, iso, now_iso
from .pricing import Cost, ModelPrice, Usage
from .tasks import Task
from .upstream import CheckinResult, Credits, TravelResult, UpstreamError, supports_checkin

logger = logging.getLogger("wb2api.ops")

# 批量任务账号间间隔，减轻上游瞬时压力
CHECKIN_ACCOUNT_DELAY = 0.2
TRAVEL_ACCOUNT_DELAY = 0.8


class ReadOnlyError(Exception):
    """服务端处于只读模式错误。"""

    code = "read_only"

    def __init__(self, message: str = "服务端已开启只读模式，写操作被禁用") -> None:
        super().__init__(message)


class DangerousDisabledError(Exception):
    """高危操作未开启错误。"""

    code = "dangerous_ops_disabled"

    def __init__(self, message: str = "该操作属高危动作，需在服务端配置开启 dangerous_ops 后才能执行") -> None:
        super().__init__(message)


def short_uid(uid: str) -> str:
    """缩写 uid 用于日志与展示。"""
    return uid[:8] if len(uid) > 8 else uid


def _parse_range_query(
    range_val: str | None,
    from_val: str | None,
    to_val: str | None,
    interval_val: str | None,
    model_val: str | None,
) -> tuple[float | None, float | None, str, str, bool]:
    """解析时间范围参数。支持 today|yesterday|7d|30d|90d|all 与 RFC3339/ISO 时间。"""
    interval = (interval_val or "hour").strip().lower()
    model = (model_val or "").strip()

    from_ts: float | None = None
    to_ts: float | None = None
    has_range = False

    if from_val and from_val.strip():
        try:
            dt = datetime.fromisoformat(from_val.strip().replace("Z", "+00:00"))
            from_ts = dt.timestamp()
            has_range = True
        except Exception:
            pass

    if to_val and to_val.strip():
        try:
            dt = datetime.fromisoformat(to_val.strip().replace("Z", "+00:00"))
            to_ts = dt.timestamp()
            has_range = True
        except Exception:
            pass

    if not has_range:
        now = datetime.now()
        r = (range_val or "").strip().lower()
        if r == "today":
            start_today = datetime(now.year, now.month, now.day)
            from_ts = start_today.timestamp()
            to_ts = (start_today + timedelta(days=1)).timestamp()
            has_range = True
        elif r == "yesterday":
            start_today = datetime(now.year, now.month, now.day)
            to_ts = start_today.timestamp()
            from_ts = (start_today - timedelta(days=1)).timestamp()
            has_range = True
        elif r in ("7d", "week"):
            from_ts = (now - timedelta(days=7)).timestamp()
            has_range = True
        elif r in ("30d", "month"):
            from_ts = (now - timedelta(days=30)).timestamp()
            has_range = True
        elif r == "90d":
            from_ts = (now - timedelta(days=90)).timestamp()
            has_range = True
        elif r == "all":
            has_range = True
        else:
            has_range = bool(interval_val and interval_val.strip()) or bool(model)

    return from_ts, to_ts, interval, model, has_range


class Service:
    """控制台业务服务层。持有 AppState 引用，提供账号管理、配置读写与任务调度。"""

    def __init__(self, state: AppState) -> None:
        self.state = state
        self._credits_cache: dict[str, tuple[Credits, float]] = {}
        self._credits_lock = threading.RLock()

    # ------------------------------------------------------------------
    # 权限检查
    # ------------------------------------------------------------------

    def ensure_writable(self) -> None:
        """写操作前置校验：只读模式下抛出 ReadOnlyError。"""
        if self.state.cfg.console.read_only:
            raise ReadOnlyError()

    def ensure_dangerous(self) -> None:
        """高危操作前置校验：需同时满足非只读且 dangerous_ops=True。"""
        self.ensure_writable()
        if not self.state.cfg.console.dangerous_ops:
            raise DangerousDisabledError()

    # ------------------------------------------------------------------
    # 积分缓存辅助
    # ------------------------------------------------------------------

    def _set_credit_cache(self, uid: str, c: Credits) -> None:
        with self._credits_lock:
            self._credits_cache[uid] = (c, time.time())

    # ------------------------------------------------------------------
    # 仪表盘总览与账号合并视图
    # ------------------------------------------------------------------

    def overview(self) -> dict[str, Any]:
        """仪表盘数据总览：聚合账号统计、网关状态、积分汇总与异常提示。"""
        accounts, file_issues = self.accounts()
        total, healthy, cooling, disabled, in_flight_full = self.state.pool.counts_detailed()
        port = self.state.cfg.host_port[1]

        expired = 0
        expiring = 0
        in_flight = 0
        warnings: list[str] = []

        credits_total: dict[str, Any] = {
            "remain": 0,
            "used": 0,
            "size": 0,
            "accounts": 0,
            "ok": 0,
            "failed": 0,
        }

        for a in accounts:
            in_flight += int(a.get("in_flight", 0))
            if a.get("expired"):
                expired += 1
            elif a.get("needs_refresh"):
                expiring += 1

            if not a.get("has_file") and a.get("in_gateway"):
                warnings.append(f"账号 {short_uid(str(a.get('uid')))} 在网关池中但磁盘无凭证文件，可点「从磁盘同步」刷新")
            if a.get("has_file") and not a.get("in_gateway"):
                warnings.append(f"账号 {short_uid(str(a.get('uid')))} 有凭证文件但不在网关池中，需重新同步或热加载")

            credits_total["accounts"] += 1
            if a.get("live_credits") is not None:
                credits_total["remain"] += int(a["live_credits"])
                credits_total["ok"] += 1
            elif a.get("in_gateway"):
                credits_total["remain"] += int(a.get("credits", 0))
                credits_total["ok"] += 1
            else:
                credits_total["failed"] += 1

        total_size = 0
        total_used = 0
        with self._credits_lock:
            for c, _ in self._credits_cache.values():
                total_size += c.size
                total_used += c.used

        credits_total["size"] = total_size
        credits_total["used"] = total_used
        credits_total["failed"] = credits_total["accounts"] - credits_total["ok"]

        return {
            "gateway_url": f"http://127.0.0.1:{port}",
            "gateway_ok": True,
            "health": {
                "healthy": healthy,
                "total": total,
                "service": "workbuddy2api",
            },
            "total": total,
            "healthy": healthy,
            "cooling": cooling,
            "disabled": disabled,
            "in_flight_full": in_flight_full,
            "in_flight": in_flight,
            "sticky_sessions": self.state.sticky.count(),
            "redis_mode": "noop",
            "file_count": len([a for a in accounts if a.get("has_file")]),
            "expired": expired,
            "expiring": expiring,
            "warnings": warnings,
            "file_issues": file_issues,
            "credits": credits_total,
            "read_only": self.state.cfg.console.read_only,
            "dangerous_ops": self.state.cfg.console.dangerous_ops,
            "server_time": now_iso(),
        }

    def accounts(self) -> tuple[list[dict[str, Any]], list[str]]:
        """返回「磁盘凭证 ∪ 内存池运行态」的合并账号列表与文件警告。"""
        disk_accounts, file_issues = self.state.store.list()
        pool_status_list = self.state.pool.list()

        by_uid: dict[str, dict[str, Any]] = {}
        for a in disk_accounts:
            file_name = os.path.basename(a.file_path) if a.file_path else f"workbuddy-{a.uid}.json"
            by_uid[a.uid] = {
                "uid": a.uid,
                "nickname": a.nickname,
                "enterprise_id": a.enterprise_id,
                "domain": a.domain,
                "has_file": True,
                "file_name": file_name,
                "expires_at": a.expires_at,
                "expired": a.expired(),
                "needs_refresh": a.needs_refresh(600),
                "has_refresh_token": bool(a.refresh_token and a.refresh_token.strip()),
                "in_gateway": False,
                "status": "unknown",
                "cooling": False,
                "cool_kind": "",
                "cool_remaining_sec": 0,
                "disabled": False,
                "reason": "",
                "in_flight": 0,
                "breaker_fails": 0,
                "breaker_until": "",
                "soft_streak": 0,
                "success_count": 0,
                "err_total": 0,
                "last_success": "",
                "last_err": "",
                "credits": 0,
            }

        for ga in pool_status_list:
            v = by_uid.get(ga.uid)
            if not v:
                v = {
                    "uid": ga.uid,
                    "nickname": ga.nickname,
                    "enterprise_id": "",
                    "domain": "",
                    "has_file": False,
                    "file_name": "",
                    "expires_at": 0,
                    "expired": False,
                    "needs_refresh": False,
                    "has_refresh_token": False,
                    "in_gateway": True,
                    "status": "unknown",
                }
                by_uid[ga.uid] = v

            v["in_gateway"] = True
            v["cooling"] = ga.cooling
            v["cool_kind"] = ga.cool_kind
            v["cool_remaining_sec"] = ga.cool_remaining_sec
            v["disabled"] = ga.disabled
            v["reason"] = ga.reason
            v["in_flight"] = ga.in_flight
            v["breaker_fails"] = ga.breaker_fails
            v["breaker_until"] = ga.breaker_until
            v["soft_streak"] = ga.soft_streak
            v["success_count"] = ga.success_count
            v["err_total"] = ga.err_total
            v["last_success"] = ga.last_success
            v["last_err"] = ga.last_err
            v["credits"] = ga.credits
            if not v.get("nickname") and ga.nickname:
                v["nickname"] = ga.nickname

            if ga.disabled:
                v["status"] = "disabled"
            elif ga.cooling:
                v["status"] = "cooling"
            else:
                v["status"] = "healthy"

        with self._credits_lock:
            for uid, (c, at_ts) in self._credits_cache.items():
                if uid in by_uid:
                    by_uid[uid]["live_credits"] = c.remain
                    by_uid[uid]["credits_at"] = iso(at_ts)

        for v in by_uid.values():
            if v["status"] == "unknown":
                if not v["has_file"]:
                    v["status"] = "missing_credential"
                elif v["expired"]:
                    v["status"] = "token_expired"

        out = list(by_uid.values())
        out.sort(key=lambda x: (str(x.get("nickname") or ""), str(x.get("uid") or "")))
        return out, file_issues

    # ------------------------------------------------------------------
    # 单账号操作
    # ------------------------------------------------------------------

    async def _refresh_account(self, account: Account) -> None:
        """刷新 token 并原子落盘、热加入内存池。"""
        await self.state.upstream.refresh_token(account)
        self.state.store.save(account)
        self.state.pool.add(account)

    async def profile(self, uid: str, silent: bool = False) -> dict[str, Any]:
        """返回单账号详情；silent=True 时仅返回合并视图，不调用上游。"""
        accts, _ = self.accounts()
        target = next((a for a in accts if a["uid"] == uid), None)
        if target is None:
            raise FileNotFoundError(f"账号不存在: {uid}")

        out: dict[str, Any] = {"account": target}
        if silent:
            return out

        try:
            acct = self.state.store.get(uid)
        except Exception as e:
            out["upstream_error"] = f"读取凭证失败: {e}"
            return out

        if acct.needs_refresh(600):
            try:
                await self._refresh_account(acct)
            except Exception as e:
                out["upstream_error"] = f"刷新 token 失败: {e}"
                return out

        try:
            c = await self.state.upstream.user_resource(acct)
            self._set_credit_cache(uid, c)
            self.state.pool.reenable_if_credits(uid, c.remain)
            out["credits"] = {
                "remain": c.remain,
                "used": c.used,
                "size": c.size,
                "packages": c.packages,
            }
        except Exception as e:
            out["credits_error"] = str(e)

        if supports_checkin(acct):
            try:
                b = await self.state.upstream.buddy_info(acct)
                out["buddy"] = {"id": b.id, "name": b.name} if b else None
            except Exception as e:
                out["buddy_error"] = str(e)

            try:
                ts = await self.state.upstream.travel_status(acct)
                out["travel"] = {
                    "state": ts.state,
                    "daily_limit_reached": ts.daily_limit_reached,
                    "record_id": ts.record_id,
                    "reward_credit": ts.reward_credit,
                }
            except Exception as e:
                out["travel_error"] = str(e)
        else:
            # 国际版无成长中心接口：不发起请求，明确标注不支持。
            out["buddy"] = None
            out["travel_unsupported"] = True

        return out

    async def checkin(self, uid: str) -> dict[str, Any]:
        """单账号签到：必要时先刷新 token，签到后同步刷新余额。"""
        self.ensure_writable()
        acct = self.state.store.get(uid)
        res: dict[str, Any] = {"uid": uid, "action": "checkin", "ok": True, "message": ""}

        # 国际版无签到接口：直接返回提示，不发请求。
        if not supports_checkin(acct):
            return {
                "uid": uid,
                "action": "checkin",
                "ok": False,
                "message": "该档位（国际版 workbuddy.ai）暂不支持签到",
            }

        if acct.needs_refresh(600):
            try:
                await self._refresh_account(acct)
            except Exception as e:
                return {"uid": uid, "action": "checkin", "ok": False, "message": f"刷新 token 失败: {e}"}

        try:
            cr = await self.state.upstream.daily_checkin(acct)
            res["message"] = "今日已签到" if cr.already else (cr.message or "签到成功")
        except Exception as e:
            res["ok"] = False
            res["message"] = f"签到失败: {e}"

        try:
            c = await self.state.upstream.user_resource(acct)
            self._set_credit_cache(uid, c)
            self.state.pool.reenable_if_credits(uid, c.remain)
            res["data"] = {
                "credits": {
                    "remain": c.remain,
                    "used": c.used,
                    "size": c.size,
                    "packages": c.packages,
                }
            }
        except Exception:
            pass

        return res

    async def refresh(self, uid: str) -> dict[str, Any]:
        """刷新单账号 token 并落盘与热应用。"""
        self.ensure_writable()
        acct = self.state.store.get(uid)
        before = acct.expires_at

        try:
            await self._refresh_account(acct)
        except Exception as e:
            return {"uid": uid, "action": "refresh", "ok": False, "message": str(e)}

        dt_str = datetime.fromtimestamp(acct.expires_at, tz=timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")
        return {
            "uid": uid,
            "action": "refresh",
            "ok": True,
            "message": f"token 已刷新，有效期至 {dt_str}",
            "data": {
                "expires_at": acct.expires_at,
                "expires_before": before,
                "expires_at_human": iso(acct.expires_at),
            },
        }

    async def travel(self, uid: str) -> dict[str, Any]:
        """推进单账号猫猫旅行一趟。"""
        self.ensure_writable()
        acct = self.state.store.get(uid)
        # 国际版无成长中心接口：直接返回提示，不发请求。
        if not supports_checkin(acct):
            return {
                "uid": uid,
                "action": "travel",
                "ok": False,
                "message": "该档位（国际版 workbuddy.ai）暂不支持猫猫旅行",
            }
        if acct.needs_refresh(600):
            try:
                await self._refresh_account(acct)
            except Exception as e:
                return {"uid": uid, "action": "travel", "ok": False, "message": f"刷新 token 失败: {e}"}

        try:
            tr = await self.state.upstream.travel_once(acct)
        except Exception as e:
            return {"uid": uid, "action": "travel", "ok": False, "message": str(e)}

        ok = tr.action != "error"
        return {
            "uid": uid,
            "action": "travel",
            "ok": ok,
            "message": tr.message,
            "reward": tr.reward,
            "data": {"travel_action": tr.action, "buddy": tr.buddy},
        }

    async def credits_for(self, uid: str) -> dict[str, Any]:
        """查询单账号积分并更新缓存。"""
        acct = self.state.store.get(uid)
        if acct.needs_refresh(600):
            await self._refresh_account(acct)

        c = await self.state.upstream.user_resource(acct)
        self._set_credit_cache(uid, c)
        self.state.pool.reenable_if_credits(uid, c.remain)
        return {
            "remain": c.remain,
            "used": c.used,
            "size": c.size,
            "packages": c.packages,
        }

    def import_account(self, payload: dict[str, Any]) -> dict[str, Any]:
        """导入凭证：支持原始 JSON 或零散字段，落盘后立即热加载入池。"""
        self.ensure_writable()
        raw_json = str(payload.get("raw_json") or "").strip()
        if raw_json:
            try:
                account = parse_auth(raw_json)
            except Exception as e:
                return {"action": "import", "ok": False, "message": f"凭证解析失败: {e}"}
        else:
            access_token = str(payload.get("access_token") or "").strip()
            if not access_token:
                return {"action": "import", "ok": False, "message": "accessToken 不能为空"}
            account = Account(
                access_token=access_token,
                refresh_token=str(payload.get("refresh_token") or "").strip(),
                uid=str(payload.get("uid") or "").strip(),
                nickname=str(payload.get("nickname") or "").strip(),
                enterprise_id=str(payload.get("enterprise_id") or "").strip(),
                domain=str(payload.get("domain") or "").strip(),
                expires_at=int(payload.get("expires_at") or 0),
            )

        if not account.uid:
            return {"action": "import", "ok": False, "message": "缺少 uid：无法确定凭证文件名，请补全 uid 字段"}
        if not valid_uid(account.uid):
            return {"action": "import", "ok": False, "message": f"uid 含非法字符（仅允许字母数字 . _ -）：{account.uid!r}"}
        if not account.domain:
            account.domain = "copilot.tencent.com"
        if account.expires_at <= 0:
            account.expires_at = 0

        try:
            self.state.store.save(account)
            self.state.add_account_live(account)
        except Exception as e:
            return {"uid": account.uid, "action": "import", "ok": False, "message": f"保存失败: {e}"}

        file_name = os.path.basename(account.file_path) if account.file_path else f"workbuddy-{account.uid}.json"
        return {
            "uid": account.uid,
            "action": "import",
            "ok": True,
            "message": f"凭证已保存到 {file_name}，并已热加载到账号池，无需重启",
            "data": {"file": file_name},
        }

    def delete_account(self, uid: str) -> None:
        """删除凭证并从账号池中移出（高危操作）。"""
        self.ensure_dangerous()
        self.state.remove_account_live(uid)

    def sync_accounts(self) -> dict[str, Any]:
        """从磁盘重新扫描凭证并原地同步到账号池（新增/移除），不重启进程。

        用途：凭证文件由外部（login.sh / 另一台机器拷贝 / 手工放置）写入或删除后，
        运行中的网关不会自动感知；调用本方法即可热同步。
        注意：这是「读取磁盘」操作，不受 read_only 限制（不写文件）。
        """
        before = {st.uid for st in self.state.pool.list()}
        accounts, warnings = self.state.sync_accounts()
        after = {st.uid for st in self.state.pool.list()}
        added = sorted(after - before)
        removed = sorted(before - after)
        parts = []
        if added:
            parts.append(f"新增 {len(added)} 个")
        if removed:
            parts.append(f"移除 {len(removed)} 个")
        summary = "、".join(parts) if parts else "无变化"
        return {
            "ok": True,
            "added": added,
            "removed": removed,
            "total": len(after),
            "file_issues": warnings,
            "message": f"已从磁盘同步账号池：{summary}（当前共 {len(after)} 个），无需重启",
        }

    # ------------------------------------------------------------------
    # 批量操作与异步任务
    # ------------------------------------------------------------------

    def target_accounts(self, uids: list[str] | None = None) -> list[Account]:
        """按给定 uid 列表过滤账号，为空则返回全量磁盘账号。"""
        if uids:
            out: list[Account] = []
            for uid in uids:
                uid_str = str(uid).strip()
                if not uid_str:
                    continue
                out.append(self.state.store.get(uid_str))
            return out
        accounts, _ = self.state.store.list()
        return accounts

    def batch_checkin(self, uids: list[str] | None = None) -> dict[str, Any]:
        """批量签到异步任务。"""
        self.ensure_writable()
        targets = self.target_accounts(uids)
        title = f"批量签到（{len(targets)} 个账号）"

        async def runner(task: Task) -> None:
            for i, a in enumerate(targets):
                if i > 0:
                    await asyncio.sleep(CHECKIN_ACCOUNT_DELAY)
                start_iso = now_iso()
                ok = True
                msg = ""
                if a.needs_refresh(600):
                    try:
                        await self._refresh_account(a)
                    except Exception as e:
                        task.add_item(
                            TaskItem(
                                uid=a.uid,
                                nickname=a.nickname,
                                action="checkin",
                                ok=False,
                                message=f"刷新 token 失败: {e}",
                                started_at=start_iso,
                                ended_at=now_iso(),
                            )
                        )
                        continue

                try:
                    cr = await self.state.upstream.daily_checkin(a)
                    msg = "今日已签到" if cr.already else (cr.message or "签到成功")
                except Exception as e:
                    ok = False
                    msg = str(e)

                try:
                    c = await self.state.upstream.user_resource(a)
                    self._set_credit_cache(a.uid, c)
                    self.state.pool.reenable_if_credits(a.uid, c.remain)
                    msg += f"｜余额 {c.remain}"
                except Exception:
                    pass

                task.add_item(
                    TaskItem(
                        uid=a.uid,
                        nickname=a.nickname,
                        action="checkin",
                        ok=ok,
                        message=msg,
                        started_at=start_iso,
                        ended_at=now_iso(),
                    )
                )

        return self.state.tasks.new("checkin", title, runner)

    def batch_refresh(self, uids: list[str] | None = None) -> dict[str, Any]:
        """批量刷新 Token 异步任务。"""
        self.ensure_writable()
        targets = self.target_accounts(uids)
        title = f"批量刷新 Token（{len(targets)} 个账号）"

        async def runner(task: Task) -> None:
            for i, a in enumerate(targets):
                if i > 0:
                    await asyncio.sleep(CHECKIN_ACCOUNT_DELAY)
                start_iso = now_iso()
                ok = True
                try:
                    await self._refresh_account(a)
                    dt_str = datetime.fromtimestamp(a.expires_at, tz=timezone(timedelta(hours=8))).strftime(
                        "%Y-%m-%d %H:%M:%S"
                    )
                    msg = f"有效期至 {dt_str}"
                except Exception as e:
                    ok = False
                    msg = str(e)

                task.add_item(
                    TaskItem(
                        uid=a.uid,
                        nickname=a.nickname,
                        action="refresh",
                        ok=ok,
                        message=msg,
                        started_at=start_iso,
                        ended_at=now_iso(),
                    )
                )

        return self.state.tasks.new("refresh", title, runner)

    def batch_travel(self, uids: list[str] | None = None) -> dict[str, Any]:
        """批量猫猫旅行异步任务。"""
        self.ensure_writable()
        targets = self.target_accounts(uids)
        title = f"批量猫猫旅行（{len(targets)} 个账号）"

        async def runner(task: Task) -> None:
            for i, a in enumerate(targets):
                if i > 0:
                    await asyncio.sleep(TRAVEL_ACCOUNT_DELAY)
                start_iso = now_iso()
                if a.needs_refresh(600):
                    try:
                        await self._refresh_account(a)
                    except Exception as e:
                        task.add_item(
                            TaskItem(
                                uid=a.uid,
                                nickname=a.nickname,
                                action="travel",
                                ok=False,
                                message=f"刷新 token 失败: {e}",
                                started_at=start_iso,
                                ended_at=now_iso(),
                            )
                        )
                        continue

                try:
                    tr = await self.state.upstream.travel_once(a)
                    task.add_item(
                        TaskItem(
                            uid=a.uid,
                            nickname=a.nickname,
                            action="travel",
                            ok=tr.action != "error",
                            message=tr.message,
                            reward=tr.reward,
                            started_at=start_iso,
                            ended_at=now_iso(),
                        )
                    )
                except Exception as e:
                    task.add_item(
                        TaskItem(
                            uid=a.uid,
                            nickname=a.nickname,
                            action="travel",
                            ok=False,
                            message=str(e),
                            started_at=start_iso,
                            ended_at=now_iso(),
                        )
                    )

        return self.state.tasks.new("travel", title, runner)

    def batch_credits(self, uids: list[str] | None = None) -> dict[str, Any]:
        """批量查询积分异步任务。"""
        targets = self.target_accounts(uids)
        title = f"批量查询积分（{len(targets)} 个账号）"

        async def runner(task: Task) -> None:
            for i, a in enumerate(targets):
                if i > 0:
                    await asyncio.sleep(CHECKIN_ACCOUNT_DELAY)
                start_iso = now_iso()
                if a.needs_refresh(600):
                    try:
                        await self._refresh_account(a)
                    except Exception as e:
                        task.add_item(
                            TaskItem(
                                uid=a.uid,
                                nickname=a.nickname,
                                action="credits",
                                ok=False,
                                message=f"刷新 token 失败: {e}",
                                started_at=start_iso,
                                ended_at=now_iso(),
                            )
                        )
                        continue

                try:
                    c = await self.state.upstream.user_resource(a)
                    self._set_credit_cache(a.uid, c)
                    self.state.pool.reenable_if_credits(a.uid, c.remain)
                    msg = f"剩余 {c.remain} / 总量 {c.size}（{c.packages} 个套餐）"
                    task.add_item(
                        TaskItem(
                            uid=a.uid,
                            nickname=a.nickname,
                            action="credits",
                            ok=True,
                            message=msg,
                            started_at=start_iso,
                            ended_at=now_iso(),
                        )
                    )
                except Exception as e:
                    task.add_item(
                        TaskItem(
                            uid=a.uid,
                            nickname=a.nickname,
                            action="credits",
                            ok=False,
                            message=str(e),
                            started_at=start_iso,
                            ended_at=now_iso(),
                        )
                    )

        return self.state.tasks.new("credits", title, runner)

    # ------------------------------------------------------------------
    # 配置文件读写与回滚
    # ------------------------------------------------------------------

    def read_config(self) -> tuple[dict[str, Any], dict[str, Any]]:
        """读取当前配置及元信息。"""
        path = self.state.config_manager.path
        p = Path(path)
        meta: dict[str, Any] = {
            "path": path,
            "exists": p.is_file(),
            "size": 0,
            "is_valid_json": True,
            "restart_note": "账号与配置均支持热加载，无需重启",
        }
        backup_path = f"{path}.gui.bak"
        bp = Path(backup_path)
        if bp.is_file():
            meta["backup_path"] = backup_path
            meta["backup_at"] = iso(bp.stat().st_mtime)

        if p.is_file():
            st = p.stat()
            meta["size"] = st.st_size
            meta["mod_time"] = iso(st.st_mtime)
            try:
                raw = p.read_text(encoding="utf-8")
                if raw.strip():
                    json.loads(raw)
                meta["is_valid_json"] = True
            except Exception as e:
                meta["parse_error"] = str(e)
                meta["is_valid_json"] = False

        doc = self.state.cfg.public_dict()
        return doc, meta

    def save_config(self, doc: dict[str, Any]) -> tuple[bool, str, bool]:
        """保存配置、备份初版并热应用。"""
        self.ensure_writable()
        path = self.state.config_manager.path
        p = Path(path)
        backup_path = Path(f"{path}.gui.bak")

        if p.is_file() and not backup_path.exists():
            try:
                backup_path.parent.mkdir(parents=True, exist_ok=True)
                backup_path.write_bytes(p.read_bytes())
            except Exception as e:
                raise RuntimeError(f"写入备份失败（已中止，未改动原文件）: {e}") from e

        _new_cfg, listen_changed = self.state.write_config(doc)
        msg = "配置已保存并热加载生效"
        if listen_changed:
            msg += "。注意：监听地址变更需重启进程才生效"
        return True, msg, False

    def reset_config(self) -> None:
        """从备份恢复初始配置（高危操作）。"""
        self.ensure_dangerous()
        path = self.state.config_manager.path
        backup_path = Path(f"{path}.gui.bak")
        if not backup_path.is_file():
            raise FileNotFoundError("没有可用的备份文件（保存过一次配置后才会生成）")

        raw = backup_path.read_text(encoding="utf-8")
        try:
            doc = json.loads(raw)
        except Exception as e:
            raise ValueError(f"备份文件不是合法 JSON，拒绝恢复: {e}") from e

        self.state.write_config(doc)

    # ------------------------------------------------------------------
    # OAuth 登录
    # ------------------------------------------------------------------

    async def start_login(self, region: str = "cn") -> LoginSession:
        """发起网页授权登录。"""
        self.ensure_writable()
        return await self.state.logins.start(region)

    async def poll_login(self, id: str) -> LoginSession:
        """轮询登录状态：成功后自动落盘并热加入账号池。"""
        sess = await self.state.logins.poll(id)
        if sess.status != "success":
            return sess

        acct = self.state.logins.take_account(id)
        if acct is None:
            return sess

        file_name = f"workbuddy-{acct.uid}.json"
        try:
            self.state.store.save(acct)
            self.state.pool.add(acct)
            self.state.logins.mark_saved(id, file_name, "凭证已保存并热加载到账号池，无需重启")
        except Exception as e:
            self.state.logins.mark_saved(id, "", f"凭证保存失败: {e}")

        return self.state.logins.get(id)

    def login_status(self, id: str) -> LoginSession:
        """获取登录会话当前状态。"""
        return self.state.logins.get(id)

    def cancel_login(self, id: str) -> LoginSession:
        """取消登录会话。"""
        return self.state.logins.cancel(id)

    # ------------------------------------------------------------------
    # 价格与统计
    # ------------------------------------------------------------------

    def pricing_update(self, req: dict[str, Any]) -> None:
        """更新单个模型官方单价。"""
        self.ensure_writable()
        if not self.state.cfg.pricing_file:
            raise ValueError("服务端未配置价格表路径（pricing_file），无法保存")

        model = str(req.get("model") or "").strip()
        if not model:
            raise ValueError("模型名不能为空")

        cached_input = float(req.get("cached_input") or 0.0)
        miss_input = float(req.get("miss_input") or 0.0)
        output = float(req.get("output") or 0.0)
        if cached_input < 0 or miss_input < 0 or output < 0:
            raise ValueError("单价不能为负数")

        off_peak_ratio = float(req.get("off_peak_ratio") or 0.0)
        note = str(req.get("note") or "")

        price = ModelPrice(
            cached_input=cached_input,
            miss_input=miss_input,
            output=output,
            off_peak_ratio=off_peak_ratio,
            note=note,
        )
        self.state.pricing.set(model, price)
        self.state.pricing.save()

    def pricing_delete(self, model: str) -> None:
        """删除单个模型单价。"""
        self.ensure_writable()
        if not self.state.cfg.pricing_file:
            raise ValueError("服务端未配置价格表路径（pricing_file），无法保存")

        self.state.pricing.delete(model)
        self.state.pricing.save()

    def stats(
        self,
        mode: str = "peak",
        range_param: str | None = None,
        from_param: str | None = None,
        to_param: str | None = None,
        interval_param: str | None = None,
        model_param: str | None = None,
    ) -> dict[str, Any]:
        """返回聚合请求统计及官方计费换算。"""
        m = "offpeak" if (mode or "").lower() == "offpeak" else "peak"
        if not self.state.cfg.server.metrics_enabled:
            st = {
                "enabled": False,
                "message": "统计未启用（server.metrics_enabled=false）",
                "since": "",
                "now": now_iso(),
                "uptime_sec": 0,
                "total": {},
                "models": [],
            }
            models_list: list[dict[str, Any]] = []
        else:
            derived = self.state.metrics.derived()
            st = {
                "enabled": True,
                "since": derived["since"],
                "now": derived["now"],
                "uptime_sec": derived["uptime_sec"],
                "total": derived["total"],
                "models": derived["models"],
                "series_buckets": self.state.metrics.series_buckets(),
            }
            from_ts, to_ts, interval, model_filter, has_range = _parse_range_query(
                range_param, from_param, to_param, interval_param, model_param
            )
            if has_range:
                st["range"] = self.state.metrics.range_query(
                    from_ts=from_ts,
                    to_ts=to_ts,
                    interval=interval,
                    model=model_filter,
                )
            models_list = derived.get("models") or []

        table = self.state.pricing
        costs: dict[str, dict[str, Any]] = {}
        official_total = 0.0
        cached_cost = 0.0
        miss_cost = 0.0
        output_cost = 0.0
        priced_models: list[str] = []
        unpriced_models: list[str] = []

        for m_stat in models_list:
            m_name = m_stat.get("model", "")
            usage = Usage(
                prompt_tokens=m_stat.get("prompt_tokens", 0),
                cache_hit_tokens=m_stat.get("cache_hit_tokens", 0),
                cache_miss_tokens=m_stat.get("cache_miss_tokens", 0),
                completion_tokens=m_stat.get("completion_tokens", 0),
            )
            cost_obj = table.compute(m_name, usage, m)
            costs[m_name] = cost_obj.to_dict()
            if not cost_obj.priced:
                unpriced_models.append(m_name)
            else:
                priced_models.append(m_name)
                official_total += cost_obj.total
                cached_cost += cost_obj.cached_input_cost
                miss_cost += cost_obj.miss_input_cost
                output_cost += cost_obj.output_cost

        total_cost = {
            "model": "(all)",
            "priced": True,
            "cached_input_cost": cached_cost,
            "miss_input_cost": miss_cost,
            "output_cost": output_cost,
            "total": official_total,
            "cached_input_tokens": 0,
            "miss_input_tokens": 0,
            "output_tokens": 0,
        }

        models_dict = {k: v.to_dict() for k, v in table.models_copy().items()}

        return {
            "stats": st,
            "mode": m,
            "costs": costs,
            "total": total_cost,
            "priced": priced_models,
            "unpriced": unpriced_models,
            "pricing": {
                "models": models_dict,
                "source": "https://api-docs.deepseek.com/zh-cn/quick_start/pricing",
                "updated_at": "2026-09-14",
                "editable": bool(self.state.cfg.pricing_file),
            },
        }

    # ------------------------------------------------------------------
    # 系统信息
    # ------------------------------------------------------------------

    def system(self) -> dict[str, Any]:
        """返回系统运行状态（无 Docker 依赖）。"""
        port = self.state.cfg.host_port[1]
        total, healthy, _cooling, _disabled, _in_flight_full = self.state.pool.counts_detailed()
        uptime = int(time.time() - self.state.started_at) if self.state.started_at > 0 else 0

        return {
            "version": self.state.version,
            "started_at": iso(self.state.started_at),
            "uptime_sec": uptime,
            "read_only": self.state.cfg.console.read_only,
            "dangerous_ops": self.state.cfg.console.dangerous_ops,
            "auth_dir": self.state.cfg.auth_dir,
            "config_file": self.state.config_manager.path,
            "gateway_url": f"http://127.0.0.1:{port}",
            "using_default_password": self.state.cfg.using_default_password(),
            "docker_available": False,
            "hot_reload": True,
            "container": {
                "name": "",
                "available": False,
                "exists": False,
                "running": False,
                "status": "disabled",
                "health": "",
                "image": "",
                "disabled": True,
            },
            "gateway_health": {
                "healthy": healthy,
                "total": total,
                "service": "workbuddy2api",
            },
        }
