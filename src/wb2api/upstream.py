"""直连 WorkBuddy 上游（chat / billing / auth / growth）异步客户端。

严格对齐 Go 实现：
- workbuddy2api/internal/upstream/client.go, resource.go, idle.go
- workbuddy2api-gui/internal/upstream/client.go
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
import json
import logging
import re
import time
from typing import Any

import httpx

from .headers import (
    CLIENT_UA,
    billing_headers,
    chat_headers,
    common_headers,
    refresh_headers,
)
from .models import Account
from .payload import prepare_body

logger = logging.getLogger("wb2api.upstream")

# ---------------------------------------------------------------------------
# 上游基址与常量
# ---------------------------------------------------------------------------

CHAT_BASE_CN = "https://copilot.tencent.com"
BILLING_BASE_CN = "https://www.codebuddy.cn"
CHAT_BASE_GLOBAL = "https://www.workbuddy.ai"
BILLING_BASE_GLOBAL = "https://www.workbuddy.ai"

_CST_TZ = timezone(timedelta(hours=8))
_MODEL_RATE_LIMIT_RE = re.compile(r'"code"\s*:\s*"?6004"?')
_SOFT_RATE_RESET_RE = re.compile(r"将在 (.+?) 重置")

# ---------------------------------------------------------------------------
# 错误分类
# ---------------------------------------------------------------------------


class ErrKind(str, Enum):
    """上游错误类别枚举。"""

    none = "none"
    hard_credit = "hard_credit"
    soft_rate = "soft_rate"
    session_dead = "session_dead"
    not_found = "not_found"
    server = "server"
    bad_params = "bad_params"
    client = "client"
    # fingerprint：网关客户端指纹拦截（业务 code 10085「请求不合法」）。
    # 与普通 client 错误区分开：它由请求指纹（User-Agent / Origin 等）触发，
    # 不是账号问题，也不应触发路径回退或换号。
    fingerprint = "fingerprint"


class UpstreamError(Exception):
    """带分类与状态码的上游错误。"""

    def __init__(self, kind: ErrKind | str, status: int = 0, msg: str = "") -> None:
        if isinstance(kind, str):
            try:
                self.kind = ErrKind(kind)
            except ValueError:
                self.kind = ErrKind.client
        else:
            self.kind = kind
        self.status = status
        self.msg = msg
        super().__init__(f"upstream {self.kind.value} (http {self.status}): {self.msg}")


HARD_MARKERS = [
    "insufficient credit",
    "no credit",
    "credit exhausted",
    "out of credit",
    "quota exceeded",
    "quota exhaust",
    "payment required",
    "credit not enough",
    "not enough credit",
    "积分不足",
    "额度不足",
    "余额不足",
    "积分用完",
    "额度用尽",
    "没有积分",
]

SESSION_DEAD_MARKERS = [
    "Offline user session not found",
    "12153",
]

SOFT_RATE_MARKERS = [
    "rate limit",
    "rate-limiting",
    "rate-limited",
    "too many requests",
    "too many",
    "usage limit",
    "请求过于频繁",
    "限流",
]

BAD_PARAMS_MARKER_MSG = "Unmarshal chat params failed"
BAD_PARAMS_MARKER_CODE = '"code":11101'

# 网关客户端指纹拦截：业务 code 10085「请求不合法，如有疑问请联系客服」。
# 由请求指纹（User-Agent / Origin / Referer 等）触发，非账号问题。
FINGERPRINT_CODE = 10085
FINGERPRINT_MARKERS = ("10085", "请求不合法")


def is_fingerprint_rejected(body: str) -> bool:
    """报告响应是否指向网关客户端指纹拦截（code 10085「请求不合法」）。

    参考实现（workbuddy-switch）明确：401/403 是鉴权问题、10085 是网关客户端
    指纹拦截。它必须原样暴露，不能被当成「路径不存在」而回退或换号。
    """
    return any(m in body for m in FINGERPRINT_MARKERS)


def classify(status: int, body: str) -> ErrKind:
    """按 HTTP 状态码 + body 判定错误类别（判定顺序：自严到宽）。"""
    if status == 402:
        return ErrKind.hard_credit

    lower = body.lower()
    for m in HARD_MARKERS:
        if m.lower() in lower or m in body:
            return ErrKind.hard_credit

    for m in SESSION_DEAD_MARKERS:
        if m in body:
            return ErrKind.session_dead

    # 指纹拦截优先于限流/404 兜底：它是明确的客户端问题，必须原样暴露。
    if is_fingerprint_rejected(body):
        return ErrKind.fingerprint

    for m in SOFT_RATE_MARKERS:
        if m.lower() in lower or m in body:
            return ErrKind.soft_rate

    if status == 429:
        return ErrKind.soft_rate

    if status == 404:
        return ErrKind.not_found

    if status >= 500:
        return ErrKind.server

    if status >= 400:
        if status == 400 and (BAD_PARAMS_MARKER_MSG in body or BAD_PARAMS_MARKER_CODE in body):
            return ErrKind.bad_params
        return ErrKind.client

    return ErrKind.none


Classify = classify


def is_model_rate_limit(body: str) -> bool:
    """报告 body 是否明确指向模型级限流（业务 code 6004）。"""
    return bool(_MODEL_RATE_LIMIT_RE.search(body))


def parse_soft_rate_reset(body: str) -> float | None:
    """从 429 body 解析「将在 … 重置」时间（UTC+8）。成功返回 epoch 秒，失败返回 None。"""
    if not is_model_rate_limit(body):
        return None
    m = _SOFT_RATE_RESET_RE.search(body)
    if not m:
        return None
    ts = m.group(1).strip()
    if ts.endswith(" UTC+8"):
        ts = ts[:-6].strip()
    try:
        dt = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S").replace(tzinfo=_CST_TZ)
        return dt.timestamp()
    except Exception:
        return None


def is_already_checked_in(err: Exception | str) -> bool:
    """判定签到接口返回的是否为「今日已签到」等幂等提示。"""
    s = str(err).lower()
    for m in ("已签到", "already", "checkin", "code=10001", "签到过"):
        if m in s:
            return True
    return False


def is_buddy_task_incomplete(err: Exception) -> bool:
    """判定领养门槛未达标（HTTP 400 + first_buddy task not completed yet）。"""
    if isinstance(err, UpstreamError):
        return err.status == 400 and "first_buddy task not completed yet" in err.msg.lower()
    s = str(err).lower()
    return "400" in s and "first_buddy task not completed yet" in s


def supports_checkin(account: Account) -> bool:
    """该账号档位是否支持签到 / 成长中心（猫猫旅行）。

    实测：签到与成长中心接口为**国内版专有**，国际版（workbuddy.ai）没有对应实现。
    对国际版账号直接跳过，避免发出必然失败的请求（参考实现同此语义）。
    """
    return not account.is_global()


# billing 域路径前缀：国内版固定 /v2/billing/meter；国际版先试 /billing/meter，
# 仅在 404（路径不存在）时回退到 /v2/billing/meter。
BILLING_PREFIX_CN = "/v2/billing/meter"
BILLING_PREFIX_WEB = "/billing/meter"


def billing_path_candidates(account: Account, suffix: str) -> list[str]:
    """返回 billing 路径候选（suffix 形如 "/get-user-resource"）。

    国内版只有一种写法；国际版先网页写法，再回退旧写法。
    """
    if account.is_global():
        primary = f"{BILLING_PREFIX_WEB}{suffix}"
        fallback = f"{BILLING_PREFIX_CN}{suffix}"
        return [primary, fallback]
    return [f"{BILLING_PREFIX_CN}{suffix}"]


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------


@dataclass
class ModelInfo:
    """动态模型信息。"""

    id: str
    name: str
    context_window: int = 0
    max_tokens: int = 0
    efforts: list[str] = field(default_factory=list)


@dataclass
class Credits:
    """账号积分余额。"""

    remain: int = 0
    used: int = 0
    size: int = 0
    packages: int = 0


@dataclass
class ResourcePackage:
    """单个计费套餐包详情。"""

    package_name: str
    cycle_capacity_size: int = 0
    cycle_capacity_used: int = 0
    cycle_capacity_remain: int = 0
    cycle_end_time: str = ""
    recurring: bool = False


@dataclass
class CheckinResult:
    """签到结果。"""

    already: bool
    message: str
    raw: str = ""
    # unsupported：该档位（国际版）无签到接口，未发起请求。
    unsupported: bool = False


@dataclass
class Buddy:
    """猫档案。"""

    id: int
    name: str


@dataclass
class TravelState:
    """猫猫旅行状态。"""

    state: str
    daily_limit_reached: bool = False
    record_id: int = 0
    reward_credit: int = 0


@dataclass
class TravelResult:
    """单账号单趟旅行巡检结果。"""

    uid: str
    action: str
    message: str
    reward: int = 0
    buddy: str = ""


# ---------------------------------------------------------------------------
# 计费包容量聚合辅助函数
# ---------------------------------------------------------------------------


def package_remain_used(p: dict[str, Any]) -> tuple[int, int, int]:
    """按 Cycle* 优先口径计算单个套餐包的 (remain, used, size)。"""
    cycle_size = int(p.get("CycleCapacitySize") or 0)
    cycle_remain = int(p.get("CycleCapacityRemain") or 0)
    cycle_used = int(p.get("CycleCapacityUsed") or 0)
    cap_size = int(p.get("CapacitySize") or 0)
    cap_remain = int(p.get("CapacityRemain") or 0)
    cap_used = int(p.get("CapacityUsed") or 0)

    if cycle_size > 0:
        remain = cycle_remain
        size = cycle_size
        if remain < 0:
            remain = 0
        if remain > size:
            remain = size
        used = size - remain
        if cycle_used > used:
            used = cycle_used
            if size >= used:
                remain = size - used
        return remain, used, size

    remain = cap_remain
    used = cap_used
    size = cap_size
    if used == 0 and size > remain:
        used = size - remain
    return remain, used, size


# ---------------------------------------------------------------------------
# SSE 流式行迭代与空闲监控
# ---------------------------------------------------------------------------


async def iter_lines_with_idle(
    response: httpx.Response,
    idle_timeout: float = 0.0,
) -> AsyncIterator[str]:
    """遍历 SSE 行流，单行读取超时（静默超时）时自动关闭响应并抛出 UpstreamError。"""
    aiter = response.aiter_lines()
    try:
        while True:
            if idle_timeout > 0:
                try:
                    line = await asyncio.wait_for(aiter.__anext__(), timeout=idle_timeout)
                except asyncio.TimeoutError as e:
                    await response.aclose()
                    raise UpstreamError(kind=ErrKind.server, msg="stream idle timeout") from e
                except StopAsyncIteration:
                    break
            else:
                try:
                    line = await aiter.__anext__()
                except StopAsyncIteration:
                    break
            yield line
    finally:
        await response.aclose()


# ---------------------------------------------------------------------------
# 客户端主体
# ---------------------------------------------------------------------------


class UpstreamClient:
    """CodeBuddy / WorkBuddy 统一异步客户端。"""

    def __init__(
        self,
        timeout_seconds: float = 120,
        header_timeout_seconds: float = 0,
        idle_timeout_seconds: float = 0,
        sanitize: bool = True,
        *,
        chat_base_cn: str = CHAT_BASE_CN,
        billing_base_cn: str = BILLING_BASE_CN,
        chat_base_global: str = CHAT_BASE_GLOBAL,
        billing_base_global: str = BILLING_BASE_GLOBAL,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.sanitize = sanitize
        self.idle_timeout_seconds = idle_timeout_seconds
        self.efforts: dict[str, list[str]] = {}

        self.chat_base_cn = chat_base_cn
        self.billing_base_cn = billing_base_cn
        self.chat_base_global = chat_base_global
        self.billing_base_global = billing_base_global

        header_timeout = header_timeout_seconds if header_timeout_seconds > 0 else (timeout_seconds or 120)
        limits = httpx.Limits(max_keepalive_connections=50, max_connections=100, keepalive_expiry=90.0)

        # 常规 RPC client（带总超时）
        self.http = httpx.AsyncClient(
            timeout=timeout_seconds,
            limits=limits,
            transport=transport,
        )

        # 聊天 SSE client（无总超时，仅首字节/头超时）
        chat_timeout = httpx.Timeout(
            header_timeout,
            connect=header_timeout,
            read=None,
            write=header_timeout,
            pool=header_timeout,
        )
        self.chat_http = httpx.AsyncClient(
            timeout=chat_timeout,
            limits=limits,
            transport=transport,
        )

    async def aclose(self) -> None:
        """关闭客户端底层连接。"""
        await self.http.aclose()
        await self.chat_http.aclose()

    def _chat_base(self, account: Account | None = None, region: str = "") -> str:
        if (account is not None and account.is_global()) or region.lower() == "global":
            return self.chat_base_global
        return self.chat_base_cn

    def _billing_base(self, account: Account | None = None, region: str = "") -> str:
        if (account is not None and account.is_global()) or region.lower() == "global":
            return self.billing_base_global
        return self.billing_base_cn

    async def _do_json(self, request: httpx.Request) -> Any:
        """发送请求并解信封；HTTP 非 2xx 或业务 code != 0 时抛出 UpstreamError。"""
        resp = await self.http.send(request)
        raw = await resp.aread()
        raw_text = raw.decode("utf-8", errors="replace")

        if resp.status_code >= 400:
            kind = classify(resp.status_code, raw_text)
            msg = raw_text.strip().replace("\n", " ")
            if len(msg) > 200:
                msg = msg[:200]
            raise UpstreamError(kind=kind, status=resp.status_code, msg=msg)

        try:
            env = json.loads(raw_text)
        except Exception as e:
            msg = raw_text.strip().replace("\n", " ")
            if len(msg) > 120:
                msg = msg[:120]
            raise UpstreamError(
                kind=ErrKind.client,
                status=resp.status_code,
                msg=f"parse failed: {e} (body: {msg})",
            ) from e

        if not isinstance(env, dict):
            raise UpstreamError(
                kind=ErrKind.client,
                status=resp.status_code,
                msg=f"parse failed: response is not a dict (body: {raw_text[:120]})",
            )

        code = env.get("code", 0)
        msg = str(env.get("msg", ""))
        if code != 0:
            kind = classify(resp.status_code, msg)
            if code == FINGERPRINT_CODE:
                kind = ErrKind.fingerprint
            elif code == 404:
                kind = ErrKind.not_found
            if kind == ErrKind.none:
                kind = ErrKind.client
            truncated_msg = msg.strip().replace("\n", " ")
            if len(truncated_msg) > 160:
                truncated_msg = truncated_msg[:160]
            hint = ""
            if kind == ErrKind.fingerprint:
                hint = "（网关客户端指纹拦截，请检查请求 User-Agent/Origin；非账号问题）"
            raise UpstreamError(
                kind=kind,
                status=resp.status_code,
                msg=f"code={code} msg={truncated_msg}{hint}",
            )

        return env.get("data")

    async def _billing_json(self, account: Account, suffix: str, body: Any) -> Any:
        """发 billing 域请求（POST），带**区域感知路径回退**。

        路径候选见 ``billing_path_candidates``。回退**仅在 404（路径不存在）时**发生；
        401/403/10085（指纹）/传输错误一律原样抛出——把它们当成路径问题会掩盖真因，
        并多打一次无意义的请求。
        """
        base = self._billing_base(account)
        headers = billing_headers(account)
        candidates = billing_path_candidates(account, suffix)
        last_exc: UpstreamError | None = None
        for i, path in enumerate(candidates):
            req = self.http.build_request("POST", base + path, headers=headers, json=body)
            try:
                return await self._do_json(req)
            except UpstreamError as e:
                last_exc = e
                if i + 1 < len(candidates) and e.kind == ErrKind.not_found:
                    logger.info("billing 路径 %s 不存在，回退下一候选", path)
                    continue
                raise
        if last_exc is not None:
            raise last_exc
        return None

    # -----------------------------------------------------------------------
    # 登录 / OAuth
    # -----------------------------------------------------------------------

    async def start_login(self, region: str) -> tuple[str, str]:
        """发起设备授权，返回 (state, auth_url)。"""
        base = self._chat_base(region=region)
        url = f"{base}/v2/plugin/auth/state?platform=CLI"
        dummy = Account(domain="www.workbuddy.ai" if region.lower() == "global" else "copilot.tencent.com")
        headers = common_headers(dummy)
        req = self.http.build_request("POST", url, headers=headers, json={})
        data = await self._do_json(req)

        if not isinstance(data, dict):
            raise UpstreamError(kind=ErrKind.client, status=200, msg="授权响应格式错误")

        state = str(data.get("state") or "")
        auth_url = str(data.get("authUrl") or "")
        if not state:
            raise UpstreamError(kind=ErrKind.client, status=200, msg="授权响应缺少 state")
        if not auth_url:
            auth_url = f"{base}/login?state={state}&platform=CLI"
        return state, auth_url

    async def poll_login(self, region: str, state: str) -> Account | None:
        """轮询登录结果。尚未完成返回 None；成功返回 Account。"""
        if not state or not state.strip():
            raise ValueError("缺少 state")

        base = self._chat_base(region=region)
        url = f"{base}/v2/plugin/auth/token?state={state}"
        dummy = Account(domain="www.workbuddy.ai" if region.lower() == "global" else "copilot.tencent.com")
        headers = common_headers(dummy)
        req = self.http.build_request("GET", url, headers=headers)

        try:
            data = await self._do_json(req)
        except UpstreamError as ue:
            if 0 < ue.status < 500:
                return None
            raise

        if not isinstance(data, dict):
            return None

        access_token = str(data.get("accessToken") or "")
        if not access_token:
            return None

        refresh_token = str(data.get("refreshToken") or "")
        domain = str(data.get("domain") or "")
        if not domain:
            domain = "www.workbuddy.ai" if region.lower() == "global" else "copilot.tencent.com"

        expires_in = int(data.get("expiresIn") or 0)
        expires_at = int(time.time() + expires_in) if expires_in > 0 else 0

        acct = Account(
            access_token=access_token,
            refresh_token=refresh_token,
            expires_at=expires_at,
            domain=domain,
        )

        # 尝试拉取 account 信息（获取 uid / enterpriseId / nickname）
        acct_url = f"{base}/v2/plugin/login/account?state={state}"
        acct_headers = common_headers(dummy)
        acct_headers["Authorization"] = f"Bearer {access_token}"
        acct_req = self.http.build_request("GET", acct_url, headers=acct_headers)
        try:
            resp = await self.http.send(acct_req)
            raw = await resp.aread()
            env = json.loads(raw.decode("utf-8", errors="replace"))
            if isinstance(env, dict) and env.get("code") == 0:
                info = env.get("data")
                if isinstance(info, dict):
                    acct.uid = str(info.get("uid") or "")
                    acct.enterprise_id = str(info.get("enterpriseId") or "")
                    acct.nickname = str(info.get("nickname") or "")
        except Exception:
            pass

        if not acct.uid:
            raise ValueError("登录成功但未能获取 uid")

        return acct

    async def refresh_token(self, account: Account) -> None:
        """刷新 access token，就地更新 account 字段。"""
        if not account.refresh_token or not account.refresh_token.strip():
            raise ValueError("no refreshToken")

        url = f"{self._chat_base(account)}/v2/plugin/auth/token/refresh"
        headers = refresh_headers(account)
        req = self.http.build_request("POST", url, headers=headers)
        data = await self._do_json(req)

        if not isinstance(data, dict) or not data.get("accessToken"):
            raise UpstreamError(
                kind=ErrKind.client,
                msg="refresh_failed: no accessToken in response — re-login required",
            )

        account.access_token = str(data["accessToken"])
        if data.get("refreshToken"):
            account.refresh_token = str(data["refreshToken"])
        if data.get("domain"):
            account.domain = str(data["domain"])
        expires_in = int(data.get("expiresIn") or 0)
        if expires_in > 0:
            account.expires_at = int(time.time() + expires_in)

    # -----------------------------------------------------------------------
    # 聊天与模型
    # -----------------------------------------------------------------------

    async def chat_stream(
        self,
        account: Account,
        body: bytes,
    ) -> tuple[httpx.Response | None, int, bytes]:
        """发起聊天 SSE 请求。2xx 返回 (response, 200, b"")，非 2xx 返回 (None, status, raw_bytes)。"""
        url = f"{self._chat_base(account)}/v2/chat/completions"
        headers = chat_headers(account)
        out_body = prepare_body(body, sanitize=self.sanitize, efforts=self.efforts)
        req = self.chat_http.build_request("POST", url, headers=headers, content=out_body)

        try:
            resp = await self.chat_http.send(req, stream=True)
        except Exception as e:
            raise UpstreamError(kind=ErrKind.server, msg=f"chat transport error: {e}") from e

        if resp.status_code >= 400:
            raw_bytes = await resp.aread()
            await resp.aclose()
            return None, resp.status_code, raw_bytes

        return resp, 200, b""

    async def fetch_models(self, account: Account) -> list[ModelInfo]:
        """获取上游模型列表并刷新 supportedEfforts 缓存。"""
        chat_base = self._chat_base(account)
        url = f"{chat_base}/console/enterprises/personal/models"
        origin = "https://www.workbuddy.ai" if account.is_global() else "https://www.codebuddy.cn"
        headers = {
            "Authorization": f"Bearer {account.access_token}",
            "Accept": "application/json",
            "Origin": origin,
            "Referer": f"{origin}/",
            "User-Agent": CLIENT_UA,
        }
        req = self.http.build_request("GET", url, headers=headers)
        resp = await self.http.send(req)
        raw = await resp.aread()

        if resp.status_code != 200:
            raw_text = raw.decode("utf-8", errors="replace")[:120]
            raise UpstreamError(
                kind=classify(resp.status_code, raw_text),
                status=resp.status_code,
                msg=f"models api status {resp.status_code}: {raw_text}",
            )

        try:
            env = json.loads(raw.decode("utf-8", errors="replace"))
        except Exception as e:
            raise UpstreamError(kind=ErrKind.client, status=resp.status_code, msg=f"models parse: {e}") from e

        if env.get("code") != 0:
            raise UpstreamError(kind=ErrKind.client, status=resp.status_code, msg=f"models api code={env.get('code')}")

        data = env.get("data") or {}
        models_list = data.get("models") or []
        agents_list = data.get("agents") or []

        cli_ids: list[str] = []
        for ag in agents_list:
            if ag.get("name") == "cli":
                cli_ids = ag.get("models") or []
                break

        if not cli_ids:
            raise UpstreamError(kind=ErrKind.client, msg="no cli agent models found")

        dyn_map = {}
        for m in models_list:
            mid = m.get("id")
            reasoning = m.get("reasoning") or {}
            efforts = reasoning.get("supportedEfforts") or []
            dyn_map[mid] = {
                "id": mid,
                "name": m.get("name", ""),
                "context_window": int(m.get("maxInputTokens") or 0),
                "max_tokens": int(m.get("maxOutputTokens") or 0),
                "disabled": bool(m.get("disabled", False)),
                "efforts": efforts,
            }

        out: list[ModelInfo] = []
        for mid in cli_ids:
            m = dyn_map.get(mid)
            if not m or m["disabled"]:
                continue
            out.append(
                ModelInfo(
                    id=m["id"],
                    name=m["name"],
                    context_window=m["context_window"],
                    max_tokens=m["max_tokens"],
                    efforts=m["efforts"],
                )
            )

        if not out:
            raise UpstreamError(kind=ErrKind.client, msg="models api returned empty list")

        # 刷新 effort 缓存
        new_efforts: dict[str, list[str]] = {}
        for mi in out:
            if mi.efforts:
                new_efforts[mi.id] = list(mi.efforts)
        self.efforts = new_efforts

        return out

    # -----------------------------------------------------------------------
    # 计费与签到
    # -----------------------------------------------------------------------

    async def user_resource(self, account: Account) -> Credits:
        """查询账号当前可花费积分余额（所有套餐聚合）。"""
        now = datetime.now()
        begin = now.strftime("%Y-%m-%d %H:%M:%S")
        end = (now + timedelta(days=365 * 101)).strftime("%Y-%m-%d %H:%M:%S")
        body = {
            "PageNumber": 1,
            "PageSize": 100,
            "ProductCode": "p_tcaca",
            "Status": [0, 3],
            "PackageEndTimeRangeBegin": begin,
            "PackageEndTimeRangeEnd": end,
        }
        data = await self._billing_json(account, "/get-user-resource", body)

        response_data = (data or {}).get("Response", {}).get("Data", {}) if isinstance(data, dict) else {}
        total_dosage = int(response_data.get("TotalDosage") or 0)
        accounts = response_data.get("Accounts") or []

        out = Credits(packages=len(accounts))
        for p in accounts:
            remain, used, size = package_remain_used(p)
            out.remain += remain
            out.used += used
            out.size += size

        if out.size > 0:
            derived = out.size - out.remain
            if derived > out.used:
                out.used = derived
        if total_dosage > out.size:
            out.size = total_dosage
            derived = out.size - out.remain
            if derived > out.used:
                out.used = derived
        if out.remain < 0:
            out.remain = 0

        return out

    async def resource_packages(self, account: Account) -> list[ResourcePackage]:
        """获取账号完整套餐包列表。"""
        body = {"PageNumber": 1, "PageSize": 200}
        data = await self._billing_json(account, "/get-user-resource", body)

        response_data = (data or {}).get("Response", {}).get("Data", {}) if isinstance(data, dict) else {}
        accounts = response_data.get("Accounts") or []

        packages: list[ResourcePackage] = []
        for acct in accounts:
            cycle_size = int(acct.get("CycleCapacitySize") or 0)
            cycle = cycle_size > 0
            if cycle:
                total = cycle_size
                used = int(acct.get("CycleCapacityUsed") or 0)
                remain = int(acct.get("CycleCapacityRemain") or 0)
            else:
                total = int(acct.get("CapacitySize") or 0)
                used = int(acct.get("CapacityUsed") or 0)
                remain = int(acct.get("CapacityRemain") or 0)
            if remain < 0:
                remain = 0
            name = str(acct.get("PackageName") or "")
            if not name:
                name = "Credit Package"
            packages.append(
                ResourcePackage(
                    package_name=name,
                    cycle_capacity_size=total,
                    cycle_capacity_used=used,
                    cycle_capacity_remain=remain,
                    cycle_end_time=str(acct.get("CycleEndTime") or ""),
                    recurring=cycle,
                )
            )
        return packages

    async def daily_checkin(self, account: Account) -> CheckinResult:
        """执行每日签到。已签到不抛异常，返回 already=True。

        国际版（workbuddy.ai）没有签到接口：直接返回 unsupported，**不发请求**。
        """
        if not supports_checkin(account):
            return CheckinResult(
                already=False,
                message="该档位（国际版）暂无签到接口",
                raw="",
                unsupported=True,
            )
        try:
            data = await self._billing_json(account, "/daily-checkin", {})
        except Exception as err:
            if is_already_checked_in(err):
                return CheckinResult(already=True, message=str(err), raw="")
            raise

        raw = json.dumps(data, ensure_ascii=False) if data is not None else ""
        msg = "签到成功"
        if raw and raw != "null":
            msg = f"签到成功：{raw[:160]}"
        return CheckinResult(already=False, message=msg, raw=raw)

    # -----------------------------------------------------------------------
    # 猫猫旅行（Growth 域）
    # -----------------------------------------------------------------------

    def _require_growth(self, account: Account) -> None:
        """成长中心（猫猫旅行）为国内版专有；国际版直接拒绝，不发请求。"""
        if not supports_checkin(account):
            raise UpstreamError(
                kind=ErrKind.client,
                status=0,
                msg="该档位（国际版）暂无成长中心接口",
            )

    async def _growth_json(
        self,
        account: Account,
        method: str,
        path: str,
        json_body: Any = None,
    ) -> Any:
        self._require_growth(account)
        chat_base = self._chat_base(account)
        url = f"{chat_base}{path}"
        headers = billing_headers(account)
        req = self.http.build_request(
            method,
            url,
            headers=headers,
            json=json_body if json_body is not None else None,
        )
        return await self._do_json(req)

    async def buddy_info(self, account: Account) -> Buddy | None:
        """查询猫档案；无猫返回 None。"""
        data = await self._growth_json(account, "GET", "/activity/growth/buddy/info")
        if not isinstance(data, dict):
            return None
        buddy = data.get("buddy")
        if not buddy or buddy == "null" or not isinstance(buddy, dict):
            return None
        return Buddy(id=int(buddy.get("id") or 0), name=str(buddy.get("name") or ""))

    async def buddy_first(self, account: Account) -> None:
        """领养第一只猫。"""
        await self._growth_json(account, "POST", "/activity/growth/buddy/first", {})

    async def buddy_agreement(self, account: Account) -> None:
        """同意活动协议（幂等）。"""
        await self._growth_json(account, "POST", "/activity/growth/buddy/agreement", {"agree": True})

    async def travel_status(self, account: Account) -> TravelState:
        """查询旅行状态。"""
        data = await self._growth_json(account, "GET", "/activity/growth/buddy/travel/status")
        if not isinstance(data, dict):
            data = {}
        return TravelState(
            state=str(data.get("state") or ""),
            daily_limit_reached=bool(data.get("daily_limit_reached", False)),
            record_id=int(data.get("record_id") or 0),
            reward_credit=int(data.get("reward_credit") or 0),
        )

    async def travel_depart(self, account: Account, location_id: int = 4) -> None:
        """派出猫去指定地点旅行（默认 4：古镇客栈）。"""
        await self._growth_json(
            account,
            "POST",
            "/activity/growth/buddy/travel/depart",
            {"location_id": location_id},
        )

    async def travel_claim(self, account: Account, record_id: int) -> int:
        """领取旅行到站奖励，返回 reward_credit。"""
        data = await self._growth_json(
            account,
            "POST",
            "/activity/growth/buddy/travel/claim",
            {"record_id": record_id},
        )
        if isinstance(data, dict):
            return int(data.get("reward_credit") or 0)
        return 0

    async def travel_once(self, account: Account) -> TravelResult:
        """对单账号推进一趟旅行状态机（单趟只执行一个动作）。

        国际版无成长中心接口：直接返回 skip，**不发请求**。
        """
        if not supports_checkin(account):
            return TravelResult(
                uid=account.uid,
                action="skip",
                message="该档位（国际版）暂无成长中心接口",
            )
        res = TravelResult(uid=account.uid, action="", message="")
        try:
            buddy = await self.buddy_info(account)
        except Exception as err:
            res.action = "error"
            res.message = f"查询猫档案失败: {err}"
            return res

        if buddy is None:
            try:
                await self.buddy_agreement(account)
            except Exception as err:
                res.action = "error"
                res.message = f"同意协议失败: {err}"
                return res

            try:
                await self.buddy_first(account)
            except Exception as err:
                if is_buddy_task_incomplete(err):
                    res.action = "skip"
                    res.message = "领养门槛未达标（今日不再重试）"
                    return res
                res.action = "error"
                res.message = f"领养失败: {err}"
                return res

            res.action = "adopt"
            res.message = "领养成功（+300 积分）"
            return res

        res.buddy = buddy.name
        try:
            st = await self.travel_status(account)
        except Exception as err:
            res.action = "error"
            res.message = f"查询旅行状态失败: {err}"
            return res

        if st.state == "idle":
            if st.daily_limit_reached:
                res.action = "skip"
                res.message = "今日已派出，等待归来"
                return res
            try:
                await self.travel_depart(account, 4)
            except Exception as err:
                if is_buddy_task_incomplete(err):
                    res.action = "skip"
                    res.message = "派出条件未满足，今日跳过"
                    return res
                res.action = "error"
                res.message = f"派出失败: {err}"
                return res
            res.action = "depart"
            res.message = "已派猫出门（古镇客栈）"
            return res
        elif st.state == "arrived":
            try:
                reward = await self.travel_claim(account, st.record_id)
            except Exception as err:
                res.action = "error"
                res.message = f"领奖失败: {err}"
                return res
            res.action = "claim"
            res.reward = reward
            res.message = f"已领取到站奖励 +{reward}"
            return res
        elif st.state == "traveling":
            res.action = "skip"
            res.message = "猫正在旅行中"
            return res
        else:
            res.action = "skip"
            res.message = f"未知状态 {st.state!r}，跳过"
            return res
