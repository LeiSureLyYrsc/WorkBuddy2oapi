"""出站 chat 请求体改写：强制 stream、归一 tool_choice / role / effort、指纹脱敏。

逐项对齐旧网关 ``internal/upstream/payload.go`` + ``sanitize.go``。
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

log = logging.getLogger("wb2api.payload")

# ---------------------------------------------------------------------------
# reasoning_effort 降级
# ---------------------------------------------------------------------------

EFFORT_RANK = {
    "off": 0,
    "minimal": 1,
    "low": 2,
    "medium": 3,
    "high": 4,
    "xhigh": 5,
    "max": 6,
}


def normalize_reasoning_effort(obj: dict[str, Any], efforts: dict[str, list[str]] | None) -> None:
    """按模型 supportedEfforts 降级 reasoning_effort（snake/camel 双字段兼容）。"""
    if not efforts:
        return
    model = obj.get("model")
    if not isinstance(model, str) or not model:
        return
    supported = efforts.get(model)
    if not supported:
        return

    key = ""
    if "reasoning_effort" in obj:
        key = "reasoning_effort"
    elif "reasoningEffort" in obj:
        key = "reasoningEffort"
    else:
        return

    req_raw = obj.get(key)
    if not isinstance(req_raw, str):
        return
    req = req_raw.strip().lower()
    req_idx = EFFORT_RANK.get(req)
    if req_idx is None:
        return

    best, best_idx = "", -1
    for s in supported:
        idx = EFFORT_RANK.get(str(s).strip().lower())
        if idx is not None and idx <= req_idx and idx > best_idx:
            best, best_idx = s, idx
    if best:
        if best.lower() != req:
            obj[key] = best
            log.info("reasoning_effort downgraded model=%s %s -> %s", model, req, best)
        return

    lowest, lowest_idx = "", 1 << 30
    for s in supported:
        idx = EFFORT_RANK.get(str(s).strip().lower())
        if idx is not None and idx < lowest_idx:
            lowest, lowest_idx = s, idx
    if lowest:
        obj[key] = lowest
        log.info("reasoning_effort floored model=%s %s -> %s", model, req, lowest)


# ---------------------------------------------------------------------------
# role 归一：developer -> system
# ---------------------------------------------------------------------------


def normalize_roles(obj: dict[str, Any]) -> None:
    msgs = obj.get("messages")
    if not isinstance(msgs, list):
        return
    for i, m in enumerate(msgs):
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        if isinstance(role, str) and role.strip().lower() == "developer":
            m["role"] = "system"
            log.info("role normalized developer->system idx=%d", i)


# ---------------------------------------------------------------------------
# tool_choice 归一（上游该字段是 string）
# ---------------------------------------------------------------------------


def normalize_tool_choice(obj: dict[str, Any]) -> None:
    def suppress() -> None:
        obj.pop("tools", None)
        obj.pop("functions", None)

    if "tool_choice" not in obj:
        return
    tc = obj["tool_choice"]
    if isinstance(tc, str):
        if tc.strip().lower() == "none":
            obj.pop("tool_choice", None)
            suppress()
        return
    if isinstance(tc, dict):
        typ = str(tc.get("type", "")).strip().lower()
        if typ == "none":
            obj.pop("tool_choice", None)
            suppress()
        elif typ in ("auto", "required"):
            obj["tool_choice"] = typ
        elif typ == "function":
            name = ""
            fn = tc.get("function")
            if isinstance(fn, dict):
                name = str(fn.get("name", "") or "")
            if not name:
                name = str(tc.get("name", "") or "")
            name = name.strip()
            obj["tool_choice"] = name if name else "auto"
        else:
            obj.pop("tool_choice", None)
        return
    obj.pop("tool_choice", None)


# ---------------------------------------------------------------------------
# 指纹脱敏
# ---------------------------------------------------------------------------

_SANITIZE_FEATURES = (
    "x-anthropic-billing-header",
    "cc_entrypoint=",
    "You are Claude Code",
    "Main branch (",
)

_SANITIZE_HDR_RE = re.compile(r"(?i)x-anthropic-billing-header:[^;\n]*;?\s*")
_SANITIZE_KV_RE = re.compile(r"(?i)\bcc_[a-z0-9_]+=[^;\n]*;?\s*")

_SANITIZE_REWRITES = (
    (
        "You are Claude Code, Anthropic's official CLI for Claude.",
        "You are Claude Code, Anthropic's official CLI tool for Claude.",
    ),
    (
        "Main branch (you will usually use this for PRs)",
        "Default branch (you will usually use this for PRs)",
    ),
)


def _has_fingerprint(text: str) -> bool:
    for f in _SANITIZE_FEATURES:
        if f in text:
            return True
    return bool(_SANITIZE_HDR_RE.search(text))


def sanitize_text(text: str) -> str:
    if not _has_fingerprint(text):
        return text
    for old, new in _SANITIZE_REWRITES:
        text = text.replace(old, new)
    if _SANITIZE_HDR_RE.search(text):
        text = _SANITIZE_HDR_RE.sub("", text)
    if "cc_" in text:
        prev = None
        while prev != text:
            prev = text
            text = _SANITIZE_KV_RE.sub("", text)
    return text.strip()


def sanitize_content(value: Any) -> Any:
    if isinstance(value, str):
        return sanitize_text(value)
    if isinstance(value, list):
        for part in value:
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                part["text"] = sanitize_text(part["text"])
        return value
    return value


def sanitize_messages(messages: list[Any]) -> None:
    for m in messages:
        if isinstance(m, dict) and "content" in m:
            m["content"] = sanitize_content(m["content"])


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------


def prepare_body(
    src: bytes | str,
    sanitize: bool = True,
    efforts: dict[str, list[str]] | None = None,
) -> bytes:
    """单 pass 改写出站 chat 请求体。

    sanitize=False 时仍强制 stream 并归一 tool_choice/role（协议兼容，非内容脱敏）。
    """
    if not src:
        return src if isinstance(src, bytes) else src.encode()
    raw = src.decode("utf-8") if isinstance(src, bytes) else src
    try:
        obj = json.loads(raw)
    except (ValueError, TypeError):
        return raw.encode()
    if not isinstance(obj, dict):
        return raw.encode()

    obj["stream"] = True
    normalize_tool_choice(obj)
    normalize_roles(obj)
    normalize_reasoning_effort(obj, efforts)
    if sanitize and isinstance(obj.get("messages"), list):
        sanitize_messages(obj["messages"])

    return json.dumps(obj, ensure_ascii=False).encode()
