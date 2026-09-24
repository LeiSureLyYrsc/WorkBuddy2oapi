"""异步批量任务管理：批量签到/刷新/旅行等长耗时任务。"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Awaitable, Callable
from typing import Any

from wb2api.models import TaskItem, TaskView, now_iso


class Task:
    """单个批量任务实例。"""

    def __init__(self, id: str, kind: str, title: str) -> None:
        self.id = id
        self.kind = kind
        self.title = title
        self.running = True
        self.error = ""
        self.started_at = now_iso()
        self.finished_at = ""
        self._items: list[dict[str, Any]] = []
        self._lock = threading.RLock()

    def add_item(self, item: TaskItem | dict[str, Any]) -> None:
        """追加一条账号执行结果。"""
        if isinstance(item, TaskItem):
            d = item.to_dict()
        elif isinstance(item, dict):
            d = dict(item)
        else:
            raise TypeError(f"item 必须为 TaskItem 或 dict: {type(item)}")
        with self._lock:
            self._items.append(d)

    def finish(self, error: str | Exception | None = None) -> None:
        """标记任务结束。"""
        with self._lock:
            self.running = False
            self.finished_at = now_iso()
            if error is not None:
                self.error = str(error)

    def view(self) -> TaskView:
        """生成只读快照。"""
        with self._lock:
            items_copy = list(self._items)
            total = len(items_copy)
            done = total
            ok = sum(1 for it in items_copy if it.get("ok"))
            failed = total - ok
            return TaskView(
                id=self.id,
                kind=self.kind,
                title=self.title,
                running=self.running,
                error=self.error,
                started_at=self.started_at,
                finished_at=self.finished_at,
                total=total,
                done=done,
                ok=ok,
                failed=failed,
                items=items_copy,
            )


class TaskManager:
    """进程内任务表（保留最近若干条任务）。"""

    def __init__(self, max_keep: int = 50) -> None:
        self.max_keep = max_keep
        self._tasks: dict[str, Task] = {}
        self._order: list[str] = []
        self._seq = 0
        self._lock = threading.RLock()

    def new(
        self,
        kind: str,
        title: str,
        runner: Callable[[Task], Awaitable[None]],
    ) -> dict[str, Any]:
        """创建任务并在独立 asyncio 协程中执行。"""
        with self._lock:
            self._seq += 1
            seq = self._seq
            task_id = f"{kind}-{seq}"
            task = Task(id=task_id, kind=kind, title=title)
            self._tasks[task_id] = task
            self._order.append(task_id)
            while len(self._order) > self.max_keep:
                old_id = self._order.pop(0)
                self._tasks.pop(old_id, None)

        async def _run() -> None:
            try:
                await runner(task)
                task.finish(None)
            except Exception as e:
                task.finish(f"任务内部错误: {e}")

        try:
            loop = asyncio.get_running_loop()
            loop.create_task(_run())
        except RuntimeError:
            pass

        return task.view().to_dict()

    def get(self, id: str) -> dict[str, Any] | None:
        """按 ID 取任务快照字典。"""
        with self._lock:
            task = self._tasks.get(id)
            if task is None:
                return None
            return task.view().to_dict()

    def recent(self, limit: int = 20) -> list[dict[str, Any]]:
        """返回最近的任务列表（最新在前，不含明细）。"""
        with self._lock:
            ids = list(self._order)

        if limit <= 0 or limit > len(ids):
            limit = len(ids)

        out: list[dict[str, Any]] = []
        for task_id in reversed(ids):
            if len(out) >= limit:
                break
            with self._lock:
                task = self._tasks.get(task_id)
            if task:
                v = task.view().to_dict()
                v["items"] = []
                out.append(v)
        return out

    def running(self) -> list[dict[str, Any]]:
        """返回当前正在运行的任务列表（最新在前，不含明细）。"""
        with self._lock:
            tasks = list(self._tasks.values())

        out: list[dict[str, Any]] = []
        for t in tasks:
            if t.running:
                v = t.view().to_dict()
                v["items"] = []
                out.append(v)
        out.sort(key=lambda x: str(x.get("started_at", "")), reverse=True)
        return out
