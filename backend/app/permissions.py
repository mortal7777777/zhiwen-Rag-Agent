"""人工确认（HITL）：敏感工具判定 + 审批卡展示数据 + 会话级命令白名单。

2026-09-29 改造：审批执行改走 LangGraph 原生 interrupt 挂起（tools 节点收决定、
POST /agent/resume 续跑），本模块不再持有待审批请求，只剩两件事：
- 判定与展示：is_sensitive_tool / describe_tool_call / display_args / build_diff_lines；
- 会话级"不再询问"命令白名单（进程内单例，随会话生命周期）。

策略（tool_permission_mode）：
- ask   （默认）：敏感操作必须人工确认（对应 Claude Code 默认行为）；
- allow        ：自动批准所有敏感操作（对应 Claude Code --dangerously-skip-permissions）。

命令白名单是"自动放行前缀"：命中白名单的命令无需确认直接执行，
未命中的命令在 ask 模式下仍需人工确认——白名单不是唯一通行证，确认才是兜底。
"""

from __future__ import annotations

import difflib
import logging
import threading
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# 需要人工确认的工具名（写/改/删/命令）；读类工具不在此列，自动执行
SENSITIVE_TOOLS = {
    "write_file",
    "edit_file",
    "delete_file",
    "bash",
    "command_tool",
    "add_document",
    "write_skill",
}

# 旧版 file_tool 中需要确认的操作（兼容保留）
SENSITIVE_FILE_OPS = {"write", "append", "edit", "delete", "move", "mkdir"}


def _preview(text: Any, limit: int = 200) -> str:
    s = str(text or "").replace("\r\n", "\n")
    if len(s) <= limit:
        return s
    return s[:limit] + f"…（共 {len(s)} 字符）"


def build_diff_lines(old: str, new: str, max_lines: int = 80) -> list[dict]:
    """生成 edit_file 审批用的行级 diff（op: "-" 删除 / "+" 新增），超限截断。

    只对比 old_string 与 new_string 本身，不需要读文件内容；
    供 CLI 弹窗与 Web 审批卡渲染同一份数据（红删绿增）。
    """
    old_lines = old.splitlines()
    new_lines = new.splitlines()
    out: list[dict] = []
    matcher = difflib.SequenceMatcher(a=old_lines, b=new_lines, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag in ("replace", "delete"):
            out.extend({"op": "-", "text": line} for line in old_lines[i1:i2])
        if tag in ("replace", "insert"):
            out.extend({"op": "+", "text": line} for line in new_lines[j1:j2])
        if len(out) >= max_lines:
            return out[:max_lines] + [{"op": "…", "text": "（diff 过长已截断）"}]
    return out


def is_sensitive_tool(name: str, args: dict | None = None) -> bool:
    """判断该工具调用是否属于敏感操作（需要人工确认）。"""
    if name in SENSITIVE_TOOLS:
        return True
    # 用户自写工具（user_tools 目录）：按模块声明的 SENSITIVE 标记判断；
    # 未声明标记的工具在加载器里已默认按敏感处理
    try:
        from .user_tool_loader import is_user_tool_sensitive

        user_sensitive = is_user_tool_sensitive(name)
        if user_sensitive is not None:
            return user_sensitive
    except Exception:
        pass
    if name == "file_tool":
        op = str((args or {}).get("operation") or "").strip().lower()
        if op in SENSITIVE_FILE_OPS:
            return True
    return False


def display_args(name: str, args: dict | None = None) -> dict:
    """把工具参数转成适合展示的形态（大段内容只留预览，避免刷屏）。"""
    args = args or {}
    if name in ("bash", "command_tool"):
        return {
            "command": str(args.get("command") or ""),
            "cwd": str(args.get("cwd") or ""),
        }
    if name == "write_file":
        return {
            "path": str(args.get("path") or ""),
            "content_preview": _preview(args.get("content")),
        }
    if name == "edit_file":
        old_text = str(args.get("old_string") or "")
        new_text = str(args.get("new_string") or "")
        return {
            "path": str(args.get("path") or ""),
            # 预览放宽到 800 字符：整块替换的兜底展示仍可读
            "old_string": _preview(old_text, 800),
            "new_string": _preview(new_text, 800),
            "replace_all": bool(args.get("replace_all", False)),
            # 行级 diff（红删绿增）：CLI/Web 审批界面的首选渲染数据
            "diff_lines": build_diff_lines(old_text, new_text),
        }
    if name == "delete_file":
        return {"path": str(args.get("path") or "")}
    if name == "write_skill":
        return {
            "name": str(args.get("name") or ""),
            "category": str(args.get("category") or "other"),
            "description": str(args.get("description") or ""),
            "content_preview": _preview(args.get("content"), 800),
        }
    if name == "add_document":
        return {
            "filename": str(args.get("filename") or ""),
            "content_preview": _preview(args.get("content")),
            "source_note": str(args.get("source_note") or ""),
        }
    if name == "file_tool":
        return {
            "operation": str(args.get("operation") or ""),
            "path": str(args.get("path") or ""),
            "content_preview": _preview(args.get("content")),
        }
    return {str(k): _preview(v, 120) for k, v in args.items()}


# 常见命令前缀 → 一句话说明（审批卡"总结描述"；命令原文在内容区/参数区展示）
_BASH_HINTS = {
    "head": "查看文件开头", "tail": "查看文件末尾", "cat": "查看文件内容",
    "ls": "列出目录内容", "dir": "列出目录内容", "grep": "搜索文本",
    "find": "查找文件", "python": "执行 Python 代码", "python3": "执行 Python 代码",
    "py": "执行 Python 代码", "cd": "切换目录", "mkdir": "创建目录",
    "cp": "复制文件", "mv": "移动或重命名文件", "rm": "删除文件",
    "echo": "输出文本", "curl": "发起网络请求", "ping": "测试网络连通性",
    "git": "执行 git 操作", "pip": "安装或管理 Python 包",
    "powershell": "执行 PowerShell 命令", "pwd": "查看当前目录",
    "df": "查看磁盘空间", "du": "查看目录占用", "ps": "查看进程",
    "systemctl": "管理系统服务", "nohup": "后台运行程序", "tar": "打包或解压文件",
    "zip": "压缩文件", "unzip": "解压文件", "tree": "查看目录树",
}


def describe_tool_call(name: str, args: dict | None = None) -> str:
    """生成一句话操作描述，用于审批卡片与状态栏。"""
    args = args or {}
    path = str(args.get("path") or "")
    if name in ("bash", "command_tool"):
        command = str(args.get("command") or "")
        first = command.strip().split()[0].lower() if command.strip() else ""
        hint = _BASH_HINTS.get(first, "执行命令")
        return f"{hint}（{first or '?'}）"
    if name == "write_file":
        return f"写入文件 {path}（{len(str(args.get('content') or ''))} 字符）"
    if name == "edit_file":
        return f"编辑文件 {path}：替换 {len(str(args.get('old_string') or ''))} 字符"
    if name == "delete_file":
        return f"删除文件 {path}"
    if name == "write_skill":
        return f"创建技能 {str(args.get('name') or '')}（{str(args.get('category') or 'other')}）"
    if name == "add_document":
        return f"向知识库添加文档 {str(args.get('filename') or '')}"
    if name == "file_tool":
        op = str(args.get("operation") or "")
        return f"{op} {path}"
    return f"{name}({str(args)[:80]})"


@dataclass
class PermissionManager:
    """会话级"不再询问"命令白名单（原生挂起改造后仅此职责）。"""

    _session_allowed: dict[int, set[str]] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def is_session_allowed(self, conversation_id: int | None, command: str) -> bool:
        if not conversation_id:
            return False
        with self._lock:
            return command.strip() in self._session_allowed.get(conversation_id, set())

    def mark_session_allowed(self, conversation_id: int | None, command: str) -> None:
        if not conversation_id or not command.strip():
            return
        with self._lock:
            self._session_allowed.setdefault(conversation_id, set()).add(command.strip())


_manager = PermissionManager()


def get_permission_manager() -> PermissionManager:
    """进程内单例：单用户本地应用，无需多租户隔离。"""
    return _manager
