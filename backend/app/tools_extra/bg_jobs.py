"""后台任务（bash_background / bash_output / bash_kill）：进程、日志、增量读取与清理。"""

from __future__ import annotations

import atexit
import logging
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from langchain_core.tools import BaseTool, StructuredTool

from ..runtime_config import effective
from .common import _atomic_write, _bash_argv, _kill_process_tree, _resolve_workspace, _safe_path, _truncate, command_allowed

logger = logging.getLogger(__name__)


_BG_LOCK = threading.Lock()


_BG_JOBS: dict[str, "_BgJob"] = {}


_BG_MAX_TOTAL = 10


_BG_MAX_PER_CONV = 3


_BG_DEFAULT_MAX_S = 1800   # 默认单任务 30 分钟


_BG_MAX_ALLOWED_S = 21600  # 单任务上限 6 小时（RAGAS 等长评测）


_BG_RETAIN_S = 3600        # 已结束任务保留 1 小时供读取


_BG_MAX_LOG_BYTES = 8 * 1024 * 1024


_BG_MAX_POLL_BYTES = 16000


_BG_MAX_WAIT_MS = 30000


@dataclass
class _BgJob:
    id: str
    conv_id: int
    command: str
    proc: subprocess.Popen
    log_path: Path
    started: float
    max_seconds: int = _BG_DEFAULT_MAX_S
    offset: int = 0
    killed: bool = False
    timed_out: bool = False

    @property
    def status(self) -> str:
        rc = self.proc.poll()
        if rc is None:
            return "running"
        if self.timed_out:
            return "timeout"
        if self.killed:
            return "killed"
        return "exited"


def _bg_cleanup_locked() -> None:
    """清掉过期任务；超长运行的任务直接杀。调用方必须持有 _BG_LOCK。"""
    now = time.time()
    for jid, job in list(_BG_JOBS.items()):
        age = now - job.started
        if job.proc.poll() is None and age > job.max_seconds:
            job.timed_out = True
            _kill_process_tree(job.proc)
        if job.proc.poll() is not None and age > _BG_RETAIN_S:
            try:
                job.log_path.unlink()
            except OSError:
                pass
            _BG_JOBS.pop(jid, None)


def _bg_clamp_seconds(value) -> int:
    try:
        seconds = int(value or _BG_DEFAULT_MAX_S)
    except (TypeError, ValueError):
        seconds = _BG_DEFAULT_MAX_S
    return max(1, min(_BG_MAX_ALLOWED_S, seconds))


def _bg_start(
    settings,
    command: str,
    cwd: str,
    project_dir: str | None,
    sandbox_override: str | None,
    conv_id: int | None,
    max_seconds: int = _BG_DEFAULT_MAX_S,
) -> dict:
    command = (command or "").strip()
    max_seconds = _bg_clamp_seconds(max_seconds)
    if not command:
        return {"error": "命令不能为空", "summary": "后台命令不能为空"}
    if conv_id is None:
        return {"error": "后台任务需要已有会话", "summary": "后台任务需要已有会话"}
    sandbox = str(
        sandbox_override
        if sandbox_override is not None
        else effective(settings, "command_sandbox") or "subprocess"
    ).strip().lower()
    if sandbox == "docker":
        return {
            "error": "后台任务暂不支持 docker 沙箱，请用前台 bash 或切换到 subprocess",
            "summary": "docker 沙箱不支持后台任务",
        }
    workspace = _resolve_workspace(settings, project_dir)
    workdir = workspace if not cwd else _safe_path(workspace, cwd)
    with _BG_LOCK:
        _bg_cleanup_locked()
        running = sum(
            1
            for j in _BG_JOBS.values()
            if j.conv_id == conv_id and j.proc.poll() is None
        )
        if running >= _BG_MAX_PER_CONV:
            return {
                "error": f"当前会话已有 {running} 个后台任务在跑（上限 {_BG_MAX_PER_CONV}）",
                "summary": "后台任务数量已达上限",
            }
        if len(_BG_JOBS) >= _BG_MAX_TOTAL:
            exited = sorted(
                (j for j in _BG_JOBS.values() if j.proc.poll() is not None),
                key=lambda j: j.started,
            )
            for job in exited[: max(1, len(_BG_JOBS) - _BG_MAX_TOTAL + 1)]:
                try:
                    job.log_path.unlink()
                except OSError:
                    pass
                _BG_JOBS.pop(job.id, None)
            if len(_BG_JOBS) >= _BG_MAX_TOTAL:
                return {
                    "error": f"后台任务总数已达上限 {_BG_MAX_TOTAL}",
                    "summary": "后台任务总数已达上限",
                }
        jid = uuid.uuid4().hex[:12]
        root = Path(settings.meta_dir) / "background_jobs"
        root.mkdir(parents=True, exist_ok=True)
        log_path = root / f"{jid}.log"
        log_path.write_bytes(b"")
        argv = _bash_argv(command, settings)
        log = open(log_path, "wb")
        try:
            proc = subprocess.Popen(
                argv if argv else command,
                shell=argv is None,
                cwd=workdir,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=os.name != "nt",
                creationflags=(
                    subprocess.CREATE_NEW_PROCESS_GROUP
                    if os.name == "nt"
                    else 0
                ),
            )
        except Exception as exc:  # noqa: BLE001
            try:
                log_path.unlink()
            except OSError:
                pass
            return {"error": str(exc), "summary": f"后台任务启动失败：{exc}"}
        finally:
            log.close()
        _BG_JOBS[jid] = _BgJob(
            id=jid,
            conv_id=conv_id,
            command=command,
            proc=proc,
            log_path=log_path,
            started=time.time(),
            max_seconds=max_seconds,
        )
    timer = threading.Timer(max_seconds, _bg_expire, args=(jid,))
    timer.daemon = True
    timer.start()
    return {
        "job_id": jid,
        "status": "running",
        "pid": proc.pid,
        "command": command,
        "cwd": str(workdir),
        "summary": f"后台任务已启动（{jid}，PID {proc.pid}）",
        "hint": (
            "用 bash_output 轮询新增输出（wait_ms 最多 30000）；"
            f"bash_kill 终止；本任务上限 {max_seconds}s"
        ),
    }


def _bg_register_existing(
    conv_id: int | None,
    job_id: str,
    command: str,
    proc: subprocess.Popen,
    log_path: Path,
    started: float,
    offset: int = 0,
    max_seconds: int = _BG_DEFAULT_MAX_S,
) -> str | None:
    """把已存在的进程（前台超时）登记为后台任务；超限返回 None。"""
    if conv_id is None:
        return None
    max_seconds = _bg_clamp_seconds(max_seconds)
    with _BG_LOCK:
        _bg_cleanup_locked()
        running = sum(
            1
            for j in _BG_JOBS.values()
            if j.conv_id == conv_id and j.proc.poll() is None
        )
        if running >= _BG_MAX_PER_CONV or len(_BG_JOBS) >= _BG_MAX_TOTAL:
            return None
        if job_id in _BG_JOBS:
            return None
        _BG_JOBS[job_id] = _BgJob(
            id=job_id,
            conv_id=conv_id,
            command=command,
            proc=proc,
            log_path=log_path,
            started=started,
            max_seconds=max_seconds,
            offset=max(0, int(offset or 0)),
        )
    timer = threading.Timer(max_seconds, _bg_expire, args=(job_id,))
    timer.daemon = True
    timer.start()
    return job_id


def _bg_read(conv_id: int | None, job_id: str, wait_ms: int = 0) -> dict:
    job_id = str(job_id or "").strip()
    try:
        wait_ms = max(0, min(int(wait_ms or 0), _BG_MAX_WAIT_MS))
    except (TypeError, ValueError):
        wait_ms = 0
    with _BG_LOCK:
        _bg_cleanup_locked()
        job = _BG_JOBS.get(job_id)
        if job is None or job.conv_id != conv_id:
            return {"error": "后台任务不存在或不属于当前会话", "summary": "后台任务不存在"}
    deadline = time.time() + wait_ms / 1000
    while wait_ms and time.time() < deadline and job.proc.poll() is None:
        try:
            if job.log_path.stat().st_size > job.offset:
                break
        except OSError:
            break
        time.sleep(0.1)

    with _BG_LOCK:
        _bg_cleanup_locked()
        job = _BG_JOBS.get(job_id)
        if job is None or job.conv_id != conv_id:
            return {"error": "后台任务不存在或不属于当前会话", "summary": "后台任务不存在"}
        try:
            size = job.log_path.stat().st_size
        except OSError:
            size = job.offset
        if size > _BG_MAX_LOG_BYTES:
            job.timed_out = True
            _kill_process_tree(job.proc)
            return {
                "job_id": job_id,
                "status": "timeout",
                "error": f"后台任务输出超过 {_BG_MAX_LOG_BYTES // (1024 * 1024)}MB，已终止",
                "summary": "后台任务输出过大已终止",
            }
        output = ""
        if size > job.offset:
            try:
                with open(job.log_path, "rb") as f:
                    f.seek(job.offset)
                    chunk = f.read(_BG_MAX_POLL_BYTES)
                job.offset += len(chunk)
                output = chunk.decode("utf-8", errors="replace")
            except OSError as exc:
                output = f"[读取输出失败：{exc}]"
        status = job.status
        rc = job.proc.poll()
        has_more = size > job.offset
        elapsed = round(time.time() - job.started, 1)
    if status == "running":
        summary = f"后台任务运行中（已 {elapsed}s）"
        if not output:
            summary += "，暂无新输出"
    elif status == "exited":
        summary = f"后台任务已结束（exit={rc}，{elapsed}s）"
    elif status == "killed":
        summary = f"后台任务已终止（{elapsed}s）"
    else:
        summary = f"后台任务超时（>{job.max_seconds}s）已终止"
    return {
        "job_id": job_id,
        "status": status,
        "exit_code": rc,
        "elapsed_s": elapsed,
        "output": output,
        "has_more": has_more,
        "summary": summary,
        "hint": "还有更多输出，请再次调用 bash_output" if has_more else "",
    }


def _bg_kill(conv_id: int | None, job_id: str) -> dict:
    job_id = str(job_id or "").strip()
    with _BG_LOCK:
        _bg_cleanup_locked()
        job = _BG_JOBS.get(job_id)
        if job is None or job.conv_id != conv_id:
            return {"error": "后台任务不存在或不属于当前会话", "summary": "后台任务不存在"}
        if job.proc.poll() is None:
            job.killed = True
            _kill_process_tree(job.proc)
        rc = job.proc.poll()
        status = job.status
        elapsed = round(time.time() - job.started, 1)
    return {
        "job_id": job_id,
        "status": status,
        "exit_code": rc,
        "elapsed_s": elapsed,
        "summary": f"后台任务已终止（{elapsed}s）" if status != "running" else "已发送终止信号",
    }


def _bg_expire(job_id: str) -> None:
    """单任务寿命兜底：即使模型不再轮询，也按时杀掉防孤儿。"""
    with _BG_LOCK:
        job = _BG_JOBS.get(job_id)
        if job is not None and job.proc.poll() is None:
            job.timed_out = True
            _kill_process_tree(job.proc)


def _bg_kill_all() -> None:
    with _BG_LOCK:
        jobs = list(_BG_JOBS.values())
    for job in jobs:
        if job.proc.poll() is None:
            job.killed = True
            _kill_process_tree(job.proc)


def make_background_tools(
    settings,
    project_dir: str | None,
    sandbox_override: str | None,
    conv_id: int | None,
) -> list[BaseTool]:
    """后台任务三件套：启动 / 增量读取 / 终止（仅已有会话）。"""
    if conv_id is None:
        return []

    def _start(
        command: str, cwd: str = "", max_seconds: int = _BG_DEFAULT_MAX_S
    ) -> dict:
        return _bg_start(
            settings,
            command,
            cwd,
            project_dir,
            sandbox_override,
            conv_id,
            max_seconds,
        )

    def _output(job_id: str, wait_ms: int = 0) -> dict:
        return _bg_read(conv_id, job_id, wait_ms)

    def _kill(job_id: str) -> dict:
        return _bg_kill(conv_id, job_id)

    return [
        StructuredTool.from_function(
            func=_start,
            name="bash_background",
            description=(
                "后台执行 shell 命令，立即返回 job_id；适合服务、构建、长测试等"
                "前台会超时的任务。之后用 bash_output 轮询新增输出、bash_kill 终止。"
                "max_seconds 默认 1800，最大 21600（RAGAS/全量评测建议传 7200）。"
                "仅 subprocess 沙箱；需要已有会话。"
                "敏感操作：默认请求用户确认，命中命令白名单自动放行。"
            ),
            args_schema=None,
        ),
        StructuredTool.from_function(
            func=_output,
            name="bash_output",
            description=(
                "读取后台任务的新增输出，返回 status（running/exited/timeout/killed）、"
                "exit_code、elapsed_s、output、has_more。wait_ms 可等待最多 30000ms；"
                "任务未结束时反复调用即可持续监控。"
            ),
            args_schema=None,
        ),
        StructuredTool.from_function(
            func=_kill,
            name="bash_kill",
            description="终止指定后台任务（杀进程树）；任务已结束时返回最终状态。",
            args_schema=None,
        ),
    ]


atexit.register(_bg_kill_all)
