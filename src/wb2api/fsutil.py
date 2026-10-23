"""落盘工具：原子替换 + 「单文件挂载」降级。

为什么需要这个模块
------------------
``os.replace(tmp, target)`` 靠**改目录项**实现原子替换，这在普通文件系统上没问题。
但当 ``target`` 是 Docker **单文件绑定挂载**（如 ``./config.json:/app/config.json``）
的挂载点时，该路径被内核钉在一个固定 inode 上：挂载点不可 rename，
``os.replace`` 会直接抛 ``OSError: [Errno 16] Device or resource busy``。
表现就是容器里"保存配置"永远失败，而本地直接跑却完全正常。

因此统一提供 :func:`atomic_write_text`：

1. 首选「写临时文件 → ``os.replace``」的原子路径；
2. 若目标不可替换（EBUSY / EXDEV / 权限被挂载点拒绝），降级为**就地重写**：
   截断并覆写原 inode，挂载点因此继续有效（牺牲原子性，换取可用性）。
"""

from __future__ import annotations

import errno
import logging
import os
import threading
from pathlib import Path

logger = logging.getLogger("wb2api.fsutil")

# 出现这些错误说明"无法用 rename 覆盖目标"，应降级为就地重写。
# EBUSY  = 目标为挂载点（Docker 单文件挂载的典型症状）
# EXDEV  = 临时文件与目标不在同一文件系统
# EPERM / EACCES = Windows 上目标被其他句柄占用，或目录不允许改目录项
# EROFS  = 目标是只读挂载（就地重写会给出更明确的错误）
_FALLBACK_ERRNOS = frozenset({errno.EBUSY, errno.EXDEV, errno.EPERM, errno.EACCES, errno.EROFS})

_warned_paths: set[str] = set()
_warn_lock = threading.Lock()


def _warn_degraded(path: Path, exc: OSError) -> None:
    """降级写盘只警告一次，避免每次保存都刷日志。"""
    key = str(path)
    with _warn_lock:
        if key in _warned_paths:
            return
        _warned_paths.add(key)
    logger.warning(
        "无法原子替换 %s（%s）：该路径可能是 Docker 单文件挂载点，已降级为就地重写。"
        "如需恢复原子性，请改为挂载目录而非单个文件。",
        path,
        exc,
    )


def _safe_chmod(path: Path, mode: int | None) -> None:
    """尽力修正权限；失败不致命（挂载文件可能由宿主机其他用户持有）。"""
    if mode is None:
        return
    try:
        os.chmod(path, mode)
    except OSError:
        pass


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        if written <= 0:  # pragma: no cover - 防御性分支
            raise OSError("写入文件时未能推进偏移")
        view = view[written:]


def _overwrite_in_place(target: Path, data: bytes) -> None:
    """截断并覆写已有 inode（不改变 inode，挂载点保持有效）。"""
    fd = os.open(target, os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        os.ftruncate(fd, 0)
        os.lseek(fd, 0, os.SEEK_SET)
        _write_all(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_write_text(
    path: str | os.PathLike[str],
    text: str,
    *,
    encoding: str = "utf-8",
    mode: int | None = 0o600,
) -> None:
    """原子写文本；目标不可替换时自动降级为就地重写。

    ``mode=None`` 表示沿用系统默认权限（不显式 chmod）。
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f"{target.name}.tmp")
    data = text.encode(encoding)

    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())

        try:
            os.replace(tmp, target)
        except OSError as exc:
            if exc.errno not in _FALLBACK_ERRNOS:
                raise
            _warn_degraded(target, exc)
            _overwrite_in_place(target, data)

        _safe_chmod(target, mode)
    finally:
        # 成功替换后 tmp 已不存在；失败时清理残留，避免污染目录。
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
