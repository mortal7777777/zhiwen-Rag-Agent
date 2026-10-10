"""SSE 协议守门脚本的自测（P1-1）。

守门脚本 `scripts/check_sse_protocol.py` 在 CI/pre-commit 里跑；这里把它当模块
导入，断言"后端 emit 的事件都被前端处理或显式豁免"这一条在两个方向上都成立，
避免脚本本身失效（比如正则改坏后永远返回通过）。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "check_sse_protocol.py"


def _load_guard():
    spec = importlib.util.spec_from_file_location("check_sse_protocol", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_guard_parses_both_sides():
    guard = _load_guard()
    backend = guard.backend_events()
    frontend = guard.frontend_events()
    # 后端事件集应覆盖协议里最关键的几个
    for name in ("session", "stream_delta", "stream_end", "run_suspended", "todos"):
        assert name in backend, f"未扫到后端事件 {name}"
    # 前端至少解析到处理分支（正则失效时会变成空集）
    assert len(frontend) >= 10
    assert "session" in frontend


def test_no_unhandled_events_beyond_allowlist():
    guard = _load_guard()
    backend = guard.backend_events()
    frontend = guard.frontend_events()
    unhandled = backend - frontend - set(guard.ALLOWED_UNHANDLED)
    assert not unhandled, (
        f"以下事件后端会发但前端没有处理分支：{sorted(unhandled)}；"
        "请补 consumeAgentStream 分支或加入脚本的 ALLOWED_UNHANDLED 并写明原因"
    )


def test_allowlist_entries_still_needed():
    """豁免项不应永久堆积：后端不再发射的事件要从豁免表里删掉。"""
    guard = _load_guard()
    backend = guard.backend_events()
    stale = sorted(set(guard.ALLOWED_UNHANDLED) - backend)
    assert not stale, f"豁免表里的这些事件后端已不再发射，请删除：{stale}"
