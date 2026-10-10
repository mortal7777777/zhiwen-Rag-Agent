"""单实例锁：第二个进程必须启动失败，进程退出（含崩溃）后锁自动释放。

背景（P0）：会话闸门 / 审批 watchdog / 挂起用量快照都在进程内存，多开进程
共享同一 meta_dir 会静默破坏挂起语义。锁是 OS 级文件锁，跨进程才冲突
（Windows 同进程不同句柄不冲突），所以冲突路径必须用子进程验证。
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import single_instance
from app.single_instance import (
    InstanceLockError,
    acquire_instance_lock,
    lock_status,
)

BACKEND_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _cleanup_locks():
    """兜底清理：任何用例失败留下的持锁都解锁，避免污染后续用例。"""
    yield
    with single_instance._held_lock:
        entries = list(single_instance._held.items())
        single_instance._held.clear()
    for _key, entry in entries:
        try:
            single_instance._unlock_handle(entry[0])
        except OSError:
            pass
        try:
            entry[0].close()
        except OSError:
            pass


def _run_child(script: str) -> subprocess.CompletedProcess:
    """在子进程里执行脚本（cwd=backend，保证 import app 可用）。"""
    try:
        return subprocess.run(
            [sys.executable, "-c", script],
            cwd=str(BACKEND_ROOT),
            capture_output=True,
            text=True,
            timeout=120,
        )
    except OSError as exc:  # 环境不允许起子进程：跳过而不是误报失败
        pytest.skip(f"当前环境无法启动子进程：{exc}")


# ---------------- 基本语义 ----------------


def test_acquire_creates_lock_file_and_status(tmp_path):
    lock = acquire_instance_lock(tmp_path)
    try:
        assert lock.path == tmp_path / single_instance.LOCK_FILE_NAME
        assert lock.path.exists()
        status = lock_status()
        assert status["held"] is True
        assert status["pid"] == os.getpid()
        assert str(lock.path) in str(status["path"])
        # 持有者信息在旁路文件里（锁文件本身被强制锁住，读不了）
        holder = lock.path.parent / single_instance.HOLDER_FILE_NAME
        content = holder.read_text(encoding="utf-8")
        assert str(os.getpid()) in content
    finally:
        lock.release()
    assert lock_status()["held"] is False


def test_reentrant_acquire_released_by_refcount(tmp_path):
    """同进程重复获取放行（uvicorn --reload / 嵌套调用），按引用计数释放。"""
    first = acquire_instance_lock(tmp_path)
    second = acquire_instance_lock(tmp_path)
    assert lock_status()["refcount"] == 2

    first.release()
    assert lock_status()["held"] is True, "还有一份持有，不能提前解锁"

    second.release()
    assert lock_status()["held"] is False

    # 释放后可以重新获取
    again = acquire_instance_lock(tmp_path)
    again.release()


def test_release_is_idempotent(tmp_path):
    lock = acquire_instance_lock(tmp_path)
    lock.release()
    lock.release()  # 重复释放不应报错，也不应影响后续获取
    lock2 = acquire_instance_lock(tmp_path)
    lock2.release()


def test_os_error_maps_to_instance_lock_error_with_holder_hint(tmp_path, monkeypatch):
    """加锁失败要转成 InstanceLockError，并把锁文件里的持有者信息带进消息。"""
    holder = tmp_path / single_instance.HOLDER_FILE_NAME
    holder.write_text("12345\n2026-10-08T10:00:00\n", encoding="utf-8")

    def _conflict(handle):
        raise OSError("lock violation")

    monkeypatch.setattr(single_instance, "_lock_handle", _conflict)
    with pytest.raises(InstanceLockError) as exc:
        acquire_instance_lock(tmp_path)
    message = str(exc.value)
    assert "12345" in message
    assert str(tmp_path) in message


# ---------------- 跨进程冲突 ----------------


def test_second_process_refused(tmp_path):
    holder = acquire_instance_lock(tmp_path)
    try:
        result = _run_child(
            "from app.single_instance import acquire_instance_lock\n"
            f"acquire_instance_lock({str(tmp_path)!r})\n"
            "print('acquired-in-child')\n"
        )
        assert "acquired-in-child" not in result.stdout
        assert result.returncode != 0
        output = result.stdout + result.stderr
        assert "InstanceLockError" in output
        # 报错要能指出持有者是谁（pid 来自锁文件）
        assert str(os.getpid()) in output
        # 也要带上锁文件路径（运维照着就能查是谁占着）
        assert str(tmp_path) in output
    finally:
        holder.release()


def test_lock_released_when_holder_process_dies(tmp_path):
    """持有者不 release 直接退出（模拟崩溃）：内核释放锁，新进程可立即获取。"""
    result = _run_child(
        "import os\n"
        "from app.single_instance import acquire_instance_lock\n"
        f"acquire_instance_lock({str(tmp_path)!r})\n"
        "print('held', flush=True)\n"
        "os._exit(0)\n"  # 故意不走 release()
    )
    assert result.returncode == 0, result.stderr
    assert "held" in result.stdout

    lock = acquire_instance_lock(tmp_path)
    lock.release()


def test_second_process_succeeds_after_release(tmp_path):
    holder = acquire_instance_lock(tmp_path)
    holder.release()
    result = _run_child(
        "from app.single_instance import acquire_instance_lock\n"
        f"lock = acquire_instance_lock({str(tmp_path)!r})\n"
        "print('acquired-in-child')\n"
        "lock.release()\n"
    )
    assert "acquired-in-child" in result.stdout, result.stderr
    assert result.returncode == 0


# ---------------- 应用接线（lifespan / health） ----------------


def test_lifespan_acquires_and_releases_instance_lock(tmp_path, monkeypatch):
    """启动流程必须取锁、关闭时释放；拿不到锁要直接失败（不降级继续跑）。"""
    import app.main as main_module
    from app.api import deps as deps_module

    warmup_called = threading.Event()

    def _fake_get_service():
        # 预热线程里调用；这里直接抛错（线程内部会捕获并记 warning），
        # 避免测试加载真实的本地 BGE 模型。
        warmup_called.set()
        raise RuntimeError("测试环境跳过模型预热")

    monkeypatch.setattr(
        main_module,
        "settings",
        SimpleNamespace(
            app_name="测试",
            version="0",
            meta_dir=tmp_path,
            ensure_dirs=lambda: None,
        ),
    )
    monkeypatch.setattr(main_module, "init_db", lambda: False)
    monkeypatch.setattr(deps_module, "get_service", _fake_get_service)

    async def _scenario():
        async with main_module.lifespan(None):
            assert lock_status()["held"] is True

    asyncio.run(_scenario())
    assert lock_status()["held"] is False
    assert warmup_called.wait(2.0), "预热线程应在补丁生效期内执行完"


def test_lifespan_refuses_when_lock_taken(tmp_path, monkeypatch):
    """启动流程拿不到锁时必须直接失败（跨进程冲突本身见上面子进程用例）。"""
    import app.main as main_module

    monkeypatch.setattr(
        main_module,
        "settings",
        SimpleNamespace(
            app_name="测试",
            version="0",
            meta_dir=tmp_path,
            ensure_dirs=lambda: None,
        ),
    )
    monkeypatch.setattr(main_module, "init_db", lambda: False)

    def _conflict(meta_dir):
        raise InstanceLockError("meta_dir 已被另一个后端进程占用")

    monkeypatch.setattr(main_module, "acquire_instance_lock", _conflict)

    async def _scenario():
        async with main_module.lifespan(None):
            raise AssertionError("拿不到锁时不应进入服务运行态")

    with pytest.raises(InstanceLockError):
        asyncio.run(_scenario())


def test_health_reports_instance_lock():
    from app.api.health import health

    payload = health()
    assert payload["status"] == "ok"
    assert isinstance(payload["instance_lock"], dict)
    assert "held" in payload["instance_lock"]
