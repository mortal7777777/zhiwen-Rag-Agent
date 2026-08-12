"""人工确认（HITL）权限管理：Claude Code 式敏感操作审批。

流程：
- Agent 要执行写文件 / 编辑 / 删除 / 终端命令等敏感操作时，先在 tools 节点
  生成 permission_request 请求并阻塞等待；
- 前端 / 终端收到 permission_request 后展示操作内容，由用户批准或拒绝；
- 用户通过 POST /api/agent/permission/{id}/resolve 提交决定，等待线程被唤醒，
  Agent 继续执行（或跳过该操作并把拒绝原因回传给模型）。

策略（tool_permission_mode）：
- ask   （默认）：敏感操作必须人工确认（对应 Claude Code 默认行为）；
- allow        ：自动批准所有敏感操作（对应 Claude Code --dangerously-skip-permissions）。

命令白名单是"自动放行前缀"：命中白名单的命令无需确认直接执行，
未命中的命令在 ask 模式下仍需人工确认——白名单不是唯一通行证，确认才是兜底。
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# 需要人工确认的工具名（写/改/删/命令）；读类工具不在此列，自动执行
SENSITIVE_TOOLS = {"write_file", "edit_file", "delete_file", "bash", "command_tool"}

# 旧版 file_tool 中需要确认的操作（兼容保留）
SENSITIVE_FILE_OPS = {"write", "append", "edit", "delete", "move", "mkdir"}


def _preview(text: Any, limit: int = 200) -> str:
    s = str(text or "").replace("\r\n", "\n")
    if len(s) <= limit:
        return s
    return s[:limit] + f"…（共 {len(s)} 字符）"


def is_sensitive_tool(name: str, args: dict | None = None) -> bool:
    """判断该工具调用是否属于敏感操作（需要人工确认）。"""
    if name in SENSITIVE_TOOLS:
        return True
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
        return {
            "path": str(args.get("path") or ""),
            "old_string": _preview(args.get("old_string"), 120),
            "new_string": _preview(args.get("new_string"), 120),
            "replace_all": bool(args.get("replace_all", False)),
        }
    if name == "delete_file":
        return {"path": str(args.get("path") or "")}
    if name == "file_tool":
        return {
            "operation": str(args.get("operation") or ""),
            "path": str(args.get("path") or ""),
            "content_preview": _preview(args.get("content")),
        }
    return {str(k): _preview(v, 120) for k, v in args.items()}


def describe_tool_call(name: str, args: dict | None = None) -> str:
    """生成一句话操作描述，用于审批卡片与状态栏。"""
    args = args or {}
    path = str(args.get("path") or "")
    if name in ("bash", "command_tool"):
        return f"执行命令：{str(args.get('command') or '')[:120]}"
    if name == "write_file":
        return f"写入文件 {path}（{len(str(args.get('content') or ''))} 字符）"
    if name == "edit_file":
        return f"编辑文件 {path}：替换 {len(str(args.get('old_string') or ''))} 字符"
    if name == "delete_file":
        return f"删除文件 {path}"
    if name == "file_tool":
        op = str(args.get("operation") or "")
        return f"{op} {path}"
    return f"{name}({str(args)[:80]})"


@dataclass
class PendingRequest:
    """一条待人工确认的审批请求。"""

    id: str
    tool: str
    arguments: dict
    summary: str
    conversation_id: int | None = None
    created_at: float = field(default_factory=time.time)
    status: str = "pending"  # pending | approved | denied
    decision: bool | None = None
    reason: str = ""
    remember_forever: bool = False  # 批准并永久记住（写入命令白名单）
    remember_session: bool = False  # 批准并在本会话内不再询问
    _event: threading.Event = field(default_factory=threading.Event, repr=False)

    def snapshot(self) -> dict:
        return {
            "id": self.id,
            "tool": self.tool,
            "arguments": self.arguments,
            "summary": self.summary,
            "status": self.status,
            "reason": self.reason,
            "remember_forever": self.remember_forever,
            "remember_session": self.remember_session,
            "created_at": self.created_at,
        }


class PermissionManager:
    """审批请求注册表：提交、阻塞等待、批准/拒绝、过期清理。"""

    def __init__(self) -> None:
        self._requests: dict[str, PendingRequest] = {}
        self._session_allowed: dict[int, set[str]] = {}
        self._lock = threading.Lock()

    def submit(
        self,
        tool: str,
        arguments: dict,
        summary: str,
        conversation_id: int | None = None,
    ) -> PendingRequest:
        req = PendingRequest(
            id=f"perm_{uuid.uuid4().hex[:12]}",
            tool=tool,
            arguments=arguments,
            summary=summary,
            conversation_id=conversation_id,
        )
        with self._lock:
            self._requests[req.id] = req
        return req

    def wait(
        self,
        req: PendingRequest,
        timeout: int = 300,
        stop_event: threading.Event | None = None,
    ) -> bool:
        """阻塞等待用户决定；返回 True=批准。支持停止信号与超时自动拒绝。"""
        deadline = time.time() + max(1, timeout)
        while time.time() < deadline:
            if stop_event is not None and stop_event.is_set():
                self._mark(req, False, "用户停止了回答")
                return False
            if req._event.wait(timeout=0.5):
                break
        else:
            self._mark(req, False, "等待确认超时，已自动取消")
            return False
        return bool(req.decision)

    def resolve(
        self,
        request_id: str,
        approve: bool,
        reason: str = "",
        remember_forever: bool = False,
        remember_session: bool = False,
    ) -> PendingRequest | None:
        """用户通过 API 提交决定；请求不存在或已处理时返回 None。"""
        with self._lock:
            req = self._requests.get(request_id)
            if req is None or req.status != "pending":
                return None
            req.remember_forever = remember_forever
            req.remember_session = remember_session
        self._mark(req, approve, reason)
        return req

    def _mark(self, req: PendingRequest, approve: bool, reason: str) -> None:
        with self._lock:
            if req.status != "pending":
                return
            req.status = "approved" if approve else "denied"
            req.decision = approve
            req.reason = (reason or "").strip()
            req._event.set()

    def pending(self) -> list[dict]:
        with self._lock:
            return [
                req.snapshot()
                for req in self._requests.values()
                if req.status == "pending"
            ]

    # ---- 会话级"不再询问"（批准后同会话同命令自动放行） ----

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

    def cleanup(self, max_age: int = 3600) -> int:
        """清理已结束且超过 max_age 的请求，防止内存膨胀。"""
        now = time.time()
        with self._lock:
            stale = [
                rid
                for rid, req in self._requests.items()
                if req.status != "pending" and now - req.created_at > max_age
            ]
            for rid in stale:
                self._requests.pop(rid, None)
        if stale:
            logger.info("清理过期审批请求 %d 条", len(stale))
        return len(stale)


_manager = PermissionManager()


def get_permission_manager() -> PermissionManager:
    """进程内单例：单用户本地应用，无需多租户隔离。"""
    return _manager
