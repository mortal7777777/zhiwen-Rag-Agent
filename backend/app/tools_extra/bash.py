"""命令执行工具：subprocess/docker 沙箱、后台自动接管、技能命令提取与沙箱执行。"""

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
from .bg_jobs import _BG_MAX_ALLOWED_S, _bg_register_existing

logger = logging.getLogger(__name__)


def _run_command(
    settings,
    command: str,
    cwd: str = "",
    project_dir: str | None = None,
    sandbox_override: str | None = None,
    conv_id: int | None = None,
) -> dict:
    """执行命令：默认在工作目录根执行，支持子目录 + 超时 + 截断。

    - 输出走文件而不是管道：后台进程继承管道会让读取端永久挂住；
    - 超时后如果会话可用：**不杀进程，自动登记为后台任务**并返回 job_id，
      后续用 bash_output 轮询（Claude Code 的 auto-background 语义）；
    - 没有会话上下文（旧工具/单测）时保持原行为：超时杀进程树并报错。
    """
    workspace = _resolve_workspace(settings, project_dir)
    workdir = workspace if not cwd else _safe_path(workspace, cwd)
    sandbox = str(
        sandbox_override
        if sandbox_override is not None
        else effective(settings, "command_sandbox")
        or "subprocess"
    ).strip().lower()
    if sandbox == "docker":
        return _run_docker_command(settings, command, workdir)
    meta_dir = getattr(settings, "meta_dir", None)
    persistent = conv_id is not None and meta_dir is not None
    job_id = uuid.uuid4().hex[:12]
    log_path: Path | None = None
    log = None
    handed_off = False
    try:
        argv = _bash_argv(command, settings)
        timeout = max(1, int(effective(settings, "command_timeout") or 60))
        if persistent:
            root = Path(meta_dir) / "background_jobs"
            root.mkdir(parents=True, exist_ok=True)
            log_path = root / f"{job_id}.log"
            log = open(log_path, "w+b")
        else:
            log = tempfile.TemporaryFile("w+b")
        proc = subprocess.Popen(
            argv if argv else command,
            shell=argv is None,
            cwd=workdir,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=os.name != "nt",
            creationflags=(
                subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
            ),
        )
        started = time.time()
        timed_out = False
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            log.flush()
            log.seek(0)
            current = log.read()
            registered = None
            if persistent and log_path is not None:
                registered = _bg_register_existing(
                    conv_id,
                    job_id,
                    command,
                    proc,
                    log_path,
                    started,
                    offset=len(current),
                    max_seconds=_BG_MAX_ALLOWED_S,
                )
            if registered:
                handed_off = True
                text = current.decode("utf-8", errors="replace")
                log.close()
                log = None
                return {
                    "job_id": registered,
                    "status": "running",
                    "summary": (
                        f"命令超过 {timeout}s，已自动转为后台任务（{registered}）；"
                        "用 bash_output 轮询，bash_kill 终止"
                    ),
                    "output": _truncate(text),
                }
            _kill_process_tree(proc)
        log.flush()
        log.seek(0)
        raw = log.read()
    except Exception as exc:
        return {"error": str(exc), "summary": f"命令执行失败：{exc}"}
    finally:
        if log is not None:
            try:
                log.close()
            except Exception:
                pass
        if not handed_off and log_path is not None:
            try:
                log_path.unlink()
            except OSError:
                pass
    output = raw.decode("utf-8", errors="replace")
    if timed_out:
        return {
            "error": f"命令超时（>{timeout}s）已终止",
            "summary": "命令超时已终止",
            "output": _truncate(output),
        }
    # "伪成功"盲区提示：exit=0 但输出全空——命令很可能没按预期执行。
    if proc.returncode == 0 and not output:
        summary = (
            "命令执行完成（exit=0）但没有任何输出——命令可能没按预期执行："
            "请检查引号转义、python -c 多行参数、路径写法（cd /d 是 cmd "
            "语法，bash 里不适用）。"
        )
    else:
        summary = f"命令执行完成（exit={proc.returncode}）"
    return {
        "summary": summary,
        "exit_code": proc.returncode,
        "output": _truncate(output),
    }


def _run_docker_command(settings, command: str, workdir: Path) -> dict:
    """在 Docker 容器内执行命令（真正的进程/网络沙箱）。

    - 容器根文件系统只读（--read-only），临时目录用内存盘（/tmp）；
    - 挂载工作目录到容器 /workspace；开启只读模式时以 :ro 挂载、工作目录为 /scratch；
    - 默认 --network=none（沙箱内无外网）、限内存/CPU/进程数、去掉全部 Linux 能力；
    - 超时 = command_timeout + 30s（含镜像启动开销）。
    """
    cmd = _docker_run_cmd(settings, command, workdir)
    timeout = max(1, int(effective(settings, "command_timeout") or 60)) + 30
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            encoding="utf-8",
            errors="replace",
        )
        output = (proc.stdout or "") + (
            ("\n[stderr] " + proc.stderr) if proc.stderr else ""
        )
        return {
            "summary": f"沙箱命令执行完成（exit={proc.returncode}）",
            "exit_code": proc.returncode,
            "sandbox": "docker",
            "output": _truncate(output),
        }
    except subprocess.TimeoutExpired:
        return {
            "error": f"沙箱命令超时（>{timeout - 30}s）已终止",
            "summary": "沙箱命令超时已终止",
        }
    except FileNotFoundError:
        return {
            "error": "未找到 docker 命令，请确认 Docker Desktop 已启动",
            "summary": "沙箱执行失败：docker 不可用",
        }
    except Exception as exc:
        return {"error": str(exc), "summary": f"沙箱命令执行失败：{exc}"}


def _docker_run_cmd(settings, command: str, workdir: Path) -> list[str]:
    """构建 docker run 参数列表（独立成函数便于单测）。"""
    image = str(effective(settings, "sandbox_image") or "python:3.11-slim").strip()
    host_dir = str(workdir.resolve())
    readonly = bool(effective(settings, "sandbox_workspace_readonly"))
    mount = f"{host_dir}:/workspace:ro" if readonly else f"{host_dir}:/workspace"
    container_wd = "/scratch" if readonly else "/workspace"
    options = [
        "docker",
        "run",
        "--rm",
        "--network=none",
        "--memory=512m",
        "--cpus=1",
        "--pids-limit=256",
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--tmpfs",
        "/tmp:rw,size=256m",
    ]
    if readonly:
        options.extend(["--tmpfs", "/scratch:rw,size=256m"])
    options.extend(
        [
        "-v",
        mount,
        "-w",
        container_wd,
        ]
    )
    return options + [image, "sh", "-c", command]


def make_bash_tool(
    settings,
    project_dir: str | None = None,
    sandbox_override: str | None = None,
    conv_id: int | None = None,
) -> BaseTool:
    def _invoke(command: str, cwd: str = "") -> dict:
        return _run_command(
            settings, command, cwd, project_dir, sandbox_override, conv_id
        )

    return StructuredTool.from_function(
        func=_invoke,
        name="bash",
        description=(
            "在工作目录内执行 shell 命令（Python/Node/Git 等），带超时与输出截断。"
            "超过 command_timeout 会自动转为后台任务并返回 job_id（用 bash_output 轮询）。"
            "环境是 Git Bash：路径用正斜杠（C:/Users/... 或 /c/Users/...），"
            "空设备写 /dev/null 而不是 NUL，"
            "不要用 cd /d 这类 cmd 语法。多行脚本先写入文件再执行，"
            "避免 python -c 的引号转义坑。敏感操作：默认每次请求用户确认；"
            "命中命令白名单的自动放行。"
        ),
        args_schema=None,
    )


def make_command_tool(settings) -> BaseTool:
    """命令工具：白名单命令 + 超时执行（设置页测试用）。"""

    def _invoke(command: str, cwd: str = "") -> dict:
        allowed, reason = command_allowed(settings, command)
        if not allowed:
            return {"error": reason or "命令执行未开启（未配置 COMMAND_ALLOWLIST 白名单）", "summary": reason or "命令被拒绝"}
        return _run_command(settings, command, cwd)

    return StructuredTool.from_function(
        func=_invoke,
        name="command_tool",
        description=(
            "在受控沙箱中执行白名单命令（需管理员在设置中开启并配置白名单）。"
            "用于运行脚本、查看目录、执行简单工具；不在白名单的命令会被拒绝。"
        ),
        args_schema=None,
    )


def extract_commands_from_skill(text: str, limit: int = 8) -> list[str]:
    """从 SKILL.md 的 bash 代码块提取命令（供沙箱执行）。"""
    blocks = re.findall(r"```(?:bash|sh|powershell|cmd)?\s*\n(.*?)```", text, re.DOTALL)
    commands: list[str] = []
    for block in blocks:
        for line in block.splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                commands.append(line)
            if len(commands) >= limit:
                return commands
    return commands


def run_sandbox_command(settings, command: str, timeout: int | None = None) -> dict:
    """技能沙箱执行入口：白名单 + 临时目录 + 超时。"""
    allowed, reason = command_allowed(settings, command)
    if not allowed:
        return {"ok": False, "error": reason or "命令未在白名单内"}
    try:
        with tempfile.TemporaryDirectory(prefix="rag_skill_") as tmp:
            proc = subprocess.run(
                command,
                shell=True,
                cwd=tmp,
                capture_output=True,
                text=True,
                timeout=timeout or max(1, int(effective(settings, "command_timeout") or 60)),
                encoding="utf-8",
                errors="replace",
            )
            return {
                "ok": proc.returncode == 0,
                "exit_code": proc.returncode,
                "output": _truncate((proc.stdout or "") + proc.stderr, 3000),
            }
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "命令超时已终止"}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
