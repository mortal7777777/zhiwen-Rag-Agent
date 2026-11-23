"""子代理分支控制：整轮取消令牌 + 按 subtask 的事件注册表（2026-11-21 6.7）。

一轮运行原本只有一枚 stop_event（整轮停止）。并行 Send 的子代理还需要
"只取消我一个"的入口：取消令牌继承 threading.Event（既有 set/is_set 语义
不变），额外维护 subtask_id -> Event 的分支表；子代理循环同时检查两枚信号。

取消是协作式的：正在执行的工具/LLM 请求不会被强杀，信号在本次调用返回后的
下一个检查点生效。注册表挂在请求级令牌上，不进检查点。
"""

from __future__ import annotations

import threading


class RunCancellation(threading.Event):
    """整轮停止信号 + 子代理分支取消注册表。"""

    def __init__(self) -> None:
        super().__init__()
        self._branches: dict[str, threading.Event] = {}
        self._branches_lock = threading.Lock()

    def register_branch(self, subtask_id: str) -> threading.Event:
        """登记一个并行分支并返回它自己的取消信号。"""
        event = threading.Event()
        with self._branches_lock:
            self._branches[subtask_id] = event
        # 整轮已经停止：新分支立刻视为已取消，避免注册到已终止的运行上
        if self.is_set():
            event.set()
        return event

    def unregister_branch(
        self, subtask_id: str, event: threading.Event | None = None
    ) -> None:
        with self._branches_lock:
            if event is None or self._branches.get(subtask_id) is event:
                self._branches.pop(subtask_id, None)

    def cancel_branch(self, subtask_id: str) -> bool:
        with self._branches_lock:
            event = self._branches.get(subtask_id)
        if event is None:
            return False
        event.set()
        return True

    def branch_ids(self) -> list[str]:
        with self._branches_lock:
            return list(self._branches)

    def set(self) -> None:
        # 整轮停止：分支信号一并置位，子代理尽快退出循环
        super().set()
        with self._branches_lock:
            events = list(self._branches.values())
        for event in events:
            event.set()
