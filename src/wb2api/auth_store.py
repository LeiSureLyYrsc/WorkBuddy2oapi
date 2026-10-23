"""账号凭证持久化：读取/写入 workbuddy-<uid>.json 凭证文件。

磁盘格式与 Go 端一致（嵌套形）：
    {
      "account": {"uid", "enterpriseId", "nickname"},
      "auth": {"accessToken", "refreshToken", "expiresAt", "domain"}
    }
兼容读取扁平形（手写/旧版）。写回一律用嵌套形，保证兼容性。
"""

from __future__ import annotations

import json
import os
import re
import threading
from pathlib import Path

from wb2api.models import Account
from wb2api.fsutil import atomic_write_text

_UID_PATTERN = re.compile(r"^[A-Za-z0-9._-]{6,128}$")


def valid_uid(uid: str) -> bool:
    """校验 uid 是否合法且无路径穿越风险。"""
    if not uid:
        return False
    return bool(_UID_PATTERN.match(uid)) and ".." not in uid


class NoCredentialFileError(FileNotFoundError):
    """账号没有本地凭证文件。"""


def parse(raw: bytes | str) -> Account:
    """解析两种磁盘形态：嵌套形（插件 OAuth 输出）与扁平形（手写/旧版）。"""
    if not raw:
        raise ValueError("空凭证文件")
    if isinstance(raw, bytes):
        text = raw.decode("utf-8-sig")
    else:
        text = raw
        if text.startswith("\ufeff"):
            text = text.lstrip("\ufeff")

    if not text.strip():
        raise ValueError("空凭证文件")

    try:
        data = json.loads(text)
    except Exception as e:
        raise ValueError(f"凭证 JSON 解析失败: {e}") from e

    if not isinstance(data, dict):
        raise ValueError("凭证 JSON 根节点必须为对象")

    if "auth" in data and isinstance(data["auth"], dict):
        auth_obj = data.get("auth") or {}
        acct_obj = data.get("account") or {}
        uid = str(acct_obj.get("uid") or "")
        enterprise_id = str(acct_obj.get("enterpriseId") or "")
        nickname = str(acct_obj.get("nickname") or "")
        access_token = str(auth_obj.get("accessToken") or "")
        refresh_token = str(auth_obj.get("refreshToken") or "")
        expires_at = int(auth_obj.get("expiresAt") or 0)
        domain = str(auth_obj.get("domain") or "")
    else:
        uid = str(data.get("uid") or "")
        enterprise_id = str(data.get("enterpriseId") or "")
        nickname = str(data.get("nickname") or "")
        access_token = str(data.get("accessToken") or "")
        refresh_token = str(data.get("refreshToken") or "")
        expires_at = int(data.get("expiresAt") or 0)
        domain = str(data.get("domain") or "")

    if not access_token.strip():
        raise ValueError("缺少 accessToken")

    return Account(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_at=expires_at,
        domain=domain,
        uid=uid,
        enterprise_id=enterprise_id,
        nickname=nickname,
    )


class AccountStore:
    """账号凭证目录访问器。线程安全：写操作在进程内互斥。"""

    def __init__(self, dir: str) -> None:
        if not dir or not dir.strip():
            raise ValueError("账号凭证目录（auth_dir）未配置")
        self._dir = str(Path(dir).resolve())
        self._lock = threading.RLock()

    @property
    def dir(self) -> str:
        return self._dir

    @staticmethod
    def valid_uid(uid: str) -> bool:
        return valid_uid(uid)

    @staticmethod
    def parse(raw: bytes | str) -> Account:
        return parse(raw)

    def path_for(self, uid: str) -> str:
        """返回 uid 对应的凭证文件绝对路径；uid 非法时抛出 ValueError。"""
        if not valid_uid(uid):
            raise ValueError(f"非法 uid: {uid!r}")
        return str(Path(self._dir) / f"workbuddy-{uid}.json")

    def list(self) -> tuple[list[Account], list[str]]:
        """扫描目录下全部 workbuddy*.json 并按 uid 排序返回。

        单个文件解析失败不中断：跳过并在警告列表中报告。
        """
        dir_path = Path(self._dir)
        if not dir_path.exists():
            return [], []

        files = sorted(dir_path.glob("workbuddy*.json"))
        accounts: list[Account] = []
        warnings: list[str] = []

        for f in files:
            try:
                raw = f.read_bytes()
                acct = parse(raw)
                acct.file_path = str(f.resolve())
                accounts.append(acct)
            except Exception as e:
                warnings.append(f"{f.name}: {e}")

        accounts.sort(key=lambda a: a.uid)
        return accounts, warnings

    def get(self, uid: str) -> Account:
        """按 uid 读取单个账号；不存在抛出 FileNotFoundError。"""
        path = self.path_for(uid)
        file_path = Path(path)
        if not file_path.exists():
            raise FileNotFoundError(f"凭证文件不存在: {path}")

        raw = file_path.read_bytes()
        acct = parse(raw)
        acct.file_path = str(file_path.resolve())
        return acct

    def save(self, account: Account) -> None:
        """原子写回账号凭证（嵌套形，0600 权限）。

        accessToken 为空或 uid 非法时拒绝写入。
        """
        if not account.access_token or not account.access_token.strip():
            raise ValueError("拒绝写入：accessToken 为空")
        if not valid_uid(account.uid):
            raise ValueError(f"拒绝写入：非法 uid {account.uid!r}")

        path = account.file_path or self.path_for(account.uid)
        account.file_path = path

        doc = {
            "account": {
                "uid": account.uid,
                "enterpriseId": account.enterprise_id,
                "nickname": account.nickname,
            },
            "auth": {
                "accessToken": account.access_token,
                "refreshToken": account.refresh_token,
                "expiresAt": account.expires_at,
                "domain": account.domain,
            },
        }
        raw = json.dumps(doc, indent=2, ensure_ascii=False) + "\n"

        target = Path(path)
        with self._lock:
            atomic_write_text(target, raw, mode=0o600)

    def path_by_uid(self, uid: str) -> str | None:
        """按内容扫描目录，返回该 uid 实际所在的凭证文件路径。

        规范文件名是 ``workbuddy-<uid>.json``，但外部拷贝/手工重命名的文件
        （如 ``workbuddy-custom-name.json``）同样会被 :meth:`list` 收录并展示在
        控制台里。仅凭 uid 推导路径会漏删这些文件，因此这里以**文件内容**为准。
        """
        if not valid_uid(uid):
            return None
        try:
            files = sorted(Path(self._dir).glob("workbuddy*.json"))
        except OSError:
            return None
        for f in files:
            try:
                acct = parse(f.read_bytes())
            except Exception:
                continue
            if acct.uid == uid:
                return str(f.resolve())
        return None

    def delete(self, uid: str) -> str:
        """删除账号凭证文件并返回被删除的文件路径；无文件时抛出 NoCredentialFileError。

        先按规范文件名定位，未命中再按内容扫描匹配 uid，保证控制台里
        「有凭证文件」的账号一定能被删掉。
        """
        if not valid_uid(uid):
            raise ValueError(f"非法 uid: {uid!r}")

        with self._lock:
            path = self.path_for(uid)
            if not Path(path).exists():
                scanned = self.path_by_uid(uid)
                if scanned is not None:
                    path = scanned
            try:
                os.remove(path)
            except FileNotFoundError:
                raise NoCredentialFileError(f"该账号没有本地凭证文件: {uid}")
            return path
