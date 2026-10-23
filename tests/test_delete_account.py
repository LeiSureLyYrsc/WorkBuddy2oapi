"""删除账号链路测试：WebUI 二次确认删除的端到端行为。

覆盖此前的真实故障：
* 默认配置 ``dangerous_ops=false`` 时删除按钮被永久禁用 / 接口 403；
* 凭证文件名非 ``workbuddy-<uid>.json`` 时按 uid 推导路径删不到文件；
* 账号只在内存池（磁盘无文件）时无从清理；
* 删除后 state.json 残留、列表仍显示。
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from wb2api.auth_store import AccountStore, NoCredentialFileError
from wb2api.main import create_app

UID = "232b76a9-2f49-42f7-97f1-deec3df5cbb5"


def _credential_doc(uid: str = UID, nickname: str = "曲奇") -> str:
    return json.dumps(
        {
            "account": {"uid": uid, "enterpriseId": "", "nickname": nickname},
            "auth": {
                "accessToken": "tok_test",
                "refreshToken": "ref_test",
                "expiresAt": 9999999999,
                "domain": "copilot.tencent.com",
            },
        },
        ensure_ascii=False,
    )


@pytest.fixture()
def make_client(tmp_path: Path) -> Iterator:
    """构造一个可配置 dangerous_ops / read_only / 文件名的控制台客户端。"""

    def _build(
        *,
        dangerous_ops: bool = False,
        read_only: bool = False,
        file_name: str | None = None,
        write_file: bool = True,
    ) -> tuple[TestClient, Path]:
        auth_dir = tmp_path / "auths"
        auth_dir.mkdir(exist_ok=True)
        if write_file:
            name = file_name if file_name is not None else f"workbuddy-{UID}.json"
            (auth_dir / name).write_text(_credential_doc(), encoding="utf-8")

        cfg = {
            "listen": ":17899",
            "api_key": "",
            "auth_dir": str(auth_dir),
            "state_file": str(tmp_path / "data" / "state.json"),
            "pricing_file": str(tmp_path / "data" / "pricing.json"),
            "console": {
                "username": "admin",
                "password": "workbuddy",
                "credentials_file": str(tmp_path / "data" / "credentials.json"),
                "read_only": read_only,
                "dangerous_ops": dangerous_ops,
            },
        }
        cfg_path = tmp_path / "config.json"
        cfg_path.write_text(json.dumps(cfg), encoding="utf-8")

        client = TestClient(create_app(str(cfg_path)))
        resp = client.post("/api/login", json={"username": "admin", "password": "workbuddy"})
        assert resp.status_code == 200, resp.text
        return client, auth_dir

    yield _build


def _uids(client: TestClient) -> list[str]:
    return [a["uid"] for a in client.get("/api/accounts").json()["accounts"]]


# ---------------------------------------------------------------------------
# HTTP 层
# ---------------------------------------------------------------------------


def test_delete_works_without_dangerous_ops(make_client) -> None:
    """默认配置（dangerous_ops=false）下，二次确认后删除必须成功。"""
    client, auth_dir = make_client(dangerous_ops=False)
    assert _uids(client) == [UID]

    resp = client.delete(f"/api/accounts/{UID}", params={"confirm": UID})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["deleted"]["had_file"] is True
    assert body["deleted"]["file_name"] == f"workbuddy-{UID}.json"
    assert not list(auth_dir.glob("*.json"))
    assert _uids(client) == []


def test_delete_requires_matching_confirm(make_client) -> None:
    """缺少或错误的 confirm 一律拒绝，且不产生副作用。"""
    client, auth_dir = make_client(dangerous_ops=True)

    assert client.delete(f"/api/accounts/{UID}").status_code == 400
    assert client.delete(f"/api/accounts/{UID}", params={"confirm": "nope"}).status_code == 400

    assert len(list(auth_dir.glob("*.json"))) == 1
    assert _uids(client) == [UID]


def test_delete_read_only_is_rejected(make_client) -> None:
    """只读模式下删除必须被拒绝。"""
    client, auth_dir = make_client(dangerous_ops=True, read_only=True)

    resp = client.delete(f"/api/accounts/{UID}", params={"confirm": UID})

    assert resp.status_code == 403
    assert resp.json()["code"] == "read_only"
    assert len(list(auth_dir.glob("*.json"))) == 1


def test_delete_unknown_uid_is_404(make_client) -> None:
    """删除不存在的账号返回 404，而不是 500。"""
    client, _ = make_client(dangerous_ops=True)
    missing = "does-not-exist-uid"

    resp = client.delete(f"/api/accounts/{missing}", params={"confirm": missing})

    assert resp.status_code == 404


def test_delete_twice_is_404_not_error(make_client) -> None:
    """重复删除同一个账号：第一次 200，第二次 404。"""
    client, _ = make_client(dangerous_ops=True)

    assert client.delete(f"/api/accounts/{UID}", params={"confirm": UID}).status_code == 200
    assert client.delete(f"/api/accounts/{UID}", params={"confirm": UID}).status_code == 404


def test_delete_clears_pool_state_file(make_client) -> None:
    """删除后 state.json 不应残留该账号。"""
    client, _ = make_client(dangerous_ops=True)
    state_file = Path(client.app.state.wb.pool.state_file)

    assert client.delete(f"/api/accounts/{UID}", params={"confirm": UID}).status_code == 200

    doc = json.loads(state_file.read_text(encoding="utf-8")) if state_file.exists() else {"accounts": {}}
    assert UID not in doc.get("accounts", {})


# ---------------------------------------------------------------------------
# 非规范文件名 / 幽灵账号
# ---------------------------------------------------------------------------


def test_delete_handles_non_canonical_file_name(make_client) -> None:
    """凭证文件名不合规范（但内容里有 uid）时，也必须能删干净。"""
    client, auth_dir = make_client(dangerous_ops=True, file_name="workbuddy-custom-name.json")
    accounts = client.get("/api/accounts").json()["accounts"]
    assert accounts[0]["has_file"] is True
    assert accounts[0]["file_name"] == "workbuddy-custom-name.json"

    resp = client.delete(f"/api/accounts/{UID}", params={"confirm": UID})

    assert resp.status_code == 200, resp.text
    assert resp.json()["deleted"]["file_name"] == "workbuddy-custom-name.json"
    assert not list(auth_dir.glob("*.json"))
    assert _uids(client) == []


def test_delete_pool_only_account_without_file(make_client) -> None:
    """账号只在池中、磁盘无文件时，删除应把它移出池而不是报错。"""
    client, auth_dir = make_client(dangerous_ops=True)
    for p in auth_dir.glob("*.json"):
        p.unlink()  # 模拟启动后文件被外部删除

    accounts = client.get("/api/accounts").json()["accounts"]
    assert accounts and accounts[0]["has_file"] is False

    resp = client.delete(f"/api/accounts/{UID}", params={"confirm": UID})

    assert resp.status_code == 200, resp.text
    assert resp.json()["deleted"]["had_file"] is False
    assert _uids(client) == []


# ---------------------------------------------------------------------------
# 存储层
# ---------------------------------------------------------------------------


def test_store_delete_resolves_by_file_content(tmp_path: Path) -> None:
    """AccountStore.delete 应按文件内容定位 uid，而非只认规范文件名。"""
    store = AccountStore(str(tmp_path))
    (tmp_path / "workbuddy-odd-name.json").write_text(_credential_doc(), encoding="utf-8")

    deleted = store.delete(UID)

    assert Path(deleted).name == "workbuddy-odd-name.json"
    assert not list(tmp_path.glob("*.json"))


def test_store_delete_missing_raises(tmp_path: Path) -> None:
    """没有对应凭证文件时抛出 NoCredentialFileError。"""
    store = AccountStore(str(tmp_path))

    with pytest.raises(NoCredentialFileError):
        store.delete(UID)


def test_store_delete_rejects_path_traversal(tmp_path: Path) -> None:
    """非法 uid（路径穿越）不得触碰文件系统。"""
    store = AccountStore(str(tmp_path))
    (tmp_path / "keep.json").write_text(_credential_doc(), encoding="utf-8")

    with pytest.raises(ValueError):
        store.delete("../etc/passwd")

    assert (tmp_path / "keep.json").exists()
