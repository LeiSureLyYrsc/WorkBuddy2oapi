"""会话粘性路由测试。"""

from __future__ import annotations

import json

import pytest

from wb2api.session import StickyRouter, extract_key, fnv1a_32, hash_index


def test_extract_key_priorities() -> None:
    # 1. metadata.conversation_id 优先级最高
    b1 = json.dumps(
        {
            "metadata": {
                "conversation_id": "conv_meta_snake",
                "conversationId": "conv_meta_camel",
                "user_id": "usr_meta",
            },
            "conversation_id": "conv_root_snake",
            "conversationId": "conv_root_camel",
        }
    ).encode("utf-8")
    assert extract_key(b1) == "conv_meta_snake"

    # 2. metadata.conversationId 次之
    b2 = json.dumps(
        {
            "metadata": {
                "conversationId": "conv_meta_camel",
                "user_id": "usr_meta",
            },
            "conversation_id": "conv_root_snake",
            "conversationId": "conv_root_camel",
        }
    ).encode("utf-8")
    assert extract_key(b2) == "conv_meta_camel"

    # 3. metadata.user_id 优于顶层
    b3 = json.dumps(
        {
            "metadata": {
                "user_id": "usr_meta",
            },
            "conversation_id": "conv_root_snake",
            "conversationId": "conv_root_camel",
        }
    ).encode("utf-8")
    assert extract_key(b3) == "usr_meta"

    # 4. conversation_id 优于 conversationId
    b4 = json.dumps(
        {
            "conversation_id": "conv_root_snake",
            "conversationId": "conv_root_camel",
        }
    ).encode("utf-8")
    assert extract_key(b4) == "conv_root_snake"

    # 5. conversationId
    b5 = json.dumps(
        {
            "conversationId": "conv_root_camel",
        }
    ).encode("utf-8")
    assert extract_key(b5) == "conv_root_camel"

    # 空或异常输入
    assert extract_key(b"") == ""
    assert extract_key(b"not a json") == ""
    assert extract_key(b"{}") == ""
    assert extract_key(b'{"metadata": {}}') == ""


def test_hash_index_fnv1a() -> None:
    assert fnv1a_32("") == 2166136261
    idx = hash_index("session-12345", 5)
    assert 0 <= idx < 5
    assert hash_index("session-12345", 0) == 0


def test_sticky_router_resolve_sticky() -> None:
    available_list = ["uid_1", "uid_2", "uid_3"]
    router = StickyRouter(ttl_seconds=1800, available=lambda: available_list)

    # 第一次解析，分配一个账号
    res1 = router.resolve("conv_a")
    assert res1 in available_list

    # 多次解析保持粘性
    res2 = router.resolve("conv_a")
    assert res2 == res1

    # 无可用账号时返回 None
    empty_router = StickyRouter(ttl_seconds=1800, available=lambda: [])
    assert empty_router.resolve("conv_a") is None

    # 空 key 返回 None
    assert router.resolve("") is None


def test_double_segment_prefers_unbound() -> None:
    available_list = ["uid_1", "uid_2", "uid_3"]
    router = StickyRouter(ttl_seconds=1800, available=lambda: available_list)

    # 手动绑定 uid_1 和 uid_2
    router.bind("conv_1", "uid_1")
    router.bind("conv_2", "uid_2")

    # 对新会话 resolve，双段策略必须优先分配空闲账号 uid_3
    res = router.resolve("conv_new")
    assert res == "uid_3"


def test_resolve_when_bound_account_becomes_unavailable() -> None:
    pool = ["uid_1", "uid_2"]
    router = StickyRouter(ttl_seconds=1800, available=lambda: pool)

    router.bind("conv_x", "uid_1")
    assert router.resolve("conv_x") == "uid_1"

    # uid_1 冷却下线，可用池只剩 uid_2
    pool = ["uid_2"]
    router._available = lambda: pool

    # 原绑定失效，重新分配到 uid_2
    assert router.resolve("conv_x") == "uid_2"


def test_bind_unbind_and_count() -> None:
    router = StickyRouter(ttl_seconds=1800)
    assert router.count() == 0

    router.bind("c1", "u1")
    router.bind("c2", "u2")
    assert router.count() == 2

    assert router.unbind("c1") is True
    assert router.unbind("c1") is False
    assert router.count() == 1


def test_gc_once_and_load_from_store() -> None:
    router = StickyRouter(ttl_seconds=10, available=lambda: ["u1", "saved_u"])
    router.bind("k1", "u1")

    # t=5 未过期
    removed = router.gc_once(now=router._entries["k1"].last_active + 5)
    assert removed == 0
    assert router.count() == 1

    # t=15 已过期
    removed = router.gc_once(now=router._entries["k1"].last_active + 15)
    assert removed == 1
    assert router.count() == 0

    # load_from_store 恢复
    router.load_from_store({"saved_k": "saved_u"})
    assert router.count() == 1
    assert router.resolve("saved_k") == "saved_u"
