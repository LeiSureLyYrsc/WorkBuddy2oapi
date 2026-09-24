"""账号凭证存储测试。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from wb2api.auth_store import AccountStore, NoCredentialFileError, parse, valid_uid
from wb2api.models import Account


def test_valid_uid() -> None:
    # 合法 uid
    assert valid_uid("user_12345")
    assert valid_uid("account-abc-123")
    assert valid_uid("test.user.1")
    assert valid_uid("valid_uid_6chars")

    # 非法 uid：太短、太长、非法字符、包含路径穿越
    assert not valid_uid("")
    assert not valid_uid("short")
    assert not valid_uid("a" * 129)
    assert not valid_uid("../traversal")
    assert not valid_uid("foo/../bar")
    assert not valid_uid("user..name")
    assert not valid_uid("user/name")
    assert not valid_uid("user\\name")
    assert not valid_uid("user@name")
    assert not valid_uid("user:name")


def test_parse_nested() -> None:
    nested_json = {
        "account": {
            "uid": "user_nested_01",
            "enterpriseId": "ent_test",
            "nickname": "嵌套测试",
        },
        "auth": {
            "accessToken": "token_nested_123",
            "refreshToken": "refresh_nested_456",
            "expiresAt": 1727100000,
            "domain": "workbuddy.ai",
        },
    }
    raw = json.dumps(nested_json).encode("utf-8")
    acct = parse(raw)
    assert acct.uid == "user_nested_01"
    assert acct.enterprise_id == "ent_test"
    assert acct.nickname == "嵌套测试"
    assert acct.access_token == "token_nested_123"
    assert acct.refresh_token == "refresh_nested_456"
    assert acct.expires_at == 1727100000
    assert acct.domain == "workbuddy.ai"


def test_parse_flat() -> None:
    flat_json = {
        "uid": "user_flat_01",
        "enterpriseId": "ent_flat",
        "nickname": "扁平测试",
        "accessToken": "token_flat_123",
        "refreshToken": "refresh_flat_456",
        "expiresAt": 1727200000,
        "domain": "test.workbuddy.ai",
    }
    raw = json.dumps(flat_json).encode("utf-8")
    acct = parse(raw)
    assert acct.uid == "user_flat_01"
    assert acct.enterprise_id == "ent_flat"
    assert acct.nickname == "扁平测试"
    assert acct.access_token == "token_flat_123"
    assert acct.refresh_token == "refresh_flat_456"
    assert acct.expires_at == 1727200000
    assert acct.domain == "test.workbuddy.ai"


def test_parse_empty_and_missing_token() -> None:
    with pytest.raises(ValueError, match="空凭证文件"):
        parse(b"")

    with pytest.raises(ValueError, match="空凭证文件"):
        parse(b"   \n  ")

    with pytest.raises(ValueError, match="凭证 JSON 解析失败"):
        parse(b"invalid json")

    with pytest.raises(ValueError, match="缺少 accessToken"):
        parse(b'{"account":{"uid":"user123"}, "auth":{"accessToken":""}}')

    with pytest.raises(ValueError, match="缺少 accessToken"):
        parse(b'{"uid":"user123","accessToken":"   "}')


def test_store_save_and_get(tmp_path: Path) -> None:
    store = AccountStore(str(tmp_path))
    acct = Account(
        uid="account_001",
        enterprise_id="ent_01",
        nickname="账号1",
        access_token="tok_001",
        refresh_token="ref_001",
        expires_at=1727300000,
        domain="workbuddy.ai",
    )

    # 保存
    store.save(acct)
    file_path = Path(store.path_for("account_001"))
    assert file_path.exists()

    # 验证落盘内容为嵌套格式
    content = json.loads(file_path.read_text(encoding="utf-8"))
    assert "account" in content
    assert "auth" in content
    assert content["account"]["uid"] == "account_001"
    assert content["auth"]["accessToken"] == "tok_001"

    # 读取并验证
    loaded = store.get("account_001")
    assert loaded.uid == "account_001"
    assert loaded.access_token == "tok_001"
    assert loaded.file_path == str(file_path.resolve())


def test_save_refusal_empty_token_and_invalid_uid(tmp_path: Path) -> None:
    store = AccountStore(str(tmp_path))

    # 空 token 拒绝写入
    with pytest.raises(ValueError, match="拒绝写入：accessToken 为空"):
        store.save(Account(uid="user_good_1", access_token=""))

    # 非法 uid 拒绝写入
    with pytest.raises(ValueError, match="拒绝写入：非法 uid"):
        store.save(Account(uid="../evil_uid", access_token="tok_123"))

    # path_for 拒绝非法 uid
    with pytest.raises(ValueError, match="非法 uid"):
        store.path_for("../traversal")


def test_store_list_and_delete(tmp_path: Path) -> None:
    store = AccountStore(str(tmp_path))

    # 保存两个账号
    a2 = Account(uid="user_bbb", access_token="tok_b")
    a1 = Account(uid="user_aaa", access_token="tok_a")
    store.save(a2)
    store.save(a1)

    # 写入一个损坏的 JSON 文件
    bad_file = tmp_path / "workbuddy-corrupt.json"
    bad_file.write_text("corrupted json content", encoding="utf-8")

    accounts, warnings = store.list()
    # 按照 uid 排序
    assert len(accounts) == 2
    assert accounts[0].uid == "user_aaa"
    assert accounts[1].uid == "user_bbb"
    assert len(warnings) == 1
    assert "workbuddy-corrupt.json" in warnings[0]

    # 删除
    store.delete("user_aaa")
    accounts_after, _ = store.list()
    assert len(accounts_after) == 1
    assert accounts_after[0].uid == "user_bbb"

    # 再次删除抛出 NoCredentialFileError
    with pytest.raises(NoCredentialFileError):
        store.delete("user_aaa")


def test_empty_dir_raises() -> None:
    with pytest.raises(ValueError, match="账号凭证目录（auth_dir）未配置"):
        AccountStore("")
    with pytest.raises(ValueError, match="账号凭证目录（auth_dir）未配置"):
        AccountStore("   ")
