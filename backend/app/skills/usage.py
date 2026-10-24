"""技能 · 用量账本（data/skill_usage.json）。

记录 view/use/patch 与最近活动时间，供描述预算与 Curator 归档使用。
写入是"读-改-写"，用线程锁 + 跨进程文件锁保护，临时文件原子替换。
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from . import scan as scan_mod

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_FILE_LOCK_TIMEOUT_S = 5.0


def ledger_path() -> Path:
    # 与 AGENT_SKILLS_DIR 同级的 data/ 目录（测试会 monkeypatch AGENT_SKILLS_DIR）
    return Path(scan_mod.AGENT_SKILLS_DIR).parent / "skill_usage.json"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class _FileLock:
    """跨进程文件锁；平台不支持或超时则降级（进程内线程锁仍在）。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._fh = None

    def __enter__(self):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._fh = open(self.path, "a+b")
            self._fh.seek(0)
            if self._fh.read(1) == b"":
                self._fh.seek(0)
                self._fh.write(b"\0")
                self._fh.flush()
            deadline = time.time() + _FILE_LOCK_TIMEOUT_S
            while True:
                try:
                    self._fh.seek(0)
                    if os.name == "nt":
                        import msvcrt

                        msvcrt.locking(self._fh.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl

                        fcntl.flock(
                            self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB
                        )
                    break
                except OSError:
                    if time.time() >= deadline:
                        logger.warning("技能用量账本文件锁超时，降级继续")
                        break
                    time.sleep(0.05)
        except Exception as exc:
            logger.warning("技能用量账本加锁失败（降级）：%s", exc)
        return self

    def __exit__(self, *_exc):
        try:
            if self._fh is not None:
                if os.name == "nt":
                    import msvcrt

                    try:
                        self._fh.seek(0)
                        msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
                    except OSError:
                        pass
                else:
                    import fcntl

                    try:
                        fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
                    except OSError:
                        pass
                self._fh.close()
        finally:
            self._fh = None
        return False


def _read() -> dict:
    try:
        data = json.loads(ledger_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write(data: dict) -> None:
    path = ledger_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(
        json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    os.replace(tmp, path)


def _mutate(name: str, apply) -> None:
    if not name:
        return
    with _lock, _FileLock(ledger_path().with_suffix(".lock")):
        data = _read()
        row = data.get(name)
        if not isinstance(row, dict):
            row = {}
        row.setdefault("state", "active")
        row.setdefault("pinned", False)
        apply(row)
        data[name] = row
        _write(data)


def record_view(name: str) -> None:
    """skill_view 成功时：view_count++、use_count++。"""
    stamp = _now()

    def apply(row: dict) -> None:
        row["view_count"] = int(row.get("view_count") or 0) + 1
        row["use_count"] = int(row.get("use_count") or 0) + 1
        row["last_viewed_at"] = stamp
        row["last_used_at"] = stamp

    _mutate(name, apply)


def record_patch(name: str) -> None:
    """write_skill 成功时：patch_count++。"""
    stamp = _now()

    def apply(row: dict) -> None:
        row["patch_count"] = int(row.get("patch_count") or 0) + 1
        row["last_patched_at"] = stamp
        row.setdefault("created_by", "agent")

    _mutate(name, apply)


def snapshot() -> dict:
    with _lock:
        return _read()


def set_pinned(name: str, pinned: bool) -> dict:
    _mutate(name, lambda row: row.__setitem__("pinned", bool(pinned)))
    return snapshot()
