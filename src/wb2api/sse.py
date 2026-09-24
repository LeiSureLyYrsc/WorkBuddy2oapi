"""SSE 处理：把上游流聚合成单个响应，或按 OpenAI 规范白名单重建后透传。

对齐旧网关 ``internal/upstream/sse.go``。
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from typing import Any

log = logging.getLogger("wb2api.sse")


def _sort_ints(a: list[int]) -> list[int]:
    return sorted(a)


def _merge_tool_call_delta(merged: dict[str, Any], delta: dict[str, Any]) -> None:
    """把流式 tool_call 片段合并到累计对象。"""
    v = delta.get("id")
    if isinstance(v, str) and v:
        merged["id"] = v
    v = delta.get("type")
    if isinstance(v, str) and v:
        merged["type"] = v
    df = delta.get("function")
    if not isinstance(df, dict):
        return
    mf = merged.get("function")
    if not isinstance(mf, dict):
        mf = {}
        merged["function"] = mf
    name = df.get("name")
    if isinstance(name, str) and name:
        mf["name"] = name
    args = df.get("arguments")
    if isinstance(args, str) and args:
        prev = mf.get("arguments")
        mf["arguments"] = (prev + args) if isinstance(prev, str) and prev else args


async def aggregate(lines: AsyncIterator[str]) -> dict[str, Any]:
    """读取完整 SSE 行流，聚合 delta.content 为单个 OpenAI chat.completion 响应。

    空流（无有效数据事件）抛 ValueError，由上层映射为 502 upstream_parse。
    """
    id_ = ""
    model = ""
    created = 0.0
    content_parts: list[str] = []
    reasoning_parts: list[str] = []
    role = "assistant"
    finish_reason = "stop"
    usage: dict[str, Any] | None = None
    got_any_content = False
    valid_events = 0
    tool_calls: dict[int, dict[str, Any]] = {}
    tool_order: list[int] = []

    async for line in lines:
        line = line.rstrip("\r\n")
        if not line.startswith("data: "):
            continue
        payload = line[len("data: ") :]
        if payload == "[DONE]":
            break
        try:
            chunk = json.loads(payload)
        except ValueError:
            continue
        if not isinstance(chunk, dict):
            continue
        valid_events += 1
        v = chunk.get("id")
        if isinstance(v, str) and v and not id_:
            id_ = v
        v = chunk.get("model")
        if isinstance(v, str) and v and not model:
            model = v
        v = chunk.get("created")
        if isinstance(v, (int, float)) and not created:
            created = float(v)
        u = chunk.get("usage")
        if isinstance(u, dict):
            usage = u
        choices = chunk.get("choices")
        if not isinstance(choices, list):
            continue
        for ci in choices:
            if not isinstance(ci, dict):
                continue
            fr = ci.get("finish_reason")
            if isinstance(fr, str) and fr:
                finish_reason = fr
            delta = ci.get("delta")
            if isinstance(delta, dict):
                r2 = delta.get("role")
                if isinstance(r2, str) and r2:
                    role = r2
                txt = delta.get("content")
                if isinstance(txt, str):
                    content_parts.append(txt)
                    got_any_content = True
                rc = delta.get("reasoning_content")
                if isinstance(rc, str):
                    reasoning_parts.append(rc)
                tcs = delta.get("tool_calls")
                if isinstance(tcs, list):
                    for tc in tcs:
                        if not isinstance(tc, dict):
                            continue
                        idx = 0
                        iv = tc.get("index")
                        if isinstance(iv, (int, float)):
                            idx = int(iv)
                        merged = tool_calls.get(idx)
                        if merged is None:
                            merged = {"index": idx}
                            tool_calls[idx] = merged
                            tool_order.append(idx)
                        _merge_tool_call_delta(merged, tc)
            msg = ci.get("message")
            if isinstance(msg, dict) and not got_any_content:
                txt = msg.get("content")
                if isinstance(txt, str):
                    content_parts.append(txt)

    if valid_events == 0:
        raise ValueError("upstream stream contained no valid data events")

    if not id_:
        import time

        id_ = f"chatcmpl-{time.time_ns()}"
    if not created:
        import time

        created = float(int(time.time()))

    message: dict[str, Any] = {"role": role, "content": "".join(content_parts)}
    reasoning = "".join(reasoning_parts)
    if reasoning:
        message["reasoning_content"] = reasoning
    if tool_order:
        message["tool_calls"] = [tool_calls[i] for i in _sort_ints(tool_order)]

    resp: dict[str, Any] = {
        "id": id_,
        "object": "chat.completion",
        "created": int(created),
        "model": model,
        "choices": [
            {"index": 0, "message": message, "finish_reason": finish_reason}
        ],
    }
    if usage is not None:
        resp["usage"] = usage
    return resp


def normalize_frame(obj: dict[str, Any]) -> dict[str, Any]:
    """按 OpenAI 流式规范白名单重建帧，剔除上游噪声。"""
    out: dict[str, Any] = {}
    for k in ("id", "object", "created", "model", "system_fingerprint", "service_tier"):
        if obj.get(k) is not None:
            out[k] = obj[k]
    out.setdefault("object", "chat.completion.chunk")
    out.setdefault("id", "chatcmpl-wb2api")

    choices = obj.get("choices")
    if isinstance(choices, list):
        new_choices: list[Any] = []
        for ci in choices:
            if not isinstance(ci, dict):
                continue
            nc: dict[str, Any] = {}
            if "index" in ci:
                nc["index"] = ci["index"]
            delta: dict[str, Any] = {}
            d = ci.get("delta")
            if isinstance(d, dict):
                for key in ("role", "content", "reasoning_content", "refusal"):
                    v = d.get(key)
                    if isinstance(v, str) and v:
                        delta[key] = v
                tcs = d.get("tool_calls")
                if isinstance(tcs, list) and tcs:
                    delta["tool_calls"] = tcs
                fc = d.get("function_call")
                if fc is not None:
                    keep = True
                    if isinstance(fc, dict):
                        keep = bool(fc.get("name")) or bool(fc.get("arguments"))
                    if keep:
                        delta["function_call"] = fc
            nc["delta"] = delta
            fr = ci.get("finish_reason")
            nc["finish_reason"] = fr if isinstance(fr, str) and fr else None
            new_choices.append(nc)
        out["choices"] = new_choices

    out["usage"] = obj.get("usage")
    return out


async def stream_frames(
    lines: AsyncIterator[str],
) -> AsyncIterator[str]:
    """把上游 SSE 行流规范化后逐帧产出 payload 字符串（不含 "data: " 前缀）。

    保证恰好一个 ``[DONE]``；空流先产出一帧 error 再产出 ``[DONE]``。
    """
    valid_frames = 0
    done = False
    async for line in lines:
        trimmed = line.rstrip("\r\n")
        if trimmed.startswith("data: [DONE]"):
            done = True
            break
        if trimmed.startswith("data: "):
            payload = trimmed[len("data: ") :]
            try:
                obj = json.loads(payload)
            except ValueError:
                yield payload
                continue
            if isinstance(obj, dict):
                payload = json.dumps(normalize_frame(obj), ensure_ascii=False)
            valid_frames += 1
            yield payload
        elif trimmed:
            # 注释/其他行原样透传
            yield line

    if valid_frames == 0:
        yield '{"error":{"message":"empty upstream stream","type":"upstream_error"}}'
    yield "[DONE]"
    _ = done
