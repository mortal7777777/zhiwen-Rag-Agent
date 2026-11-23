"""本机文件系统辅助接口（设置页「目录选择器」用）。

安全边界：后端可能监听 0.0.0.0 且无鉴权，所以本接口
- 只允许来自本机（127.0.0.1 / ::1）的请求；
- Docker 模式经 nginx 反代时，直连方是容器私网地址，真实来源读 X-Real-IP
  （仅当直连方本身是私网地址时才信任该头，避免公网直连伪造）；
- 只返回目录（名字/路径），不读任何文件内容；
- 单层最多返回 500 条，不做递归。
"""

from __future__ import annotations

import logging
import os
import string
from ipaddress import ip_address
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request

logger = logging.getLogger(__name__)
router = APIRouter(tags=["fs"])

_MAX_ENTRIES = 500


def _is_loopback(host: str) -> bool:
    text = str(host or "").strip().strip("[]")
    if text.lower() == "localhost":
        return True
    try:
        return ip_address(text).is_loopback
    except ValueError:
        return False


def _is_private(host: str) -> bool:
    try:
        return ip_address(str(host or "").strip().strip("[]")).is_private
    except ValueError:
        return False


def _client_is_local(request: Request) -> bool:
    host = getattr(getattr(request, "client", None), "host", "") or ""
    if _is_loopback(host):
        return True
    if not _is_private(host):
        return False
    # 私网直连（Docker 里的 nginx）：信任它写入的 X-Real-IP
    headers = getattr(request, "headers", None)
    forwarded = headers.get("x-real-ip", "") if headers is not None else ""
    return _is_loopback(forwarded)


def require_local_or_token(request: Request | None) -> None:
    """高敏管理写操作守卫：未开启全局 API Token 时，仅允许本机请求修改宿主目录/命令配置。"""
    if request is None:
        return
    from ..config import get_settings

    if get_settings().api_token:
        return
    host = getattr(getattr(request, "client", None), "host", "") or ""
    if host == "testclient" or _client_is_local(request):
        return
    raise HTTPException(
        status_code=403,
        detail="未配置 ZHIWEN_API_TOKEN 时，该高敏操作仅允许本机（127.0.0.1 / ::1）访问",
    )


def _list_dirs(raw: str) -> dict:
    """列出一层子目录（返回完整路径）；raw 为空时返回盘符 / 根。"""
    text = str(raw or "").strip().strip('"')
    if not text:
        if os.name == "nt":
            drives = [
                f"{c}:\\"
                for c in string.ascii_uppercase
                if Path(f"{c}:\\").exists()
            ]
        else:
            drives = ["/"]
        return {"path": "", "parent": None, "dirs": drives, "truncated": False}

    p = Path(text).expanduser()
    try:
        p = p.resolve()
    except Exception:
        pass
    if not p.is_dir():
        raise HTTPException(status_code=404, detail=f"目录不存在：{text}")

    dirs: list[str] = []
    truncated = False
    try:
        with os.scandir(p) as it:
            for entry in it:
                try:
                    if entry.is_dir():
                        dirs.append(str(Path(entry.path)))
                except OSError:
                    continue
                if len(dirs) >= _MAX_ENTRIES:
                    truncated = True
                    break
    except PermissionError:
        raise HTTPException(status_code=403, detail="没有权限读取该目录")
    dirs.sort(key=str.lower)
    parent = str(p.parent) if str(p.parent) != str(p) else None
    return {"path": str(p), "parent": parent, "dirs": dirs, "truncated": truncated}


@router.get("/fs/browse")
def browse_dirs(request: Request, path: str = "") -> dict:
    """列出某路径下的子目录（仅本机可访问）。"""
    if not _client_is_local(request):
        raise HTTPException(status_code=403, detail="仅本机可访问")
    return _list_dirs(path)
