"""工具层公共辅助：工作目录解析、路径边界、输出截断、命令白名单、进程树终止。"""

from __future__ import annotations

import atexit
import logging
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from langchain_core.tools import BaseTool, StructuredTool

from ..runtime_config import effective

logger = logging.getLogger(__name__)


def _resolve_workspace(settings, project_dir: str | None = None) -> Path:
    root = (effective(settings, "tool_workspace") or "").strip()
    if root:
        p = Path(root).expanduser().resolve()
    elif project_dir:
        # 会话工作目录：CLI 从哪个目录启动，文件/命令工具就在哪个目录工作
        p = Path(project_dir).expanduser().resolve()
    else:
        p = Path(__file__).resolve().parents[2]  # rag_knowledge_base/
    p.mkdir(parents=True, exist_ok=True)
    return p


def _safe_path(workspace: Path, path: str) -> Path:
    """把路径解析到工作目录内；支持相对路径与工作目录内的绝对路径，拒绝穿越。

    绝对路径只要落在工作目录内就自动接受（模型经常拿到绝对路径），
    落在外面则给出可操作的错误：工作目录根 + 改用相对路径的建议。
    """
    p = Path(path or "").expanduser()
    if not p.is_absolute():
        p = workspace / p
    target = p.resolve()
    if workspace != target and workspace not in target.parents:
        raise ValueError(
            f"路径超出工作目录（工作目录：{workspace}）。"
            f"请使用工作目录内的相对路径，如 `practice/notes.md`；"
            f"若确实需要在 {p} 等外部位置操作，请先让用户在设置中修改「工作目录」。"
        )
    return target


def _truncate(text: str, limit: int = 4000) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n…（输出已截断，共 {len(text)} 字符）"


def _atomic_write(target: Path, content: str) -> None:
    """原子写入：先写临时文件再替换，避免进程中断产生半截文件。"""
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp, target)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def command_allowed(settings, command_line: str) -> tuple[bool, str]:
    """命令自动放行判断：内置只读集合 + 用户白名单前缀。

    内置只读集合（command_risk.SAFE_READONLY_COMMANDS）覆盖 echo/pwd/ls/cat/
    git status 等常见低风险命令；含重定向/管道/串联/命令替换、提权包装、
    高危命令与解释器命令一律不自动放行，退回人工确认。
    """
    from ..command_risk import (
        _compound_reason,
        high_risk_command,
        is_interpreter_command,
        safe_readonly_command,
    )

    raw = (effective(settings, "command_allowlist") or "").strip()
    line = command_line.strip()
    if not line:
        return False, "命令为空"
    risk_reason = high_risk_command(line)
    if risk_reason:
        return False, f"高危命令（{risk_reason}）：不能自动放行，需人工确认"
    if is_interpreter_command(line):
        return False, "解释器命令不能自动放行（命令体不受白名单前缀约束）"
    if "\n" in line:
        return False, "命令包含多行（不能自动放行）"
    compound = _compound_reason(line)
    if compound:
        return False, f"{compound}（只对单条只读命令自动放行）"
    safe_reason = safe_readonly_command(line)
    if safe_reason is None:
        return True, ""
    if not raw:
        return False, ""
    for prefix in [p.strip() for p in raw.split(",") if p.strip()]:
        if line == prefix or line.startswith(prefix + " "):
            return True, ""
    return False, f"命令不在自动放行白名单内：{line[:80]}"


def _is_wsl_bash(path: str) -> bool:
    """System32/bash.exe 是 WSL 启动器，不是 Git Bash。"""
    if not path:
        return False
    root = os.environ.get("SystemRoot", r"C:\Windows")
    wsl_paths = {
        os.path.normcase(os.path.abspath(os.path.join(root, "System32", "bash.exe"))),
        os.path.normcase(os.path.abspath(os.path.join(root, "Sysnative", "bash.exe"))),
    }
    try:
        return os.path.normcase(os.path.abspath(path)) in wsl_paths
    except OSError:
        return False


def _find_git_bash() -> str | None:
    """定位 Git Bash（不返回 WSL 的 System32/bash.exe）。"""
    roots: list[Path] = []
    git = shutil.which("git") or shutil.which("git.exe")
    if git:
        try:
            roots.append(Path(git).resolve().parent.parent)
        except OSError:
            pass
    roots += [Path(r"C:\Program Files\Git"), Path(r"C:\Program Files (x86)\Git")]
    local = os.environ.get("LOCALAPPDATA")
    if local:
        roots.append(Path(local) / "Programs" / "Git")
    for root in roots:
        for rel in ("bin/bash.exe", "usr/bin/bash.exe"):
            candidate = root / rel
            try:
                if candidate.is_file():
                    return str(candidate)
            except OSError:
                continue
    bash = shutil.which("bash") or shutil.which("bash.exe")
    if bash and not _is_wsl_bash(bash):
        return bash
    return None


def _find_wsl_bash() -> str | None:
    """显式选择 WSL 时才调用：优先 wsl.exe，其次 System32/bash.exe。"""
    wsl = shutil.which("wsl") or shutil.which("wsl.exe")
    if wsl:
        return wsl
    bash = shutil.which("bash") or shutil.which("bash.exe")
    if bash and _is_wsl_bash(bash):
        return bash
    return None


def detect_bash_backend(settings=None) -> dict:
    """解析命令 shell 后端：auto | gitbash | wsl | default。

    auto 只选 Git Bash（与宿主 Windows 路径/Python 一致），**绝不静默选 WSL**：
    WSL 与宿主是两套文件系统/解释器，误选会让 `D:/...` 路径和 Windows Python
    全部失效（conv 408 的 6 次假验证失败就是这么来的）。要用 WSL 必须显式设
    `COMMAND_SHELL=wsl`，并自行使用 `/mnt/...` 路径。
    """
    choice = str(getattr(settings, "command_shell", "") or "auto").strip().lower()
    if os.name != "nt":
        return {"backend": "posix", "path": "", "is_wsl": False}
    if choice == "gitbash":
        path = _find_git_bash()
        return (
            {"backend": "gitbash", "path": path, "is_wsl": False}
            if path
            else {"backend": "missing", "path": "", "is_wsl": False}
        )
    if choice == "wsl":
        path = _find_wsl_bash()
        return (
            {"backend": "wsl", "path": path, "is_wsl": True}
            if path
            else {"backend": "missing", "path": "", "is_wsl": False}
        )
    if choice == "default":
        return {"backend": "default", "path": "", "is_wsl": False}
    path = _find_git_bash()
    if path:
        return {"backend": "gitbash", "path": path, "is_wsl": False}
    return {"backend": "default", "path": "", "is_wsl": False}


_DRIVE_PATH_RE = re.compile(r"^([A-Za-z]):[\\/](.*)$")


def shell_path(path: str, backend: str) -> str:
    """把 Windows 盘符路径翻译成 Git Bash (/d/...) 或 WSL (/mnt/d/...) 路径。"""
    if os.name != "nt" or backend not in ("gitbash", "wsl"):
        return str(path)
    raw = str(path).strip().strip('"')
    match = _DRIVE_PATH_RE.match(raw)
    if not match:
        return raw
    drive, rest = match.group(1).lower(), match.group(2).replace("\\", "/")
    prefix = "/mnt" if backend == "wsl" else ""
    return f"{prefix}/{drive}/{rest}"


def shell_python(settings=None) -> str:
    """当前 shell 后端下可执行的 Python 解释器路径。"""
    backend = detect_bash_backend(settings).get("backend", "")
    exe = sys.executable
    if backend in ("gitbash", "wsl"):
        return shell_path(exe, backend)
    return exe


def bash_backend_info(settings=None) -> dict:
    """健康检查展示：当前实际使用的 shell 后端与解释器（便于排查环境割裂）。"""
    backend = detect_bash_backend(settings)
    info = {"backend": backend.get("backend"), "path": backend.get("path") or ""}
    if os.name == "nt" and backend.get("backend") in ("gitbash", "wsl"):
        info["python"] = shell_python(settings)
    return info


def _bash_argv(command: str, settings=None) -> list[str] | None:
    """按显式/自动选择的后端执行命令；None 表示沿用默认 shell（cmd.exe）。"""
    if os.name != "nt":
        return None
    backend = detect_bash_backend(settings)
    kind = backend.get("backend")
    path = backend.get("path") or ""
    if kind == "gitbash":
        return [path, "-c", command]
    if kind == "wsl":
        if path.lower().endswith("wsl.exe"):
            return [path, "bash", "-lc", command]
        return [path, "-lc", command]
    return None


def _kill_process_tree(proc: subprocess.Popen) -> None:
    """超时后尽力杀掉整棵进程树（子 shell 可能又起了后台进程）。"""
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True,
                timeout=15,
            )
            return
        import signal

        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
