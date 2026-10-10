#!/usr/bin/env python3
"""SSE 事件协议守门：后端 emit 的事件名必须在前端有处理分支（或显式豁免）。

背景（P1-1，2026-10-08）：事件协议横跨 `backend/app/**`（emit）与
`frontend/src/api/index.js`（consumeAgentStream 的 `event === 'x'` 分支）。
两边一旦漂移，表现是"后端发了、前端当没看见"——静默丢功能。实测就抓到一起：
ChatView 写了 `onPlan` 处理器，但 consumeAgentStream 没有 `plan` 分支。

用法：python scripts/check_sse_protocol.py
退出码：0 = 一致；1 = 有事件未被前端处理且未豁免。
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP_DIR = ROOT / "backend" / "app"
# 前端 api 层已按域拆分（client/sse/agent/sessions/documents/settings），
# 事件分支在 sse.js 里；这里扫整个目录，避免再次因拆分而失效。
API_DIR = ROOT / "frontend" / "src" / "api"

# 有意不消费的事件：值 = 原因（写清楚为什么可以不处理）
ALLOWED_UNHANDLED = {
    "permission_resolved": "仅恢复段发射；CLI 消费，Web 端靠审批卡状态机自己表达",
}


def backend_events(app_dir: Path = APP_DIR) -> set[str]:
    """扫描后端 `xxx.emit("name", ...)` 的事件名。"""
    names: set[str] = set()
    for path in sorted(app_dir.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError):
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if node.func.attr != "emit" or not node.args:
                continue
            first = node.args[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                names.add(first.value)
    return names


def frontend_events(api_dir: Path = API_DIR) -> set[str]:
    """扫描前端 api 层所有 .js 里的 `event === 'x'` 分支名。"""
    names: set[str] = set()
    for path in sorted(api_dir.glob("*.js")):
        text = path.read_text(encoding="utf-8")
        names |= set(re.findall(r"event\s*===\s*'([a-z_]+)'", text))
    return names


def main() -> int:
    be = backend_events()
    fe = frontend_events()
    if not be:
        print("失败：没有从后端扫到任何事件名（解析逻辑或目录变了？）")
        return 1
    unhandled = sorted(be - fe - set(ALLOWED_UNHANDLED))
    extra = sorted(fe - be)
    if unhandled:
        print("失败：以下事件后端会发，但前端 consumeAgentStream 没有处理分支：")
        for name in unhandled:
            print(f"  {name}")
        print(
            "\n处理方式：在 frontend/src/api/index.js 加 `event === '<名>'` 分支；"
            "\n或确认无需消费后加入本脚本的 ALLOWED_UNHANDLED 并写明原因。"
        )
        return 1
    print(f"SSE 协议检查通过：后端 {len(be)} 个事件，前端处理 {len(fe)} 个"
          f"（豁免 {len(ALLOWED_UNHANDLED)} 个）。")
    if extra:
        # 前端多出来的分支不算错（自建事件/历史兼容），提示一下即可
        print(f"提示：前端有后端未发射的分支（可能是兼容保留）：{', '.join(extra)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
