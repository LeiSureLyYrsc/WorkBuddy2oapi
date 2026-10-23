"""出站 chat 请求体改写：强制 stream、归一 tool_choice / role / effort、指纹脱敏。

逐项对齐旧网关 ``internal/upstream/payload.go`` + ``sanitize.go``。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any

from .models import split_region_prefix

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
# 提示词防识别（Prompt Guard）
#
# 上游可能对请求**开头**做指纹/前缀识别。这里提供两种独立手段，默认全关：
#   1. prepend：在 messages[0] 正文前插入固定字符，打散开头前缀。
#   2. truncate：删除命中的固定句段，其余文字原样保留。
# 顺序固定为 truncate -> prepend，保证注入文本落在请求最开头的字节上。
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PromptGuard:
    """提示词防识别配置的不可变快照（从 PromptGuardConfig 转换而来）。"""

    prepend_enabled: bool = False
    prepend_text: str = "你好!"
    truncate_enabled: bool = False
    truncate_scope: str = "all"
    truncate_patterns: tuple[str, ...] = ()

    @classmethod
    def from_config(cls, cfg: Any) -> PromptGuard | None:
        """从 PromptGuardConfig（或其他具备同名属性的对象）构造；None 表示无配置。"""
        if cfg is None:
            return None
        if isinstance(cfg, PromptGuard):
            return cfg
        patterns = getattr(cfg, "truncate_patterns", ()) or ()
        scope = str(getattr(cfg, "truncate_scope", "all") or "all").strip().lower()
        return cls(
            prepend_enabled=bool(getattr(cfg, "prepend_enabled", False)),
            prepend_text=str(getattr(cfg, "prepend_text", "") or ""),
            truncate_enabled=bool(getattr(cfg, "truncate_enabled", False)),
            truncate_scope="first" if scope == "first" else "all",
            truncate_patterns=tuple(p for p in patterns if isinstance(p, str) and p),
        )

    @property
    def active(self) -> bool:
        """是否真的会改写请求体（开关打开但内容为空时视为不生效）。"""
        prepend = self.prepend_enabled and bool(self.prepend_text)
        truncate = self.truncate_enabled and bool(self.truncate_patterns)
        return prepend or truncate


def _truncate_text(text: str, patterns: tuple[str, ...], only_first: bool) -> tuple[str, int]:
    """按字面量（大小写不敏感）删除命中的句段，返回 (新文本, 删除处数)。

    ``only_first=True`` 时最多删除一处后立即返回，用于跨消息共享"只删第一个"语义。

    用 ``re`` 做匹配而不是自己 lower() 后 find()：正则的匹配区间始终是**原串**下标，
    不会因为个别 Unicode（如 ``İ``）小写化后码位数量变化而切错位置。
    """
    if not text or not patterns:
        return text, 0
    hits = 0
    for pat in patterns:
        if not pat:
            continue
        spans = [m.span() for m in re.finditer(re.escape(pat), text, re.IGNORECASE)]
        if not spans:
            continue
        if only_first:
            start, end = spans[0]
            return text[:start] + text[end:], hits + 1
        # 倒序删除：从后往前切，前面区间的下标始终有效。
        for start, end in reversed(spans):
            text = text[:start] + text[end:]
            hits += 1
    return text, hits


def _truncate_part(part: Any, patterns: tuple[str, ...], only_first: bool) -> tuple[Any, int]:
    """处理多模态分片：只改 text 分片的 text 字段，图片等其他分片原样返回。"""
    if not isinstance(part, dict):
        return part, 0
    if part.get("type") not in (None, "text"):
        return part, 0
    body = part.get("text")
    if not isinstance(body, str):
        return part, 0
    new_body, hits = _truncate_text(body, patterns, only_first)
    if hits:
        part["text"] = new_body
    return part, hits


def _truncate_content(content: Any, patterns: tuple[str, ...], only_first: bool) -> tuple[Any, int]:
    """对单条消息的 content 做删除，返回 (content, 删除处数)。"""
    if isinstance(content, str):
        return _truncate_text(content, patterns, only_first)
    if isinstance(content, list):
        hits = 0
        for part in content:
            if only_first and hits:
                break
            _, n = _truncate_part(part, patterns, only_first)
            hits += n
        return content, hits
    return content, 0


def _blank_to_space(content: Any) -> Any:
    """整段被删空时回填一个空格，避免上游对空 content 报错（保留消息本身，不破坏 role 交替）。"""
    if isinstance(content, str) and not content.strip():
        return " "
    if isinstance(content, list):
        for part in content:
            if isinstance(part, dict) and part.get("type") in (None, "text"):
                body = part.get("text")
                if isinstance(body, str) and not body.strip():
                    part["text"] = " "
    return content


def apply_truncate(obj: dict[str, Any], guard: PromptGuard) -> int:
    """删除 messages[].content 中命中的句段，返回删除处数。"""
    messages = obj.get("messages")
    if not isinstance(messages, list):
        return 0
    only_first = guard.truncate_scope == "first"
    total = 0
    for m in messages:
        if not isinstance(m, dict) or "content" not in m:
            continue
        if only_first and total:
            break
        new_content, hits = _truncate_content(m["content"], guard.truncate_patterns, only_first)
        if hits:
            m["content"] = _blank_to_space(new_content)
            total += hits
    return total


def apply_prepend(obj: dict[str, Any], guard: PromptGuard) -> bool:
    """在会话最开头的消息正文前插入 guard.prepend_text。

    已经以该文本开头时跳过（幂等），避免轮转重试出现 "你好!你好!"。
    """
    messages = obj.get("messages")
    if not isinstance(messages, list) or not messages:
        return False
    text = guard.prepend_text
    if not text:
        return False

    first = messages[0]
    if not isinstance(first, dict):
        return False
    content = first.get("content")

    if isinstance(content, str):
        if content.startswith(text):
            return False
        first["content"] = text + content
        return True

    if isinstance(content, list):
        for part in content:
            if isinstance(part, dict) and part.get("type") in (None, "text"):
                body = part.get("text")
                if isinstance(body, str):
                    if body.startswith(text):
                        return False
                    part["text"] = text + body
                    return True
        # 首个文本分片缺失（例如首分片是图片）：在数组开头补一个文本分片。
        content.insert(0, {"type": "text", "text": text})
        return True

    if content is None:
        first["content"] = text
        return True
    return False


def apply_prompt_guard(obj: dict[str, Any], guard: PromptGuard | None) -> None:
    """先截断、再前置（顺序不可调换：前置必须落在请求最开头）。"""
    if guard is None or not guard.active:
        return
    truncated = 0
    if guard.truncate_enabled and guard.truncate_patterns:
        truncated = apply_truncate(obj, guard)
    prepended = False
    if guard.prepend_enabled and guard.prepend_text:
        prepended = apply_prepend(obj, guard)
    if truncated or prepended:
        log.info("prompt_guard applied truncated=%d prepended=%s", truncated, prepended)


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------


def prepare_body(
    src: bytes | str,
    sanitize: bool = True,
    efforts: dict[str, list[str]] | None = None,
    guard: PromptGuard | None = None,
) -> bytes:
    """单 pass 改写出站 chat 请求体。

    sanitize=False 时仍强制 stream 并归一 tool_choice/role（协议兼容，非内容脱敏）。
    guard 为 None 或未启用时不做任何提示词改写（默认行为不变）。
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
    if isinstance(obj.get("model"), str):
        obj["model"] = split_region_prefix(obj["model"])[1]
    normalize_reasoning_effort(obj, efforts)
    if sanitize and isinstance(obj.get("messages"), list):
        sanitize_messages(obj["messages"])
    apply_prompt_guard(obj, guard)

    return json.dumps(obj, ensure_ascii=False).encode()
