"""聊天转发核心：账号轮转 + 会话粘性 + 在途租约 + 错误策略 + 统计/日志。

/v1/chat/completions、/api/chat、/api/chat/stream 共用本模块，避免逻辑分叉。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any

import httpx

from .app_state import AppState
from .metrics import Delta as MetricsDelta
from .models import Account, split_region_prefix
from .session import extract_key
from .sse import aggregate, normalize_frame
from .upstream import ErrKind, UpstreamError, classify

logger = logging.getLogger("wb2api.chat")

# 进程级请求序号（表格日志）。
_chat_seq = 0
_not_found_cooldown = 60.0  # 上游 404 固定短冷却


class NoHealthyAccount(Exception):
    """所有账号不可用（冷却/禁用/占满）。"""


class ChatBadRequest(Exception):
    """客户端请求体问题（超限/畸形）。"""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


# ---------------------------------------------------------------------------
# 请求级统计 / 日志
# ---------------------------------------------------------------------------


@dataclass
class ChatStat:
    start: float
    model: str
    mode: str  # stream | sync
    uid: str = ""
    status: int = 0
    ttfb: float = 0.0
    toks: int = -1
    usage: dict[str, Any] | None = None
    logged: bool = False
    echo_model: str = ""

    def done(self, state: AppState) -> None:
        if self.logged:
            return
        self.logged = True
        self._record(state)
        self._log_row()

    def _record(self, state: AppState) -> None:
        if not state.cfg.server.metrics_enabled:
            return
        latency = time.time() - self.start
        u = self.usage or {}
        d = MetricsDelta(
            model=self.model,
            stream=self.mode == "stream",
            ok=200 <= self.status < 300,
            ttfb_ms=int(self.ttfb * 1000) if self.ttfb > 0 else 0,
            latency_ms=int(latency * 1000),
            has_usage=self.usage is not None,
        )
        if self.usage is not None:
            detail = parse_usage(u)
            d.prompt_tokens = detail["prompt_tokens"]
            d.completion_tokens = detail["completion_tokens"]
            d.total_tokens = detail["total_tokens"]
            d.cache_hit_tokens = detail["cache_hit_tokens"]
            d.cache_miss_tokens = detail["cache_miss_tokens"]
            d.cache_write_tokens = detail["cache_write_tokens"]
            d.cache_read_tokens = detail["cache_read_tokens"]
            d.cache_creation_tokens = detail["cache_creation_tokens"]
            d.credit = detail["credit"]
        state.metrics.record(d)

    def _log_row(self) -> None:
        global _chat_seq
        _chat_seq += 1
        model = self.model[:11]
        total = time.time() - self.start
        tok_field = "-"
        tokps = "-"
        if self.toks >= 0:
            tok_field = str(self.toks)
            tokps = f"{(self.toks / total):.1f}" if total > 0 else "0.0"
        ttfb = f"{int(self.ttfb * 1000)}ms" if self.ttfb > 0 else "-"
        uid = (self.uid[:8] if self.uid else "-") or "-"
        logger.info(
            "| #%03d | %s | %s | %s | %d | uid=%s | TTFB=%s | tok=%s | %stok/s | total=%.1fs |",
            _chat_seq,
            time.strftime("%H:%M:%S"),
            model,
            self.mode,
            self.status,
            uid,
            ttfb,
            tok_field,
            tokps,
            total,
        )


def parse_usage(u: dict[str, Any]) -> dict[str, Any]:
    """把上游 usage 归一化（兼容 OpenAI / Anthropic 两套缓存字段）。"""

    def as_int(key: str) -> int:
        v = u.get(key)
        if isinstance(v, bool):
            return 0
        if isinstance(v, (int, float)):
            return int(v)
        return 0

    def as_float(key: str) -> float:
        v = u.get(key)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return float(v)
        return 0.0

    d = {
        "prompt_tokens": as_int("prompt_tokens"),
        "completion_tokens": as_int("completion_tokens"),
        "total_tokens": as_int("total_tokens"),
        "cache_hit_tokens": as_int("prompt_cache_hit_tokens"),
        "cache_miss_tokens": as_int("prompt_cache_miss_tokens"),
        "cache_write_tokens": as_int("prompt_cache_write_tokens"),
        "cache_read_tokens": as_int("cache_read_input_tokens"),
        "cache_creation_tokens": as_int("cache_creation_input_tokens"),
        "credit": as_float("credit"),
    }
    cached = as_int("cached_tokens")
    if cached > d["cache_hit_tokens"]:
        d["cache_hit_tokens"] = cached
    if d["total_tokens"] == 0:
        d["total_tokens"] = d["prompt_tokens"] + d["completion_tokens"]
    return d


def peek_model(body: bytes) -> tuple[bool, str, str, str]:
    """探测请求体中的流式标志与模型名称。

    返回 (stream, region, bare, client_model)
    """
    try:
        obj = json.loads(body)
    except (ValueError, TypeError):
        return False, "", "", ""
    if not isinstance(obj, dict):
        return False, "", "", ""
    stream = bool(obj.get("stream"))
    raw_model = obj.get("model") if isinstance(obj.get("model"), str) else ""
    client_model = raw_model or ""
    region, bare = split_region_prefix(client_model)
    return stream, region, bare, client_model


def _peek(body: bytes) -> tuple[bool, str]:
    stream, _region, bare, _client_model = peek_model(body)
    return stream, bare


# ---------------------------------------------------------------------------
# 错误策略
# ---------------------------------------------------------------------------


def apply_error_policy(state: AppState, uid: str, kind: ErrKind, resp_body: str, model: str) -> None:
    """按错误分类施加冷却/禁用/熔断（对齐旧网关 applyErrorPolicy）。"""
    pool = state.pool
    soft = state.cfg.cooldown.soft_rate_seconds
    if kind == ErrKind.hard_credit:
        pool.cooldown_until_tomorrow_4am(uid, "余额不足")
    elif kind == ErrKind.soft_rate:
        from .upstream import parse_soft_rate_reset

        reset_at = parse_soft_rate_reset(resp_body)
        if reset_at is not None:
            pool.cooldown_soft_for_model(uid, soft, reset_at, model, "429 model rate limit")
        else:
            pool.cooldown(uid, "soft_rate", soft, "429 rate limit")
    elif kind == ErrKind.session_dead:
        pool.note_session_dead(uid)
    elif kind == ErrKind.not_found:
        pool.cooldown(uid, "soft_rate", _not_found_cooldown, "upstream 404")
    elif kind == ErrKind.server:
        pool.note_error(uid)
    # bad_params / client / none：只换号不罚


async def _refresh_if_needed(state: AppState, account: Account) -> bool:
    """token 临近过期则刷新；成功返回 True，session 失效返回 False。"""
    if not account.needs_refresh(600):
        return True
    try:
        await state.upstream.refresh_token(account)
    except UpstreamError as e:
        if e.kind == ErrKind.session_dead:
            state.pool.disable(account.uid, "refresh session dead")
        else:
            state.pool.note_error(account.uid)
        return False
    except Exception as e:  # noqa: BLE001
        logger.warning("refresh uid=%s error: %s", account.uid, e)
        state.pool.note_error(account.uid)
        return False
    try:
        state.store.save(account)
    except Exception as e:  # noqa: BLE001
        logger.warning("chat refresh uid=%s: save auth failed: %s", account.uid, e)
    return True


# ---------------------------------------------------------------------------
# 轮转：为一次请求选中可用账号并打开上游流
# ---------------------------------------------------------------------------


@dataclass
class _Prepared:
    account: Account
    response: httpx.Response
    uid: str
    sticky_uid: str
    sess_key: str
    tried: set[str] = field(default_factory=set)


class _Lease:
    """在途租约释放器（幂等）。"""

    def __init__(self, state: AppState, uid: str) -> None:
        self._state = state
        self._uid = uid

    def release(self) -> None:
        if self._uid:
            self._state.pool.release(self._uid)
            self._uid = ""


async def _select_account(
    state: AppState,
    tried: set[str],
    model: str,
    sticky_uid: str,
    sess_key: str,
    region: str = "",
) -> Account | None:
    """选号 + 占用租约；粘性号不可用时解绑并回落普通轮换。"""
    pool = state.pool
    acct: Account | None = None
    if sticky_uid:
        acct = pool.pick_by_uid(sticky_uid)
        if acct is not None:
            if region in ("cn", "global") and acct.is_global() != (region == "global"):
                acct = None
                if sess_key:
                    state.sticky.unbind(sess_key)
        else:
            if sess_key:
                state.sticky.unbind(sess_key)
    if acct is None:
        acct = pool.pick_excluding_for_model(tried, model, region=region)
    if acct is None:
        return None
    if not pool.acquire(acct.uid):
        if sticky_uid and acct.uid == sticky_uid and sess_key:
            state.sticky.unbind(sess_key)
        return None
    return acct


async def open_stream(
    state: AppState,
    body: bytes,
    *,
    max_rotate: int = 3,
    pinned_uid: str = "",
) -> _Prepared:
    """轮转选中账号并打开上游 SSE 流。

    成功返回 _Prepared（调用方负责遍历 response.aiter_lines() 并在结束时 release）。
    失败抛 NoHealthyAccount。
    """
    _stream, region, bare, _client_model = peek_model(body)
    pool = state.pool

    sess_key = ""
    sticky_uid = pinned_uid
    if not sticky_uid and state.cfg.session_sticky.enabled:
        sess_key = extract_key(body)
        if sess_key:
            resolved = state.sticky.resolve(sess_key)
            sticky_uid = resolved or ""

    tried: set[str] = set()
    last_err: Exception | None = None

    for _ in range(max_rotate):
        acct = await _select_account(state, tried, bare, sticky_uid, sess_key, region=region)
        if acct is None:
            break
        tried.add(acct.uid)
        lease = _Lease(state, acct.uid)
        try:
            if not await _refresh_if_needed(state, acct):
                lease.release()
                if sticky_uid and acct.uid == sticky_uid:
                    if sess_key:
                        state.sticky.unbind(sess_key)
                    sticky_uid = ""
                continue

            try:
                resp, status, raw = await state.upstream.chat_stream(acct, body)
            except UpstreamError as e:
                lease.release()
                last_err = e
                if sticky_uid and acct.uid == sticky_uid:
                    if sess_key:
                        state.sticky.unbind(sess_key)
                    sticky_uid = ""
                continue

            if status >= 400 or resp is None:
                kind = classify(status, raw.decode("utf-8", errors="replace"))
                last_err = UpstreamError(kind=kind, status=status, msg=raw.decode("utf-8", errors="replace"))
                apply_error_policy(state, acct.uid, kind, raw.decode("utf-8", errors="replace"), bare)
                lease.release()
                if sticky_uid and acct.uid == sticky_uid:
                    if sess_key:
                        state.sticky.unbind(sess_key)
                    sticky_uid = ""
                continue

            pool.note_success(acct.uid)
            if sess_key:
                state.sticky.bind(sess_key, acct.uid)
            return _Prepared(
                account=acct,
                response=resp,
                uid=acct.uid,
                sticky_uid=sticky_uid,
                sess_key=sess_key,
                tried=tried,
            )
        except Exception:
            lease.release()
            raise

    msg = "all accounts unavailable (cooling/disabled)"
    if last_err is not None:
        msg += ": " + str(last_err)
    raise NoHealthyAccount(msg)


# ---------------------------------------------------------------------------
# 非流式聚合
# ---------------------------------------------------------------------------


async def chat_non_stream(
    state: AppState,
    body: bytes,
    *,
    max_rotate: int = 3,
    pinned_uid: str = "",
) -> tuple[int, dict[str, Any], ChatStat]:
    """非流式：轮转打开上游流并本地聚合为单个响应。"""
    _stream, region, bare, client_model = peek_model(body)
    st = ChatStat(start=time.time(), model=bare or "-", mode="sync", echo_model=client_model)
    prepared = await open_stream(state, body, max_rotate=max_rotate, pinned_uid=pinned_uid)
    st.uid = prepared.uid
    lease = _Lease(state, prepared.uid)
    stream = prepared.response
    try:

        async def _lines() -> AsyncIterator[str]:
            nonlocal stream
            if stream is None:
                return
            async for line in _iter_lines(state, stream):
                yield line

        resp = await aggregate(_lines(), echo_model=st.echo_model)
        usage = resp.get("usage")
        if isinstance(usage, dict):
            st.usage = usage
            st.toks = parse_usage(usage)["completion_tokens"]
        st.status = 200
        return 200, resp, st
    finally:
        try:
            await prepared.response.aclose()
        except Exception:  # noqa: BLE001
            pass
        lease.release()
        st.done(state)


async def _iter_lines(state: AppState, response: httpx.Response) -> AsyncIterator[str]:
    """带流中空闲超时的行迭代（对齐 Go monitorBody）。"""
    idle = state.cfg.upstream.idle_timeout
    if idle <= 0:
        async for line in response.aiter_lines():
            yield line
        return
    iterator = response.aiter_lines()
    while True:
        try:
            line = await asyncio.wait_for(iterator.__anext__(), timeout=idle)
        except StopAsyncIteration:
            return
        except asyncio.TimeoutError as e:
            raise UpstreamError(kind=ErrKind.server, msg="stream idle timeout") from e
        yield line


# ---------------------------------------------------------------------------
# 流式透传
# ---------------------------------------------------------------------------


@dataclass
class StreamSession:
    """已打开的上游流，供调用方逐帧透传。"""

    stat: ChatStat
    prepared: _Prepared
    lease: _Lease
    app_state: AppState
    closed: bool = False

    async def frames(self) -> AsyncIterator[str]:
        """产出规范化后的 SSE payload 字符串（不含 "data: " 前缀）。"""
        state = self.app_state
        valid = 0
        try:
            async for line in _iter_lines(state, self.prepared.response):
                trimmed = line.rstrip("\r\n")
                if trimmed.startswith("data: [DONE]"):
                    break
                if trimmed.startswith("data: "):
                    payload = trimmed[len("data: ") :]
                    if self.stat.ttfb == 0.0:
                        self.stat.ttfb = time.time() - self.stat.start
                    try:
                        obj = json.loads(payload)
                    except ValueError:
                        yield payload
                        continue
                    if isinstance(obj, dict):
                        usage = obj.get("usage")
                        if isinstance(usage, dict):
                            self.stat.usage = usage
                            self.stat.toks = parse_usage(usage)["completion_tokens"]
                        payload = json.dumps(
                            normalize_frame(obj, echo_model=self.stat.echo_model),
                            ensure_ascii=False,
                        )
                    valid += 1
                    yield payload
                elif trimmed:
                    yield line
            if valid == 0:
                yield '{"error":{"message":"empty upstream stream","type":"upstream_error"}}'
            yield "[DONE]"
        finally:
            await self.close()

    _state: AppState = None  # type: ignore[assignment]

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            await self.prepared.response.aclose()
        except Exception:  # noqa: BLE001
            pass
        self.lease.release()
        self.stat.done(self.app_state)


async def open_stream_session(
    state: AppState,
    body: bytes,
    *,
    max_rotate: int = 3,
    pinned_uid: str = "",
) -> StreamSession:
    """打开流式会话（轮转在返回前完成，便于调用方先发 200 头）。"""
    _stream, region, bare, client_model = peek_model(body)
    st = ChatStat(start=time.time(), model=bare or "-", mode="stream", echo_model=client_model)
    prepared = await open_stream(state, body, max_rotate=max_rotate, pinned_uid=pinned_uid)
    st.uid = prepared.uid
    st.status = 200
    sess = StreamSession(stat=st, prepared=prepared, lease=_Lease(state, prepared.uid), app_state=state)
    return sess
