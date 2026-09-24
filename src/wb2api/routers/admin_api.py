"""控制台管理 HTTP 路由（admin_api）：提供控制台各项功能接口与流式聊天。

对齐 Go 实现：
- workbuddy2api-gui/internal/api/server.go
- workbuddy2api-gui/internal/api/session.go
所有端点均经过严格会话鉴权（/api/session 与 /api/login 除外）。
Docker 重启逻辑已完全移除，支持账号与配置全链路热加载。
"""

from __future__ import annotations

import hmac
import json
import logging
import os
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response, status
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

from wb2api.chat_service import NoHealthyAccount, chat_non_stream, open_stream_session
from wb2api.deps import SESSION_COOKIE, SessionStore, StateDep, require_session
from wb2api.oauth import normalize_region
from wb2api.ops import DangerousDisabledError, ReadOnlyError, Service
from wb2api.routers.openai_api import models as get_openai_models

logger = logging.getLogger("wb2api.admin_api")

router = APIRouter(prefix="/api")


# ---------------------------------------------------------------------------
# 错误响应辅助函数
# ---------------------------------------------------------------------------


def handle_error(err: Exception) -> JSONResponse:
    """把业务异常映射为前端约定的 HTTP 错误响应。"""
    if isinstance(err, ReadOnlyError):
        return JSONResponse(
            status_code=status.HTTP_403_FORBIDDEN,
            content={"error": str(err), "code": "read_only"},
        )
    if isinstance(err, DangerousDisabledError):
        return JSONResponse(
            status_code=status.HTTP_403_FORBIDDEN,
            content={"error": str(err), "code": "dangerous_ops_disabled"},
        )
    msg = str(err)
    if "不存在" in msg or isinstance(err, (FileNotFoundError, KeyError)):
        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND,
            content={"error": msg},
        )
    return JSONResponse(
        status_code=status.HTTP_400_BAD_REQUEST,
        content={"error": msg},
    )


def _get_session_store(request: Request) -> SessionStore:
    """获取会话存储对象，缺省时自动兜底初始化。"""
    store = getattr(request.app.state, "sessions", None)
    if store is None:
        store = SessionStore()
        request.app.state.sessions = store
    return store


# ---------------------------------------------------------------------------
# 1. 会话与安全
# ---------------------------------------------------------------------------


@router.get("/session")
async def session_info(request: Request, state: StateDep) -> dict[str, Any]:
    """返回当前会话信息（前端首屏初始化调用，公开端点）。"""
    store = _get_session_store(request)
    token = request.cookies.get(SESSION_COOKIE, "")
    if not token:
        authz = request.headers.get("authorization", "")
        if authz.startswith("Bearer "):
            token = authz[len("Bearer ") :]

    user = store.validate(token) if token else None
    authed = bool(user)
    port = state.cfg.host_port[1]

    return {
        "authenticated": authed,
        "username": user or "",
        "read_only": state.cfg.console.read_only,
        "dangerous_ops": state.cfg.console.dangerous_ops,
        "using_default_password": state.cfg.using_default_password(),
        "gateway_url": f"http://127.0.0.1:{port}",
        "password_changeable": bool(state.cfg.console.credentials_file),
    }


class LoginRequest(BaseModel):
    username: str
    password: str


@router.post("/login")
async def login(
    request: Request,
    response: Response,
    state: StateDep,
    payload: LoginRequest,
) -> Any:
    """控制台登录，成功后写入 HttpOnly SameSite=Strict Cookie（公开端点）。"""
    store = _get_session_store(request)
    source = request.client.host if request.client else "127.0.0.1"
    token, err = store.create(payload.username, payload.password, state.cfg.console, source)
    if not token:
        return JSONResponse(
            status_code=status.HTTP_401_UNAUTHORIZED,
            content={"error": err or "用户名或密码错误"},
        )

    response.set_cookie(
        SESSION_COOKIE,
        token,
        httponly=True,
        samesite="strict",
        max_age=int(store.ttl),
    )
    return {"token": token, "username": payload.username}


@router.post("/logout", dependencies=[Depends(require_session)])
async def logout(request: Request, response: Response) -> dict[str, Any]:
    """注销当前会话并清除 Cookie。"""
    store = _get_session_store(request)
    token = request.cookies.get(SESSION_COOKIE, "")
    if not token:
        authz = request.headers.get("authorization", "")
        if authz.startswith("Bearer "):
            token = authz[len("Bearer ") :]
    if token:
        store.revoke(token)

    response.delete_cookie(SESSION_COOKIE, path="/", samesite="strict", httponly=True)
    return {"ok": True}


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str
    new_username: str | None = None


@router.post("/password", dependencies=[Depends(require_session)])
async def change_password(
    request: Request,
    response: Response,
    state: StateDep,
    payload: ChangePasswordRequest,
) -> Any:
    """修改控制台密码并持久化到 credentials_file。"""
    cred_file = state.cfg.console.credentials_file
    if not cred_file:
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={
                "error": "服务端未配置凭据持久化路径（credentials_file），无法保存新密码",
                "code": "not_supported",
            },
        )

    current_pass = state.cfg.console.password or ""
    if not hmac.compare_digest(payload.current_password or "", current_pass):
        return JSONResponse(
            status_code=status.HTTP_403_FORBIDDEN,
            content={"error": "当前口令不正确", "code": "wrong_password"},
        )

    if not payload.new_password:
        return JSONResponse(status_code=status.HTTP_400_BAD_REQUEST, content={"error": "新口令不能为空"})
    if len(payload.new_password) < 6:
        return JSONResponse(status_code=status.HTTP_400_BAD_REQUEST, content={"error": "新口令至少 6 位"})

    target_username = (
        payload.new_username.strip()
        if payload.new_username and payload.new_username.strip()
        else state.cfg.console.username
    )

    cred_path = Path(cred_file)
    cred_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = cred_path.with_name(f"{cred_path.name}.tmp")
    data_payload = json.dumps({"username": target_username, "password": payload.new_password}, indent=2, ensure_ascii=False) + "\n"
    tmp_path.write_text(data_payload, encoding="utf-8")
    try:
        os.chmod(tmp_path, 0o600)
    except OSError:
        pass
    os.replace(tmp_path, cred_path)
    try:
        os.chmod(cred_path, 0o600)
    except OSError:
        pass

    state.config_manager.current.console.username = target_username
    state.config_manager.current.console.password = payload.new_password

    store = _get_session_store(request)
    store.revoke_all()

    source = request.client.host if request.client else "127.0.0.1"
    token, _ = store.create(target_username, payload.new_password, state.cfg.console, source)
    if token:
        response.set_cookie(
            SESSION_COOKIE,
            token,
            httponly=True,
            samesite="strict",
            max_age=int(store.ttl),
        )

    return {
        "ok": True,
        "username": target_username,
        "message": "口令已修改并保存，重启面板后依然生效",
    }


# ---------------------------------------------------------------------------
# 2. 总览与账号管理
# ---------------------------------------------------------------------------


@router.get("/overview", dependencies=[Depends(require_session)])
async def get_overview(state: StateDep) -> Any:
    """获取仪表盘总览信息。"""
    svc = Service(state)
    try:
        return svc.overview()
    except Exception as e:
        return handle_error(e)


@router.get("/accounts", dependencies=[Depends(require_session)])
async def list_accounts(state: StateDep) -> Any:
    """获取合并账号列表。"""
    svc = Service(state)
    try:
        accounts, issues = svc.accounts()
        total, healthy, cooling, disabled, in_flight_full = state.pool.counts_detailed()
        summary = {
            "total": total,
            "healthy": healthy,
            "cooling": cooling,
            "disabled": disabled,
            "in_flight_full": in_flight_full,
            "sticky_sessions": state.sticky.count(),
            "redis_mode": "noop",
        }
        return {
            "accounts": accounts,
            "file_issues": issues,
            "gateway_ok": True,
            "summary": summary,
        }
    except Exception as e:
        return handle_error(e)


@router.get("/accounts/{uid}", dependencies=[Depends(require_session)])
async def get_account(uid: str, state: StateDep, silent: bool = Query(False)) -> Any:
    """获取单个账号详情。"""
    svc = Service(state)
    try:
        return await svc.profile(uid, silent=silent)
    except Exception as e:
        return handle_error(e)


@router.delete("/accounts/{uid}", dependencies=[Depends(require_session)])
async def delete_account(uid: str, state: StateDep, confirm: str = Query("")) -> Any:
    """删除账号凭证并从池中移除（需二次确认）。"""
    if confirm != uid:
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={
                "error": "删除账号需确认：请在请求中带上 ?confirm=<uid>",
                "code": "confirm_required",
            },
        )
    svc = Service(state)
    try:
        svc.delete_account(uid)
        return {"ok": True, "message": "账号已删除并移出账号池"}
    except Exception as e:
        return handle_error(e)


@router.post("/accounts/import", dependencies=[Depends(require_session)])
async def import_account(state: StateDep, payload: dict[str, Any]) -> Any:
    """手动导入账号凭证并热加载。"""
    svc = Service(state)
    try:
        res = svc.import_account(payload)
        status_code = status.HTTP_200_OK if res.get("ok") else status.HTTP_400_BAD_REQUEST
        return JSONResponse(status_code=status_code, content=res)
    except Exception as e:
        return handle_error(e)


@router.post("/accounts/sync", dependencies=[Depends(require_session)])
async def sync_accounts(state: StateDep) -> Any:
    """从磁盘重新同步账号凭证到账号池（外部新增/删除文件后调用），无需重启。"""
    svc = Service(state)
    try:
        return svc.sync_accounts()
    except Exception as e:
        return handle_error(e)


@router.post("/accounts/{uid}/checkin", dependencies=[Depends(require_session)])
async def account_checkin(uid: str, state: StateDep) -> Any:
    """单账号签到。"""
    svc = Service(state)
    try:
        return await svc.checkin(uid)
    except Exception as e:
        return handle_error(e)


@router.post("/accounts/{uid}/refresh", dependencies=[Depends(require_session)])
async def account_refresh(uid: str, state: StateDep) -> Any:
    """单账号 Token 刷新。"""
    svc = Service(state)
    try:
        return await svc.refresh(uid)
    except Exception as e:
        return handle_error(e)


@router.post("/accounts/{uid}/travel", dependencies=[Depends(require_session)])
async def account_travel(uid: str, state: StateDep) -> Any:
    """单账号猫猫旅行。"""
    svc = Service(state)
    try:
        return await svc.travel(uid)
    except Exception as e:
        return handle_error(e)


@router.post("/accounts/{uid}/credits", dependencies=[Depends(require_session)])
async def account_credits(uid: str, state: StateDep) -> Any:
    """单账号积分查询。"""
    svc = Service(state)
    try:
        return await svc.credits_for(uid)
    except Exception as e:
        return handle_error(e)


# ---------------------------------------------------------------------------
# 3. 批量任务
# ---------------------------------------------------------------------------


class BatchTaskRequest(BaseModel):
    uids: list[str] | None = None


@router.post("/tasks/{action}", status_code=status.HTTP_202_ACCEPTED, dependencies=[Depends(require_session)])
async def create_batch_task(
    action: str,
    state: StateDep,
    payload: BatchTaskRequest | None = None,
) -> Any:
    """创建异步批量任务（checkin|refresh|travel|credits）。"""
    if action not in ("checkin", "refresh", "travel", "credits"):
        raise HTTPException(status_code=404, detail="未知任务类型")

    uids = payload.uids if payload else None
    svc = Service(state)
    try:
        if action == "checkin":
            return svc.batch_checkin(uids)
        elif action == "refresh":
            return svc.batch_refresh(uids)
        elif action == "travel":
            return svc.batch_travel(uids)
        elif action == "credits":
            return svc.batch_credits(uids)
    except Exception as e:
        return handle_error(e)


@router.get("/tasks", dependencies=[Depends(require_session)])
async def list_tasks(state: StateDep) -> dict[str, Any]:
    """获取最近任务与正在运行的任务列表。"""
    return {
        "tasks": state.tasks.recent(20),
        "running": state.tasks.running(),
    }


@router.get("/tasks/{id}", dependencies=[Depends(require_session)])
async def get_task(id: str, state: StateDep) -> Any:
    """获取指定任务快照与明细。"""
    task = state.tasks.get(id)
    if not task:
        return JSONResponse(status_code=status.HTTP_404_NOT_FOUND, content={"error": "任务不存在或已过期"})
    return task


# ---------------------------------------------------------------------------
# 4. OAuth 网页登录
# ---------------------------------------------------------------------------


class LoginStartRequest(BaseModel):
    region: str = "cn"


@router.post("/login/start", dependencies=[Depends(require_session)])
async def login_start(
    state: StateDep,
    payload: LoginStartRequest | None = None,
) -> Any:
    """发起一次 OAuth 网页登录会话。"""
    region = payload.region if payload else "cn"
    try:
        norm_region = normalize_region(region)
    except ValueError as e:
        return JSONResponse(status_code=status.HTTP_400_BAD_REQUEST, content={"error": str(e)})

    svc = Service(state)
    try:
        sess = await svc.start_login(norm_region)
        return sess.to_dict()
    except Exception as e:
        return handle_error(e)


@router.get("/login/{id}", dependencies=[Depends(require_session)])
async def login_status(id: str, state: StateDep) -> Any:
    """获取登录会话状态。"""
    svc = Service(state)
    try:
        sess = svc.login_status(id)
        if not sess:
            return JSONResponse(status_code=status.HTTP_404_NOT_FOUND, content={"error": "登录会话不存在或已过期"})
        return sess.to_dict()
    except Exception as e:
        return handle_error(e)


@router.post("/login/{id}/poll", dependencies=[Depends(require_session)])
async def login_poll(id: str, state: StateDep) -> Any:
    """轮询一次登录授权状态。"""
    svc = Service(state)
    try:
        sess = await svc.poll_login(id)
        return sess.to_dict()
    except Exception as e:
        return handle_error(e)


@router.post("/login/{id}/cancel", dependencies=[Depends(require_session)])
async def login_cancel(id: str, state: StateDep) -> Any:
    """取消登录会话。"""
    svc = Service(state)
    try:
        sess = svc.cancel_login(id)
        return sess.to_dict()
    except Exception as e:
        return handle_error(e)


# ---------------------------------------------------------------------------
# 5. 模型、统计与价格表
# ---------------------------------------------------------------------------


@router.get("/models", dependencies=[Depends(require_session)])
async def list_models(state: StateDep) -> Any:
    """获取可用模型列表（与网关端点逻辑完全对齐）。"""
    try:
        res = await get_openai_models(state)
        data = res.get("data") or []
        return {"data": data, "count": len(data)}
    except Exception as e:
        return handle_error(e)


@router.get("/stats", dependencies=[Depends(require_session)])
async def get_stats(
    state: StateDep,
    mode: str = Query("peak"),
    range: str | None = Query(None),
    from_: str | None = Query(None, alias="from"),
    to: str | None = Query(None),
    interval: str | None = Query(None),
    model: str | None = Query(None),
) -> Any:
    """获取请求统计与官方价换算。"""
    svc = Service(state)
    try:
        return svc.stats(
            mode=mode,
            range_param=range,
            from_param=from_,
            to_param=to,
            interval_param=interval,
            model_param=model,
        )
    except Exception as e:
        return handle_error(e)


@router.post("/stats/reset", dependencies=[Depends(require_session)])
async def reset_stats(state: StateDep) -> Any:
    """重置调用统计。"""
    svc = Service(state)
    try:
        svc.ensure_writable()
        state.metrics.reset()
        return {"ok": True, "message": "网关统计已重置"}
    except Exception as e:
        return handle_error(e)


@router.put("/pricing", dependencies=[Depends(require_session)])
async def update_pricing(state: StateDep, payload: dict[str, Any]) -> Any:
    """新增或更新模型官方单价。"""
    svc = Service(state)
    try:
        svc.pricing_update(payload)
        return {"ok": True, "message": "价格已保存"}
    except Exception as e:
        return handle_error(e)


@router.delete("/pricing/{model}", dependencies=[Depends(require_session)])
async def delete_pricing(model: str, state: StateDep) -> Any:
    """删除指定模型的官方单价。"""
    svc = Service(state)
    try:
        svc.pricing_delete(model)
        return {"ok": True, "message": "已移除该模型价格"}
    except Exception as e:
        return handle_error(e)


# ---------------------------------------------------------------------------
# 6. 控制台聊天测试台
# ---------------------------------------------------------------------------


class ChatPayload(BaseModel):
    model: str
    messages: list[dict[str, Any]]
    stream: bool = False
    extra: dict[str, Any] | None = None


def build_chat_body(
    model: str,
    messages: list[dict[str, Any]],
    stream: bool = False,
    extra: dict[str, Any] | None = None,
    conversation_id: str | None = None,
) -> bytes:
    """组装聊天请求体，并注入 conversation_id 支持多轮会话粘性。"""
    if not model or not model.strip():
        raise ValueError("请选择模型")
    if not messages:
        raise ValueError("消息不能为空")

    body_dict: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "stream": stream,
    }
    if extra and isinstance(extra, dict):
        for k, v in extra.items():
            if k in ("model", "messages", "stream", "conversation_id", "metadata"):
                continue
            body_dict[k] = v

    if conversation_id:
        body_dict["conversation_id"] = conversation_id
        body_dict["metadata"] = {"conversation_id": conversation_id}

    return json.dumps(body_dict, ensure_ascii=False).encode("utf-8")


@router.post("/chat", dependencies=[Depends(require_session)])
async def chat(
    state: StateDep,
    payload: ChatPayload,
    x_conversation_id: str | None = Header(None, alias="X-Conversation-Id"),
) -> Any:
    """非流式聊天测试，返回前端约定的 ChatResult 结构。"""
    try:
        body = build_chat_body(
            model=payload.model,
            messages=payload.messages,
            stream=False,
            extra=payload.extra,
            conversation_id=x_conversation_id,
        )
    except ValueError as e:
        return JSONResponse(status_code=status.HTTP_400_BAD_REQUEST, content={"error": str(e)})

    start_time = time.time()
    try:
        status_code, resp_dict, _stat = await chat_non_stream(state, body)
    except NoHealthyAccount as e:
        return JSONResponse(status_code=status.HTTP_502_BAD_GATEWAY, content={"error": str(e)})
    except Exception as e:
        return handle_error(e)

    if status_code != 200:
        err_msg = resp_dict.get("error") if isinstance(resp_dict, dict) else str(resp_dict)
        if isinstance(err_msg, dict):
            err_msg = err_msg.get("message") or str(err_msg)
        return JSONResponse(status_code=status_code, content={"error": str(err_msg or f"HTTP {status_code}")})

    choices = resp_dict.get("choices") or []
    content = ""
    reasoning_content = ""
    finish_reason = ""
    if choices and isinstance(choices[0], dict):
        msg = choices[0].get("message") or {}
        content = msg.get("content") or ""
        reasoning_content = msg.get("reasoning_content") or ""
        finish_reason = choices[0].get("finish_reason") or ""

    usage = resp_dict.get("usage") or {}
    prompt_tokens = usage.get("prompt_tokens") or 0
    completion_tokens = usage.get("completion_tokens") or 0
    total_tokens = usage.get("total_tokens") or 0

    return {
        "result": {
            "content": content,
            "reasoning_content": reasoning_content,
            "model": resp_dict.get("model") or payload.model,
            "finish_reason": finish_reason,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens,
            "raw": json.dumps(resp_dict, ensure_ascii=False),
        },
        "elapsed_ms": int((time.time() - start_time) * 1000),
    }


@router.post("/chat/stream", dependencies=[Depends(require_session)])
async def chat_stream(
    state: StateDep,
    payload: ChatPayload,
    x_conversation_id: str | None = Header(None, alias="X-Conversation-Id"),
) -> StreamingResponse:
    """流式聊天测试，向下游按 GUI 规范产出 SSE 增量事件帧。"""
    try:
        body = build_chat_body(
            model=payload.model,
            messages=payload.messages,
            stream=True,
            extra=payload.extra,
            conversation_id=x_conversation_id,
        )
    except ValueError as e:

        async def err_gen() -> AsyncIterator[str]:
            yield f"data: {json.dumps({'error': str(e)}, ensure_ascii=False)}\n\n"

        return StreamingResponse(
            err_gen(),
            media_type="text/event-stream; charset=utf-8",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    try:
        session = await open_stream_session(state, body)
    except Exception as e:

        async def err_gen2() -> AsyncIterator[str]:
            yield f"data: {json.dumps({'error': str(e)}, ensure_ascii=False)}\n\n"

        return StreamingResponse(
            err_gen2(),
            media_type="text/event-stream; charset=utf-8",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    async def event_generator() -> AsyncIterator[str]:
        start_time = time.time()
        first_token_time = 0.0
        try:
            async for frame in session.frames():
                if frame == "[DONE]":
                    break
                try:
                    obj = json.loads(frame)
                except Exception:
                    continue

                if isinstance(obj, dict) and "error" in obj:
                    err_val = obj["error"]
                    err_msg = err_val.get("message") if isinstance(err_val, dict) else str(err_val)
                    yield f"data: {json.dumps({'error': str(err_msg)}, ensure_ascii=False)}\n\n"
                    continue

                delta_dict: dict[str, Any] = {}
                choices = obj.get("choices") or []
                if choices and isinstance(choices[0], dict):
                    c0 = choices[0]
                    delta = c0.get("delta") or {}
                    c = delta.get("content")
                    if c is not None:
                        delta_dict["content"] = c
                        if first_token_time == 0.0 and c:
                            first_token_time = time.time()
                    r = delta.get("reasoning_content")
                    if r is not None:
                        delta_dict["reasoning"] = r
                    fr = c0.get("finish_reason")
                    if fr:
                        delta_dict["done"] = True

                u = obj.get("usage")
                if isinstance(u, dict):
                    delta_dict["usage"] = {
                        "prompt_tokens": int(u.get("prompt_tokens") or 0),
                        "completion_tokens": int(u.get("completion_tokens") or 0),
                        "total_tokens": int(u.get("total_tokens") or 0),
                    }

                if delta_dict:
                    yield f"data: {json.dumps(delta_dict, ensure_ascii=False)}\n\n"

            elapsed_ms = int((time.time() - start_time) * 1000)
            ttfb_ms = int((first_token_time - start_time) * 1000) if first_token_time > 0.0 else 0
            final_delta = {
                "done": True,
                "elapsed_ms": elapsed_ms,
                "ttfb_ms": ttfb_ms,
            }
            yield f"data: {json.dumps(final_delta, ensure_ascii=False)}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'error': str(e)}, ensure_ascii=False)}\n\n"
        finally:
            await session.close()

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream; charset=utf-8",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ---------------------------------------------------------------------------
# 7. 配置与系统管理
# ---------------------------------------------------------------------------


@router.get("/config", dependencies=[Depends(require_session)])
async def get_config(state: StateDep) -> Any:
    """读取网关配置及备份元信息。"""
    svc = Service(state)
    try:
        doc, meta = svc.read_config()
        return {"config": doc, "meta": meta}
    except Exception as e:
        return handle_error(e)


@router.put("/config", dependencies=[Depends(require_session)])
async def put_config(state: StateDep, payload: dict[str, Any]) -> Any:
    """保存网关配置并热应用。"""
    svc = Service(state)
    try:
        ok, msg, _fallback = svc.save_config(payload)
        return {"ok": ok, "message": msg}
    except Exception as e:
        return handle_error(e)


@router.post("/config/reset", dependencies=[Depends(require_session)])
async def reset_config(state: StateDep) -> Any:
    """从备份恢复初始配置。"""
    svc = Service(state)
    try:
        svc.reset_config()
        return {"ok": True, "message": "已从备份恢复初始配置"}
    except Exception as e:
        return handle_error(e)


@router.get("/system", dependencies=[Depends(require_session)])
async def get_system(state: StateDep) -> Any:
    """获取系统运行环境与状态信息。"""
    svc = Service(state)
    try:
        return svc.system()
    except Exception as e:
        return handle_error(e)


@router.post("/system/restart", dependencies=[Depends(require_session)])
async def restart_system() -> Any:
    """系统重启请求（热加载架构下无需重启）。"""
    return {"ok": True, "message": "账号与配置均支持热加载，无需重启"}
