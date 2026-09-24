"""tests/test_upstream.py: 上游客户端、OAuth 管理器及调度器的单元测试。"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import json
from typing import Any

import httpx
import pytest

from wb2api.config import Config
from wb2api.models import Account, AccountStatus
from wb2api.oauth import LoginManager, normalize_region
from wb2api.scheduler import Scheduler, cst_day, next_fire
from wb2api.upstream import (
    CHAT_BASE_CN,
    CHAT_BASE_GLOBAL,
    ErrKind,
    ModelInfo,
    UpstreamClient,
    UpstreamError,
    classify,
    is_already_checked_in,
    is_buddy_task_incomplete,
    is_model_rate_limit,
    iter_lines_with_idle,
    package_remain_used,
    parse_soft_rate_reset,
)

# ---------------------------------------------------------------------------
# 1. 错误分类测试 (Classify ordering)
# ---------------------------------------------------------------------------


def test_classify_ordering() -> None:
    # 1.1 402 或 hard markers 优先
    assert classify(402, "any body") == ErrKind.hard_credit
    assert classify(200, "insufficient credit") == ErrKind.hard_credit
    assert classify(200, "额度用尽，请充值") == ErrKind.hard_credit
    # hard 优于 12153 和 soft-rate
    assert classify(401, "quota exceeded 12153 rate limit") == ErrKind.hard_credit

    # 1.2 12153 / session dead 次之
    assert classify(401, "Offline user session not found") == ErrKind.session_dead
    assert classify(401, '{"code":12153,"msg":"session dead"}') == ErrKind.session_dead
    # session_dead 优于 soft-rate
    assert classify(401, "12153 rate limit too many requests") == ErrKind.session_dead

    # 1.3 soft-rate (200 / 400 等含有限流文案)
    assert classify(200, "The model provider is rate-limiting requests.") == ErrKind.soft_rate
    assert classify(200, "请求过于频繁，请稍后再试") == ErrKind.soft_rate
    assert classify(400, "rate limit exceeded") == ErrKind.soft_rate

    # 1.4 状态码 429
    assert classify(429, "") == ErrKind.soft_rate
    assert classify(429, "plain error") == ErrKind.soft_rate

    # 1.5 状态码 404
    assert classify(404, "not found") == ErrKind.not_found

    # 1.6 5xx
    assert classify(500, "internal server error") == ErrKind.server
    assert classify(502, "bad gateway") == ErrKind.server

    # 1.7 400 下的 bad_params
    assert classify(400, "Unmarshal chat params failed") == ErrKind.bad_params
    assert classify(400, '{"code":11101,"msg":"err"}') == ErrKind.bad_params

    # 1.8 常规 4xx -> client
    assert classify(400, "invalid parameters") == ErrKind.client
    assert classify(403, "forbidden") == ErrKind.client

    # 1.9 正常 200 无 marker
    assert classify(200, '{"code":0,"data":{}}') == ErrKind.none


# ---------------------------------------------------------------------------
# 2. 模型限流与重置时间解析测试
# ---------------------------------------------------------------------------


def test_model_rate_limit_and_reset() -> None:
    assert is_model_rate_limit('{"code":6004,"msg":"limit"}')
    assert is_model_rate_limit('{"code": "6004"}')
    assert is_model_rate_limit('{"code":"6004"}')
    assert not is_model_rate_limit('{"code":10000}')

    # UTC+8 时间解析
    body_with_tz = '{"code":6004,"msg":"将在 2026-10-01 12:00:00 UTC+8 重置"}'
    body_no_tz = '{"code":6004,"msg":"将在 2026-10-01 12:00:00 重置"}'
    expected_ts = datetime(2026, 10, 1, 12, 0, 0, tzinfo=timezone(timedelta(hours=8))).timestamp()

    assert parse_soft_rate_reset(body_with_tz) == expected_ts
    assert parse_soft_rate_reset(body_no_tz) == expected_ts

    # 非 6004 不解析
    assert parse_soft_rate_reset('{"code":11140,"msg":"将在 2026-10-01 12:00:00 重置"}') is None
    # 格式错误不解析
    assert parse_soft_rate_reset('{"code":6004,"msg":"invalid format"}') is None


def test_error_helper_predicates() -> None:
    assert is_already_checked_in("code=10001 今日已签到")
    assert is_already_checked_in("already checked in")
    assert not is_already_checked_in("unknown error")

    e1 = UpstreamError(kind=ErrKind.client, status=400, msg="first_buddy task not completed yet")
    assert is_buddy_task_incomplete(e1)
    e2 = UpstreamError(kind=ErrKind.client, status=500, msg="first_buddy task not completed yet")
    assert not is_buddy_task_incomplete(e2)


# ---------------------------------------------------------------------------
# 3. 计费包聚合测试 (package_remain_used & user_resource)
# ---------------------------------------------------------------------------


def test_package_remain_used() -> None:
    # 周期包 (CycleCapacitySize > 0)
    p1 = {
        "CycleCapacitySize": 1000,
        "CycleCapacityRemain": 700,
        "CycleCapacityUsed": 300,
    }
    r, u, s = package_remain_used(p1)
    assert (r, u, s) == (700, 300, 1000)

    # 普通包
    p2 = {
        "CapacitySize": 500,
        "CapacityRemain": 400,
        "CapacityUsed": 0,
    }
    r, u, s = package_remain_used(p2)
    assert (r, u, s) == (400, 100, 500)


@pytest.mark.asyncio
async def test_user_resource_aggregation() -> None:
    handler_called = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal handler_called
        assert "/v2/billing/meter/get-user-resource" in str(request.url)
        handler_called = True
        resp_data = {
            "code": 0,
            "msg": "ok",
            "data": {
                "Response": {
                    "Data": {
                        "TotalDosage": 2500,
                        "Accounts": [
                            {
                                "CycleCapacitySize": 1000,
                                "CycleCapacityRemain": 800,
                                "CycleCapacityUsed": 200,
                            },
                            {
                                "CapacitySize": 1000,
                                "CapacityRemain": 600,
                                "CapacityUsed": 400,
                            },
                        ],
                    }
                }
            },
        }
        return httpx.Response(200, json=resp_data)

    transport = httpx.MockTransport(handler)
    client = UpstreamClient(transport=transport)
    try:
        acct = Account(access_token="tok1", domain="copilot.tencent.com")
        credits = await client.user_resource(acct)
        assert handler_called
        # remain: 800 + 600 = 1400
        assert credits.remain == 1400
        # size: TotalDosage (2500) > sum(size) (2000) -> 2500
        assert credits.size == 2500
        # used: derived = 2500 - 1400 = 1100
        assert credits.used == 1100
        assert credits.packages == 2
    finally:
        await client.aclose()


# ---------------------------------------------------------------------------
# 4. 登录流程测试 (poll_login pending vs success)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_poll_login_pending_and_success() -> None:
    step = "pending"

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "/v2/plugin/auth/token" in url:
            if step == "pending":
                # 上游尚未完成时返回 400
                return httpx.Response(400, json={"code": 10002, "msg": "authorization pending"})
            else:
                return httpx.Response(
                    200,
                    json={
                        "code": 0,
                        "msg": "ok",
                        "data": {
                            "accessToken": "acc_123",
                            "refreshToken": "ref_456",
                            "expiresIn": 3600,
                            "domain": "copilot.tencent.com",
                        },
                    },
                )
        if "/v2/plugin/login/account" in url:
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "msg": "ok",
                    "data": {
                        "uid": "u_999",
                        "enterpriseId": "ent_1",
                        "nickname": "TestUser",
                    },
                },
            )
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    client = UpstreamClient(transport=transport)
    try:
        # 1. Pending 状态返回 None
        res_pending = await client.poll_login("cn", "state_abc")
        assert res_pending is None

        # 2. 成功状态返回 Account
        step = "success"
        res_success = await client.poll_login("cn", "state_abc")
        assert res_success is not None
        assert res_success.uid == "u_999"
        assert res_success.access_token == "acc_123"
        assert res_success.refresh_token == "ref_456"
        assert res_success.nickname == "TestUser"
    finally:
        await client.aclose()


# ---------------------------------------------------------------------------
# 5. 猫猫旅行分支测试 (travel_once)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_travel_once_branches() -> None:
    scenario = "no_cat"

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "/activity/growth/buddy/info" in url:
            if scenario == "no_cat":
                return httpx.Response(200, json={"code": 0, "data": {"buddy": None}})
            return httpx.Response(200, json={"code": 0, "data": {"buddy": {"id": 1, "name": "咪咪"}}})

        if "/activity/growth/buddy/agreement" in url:
            return httpx.Response(200, json={"code": 0, "data": {}})
        if "/activity/growth/buddy/first" in url:
            return httpx.Response(200, json={"code": 0, "data": {}})

        if "/activity/growth/buddy/travel/status" in url:
            if scenario == "idle":
                return httpx.Response(
                    200,
                    json={"code": 0, "data": {"state": "idle", "daily_limit_reached": False}},
                )
            if scenario == "arrived":
                return httpx.Response(
                    200,
                    json={"code": 0, "data": {"state": "arrived", "record_id": 888}},
                )
            if scenario == "traveling":
                return httpx.Response(
                    200,
                    json={"code": 0, "data": {"state": "traveling", "record_id": 888}},
                )

        if "/activity/growth/buddy/travel/depart" in url:
            return httpx.Response(200, json={"code": 0, "data": {}})

        if "/activity/growth/buddy/travel/claim" in url:
            return httpx.Response(200, json={"code": 0, "data": {"reward_credit": 50}})

        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    client = UpstreamClient(transport=transport)
    try:
        acct = Account(uid="test_user", access_token="tok")

        # 分支 1: 无猫领养
        scenario = "no_cat"
        res1 = await client.travel_once(acct)
        assert res1.action == "adopt"
        assert "领养成功" in res1.message

        # 分支 2: idle 派出
        scenario = "idle"
        res2 = await client.travel_once(acct)
        assert res2.action == "depart"
        assert res2.buddy == "咪咪"

        # 分支 3: arrived 领奖
        scenario = "arrived"
        res3 = await client.travel_once(acct)
        assert res3.action == "claim"
        assert res3.reward == 50

        # 分支 4: traveling 跳过
        scenario = "traveling"
        res4 = await client.travel_once(acct)
        assert res4.action == "skip"
    finally:
        await client.aclose()


# ---------------------------------------------------------------------------
# 6. OAuth 会话管理器测试
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_login_manager_lifecycle() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "/v2/plugin/auth/state" in url:
            return httpx.Response(
                200,
                json={"code": 0, "data": {"state": "st_xyz", "authUrl": "https://auth.com"}},
            )
        if "/v2/plugin/auth/token" in url:
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "accessToken": "tok_live",
                        "refreshToken": "ref_live",
                        "domain": "copilot.tencent.com",
                    },
                },
            )
        if "/v2/plugin/login/account" in url:
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "uid": "u_new",
                        "nickname": "Alice",
                    },
                },
            )
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    client = UpstreamClient(transport=transport)
    try:
        mgr = LoginManager(upstream=client, ttl_seconds=60)
        # 启动会话
        sess = await mgr.start("cn")
        assert sess.status == "pending"
        assert sess.auth_url == "https://auth.com"
        assert mgr.current() is not None

        # 轮询成功
        polled = await mgr.poll(sess.id)
        assert polled.status == "success"
        assert polled.uid == "u_new"
        assert polled.nickname == "Alice"

        # take_account 仅能取出一次
        acct1 = mgr.take_account(sess.id)
        assert acct1 is not None
        assert acct1.uid == "u_new"
        acct2 = mgr.take_account(sess.id)
        assert acct2 is None

        # 取消与标记
        sess2 = await mgr.start("global")
        cancelled = mgr.cancel(sess2.id)
        assert cancelled.status == "cancelled"
    finally:
        await client.aclose()


# ---------------------------------------------------------------------------
# 7. 区域规范化与基址路由
# ---------------------------------------------------------------------------


def test_normalize_region() -> None:
    assert normalize_region("") == "cn"
    assert normalize_region("cn") == "cn"
    assert normalize_region("CN  ") == "cn"
    assert normalize_region("global") == "global"
    assert normalize_region("  GLOBAL") == "global"
    with pytest.raises(ValueError):
        normalize_region("us")


def test_region_routing() -> None:
    client = UpstreamClient()
    acct_cn = Account(domain="copilot.tencent.com")
    acct_global = Account(domain="www.workbuddy.ai")

    assert client._chat_base(acct_cn) == CHAT_BASE_CN
    assert client._chat_base(acct_global) == CHAT_BASE_GLOBAL


# ---------------------------------------------------------------------------
# 8. 聊天流与空闲监控测试
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_iter_lines_with_idle() -> None:
    # 正常流
    def normal_handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="line1\nline2\nline3\n")

    transport = httpx.MockTransport(normal_handler)
    async with httpx.AsyncClient(transport=transport) as hc:
        resp = await hc.get("https://test.com", timeout=5)
        lines = [line async for line in iter_lines_with_idle(resp, idle_timeout=1.0)]
        assert lines == ["line1", "line2", "line3"]


# ---------------------------------------------------------------------------
# 9. 调度器测试
# ---------------------------------------------------------------------------


def test_scheduler_helpers() -> None:
    now = datetime(2026, 9, 24, 10, 30, 0, tzinfo=timezone.utc)
    # CST 自然日测试 (UTC+8: 2026-09-24 18:30:00)
    assert cst_day(now) == "2026-09-24"

    # next_fire 测试
    local_now = datetime(2026, 9, 24, 10, 30, 0)
    # 小时列表包含 9 和 21：10:30 之后的最近小时为当天 21:00
    fire = next_fire(local_now, [9, 21])
    assert fire == datetime(2026, 9, 24, 21, 0, 0)

    # 已过 21:00：最近小时为次日 09:00
    local_late = datetime(2026, 9, 24, 22, 0, 0)
    fire_late = next_fire(local_late, [9, 21])
    assert fire_late == datetime(2026, 9, 25, 9, 0, 0)


@pytest.mark.asyncio
async def test_scheduler_checkin_and_keepalive() -> None:
    class MockPool:
        def __init__(self) -> None:
            self.statuses = [AccountStatus(uid="u1", disabled=False)]
            self.accounts = {"u1": Account(uid="u1", refresh_token="rf_1")}
            self.reenabled: list[tuple[str, int]] = []
            self.cleared_dead: list[str] = []
            self.noted_dead: list[str] = []

        def list(self) -> list[AccountStatus]:
            return self.statuses

        def auth_by_uid(self, uid: str) -> Account | None:
            return self.accounts.get(uid)

        def reenable_if_credits(self, uid: str, remain: int) -> None:
            self.reenabled.append((uid, remain))

        def clear_session_dead(self, uid: str) -> None:
            self.cleared_dead.append(uid)

        def note_session_dead(self, uid: str) -> bool:
            self.noted_dead.append(uid)
            return True

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "/v2/billing/meter/daily-checkin" in url:
            return httpx.Response(200, json={"code": 0, "msg": "ok", "data": {}})
        if "/v2/billing/meter/get-user-resource" in url:
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "Response": {
                            "Data": {
                                "Accounts": [{"CapacityRemain": 666, "CapacitySize": 1000}],
                            }
                        }
                    },
                },
            )
        if "/v2/plugin/auth/token/refresh" in url:
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "accessToken": "new_acc",
                        "refreshToken": "new_rf",
                    },
                },
            )
        if "/activity/growth/buddy/info" in url:
            return httpx.Response(200, json={"code": 0, "data": {"buddy": {"id": 1, "name": "Cat"}}})
        if "/activity/growth/buddy/travel/status" in url:
            return httpx.Response(200, json={"code": 0, "data": {"state": "traveling"}})
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    client = UpstreamClient(transport=transport)
    pool = MockPool()
    saved: list[Account] = []

    try:
        sched = Scheduler(
            pool=pool,
            upstream=client,
            config_provider=lambda: Config(),
            save_account=lambda a: saved.append(a),
        )

        # 立即签到与解冻
        await sched.run_checkin_now()
        assert len(pool.reenabled) == 1
        assert pool.reenabled[0] == ("u1", 666)

        # 立即保活与保存
        await sched.run_keepalive_now()
        assert "u1" in pool.cleared_dead
        assert len(saved) == 1
        assert saved[0].access_token == "new_acc"
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_fetch_models_and_efforts() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert "/console/enterprises/personal/models" in str(request.url)
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {
                    "agents": [
                        {"name": "web", "models": ["gpt-web"]},
                        {"name": "cli", "models": ["claude-3-5-sonnet", "gpt-4o", "disabled-model"]},
                    ],
                    "models": [
                        {
                            "id": "claude-3-5-sonnet",
                            "name": "Claude 3.5 Sonnet",
                            "maxInputTokens": 200000,
                            "maxOutputTokens": 8192,
                            "disabled": False,
                            "reasoning": {"supportedEfforts": ["low", "medium", "high"]},
                        },
                        {
                            "id": "gpt-4o",
                            "name": "GPT-4o",
                            "maxInputTokens": 128000,
                            "maxOutputTokens": 4096,
                            "disabled": False,
                            "reasoning": {"supportedEfforts": []},
                        },
                        {
                            "id": "disabled-model",
                            "name": "Disabled",
                            "maxInputTokens": 1000,
                            "maxOutputTokens": 100,
                            "disabled": True,
                        },
                    ],
                },
            },
        )

    transport = httpx.MockTransport(handler)
    client = UpstreamClient(transport=transport)
    try:
        acct = Account(access_token="tok_models", domain="copilot.tencent.com")
        models = await client.fetch_models(acct)
        assert len(models) == 2
        assert models[0].id == "claude-3-5-sonnet"
        assert models[0].context_window == 200000
        assert models[0].max_tokens == 8192
        assert models[0].efforts == ["low", "medium", "high"]
        assert models[1].id == "gpt-4o"
        # 验证 efforts 缓存
        assert client.efforts == {"claude-3-5-sonnet": ["low", "medium", "high"]}
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_resource_packages() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {
                    "Response": {
                        "Data": {
                            "Accounts": [
                                {
                                    "PackageName": "周期包",
                                    "CycleCapacitySize": 1000,
                                    "CycleCapacityRemain": 700,
                                    "CycleCapacityUsed": 300,
                                    "CycleEndTime": "2026-10-01 00:00:00",
                                },
                                {
                                    "PackageName": "",
                                    "CapacitySize": 500,
                                    "CapacityRemain": 400,
                                    "CapacityUsed": 100,
                                    "CycleEndTime": "2026-11-01 00:00:00",
                                },
                            ]
                        }
                    }
                },
            },
        )

    transport = httpx.MockTransport(handler)
    client = UpstreamClient(transport=transport)
    try:
        acct = Account(access_token="tok_res")
        pkgs = await client.resource_packages(acct)
        assert len(pkgs) == 2
        assert pkgs[0].package_name == "周期包"
        assert pkgs[0].cycle_capacity_size == 1000
        assert pkgs[0].cycle_capacity_remain == 700
        assert pkgs[0].cycle_capacity_used == 300
        assert pkgs[0].recurring is True

        assert pkgs[1].package_name == "Credit Package"  # 默认名称
        assert pkgs[1].cycle_capacity_size == 500
        assert pkgs[1].recurring is False
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_daily_checkin() -> None:
    scenario = "new"

    def handler(request: httpx.Request) -> httpx.Response:
        if scenario == "already":
            return httpx.Response(400, json={"code": 10001, "msg": "今日已签到"})
        return httpx.Response(200, json={"code": 0, "msg": "ok", "data": "gain 50"})

    transport = httpx.MockTransport(handler)
    client = UpstreamClient(transport=transport)
    try:
        acct = Account(access_token="tok_checkin")
        # 首次签到
        res1 = await client.daily_checkin(acct)
        assert not res1.already
        assert "签到成功" in res1.message

        # 重复签到
        scenario = "already"
        res2 = await client.daily_checkin(acct)
        assert res2.already
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_chat_stream_and_idle_timeout() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "/v2/chat/completions" in url:
            if b"bad" in request.read():
                return httpx.Response(400, text="Unmarshal chat params failed")
            return httpx.Response(200, text="data: test\n\n")
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    client = UpstreamClient(transport=transport)
    try:
        acct = Account(access_token="tok_chat")
        # 400 失败
        resp_fail, status, raw = await client.chat_stream(acct, b'{"error": "bad"}')
        assert resp_fail is None
        assert status == 400
        assert b"Unmarshal chat params failed" in raw

        # 200 成功
        resp_ok, status_ok, _ = await client.chat_stream(acct, b'{"messages": []}')
        assert resp_ok is not None
        assert status_ok == 200
        await resp_ok.aclose()

        # 空闲超时模拟
        async def slow_stream():
            await asyncio.sleep(0.5)
            yield "slow line"

        class DummyResp:
            def aiter_lines(self):
                return slow_stream()

            async def aclose(self):
                pass

        with pytest.raises(UpstreamError) as exc_info:
            async for _ in iter_lines_with_idle(DummyResp(), idle_timeout=0.05):  # type: ignore[arg-type]
                pass
        assert exc_info.value.kind == ErrKind.server
        assert "idle timeout" in exc_info.value.msg
    finally:
        await client.aclose()

