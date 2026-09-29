"""本机文件系统辅助接口（设置页「目录选择器」用）。

安全边界：后端可能监听 0.0.0.0 且无鉴权，所以本接口
- 只允许来自本机（127.0.0.1 / ::1）的请求；
- 只返回目录（名字/路径），不读任何文件内容；
- 单层最多返回 500 条，不做递归。
"""

from __future__ import annotations

import logging
import os
import string
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request

logger = logging.getLogger(__name__)
router = APIRouter(tags=["fs"])

_MAX_ENTRIES = 500


def _client_is_local(request: Request) -> bool:
    host = getattr(getattr(request, "client", None), "host", "") or ""
    return host in ("127.0.0.1", "::1", "localhost")


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
