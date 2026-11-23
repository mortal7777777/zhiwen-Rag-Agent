"""文件工具：列目录/读/搜/写/编辑/删除（含 diff 回显）。"""

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


def make_list_dir_tool(settings, project_dir: str | None = None) -> BaseTool:
    workspace = _resolve_workspace(settings, project_dir)

    def _invoke(path: str = ".") -> dict:
        try:
            target = _safe_path(workspace, path)
            base = target if target.is_dir() else target.parent
            entries = [
                {
                    "name": p.name,
                    "is_dir": p.is_dir(),
                    "size": p.stat().st_size if p.is_file() else 0,
                }
                for p in sorted(base.iterdir())[:100]
            ]
            return {
                "summary": f"列出 {base.name or base} 的 {len(entries)} 项",
                "path": str(base),
                "entries": entries,
            }
        except Exception as exc:
            return {"error": str(exc), "summary": f"列出目录失败：{exc}"}

    return StructuredTool.from_function(
        func=_invoke,
        name="list_dir",
        description=(
            "列出工作目录（或子目录）中的文件和文件夹。path 为相对工作目录的路径，"
            "如 '.'、'data'、'backend/app'。用于了解项目结构时优先调用。"
        ),
        args_schema=None,
    )


def make_read_file_tool(settings, project_dir: str | None = None) -> BaseTool:
    workspace = _resolve_workspace(settings, project_dir)

    def _invoke(path: str, offset: int = 0, limit: int = 200) -> dict:
        try:
            target = _safe_path(workspace, path)
            if not target.is_file():
                raise FileNotFoundError(f"文件不存在：{path}")
            lines = target.read_text(encoding="utf-8", errors="ignore").splitlines()
            total = len(lines)
            start = max(0, int(offset))
            end = min(total, start + max(1, int(limit)))
            body = "\n".join(lines[start:end])
            truncated = end < total
            return {
                "summary": f"已读取 {path}（{total} 行，返回 {start+1}-{end} 行）",
                "path": str(target),
                "line_count": total,
                "start_line": start + 1,
                "content": _truncate(body, 8000),
                "truncated": truncated,
            }
        except Exception as exc:
            return {"error": str(exc), "summary": f"读取文件失败：{exc}"}

    return StructuredTool.from_function(
        func=_invoke,
        name="read_file",
        description=(
            "读取文本文件内容（UTF-8）。path 相对工作目录；大文件用 offset/limit "
            "按行分页读取。适合先读取文件了解现状再修改。"
        ),
        args_schema=None,
    )


def make_grep_search_tool(settings, project_dir: str | None = None) -> BaseTool:
    workspace = _resolve_workspace(settings, project_dir)
    # .pytest_cache 在 Windows 上可能有 ACL 权限异常（WinError 5），必须跳过
    _SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build", ".agent_trash", ".pytest_cache"}

    def _walk(path: Path, include: str | None, budget: list[int]):
        if budget[0] <= 0:
            return
        for p in path.iterdir():
            if budget[0] <= 0:
                return
            if p.is_dir():
                if p.name in _SKIP_DIRS:
                    continue
                yield from _walk(p, include, budget)
            elif p.is_file():
                if include and not re.search(include, p.name):
                    continue
                budget[0] -= 1
                yield p

    def _invoke(pattern: str, path: str = ".", include: str = "") -> dict:
        try:
            root = _safe_path(workspace, path)
            if not root.is_dir():
                root = root.parent
            regex = re.compile(pattern)
            matches: list[dict] = []
            budget = [300]
            for fp in _walk(root, include.strip() or None, budget):
                try:
                    with open(fp, "r", encoding="utf-8", errors="ignore") as f:
                        for lineno, line in enumerate(f, 1):
                            if regex.search(line):
                                matches.append(
                                    {
                                        "file": str(fp.relative_to(workspace)).replace("\\", "/"),
                                        "line": lineno,
                                        "text": line.rstrip()[:200],
                                    }
                                )
                                if len(matches) >= 50:
                                    break
                except OSError:
                    continue
                if len(matches) >= 50:
                    break
            return {
                "summary": f"在 {root} 中找到 {len(matches)} 处匹配",
                "pattern": pattern,
                "matches": matches,
            }
        except re.error as exc:
            return {"error": f"正则表达式错误：{exc}", "summary": f"正则错误：{exc}"}
        except Exception as exc:
            return {"error": str(exc), "summary": f"搜索失败：{exc}"}

    return StructuredTool.from_function(
        func=_invoke,
        name="grep_search",
        description=(
            "在工作目录内用正则搜索文本内容（跳过 .git/node_modules 等目录）。"
            "pattern 为正则表达式，path 为起始目录，include 可选文件名校验。"
            "适合定位符号、错误信息、关键词出现的位置。"
        ),
        args_schema=None,
    )


def make_write_file_tool(settings, project_dir: str | None = None) -> BaseTool:
    workspace = _resolve_workspace(settings, project_dir)

    def _invoke(path: str, content: str) -> dict:
        try:
            target = _safe_path(workspace, path)
            existed = target.exists()
            _atomic_write(target, content)
            return {
                "summary": f"{'覆盖' if existed else '新建'} {path}（{len(content)} 字符）",
                "path": str(target),
                "relative_path": str(target.relative_to(workspace)).replace("\\", "/"),
                # 产物证据：created 区分"本步新建"与"覆盖旧文件"，
                # 子代理回报与主 agent 核验都用它（2026-09-24 复用旧产物事故）
                "created": not existed,
                "bytes": len(content.encode("utf-8")),
                "lines": content.count("\n") + 1,
            }
        except Exception as exc:
            return {"error": str(exc), "summary": f"写入文件失败：{exc}"}

    return StructuredTool.from_function(
        func=_invoke,
        name="write_file",
        description=(
            "创建新文件或覆盖已有文件（UTF-8），content 为完整文件内容。"
            "敏感操作：执行前会请求用户确认。修改已有文件时优先用 edit_file。"
        ),
        args_schema=None,
    )


def make_edit_file_tool(settings, project_dir: str | None = None) -> BaseTool:
    workspace = _resolve_workspace(settings, project_dir)

    def _invoke(
        path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
    ) -> dict:
        try:
            target = _safe_path(workspace, path)
            if not target.is_file():
                raise FileNotFoundError(f"文件不存在：{path}")
            text = target.read_text(encoding="utf-8")
            count = text.count(old_string)
            if count == 0:
                return {
                    "error": "old_string 在文件中不存在，请提供文件中真实存在的完整片段（注意缩进与换行）",
                    "summary": "编辑失败：未找到要替换的文本",
                }
            if replace_all:
                new_text = text.replace(old_string, new_string)
            else:
                new_text = text.replace(old_string, new_string, 1)
            _atomic_write(target, new_text)
            preview_old = old_string.strip().splitlines()[0][:60]
            # diff 审批用：截取替换处前后各 1 行上下文，供前端做可视化对比
            before, after = _diff_context(text, old_string, new_string, replace_all)
            return {
                "summary": f"已编辑 {path}：替换 {count if replace_all else 1} 处"
                f"（'{preview_old}…'）",
                "path": str(target),
                "replaced": count if replace_all else 1,
                "remaining_occurrences": max(0, count - (count if replace_all else 1)),
                "diff": {"before": before, "after": after},
                # 产物证据：edit 只描述"改了哪个文件"，created 恒为 False
                "created": False,
                "bytes": len(new_text.encode("utf-8")),
                "lines": new_text.count("\n") + 1,
            }
        except Exception as exc:
            return {"error": str(exc), "summary": f"编辑文件失败：{exc}"}

    return StructuredTool.from_function(
        func=_invoke,
        name="edit_file",
        description=(
            "精确编辑文件：把 old_string 替换为 new_string（需与文件内容完全一致，"
            "含缩进换行）。replace_all=true 时替换所有出现处，否则只替换第一处。"
            "比 write_file 更安全，适合小改动。敏感操作：执行前会请求用户确认。"
        ),
        args_schema=None,
    )


def _diff_context(text: str, old_string: str, new_string: str, replace_all: bool) -> tuple[str, str]:
    """生成替换区域的 before/after 片段（含前后一行上下文），供 diff 审批展示。"""
    after_text = (
        text.replace(old_string, new_string)
        if replace_all
        else text.replace(old_string, new_string, 1)
    )
    return _diff_window(text, old_string), _diff_window(after_text, new_string)


def _diff_window(target: str, anchor: str) -> str:
    idx = target.find(anchor)
    if idx < 0:
        return anchor[:400]
    start = target.rfind("\n", 0, idx) + 1
    end = target.find("\n", idx + len(anchor))
    if end < 0:
        end = len(target)
    return target[start:end][:400]


def make_delete_file_tool(settings, project_dir: str | None = None) -> BaseTool:
    workspace = _resolve_workspace(settings, project_dir)

    def _invoke(path: str) -> dict:
        try:
            target = _safe_path(workspace, path)
            if not target.exists():
                raise FileNotFoundError(f"路径不存在：{path}")
            if target.is_dir():
                return {"error": "暂不支持删除目录，请逐文件删除", "summary": "删除失败：不支持目录"}
            trash = workspace / ".agent_trash" / time.strftime("%Y-%m-%d")
            trash.mkdir(parents=True, exist_ok=True)
            dest = trash / target.name
            n = 1
            while dest.exists():
                dest = trash / f"{target.stem}_{n}{target.suffix}"
                n += 1
            target.rename(dest)
            return {
                "summary": f"已删除 {path}（可恢复：.agent_trash/）",
                "path": str(target),
                "trash_path": str(dest),
            }
        except Exception as exc:
            return {"error": str(exc), "summary": f"删除失败：{exc}"}

    return StructuredTool.from_function(
        func=_invoke,
        name="delete_file",
        description=(
            "删除文件（不物理销毁，移入工作目录 .agent_trash/ 可恢复）。"
            "敏感操作：执行前会请求用户确认。"
        ),
        args_schema=None,
    )


def make_file_tool(settings) -> BaseTool:
    """文件工具：白名单目录内的 list/read/write/append。"""
    workspace = _resolve_workspace(settings)

    def _invoke(operation: str, path: str, content: str = "") -> dict:
        try:
            target = _safe_path(workspace, path)
            op = operation.strip().lower()
            if op in ("list", "ls"):
                base = target if target.is_dir() else target.parent
                entries = [
                    {"name": p.name, "is_dir": p.is_dir(), "size": p.stat().st_size}
                    for p in sorted(base.iterdir())[:50]
                ]
                return {
                    "summary": f"列出 {len(entries)} 项",
                    "entries": entries,
                    "workspace": str(workspace),
                }
            if op in ("read", "cat"):
                if not target.is_file():
                    raise FileNotFoundError(f"文件不存在：{path}")
                text = target.read_text(encoding="utf-8", errors="ignore")
                return {
                    "summary": f"已读取 {path}（{len(text)} 字符）",
                    "content": _truncate(text, 4000),
                }
            if op in ("write", "append"):
                target.parent.mkdir(parents=True, exist_ok=True)
                mode = "a" if op == "append" else "w"
                with open(target, mode, encoding="utf-8") as f:
                    f.write(content)
                return {"summary": f"已{'追加' if op == 'append' else '写入'} {path}"}
            return {"error": f"不支持的操作：{operation}（支持 list/read/write/append）"}
        except Exception as exc:
            return {"error": str(exc), "summary": f"文件操作失败：{exc}"}

    return StructuredTool.from_function(
        func=_invoke,
        name="file_tool",
        description=(
            "在受控工作目录内操作文件：list（列出目录）、read（读取文本）、"
            "write（写入覆盖）、append（追加）。仅限工作目录内，禁止访问其他路径。"
            "当任务需要读写用户项目/知识库文件、整理文档时使用。"
        ),
        args_schema=None,
    )
