"""OpenAI 兼容网关 HTTP 路由。

实现与 Go 版本 workbuddy2api 完全对齐的 OpenAI 兼容端点：
- POST /v1/chat/completions：聊天补全转发（流式/非流式）
- POST /v1/a/{uid}/chat/completions：指定账号固定路由聊天
- GET /healthz：负载均衡/宿主探活（无鉴权）
- GET /v1/models：模型列表（优先动态拉取，失败回退静态表）
- GET /v1/accounts：池内账号概览（含固定端点与冷却状态）
- GET /v1/quota：全池账号计费套餐包探测（15s 预算 + 30s 缓存）
- GET /v1/a/{uid}/quota：单账号计费套餐包详情
- GET /v1/stats：请求统计与时间序列聚合
- POST /v1/stats/reset：重置统计数据
- GET /status：网关内部状态透出
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from wb2api.app_state import AppState
from wb2api.chat_service import (
    ChatBadRequest,
    NoHealthyAccount,
    chat_non_stream,
    open_stream_session,
)
from wb2api.deps import StateDep, require_api_key
from wb2api.models import Account
from wb2api.upstream import ModelInfo, ResourcePackage

logger = logging.getLogger("wb2api.gateway")

router = APIRouter()

# ---------------------------------------------------------------------------
# 静态模型表（对齐 Go staticModels 与 WorkBuddy GLOBAL catalog）
# ---------------------------------------------------------------------------

STATIC_MODELS: list[dict[str, Any]] = [
    {"id": "hy4-preview", "object": "model", "created": 1753600000, "owned_by": "workbuddy", "context_length": 1000000},
    {"id": "hy3", "object": "model", "created": 1753600000, "owned_by": "workbuddy", "context_length": 131072},
    {"id": "hy3-preview", "object": "model", "created": 1753600000, "owned_by": "workbuddy", "context_length": 131072},
    {"id": "hy3-preview-agent", "object": "model", "created": 1753600000, "owned_by": "workbuddy", "context_length": 131072},
    {"id": "gpt-5.6-sol", "object": "model", "created": 1753600000, "owned_by": "workbuddy", "context_length": 400000},
    {"id": "gpt-5.6-terra", "object": "model", "created": 1753600000, "owned_by": "workbuddy", "context_length": 400000},
    {"id": "gpt-5.6-luna", "object": "model", "created": 1753600000, "owned_by": "workbuddy", "context_length": 400000},
    {"id": "gpt-5.5", "object": "model", "created": 1753600000, "owned_by": "workbuddy", "context_length": 400000},
    {"id": "gpt-5.4", "object": "model", "created": 1753600000, "owned_by": "workbuddy", "context_length": 400000},
    {"id": "gpt-5.3-codex", "object": "model", "created": 1753600000, "owned_by": "workbuddy", "context_length": 400000},
    {"id": "gemini-3.5-flash", "object": "model", "created": 1753600000, "owned_by": "workbuddy", "context_length": 1000000},
    {"id": "glm-5.3", "object": "model", "created": 1753600000, "owned_by": "workbuddy", "context_length": 200000},
    {"id": "glm-5.2", "object": "model", "created": 1753600000, "owned_by": "workbuddy", "context_length": 131072},
    {"id": "glm-5.1", "object": "model", "created": 1753600000, "owned_by": "workbuddy", "context_length": 131072},
    {"id": "glm-5v-turbo", "object": "model", "created": 1753600000, "owned_by": "workbuddy", "context_length": 131072},
    {"id": "kimi-k3", "object": "model", "created": 1753600000, "owned_by": "workbuddy", "context_length": 256000},
    {"id": "kimi-k2.7", "object": "model", "created": 1753600000, "owned_by": "workbuddy", "context_length": 131072},
    {"id": "minimax-m3", "object": "model", "created": 1753600000, "owned_by": "workbuddy", "context_length": 131072},
    {"id": "deepseek-v4-pro", "object": "model", "created": 1753600000, "owned_by": "workbuddy", "context_length": 131072},
    {"id": "deepseek-v4-flash", "object": "model", "created": 1753600000, "owned_by": "workbuddy", "context_length": 131072},
]


def _openai_error(code: str, message: str) -> dict[str, Any]:
    """生成 OpenAI 规范的错误响应体。"""
    return {
        "error": {
            "message": message,
            "type": "api_error",
            "code": code,
        }
    }


def format_remaining_seconds(seconds: float | int) -> str:
    """把冷却剩余秒数格式化成 '2h 13m 05s' 风格。"""
    s_total = int(round(seconds))
    if s_total <= 0:
        return ""
    h = s_total // 3600
    m = (s_total % 3600) // 60
    s = s_total % 60
    if h > 0:
        return f"{h}h {m:02d}m {s:02d}s"
    if m > 0:
        return f"{m}m {s:02d}s"
    return f"{s}s"


async def _read_body_limited(request: Request, limit: int) -> tuple[bytes | None, str | None, int]:
    """读取请求体并实施大小限制。超过限制直接返回 413，不喂截断内容给上游。"""
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            cl = int(content_length)
            if cl > limit:
                mb = limit >> 20
                return (
                    None,
                    f"请求体超过 {mb} MB 上限：请压缩内容或调大 server.max_body_mb 配置后重试",
                    413,
                )
        except ValueError:
            pass

    body_chunks: list[bytes] = []
    received = 0
    try:
        async for chunk in request.stream():
            received += len(chunk)
            if received > limit:
                mb = limit >> 20
                return (
                    None,
                    f"请求体超过 {mb} MB 上限：请压缩内容或调大 server.max_body_mb 配置后重试",
                    413,
                )
            body_chunks.append(chunk)
    except Exception as e:
        return None, f"read body: {e}", 400

    return b"".join(body_chunks), None, 200


async def _handle_chat(
    request: Request,
    state: AppState,
    pinned_uid: str = "",
) -> Response:
    """聊天补全主转发逻辑（统一支撑 /v1/chat/completions 和 /v1/a/{uid}/...）。"""
    limit = state.cfg.max_body_bytes
    body, err_msg, err_status = await _read_body_limited(request, limit)
    if err_msg is not None:
        code = "request_body_too_large" if err_status == 413 else "invalid_request"
        return JSONResponse(
            status_code=err_status,
            content=_openai_error(code, err_msg),
        )
    assert body is not None

    stream = False
    try:
        parsed = json.loads(body)
        if isinstance(parsed, dict):
            stream = bool(parsed.get("stream", False))
    except Exception:
        pass

    if stream:
        try:
            session = await open_stream_session(
                state, body, max_rotate=3, pinned_uid=pinned_uid
            )
        except NoHealthyAccount as e:
            return JSONResponse(
                status_code=503,
                content=_openai_error("no_healthy_account", str(e)),
            )
        except ChatBadRequest as e:
            return JSONResponse(
                status_code=e.status,
                content=_openai_error("invalid_request", str(e)),
            )
        except Exception as e:
            return JSONResponse(
                status_code=500,
                content=_openai_error("api_error", str(e)),
            )

        async def event_generator() -> AsyncIterator[str]:
            try:
                async for payload in session.frames():
                    yield f"data: {payload}\n\n"
            finally:
                await session.close()

        return StreamingResponse(
            event_generator(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    # 非流式聚合
    try:
        status_code, resp_data, _ = await chat_non_stream(
            state, body, max_rotate=3, pinned_uid=pinned_uid
        )
        return JSONResponse(status_code=status_code, content=resp_data)
    except NoHealthyAccount as e:
        return JSONResponse(
            status_code=503,
            content=_openai_error("no_healthy_account", str(e)),
        )
    except ChatBadRequest as e:
        return JSONResponse(
            status_code=e.status,
            content=_openai_error("invalid_request", str(e)),
        )
    except Exception as e:
        return JSONResponse(
            status_code=502,
            content=_openai_error("upstream_parse", str(e)),
        )


# ---------------------------------------------------------------------------
# POST /v1/chat/completions & POST /v1/a/{uid}/chat/completions
# ---------------------------------------------------------------------------


@router.post("/v1/chat/completions", dependencies=[Depends(require_api_key)])
async def chat_completions(request: Request, state: StateDep) -> Response:
    """OpenAI 兼容聊天接口，池内自动加权选号与故障轮转。"""
    return await _handle_chat(request, state, pinned_uid="")


@router.post("/v1/a/{uid}/chat/completions", dependencies=[Depends(require_api_key)])
async def pinned_chat(uid: str, request: Request, state: StateDep) -> Response:
    """指定账号固定路由聊天接口，供外层网关（如 9Router）按账号做外部分流。"""
    if not uid or not uid.strip():
        return JSONResponse(
            status_code=400,
            content=_openai_error("invalid_request", "missing account uid"),
        )
    if state.pool.peek_by_uid(uid) is None:
        return JSONResponse(
            status_code=404,
            content=_openai_error("not_found", f"account not found: {uid}"),
        )
    return await _handle_chat(request, state, pinned_uid=uid)


# ---------------------------------------------------------------------------
# GET /healthz
# ---------------------------------------------------------------------------


@router.get("/healthz")
async def healthz(state: StateDep) -> Response:
    """服务健康探活端点（恒无鉴权）。具备 ServableNow 判定与 X-Service 标头双保险。"""
    total, healthy, _, _, _ = state.pool.counts_detailed()
    status_code = 200 if state.pool.servable_now() else 503
    return JSONResponse(
        status_code=status_code,
        headers={"X-Service": "workbuddy2api"},
        content={
            "healthy": healthy,
            "total": total,
            "service": "workbuddy2api",
        },
    )


# ---------------------------------------------------------------------------
# GET /v1/models（动态探测 + 静态兜底 + 1h 缓存 + 5min 负缓存）
# ---------------------------------------------------------------------------

_models_lock = asyncio.Lock()
_cached_models: list[ModelInfo] = []
_models_fetched_at: float = 0.0
_models_last_fail: float = 0.0

DYNAMIC_MODELS_TTL = 3600.0  # 1 小时
MODELS_FAIL_COOLDOWN = 300.0  # 5 分钟


async def _fetch_dynamic_models(state: AppState) -> list[ModelInfo]:
    """从池中任一健康账号拉取模型列表，拉取失败进入负缓存。"""
    global _cached_models, _models_fetched_at, _models_last_fail
    async with _models_lock:
        now = time.time()
        if _cached_models and (now - _models_fetched_at) < DYNAMIC_MODELS_TTL:
            return _cached_models
        if _models_last_fail > 0 and (now - _models_last_fail) < MODELS_FAIL_COOLDOWN:
            return []

        acct = state.pool.pick()
        if acct is None:
            return []

        try:
            infos = await state.upstream.fetch_models(acct)
            if not infos:
                state.pool.note_error(acct.uid)
                _models_last_fail = time.time()
                return []
            _cached_models = infos
            _models_fetched_at = time.time()
            _models_last_fail = 0.0
            return _cached_models
        except Exception:
            state.pool.note_error(acct.uid)
            _models_last_fail = time.time()
            return []


@router.get("/v1/models", dependencies=[Depends(require_api_key)])
async def models(state: StateDep) -> dict[str, Any]:
    """获取可用模型列表。优先从上游动态拉取，失败或无可用账号时回退到静态模型表。"""
    infos = await _fetch_dynamic_models(state)
    if infos:
        data: list[dict[str, Any]] = [
            {
                "id": mi.id,
                "object": "model",
                "created": 1753600000,
                "owned_by": "workbuddy",
                "context_length": mi.context_window if mi.context_window > 0 else 131072,
                "max_output_tokens": mi.max_tokens,
            }
            for mi in infos
        ]
        return {"object": "list", "data": data}
    return {"object": "list", "data": STATIC_MODELS}


# ---------------------------------------------------------------------------
# GET /v1/accounts
# ---------------------------------------------------------------------------


@router.get("/v1/accounts", dependencies=[Depends(require_api_key)])
async def accounts(state: StateDep) -> dict[str, Any]:
    """枚举池内所有账号及专用子端点，供外层网关轮询分配。"""
    acct_list = state.pool.list()
    out: list[dict[str, Any]] = []
    for st in acct_list:
        a: dict[str, Any] = {
            "uid": st.uid,
            "credits": st.credits,
            "cooling": st.cooling,
            "disabled": st.disabled,
            "chat_path": f"/v1/a/{st.uid}/chat/completions",
            "quota_path": f"/v1/a/{st.uid}/quota",
        }
        if st.nickname:
            a["nickname"] = st.nickname
        if st.cooling and st.until:
            a["cool_until"] = st.until
            rem_sec = st.cool_remaining_sec
            if rem_sec <= 0:
                try:
                    dt = datetime.fromisoformat(st.until.replace("Z", "+00:00"))
                    rem_sec = int(dt.timestamp() - time.time())
                except Exception:
                    rem_sec = 0
            rem_str = format_remaining_seconds(rem_sec)
            if rem_str:
                a["cool_remaining"] = rem_str
        out.append(a)
    return {"provider": "workbuddy", "accounts": out}


# ---------------------------------------------------------------------------
# GET /v1/quota & GET /v1/a/{uid}/quota（并发探测 + 15s 总预算 + 30s 缓存）
# ---------------------------------------------------------------------------

_quota_lock = asyncio.Lock()
_quota_cached_rows: list[dict[str, Any]] = []
_quota_cached_at: float = 0.0

QUOTA_CACHE_TTL = 30.0  # 30 秒缓存
QUOTA_PROBE_TIMEOUT = 15.0  # 15 秒总预算


async def _build_account_quotas(state: AppState) -> list[dict[str, Any]]:
    """并发探测全部池内账号套餐包，受整体 15s 预算约束。"""
    accounts = state.pool.list()
    out: list[dict[str, Any]] = []
    need_probe: list[tuple[int, Account]] = []

    for idx, st in enumerate(accounts):
        rem_sec = st.cool_remaining_sec
        if rem_sec <= 0 and st.until:
            try:
                dt = datetime.fromisoformat(st.until.replace("Z", "+00:00"))
                rem_sec = int(dt.timestamp() - time.time())
            except Exception:
                rem_sec = 0

        row: dict[str, Any] = {
            "uid": st.uid,
            "credits": st.credits,
            "cooling": st.cooling,
            "disabled": st.disabled,
            "quotas": None,
        }
        if st.nickname:
            row["nickname"] = st.nickname
        if st.cool_kind:
            row["cool_kind"] = st.cool_kind
        if st.cooling and st.until:
            row["cool_until"] = st.until
            rem_str = format_remaining_seconds(rem_sec)
            if rem_str:
                row["cool_remaining"] = rem_str
        if st.reason:
            row["reason"] = st.reason
        if st.success_count:
            row["success_count"] = st.success_count
        if st.err_total:
            row["err_total"] = st.err_total

        out.append(row)

        acct = state.pool.peek_by_uid(st.uid)
        if acct is None:
            row["error"] = "account not in pool; no credential available for a quota probe"
            continue
        if st.cooling:
            row["error"] = "cooling: quota probe skipped to avoid extending rate limit"
            continue

        need_probe.append((idx, acct))

    if not need_probe:
        return out

    async def _probe(target: Account) -> list[ResourcePackage]:
        return await state.upstream.resource_packages(target)

    tasks = [asyncio.create_task(_probe(acct)) for _, acct in need_probe]

    try:
        results = await asyncio.wait_for(
            asyncio.gather(*tasks, return_exceptions=True),
            timeout=QUOTA_PROBE_TIMEOUT,
        )
        for (row_idx, _), res in zip(need_probe, results):
            if isinstance(res, BaseException):
                out[row_idx]["error"] = str(res)
            else:
                quotas_map: dict[str, Any] = {}
                seen: dict[str, int] = {}
                for p in res:
                    name = p.package_name
                    seen[name] = seen.get(name, 0) + 1
                    if seen[name] > 1:
                        name = f"{name} {seen[name]}"
                    quotas_map[name] = {
                        "packageName": p.package_name,
                        "total": p.cycle_capacity_size,
                        "used": p.cycle_capacity_used,
                        "remaining": p.cycle_capacity_remain,
                        "resetAt": p.cycle_end_time,
                        "recurring": p.recurring,
                    }
                out[row_idx]["quotas"] = quotas_map
    except asyncio.TimeoutError:
        for (row_idx, _), task in zip(need_probe, tasks):
            if task.done() and not task.cancelled():
                try:
                    res = task.result()
                    quotas_map = {}
                    seen = {}
                    for p in res:
                        name = p.package_name
                        seen[name] = seen.get(name, 0) + 1
                        if seen[name] > 1:
                            name = f"{name} {seen[name]}"
                        quotas_map[name] = {
                            "packageName": p.package_name,
                            "total": p.cycle_capacity_size,
                            "used": p.cycle_capacity_used,
                            "remaining": p.cycle_capacity_remain,
                            "resetAt": p.cycle_end_time,
                            "recurring": p.recurring,
                        }
                    out[row_idx]["quotas"] = quotas_map
                except Exception as e:
                    out[row_idx]["error"] = str(e)
            else:
                task.cancel()
                if out[row_idx].get("quotas") is None and not out[row_idx].get("error"):
                    out[row_idx]["error"] = "quota probe timed out"

    return out


async def _get_cached_quotas(state: AppState) -> list[dict[str, Any]]:
    """获取缓存的套餐包数据；过期或未缓存时触发全量探测。"""
    global _quota_cached_rows, _quota_cached_at
    async with _quota_lock:
        now = time.time()
        if _quota_cached_rows and (now - _quota_cached_at) < QUOTA_CACHE_TTL:
            return _quota_cached_rows
        rows = await _build_account_quotas(state)
        _quota_cached_rows = rows
        _quota_cached_at = time.time()
        return rows


@router.get("/v1/quota", dependencies=[Depends(require_api_key)])
async def quota(state: StateDep) -> dict[str, Any]:
    """返回全池账号的套餐包配额（9Router 配额看板格式）。"""
    return {
        "provider": "workbuddy",
        "accounts": await _get_cached_quotas(state),
    }


@router.get("/v1/a/{uid}/quota", dependencies=[Depends(require_api_key)])
async def pinned_quota(uid: str, state: StateDep) -> Response:
    """返回单个指定账号的套餐包配额（复用全量缓存避免雪崩）。"""
    if not uid or not uid.strip():
        return JSONResponse(
            status_code=400,
            content=_openai_error("invalid_request", "missing account uid"),
        )
    all_quotas = await _get_cached_quotas(state)
    for a in all_quotas:
        if a.get("uid") == uid:
            return JSONResponse(
                status_code=200,
                content={
                    "provider": "workbuddy",
                    "accounts": [a],
                },
            )
    return JSONResponse(
        status_code=404,
        content=_openai_error("not_found", f"account not found: {uid}"),
    )


# ---------------------------------------------------------------------------
# GET /v1/stats & POST /v1/stats/reset
# ---------------------------------------------------------------------------


def _parse_range_query(
    range_val: str | None,
    from_val: str | None,
    to_val: str | None,
    interval_val: str | None,
    model_val: str | None,
) -> tuple[float | None, float | None, str, str, bool]:
    """解析时间范围参数。支持 today|yesterday|7d|30d|90d|all 相对区间与 RFC3339 绝对区间。"""
    interval = (interval_val or "hour").strip().lower()
    model = (model_val or "").strip()

    from_ts: float | None = None
    to_ts: float | None = None
    has_range = False

    # 1. 绝对区间优先（忽略非法时间字符串，不报错 400）
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

    # 2. 相对区间（未指定绝对区间时生效）
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


@router.get("/v1/stats", dependencies=[Depends(require_api_key)])
async def stats(
    state: StateDep,
    range_param: str | None = Query(None, alias="range"),
    from_param: str | None = Query(None, alias="from"),
    to_param: str | None = Query(None, alias="to"),
    interval_param: str | None = Query(None, alias="interval"),
    model_param: str | None = Query(None, alias="model"),
) -> dict[str, Any]:
    """返回聚合调用统计与可选的时间序列分桶统计（供控制台统计页）。"""
    if not state.cfg.server.metrics_enabled:
        return {
            "enabled": False,
            "message": "统计未启用（server.metrics_enabled=false）",
        }

    derived = state.metrics.derived()
    resp: dict[str, Any] = {
        "enabled": True,
        "since": derived["since"],
        "now": derived["now"],
        "uptime_sec": derived["uptime_sec"],
        "total": derived["total"],
        "models": derived["models"],
        "series_buckets": state.metrics.series_buckets(),
    }

    from_ts, to_ts, interval, model_filter, has_range = _parse_range_query(
        range_param, from_param, to_param, interval_param, model_param
    )
    if has_range:
        resp["range"] = state.metrics.range_query(
            from_ts=from_ts,
            to_ts=to_ts,
            interval=interval,
            model=model_filter,
        )

    return resp


@router.post("/v1/stats/reset", dependencies=[Depends(require_api_key)])
async def stats_reset(state: StateDep) -> dict[str, Any]:
    """手动清空统计数据，便于重新采集基线。"""
    if not state.cfg.server.metrics_enabled:
        return {
            "ok": False,
            "enabled": False,
            "message": "统计未启用（server.metrics_enabled=false）",
        }
    state.metrics.reset()
    return {"ok": True, "message": "统计已重置"}


# ---------------------------------------------------------------------------
# GET /status
# ---------------------------------------------------------------------------


@router.get("/status", dependencies=[Depends(require_api_key)])
async def gateway_status(state: StateDep) -> dict[str, Any]:
    """透出网关当前运行状态，含账号池状态快照及粘性会话数。"""
    total, healthy, cooling, disabled, in_flight_full = state.pool.counts_detailed()
    sticky = state.sticky.count() if state.sticky else 0
    return {
        "accounts": [a.to_dict() for a in state.pool.list()],
        "total": total,
        "healthy": healthy,
        "cooling": cooling,
        "disabled": disabled,
        "in_flight_full": in_flight_full,
        "sticky_sessions": sticky,
        "redis_mode": "noop",
    }
