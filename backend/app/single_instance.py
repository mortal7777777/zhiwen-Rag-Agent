"""单实例互斥锁：同一 meta_dir 只允许一个后端进程持有。

背景（P0，2026-10-08 评估）：会话并发闸门（`api/agent.py` 的 `_ACTIVE_CONVS`）、
审批超时 watchdog（`threading.Timer`）、挂起前用量快照都在**进程内存**里，
而挂起状态存在 `meta_dir/checkpoints.sqlite`。两个进程共享同一 meta_dir 时
不会报错，只会静默破坏语义：

- 两边各持一份闸门 → 同一会话被并发 invoke，检查点互相覆盖；
- 审批挂起只在 A 进程可见，用户请求被路由到 B 就是"没有待确认操作"；
- watchdog 是进程内 Timer，随进程重启消失 → 挂起永久不超时。

与其在文档里写"请勿多开"，不如让第二个进程**直接启动失败**。用的是 OS 级
文件锁（Windows `msvcrt.locking` / POSIX `fcntl.flock`）：进程正常退出或崩溃
时由内核释放，不存在"残留锁文件导致起不来"的问题（锁文件本身留着无害）。

同进程内重复获取按引用计数放行——`uvicorn --reload` 的子进程重建、
测试里嵌套获取都依赖这个语义；Windows 的字节范围锁在同进程不同句柄间
并不冲突，所以引用计数不是可选项，而是唯一正确做法。

验证多实例行为需要**跨进程**（同进程不冲突），见 tests/test_single_instance.py。
"""

from __future__ import annotations

import logging
import os
import threading
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

LOCK_FILE_NAME = "backend.lock"
# 持有者信息写**旁路文件**：Windows 的字节范围锁是强制锁，被锁区间连本进程
# 换个句柄都读不了——把 pid 写进锁文件本身，冲突方就永远读不到提示。
HOLDER_FILE_NAME = f"{LOCK_FILE_NAME}.holder"

# key（锁文件绝对路径） -> [文件句柄, 引用计数]
_held: dict[str, list] = {}
_held_lock = threading.Lock()


class InstanceLockError(RuntimeError):
    """meta_dir 已被另一个后端进程占用（拒绝启动）。"""


def _lock_handle(handle) -> None:
    """对打开的文件句柄加非阻塞独占锁；冲突时抛 OSError。"""
    if os.name == "nt":
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_handle(handle) -> None:
    if os.name == "nt":
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _holder_path(lock_path: Path) -> Path:
    return lock_path.parent / HOLDER_FILE_NAME


def _holder_hint(path: Path) -> str:
    """读取旁路文件里的持有者信息（pid / 获取时间），失败返回空串。"""
    try:
        lines = [
            line.strip()
            for line in _holder_path(path)
            .read_text(encoding="utf-8", errors="replace")
            .splitlines()
            if line.strip()
        ]
    except OSError:
        return ""
    if not lines:
        return ""
    pid = lines[0]
    since = f"，自 {lines[1]}" if len(lines) > 1 else ""
    return f"（当前持有者 pid={pid}{since}）"


class InstanceLock:
    """已持有的单实例锁；`release()` 幂等，引用计数归零才真正解锁。"""

    def __init__(self, path: Path, handle, key: str) -> None:
        self.path = path
        self._handle = handle
        self._key = key

    def release(self) -> None:
        with _held_lock:
            entry = _held.get(self._key)
            if entry is None or entry[0] is not self._handle:
                return
            entry[1] -= 1
            if entry[1] > 0:
                return
            _held.pop(self._key, None)
            handle = entry[0]
        try:
            _unlock_handle(handle)
        except OSError as exc:  # 解锁失败不该阻断进程收尾
            logger.warning("释放单实例锁失败：%s", exc)
        try:
            handle.close()
        except OSError:
            pass
        try:  # 旁路文件是提示信息，删不掉不影响正确性
            _holder_path(self.path).unlink()
        except OSError:
            pass
        self._handle = None


def _try_lock_file(path: Path):
    """打开锁文件并尝试独占锁定；失败抛 InstanceLockError。"""
    # O_RDWR | O_CREAT（不截断、不追加）：截断会毁掉别人的持锁文件内容，
    # 追加模式则让后续 seek 写入失效。句柄要活到 release()。
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    handle = open(fd, "r+b", closefd=True)
    try:
        # Windows 的字节范围锁要求锁的区间存在：空文件先补一个字节，
        if path.stat().st_size == 0:
            handle.write(b"\0")
            handle.flush()
        try:
            _lock_handle(handle)
        except OSError as exc:
            handle.close()
            raise InstanceLockError(
                f"meta_dir 已被另一个后端进程占用{_holder_hint(path)}：{path}"
            ) from exc
        # 持锁成功：把持有者写到旁路文件（best-effort，失败不影响锁语义）
        try:
            _holder_path(path).write_text(
                f"{os.getpid()}\n{datetime.now().isoformat(timespec='seconds')}\n",
                encoding="utf-8",
            )
        except OSError as exc:
            logger.warning("写入单实例锁持有者信息失败：%s", exc)
    except BaseException:
        try:
            handle.close()
        except OSError:
            pass
        raise
    return handle


def acquire_instance_lock(meta_dir) -> InstanceLock:
    """获取 meta_dir 的单实例锁（同进程重复获取按引用计数放行）。

    拿不到锁时抛 InstanceLockError——调用方（应用启动流程）应当直接失败，
    而不是降级继续跑。
    """
    path = Path(meta_dir) / LOCK_FILE_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    key = str(path.resolve())
    with _held_lock:
        entry = _held.get(key)
        if entry is not None:
            entry[1] += 1
            return InstanceLock(path=path, handle=entry[0], key=key)
        handle = _try_lock_file(path)
        _held[key] = [handle, 1]
    logger.info("单实例锁已获取：%s（pid=%s）", path, os.getpid())
    return InstanceLock(path=path, handle=handle, key=key)


def lock_status() -> dict:
    """当前进程的单实例锁状态（供 /api/health 暴露单实例契约）。"""
    with _held_lock:
        if not _held:
            return {"held": False, "path": None, "pid": None, "refcount": 0}
        key, entry = next(iter(_held.items()))
        return {
            "held": True,
            "path": key,
            "pid": os.getpid(),
            "refcount": int(entry[1]),
        }
