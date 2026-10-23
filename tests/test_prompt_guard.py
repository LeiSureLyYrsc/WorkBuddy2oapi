"""tests/test_prompt_guard.py: 提示词防识别（Prompt Guard）行为测试。

覆盖两种手段：开头插入字符（prepend）与命中句段删除（truncate），以及两者同时开启的顺序。
"""

from __future__ import annotations

import json

import pytest

from wb2api.config import Config, ConfigManager, PromptGuardConfig
from wb2api.payload import PromptGuard, apply_prepend, apply_truncate, prepare_body


def _body(messages: list[dict], model: str = "hy3") -> bytes:
    return json.dumps({"model": model, "messages": messages}).encode()


def _prepend(text: str = "你好!", enabled: bool = True) -> PromptGuard:
    return PromptGuard(prepend_enabled=enabled, prepend_text=text)


def _truncate(patterns: tuple[str, ...], scope: str = "all", enabled: bool = True) -> PromptGuard:
    return PromptGuard(truncate_enabled=enabled, truncate_scope=scope, truncate_patterns=patterns)


# ---------------------------------------------------------------------------
# 默认关闭
# ---------------------------------------------------------------------------


def test_default_config_is_disabled() -> None:
    """默认配置下两个开关都关闭，且不带任何待截断句段。"""
    cfg = PromptGuardConfig()
    assert cfg.prepend_enabled is False
    assert cfg.truncate_enabled is False
    assert cfg.truncate_scope == "all"
    assert cfg.truncate_patterns == []
    assert cfg.prepend_text == "你好!"
    assert PromptGuard.from_config(cfg).active is False


def test_prepare_body_untouched_when_guard_disabled() -> None:
    """guard=None 时请求体内容保持原样（只做协议归一）。"""
    messages = [{"role": "system", "content": "You are an AI agent."}]
    out = json.loads(prepare_body(_body(messages)))
    assert out["messages"][0]["content"] == "You are an AI agent."


def test_prepare_body_untouched_when_guard_inactive() -> None:
    """开关打开但内容为空时视为不生效，请求体不变。"""
    messages = [{"role": "system", "content": "abc"}]
    inactive = PromptGuard(prepend_enabled=True, prepend_text="", truncate_enabled=True, truncate_patterns=())
    out = json.loads(prepare_body(_body(messages), guard=inactive))
    assert out["messages"][0]["content"] == "abc"


# ---------------------------------------------------------------------------
# prepend：会话最开头注入
# ---------------------------------------------------------------------------


def test_prepend_on_system_first_message() -> None:
    messages = [
        {"role": "system", "content": "You are an AI agent."},
        {"role": "user", "content": "hi"},
    ]
    out = json.loads(prepare_body(_body(messages), guard=_prepend()))
    assert out["messages"][0]["content"] == "你好!You are an AI agent."
    # 后续消息不受影响
    assert out["messages"][1]["content"] == "hi"


def test_prepend_on_first_user_message_when_no_system() -> None:
    messages = [{"role": "user", "content": "hi"}]
    out = json.loads(prepare_body(_body(messages), guard=_prepend()))
    assert out["messages"][0]["content"] == "你好!hi"


def test_prepend_is_idempotent() -> None:
    """已经以注入文本开头时不再重复添加（轮转重试安全）。"""
    messages = [{"role": "system", "content": "你好!already"}]
    out = json.loads(prepare_body(_body(messages), guard=_prepend()))
    assert out["messages"][0]["content"] == "你好!already"


def test_prepend_into_multimodal_text_part() -> None:
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "describe this"},
                {"type": "image_url", "image_url": {"url": "http://x/y.png"}},
            ],
        }
    ]
    out = json.loads(prepare_body(_body(messages), guard=_prepend()))
    assert out["messages"][0]["content"][0]["text"] == "你好!describe this"
    assert out["messages"][0]["content"][1]["type"] == "image_url"


def test_prepend_inserts_text_part_when_first_is_image() -> None:
    messages = [
        {
            "role": "user",
            "content": [{"type": "image_url", "image_url": {"url": "http://x/y.png"}}],
        }
    ]
    out = json.loads(prepare_body(_body(messages), guard=_prepend()))
    parts = out["messages"][0]["content"]
    assert parts[0] == {"type": "text", "text": "你好!"}
    assert parts[1]["type"] == "image_url"


def test_prepend_handles_missing_content() -> None:
    messages = [{"role": "system"}]
    out = json.loads(prepare_body(_body(messages), guard=_prepend()))
    assert out["messages"][0]["content"] == "你好!"


def test_prepend_noop_on_empty_messages() -> None:
    obj: dict = {"messages": []}
    assert apply_prepend(obj, _prepend()) is False
    assert obj["messages"] == []


# ---------------------------------------------------------------------------
# truncate：删除命中句段，其余保留
# ---------------------------------------------------------------------------


def test_truncate_removes_only_matched_text() -> None:
    messages = [
        {
            "role": "system",
            "content": "You are an AI agent powered by DeepSeek Harness. Answer briefly.",
        }
    ]
    out = json.loads(
        prepare_body(
            _body(messages),
            guard=_truncate(("You are an AI agent powered by DeepSeek Harness.",)),
        )
    )
    # 命中句段被删除，剩余文字（含其后的空格）原样保留
    assert out["messages"][0]["content"] == " Answer briefly."


def test_truncate_is_case_insensitive() -> None:
    messages = [{"role": "system", "content": "YOU ARE AN AI AGENT. Be nice."}]
    out = json.loads(prepare_body(_body(messages), guard=_truncate(("You are an AI agent.",))))
    assert out["messages"][0]["content"] == " Be nice."


def test_truncate_scope_all_removes_every_hit() -> None:
    messages = [
        {"role": "system", "content": "FINGERPRINT one"},
        {"role": "user", "content": "FINGERPRINT two"},
    ]
    out = json.loads(
        prepare_body(_body(messages), guard=_truncate(("FINGERPRINT",), scope="all"))
    )
    assert out["messages"][0]["content"] == " one"
    assert out["messages"][1]["content"] == " two"


def test_truncate_scope_first_removes_only_first_hit() -> None:
    messages = [
        {"role": "system", "content": "FINGERPRINT one"},
        {"role": "user", "content": "FINGERPRINT two"},
    ]
    out = json.loads(
        prepare_body(_body(messages), guard=_truncate(("FINGERPRINT",), scope="first"))
    )
    assert out["messages"][0]["content"] == " one"
    # 第二处命中保留
    assert out["messages"][1]["content"] == "FINGERPRINT two"


def test_truncate_multiple_patterns() -> None:
    messages = [{"role": "system", "content": "AAA middle BBB"}]
    out = json.loads(
        prepare_body(_body(messages), guard=_truncate(("AAA", "BBB")))
    )
    assert out["messages"][0]["content"] == " middle "


def test_truncate_multimodal_parts() -> None:
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "FINGERPRINT keep me"},
                {"type": "image_url", "image_url": {"url": "http://x/y.png"}},
            ],
        }
    ]
    out = json.loads(prepare_body(_body(messages), guard=_truncate(("FINGERPRINT",))))
    parts = out["messages"][0]["content"]
    assert parts[0]["text"] == " keep me"
    # 非文本分片原样保留
    assert parts[1]["type"] == "image_url"


def test_truncate_blanks_empty_message_to_space() -> None:
    """整段被删空时回填空格，避免上游对空 content 报错。"""
    messages = [{"role": "system", "content": "FINGERPRINT"}]
    out = json.loads(prepare_body(_body(messages), guard=_truncate(("FINGERPRINT",))))
    assert out["messages"][0]["content"] == " "
    # 消息本身不被删除
    assert len(out["messages"]) == 1


def test_truncate_no_match_keeps_body_identical() -> None:
    messages = [{"role": "system", "content": "nothing to strip"}]
    out = json.loads(prepare_body(_body(messages), guard=_truncate(("ABSENT",))))
    assert out["messages"][0]["content"] == "nothing to strip"


def test_apply_truncate_returns_hit_count() -> None:
    obj = {"messages": [{"role": "user", "content": "X X"}]}
    assert apply_truncate(obj, _truncate(("X",))) == 2


def test_truncate_survives_length_changing_case_fold() -> None:
    """İ 小写化后码位数量会变；用 re 匹配区间切原串，不能错位。"""
    obj = {"messages": [{"role": "user", "content": "İ marker İ marker"}]}
    assert apply_truncate(obj, _truncate(("İ",))) == 2
    assert obj["messages"][0]["content"] == " marker  marker"


def test_truncate_case_insensitive_with_turkish_text() -> None:
    """无大小写差异的句段仍能命中，且不会因为同段出现 İ 而失效。"""
    obj = {"messages": [{"role": "user", "content": "İ abc İ abc"}]}
    assert apply_truncate(obj, _truncate(("ABC",))) == 2
    assert obj["messages"][0]["content"] == "İ  İ "


# ---------------------------------------------------------------------------
# 同时开启：先截断再前置
# ---------------------------------------------------------------------------


def test_both_enabled_truncate_then_prepend() -> None:
    guard = PromptGuard(
        prepend_enabled=True,
        prepend_text="你好!",
        truncate_enabled=True,
        truncate_scope="all",
        truncate_patterns=("You are an AI agent powered by DeepSeek Harness.",),
    )
    messages = [
        {
            "role": "system",
            "content": "You are an AI agent powered by DeepSeek Harness. Answer briefly.",
        }
    ]
    out = json.loads(prepare_body(_body(messages), guard=guard))
    text = out["messages"][0]["content"]
    # 前置字符必须在最开头，且被命中句段已删除
    assert text.startswith("你好!")
    assert "DeepSeek Harness" not in text
    # 删除的是命中的那段字面量，其后的空格属于"其余内容"，原样保留
    assert text == "你好! Answer briefly."


def test_both_enabled_when_pattern_was_the_whole_prefix() -> None:
    """命中句段原本占据开头时，先删再前置仍能保证注入文本在最前面。"""
    guard = PromptGuard(
        prepend_enabled=True,
        prepend_text="Hi ",
        truncate_enabled=True,
        truncate_patterns=("You are an AI agent.",),
    )
    out = json.loads(prepare_body(_body([{"role": "system", "content": "You are an AI agent.Hello"}]), guard=guard))
    # 命中句段删除后剩余 "Hello"，前置文本直接拼在其前
    assert out["messages"][0]["content"] == "Hi Hello"


# ---------------------------------------------------------------------------
# 不改动调用方传入的 body（多账号轮转复用同一份原始 body）
# ---------------------------------------------------------------------------


def test_prepare_body_does_not_mutate_source_bytes() -> None:
    raw = _body([{"role": "system", "content": "FINGERPRINT keep"}])
    before = bytes(raw)
    prepare_body(raw, guard=_truncate(("FINGERPRINT",)))
    assert raw == before


def test_prepare_body_is_repeatable_on_same_source() -> None:
    raw = _body([{"role": "system", "content": "abc"}])
    guard = _prepend()
    first = json.loads(prepare_body(raw, guard=guard))
    second = json.loads(prepare_body(raw, guard=guard))
    assert first == second
    assert second["messages"][0]["content"] == "你好!abc"


# ---------------------------------------------------------------------------
# 配置接入：校验器 + 环境变量
# ---------------------------------------------------------------------------


def test_scope_validator_rejects_unknown_value() -> None:
    with pytest.raises(ValueError):
        PromptGuardConfig(truncate_scope="sometimes")


def test_scope_validator_normalizes_case() -> None:
    assert PromptGuardConfig(truncate_scope="FIRST").truncate_scope == "first"
    assert PromptGuardConfig(truncate_scope=" All ").truncate_scope == "all"


def test_patterns_validator_strips_and_dedups() -> None:
    cfg = PromptGuardConfig(truncate_patterns=["  a  ", "", "   ", "a", "b"])
    assert cfg.truncate_patterns == ["a", "b"]


def test_config_serializes_prompt_guard() -> None:
    cfg = Config()
    doc = cfg.public_dict()
    assert doc["prompt_guard"]["prepend_enabled"] is False
    assert doc["prompt_guard"]["prepend_text"] == "你好!"
    assert doc["prompt_guard"]["truncate_enabled"] is False
    assert doc["prompt_guard"]["truncate_scope"] == "all"
    assert doc["prompt_guard"]["truncate_patterns"] == []


def test_env_overrides_prompt_guard(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("WB2API_PROMPT_PREPEND_ENABLED", "true")
    monkeypatch.setenv("WB2API_PROMPT_PREPEND_TEXT", "Hi!")
    monkeypatch.setenv("WB2API_PROMPT_TRUNCATE_ENABLED", "1")
    monkeypatch.setenv("WB2API_PROMPT_TRUNCATE_SCOPE", "first")
    monkeypatch.setenv(
        "WB2API_PROMPT_TRUNCATE_PATTERNS",
        "You are an AI agent.|Another marker",
    )
    cfg = ConfigManager(str(tmp_path / "config.json")).current
    assert cfg.prompt_guard.prepend_enabled is True
    assert cfg.prompt_guard.prepend_text == "Hi!"
    assert cfg.prompt_guard.truncate_enabled is True
    assert cfg.prompt_guard.truncate_scope == "first"
    assert cfg.prompt_guard.truncate_patterns == ["You are an AI agent.", "Another marker"]


def test_env_patterns_skip_empty_items(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("WB2API_PROMPT_TRUNCATE_PATTERNS", "a||b\n")
    cfg = ConfigManager(str(tmp_path / "config.json")).current
    assert cfg.prompt_guard.truncate_patterns == ["a", "b"]


def test_from_config_accepts_none() -> None:
    assert PromptGuard.from_config(None) is None
