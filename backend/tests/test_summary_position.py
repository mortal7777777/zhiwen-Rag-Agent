"""滚动摘要的输入裁剪与输出限额（2026-10-23）。

回归两件事：
1. 输入按旧→新排列，直接 text[:N] 会丢掉"最新"消息；现在保留头 20% + 尾 80%。
2. 提示词要求 ≤ summary_max_chars，代码却接受 2×；现在硬限额并在句末回退。
"""

from __future__ import annotations

from types import SimpleNamespace

from app.agent.context import ContextService
from app.agent.context.compact import _clip_summary, _render_batch


class FakeSettings:
    summary_max_chars = 800


def _rows(n: int, size: int = 1000) -> list[dict]:
    return [
        {"role": "user" if i % 2 == 0 else "assistant", "content": f"msg-{i}-" + "x" * size}
        for i in range(n)
    ]


def test_render_batch_keeps_newest_and_oldest():
    text = _render_batch(_rows(100))
    assert "msg-0-" in text            # 最早
    assert "msg-99-" in text           # 最新（此前会被截掉）
    assert "已省略" in text
    assert "msg-50-" not in text


def test_render_batch_compresses_tool_rows():
    batch = [
        {"role": "user", "content": "请分析这份数据"},
        {"role": "tool", "content": "  {\n  \"rows\": [1, 2, 3]\n}\n" + "z" * 5000},
    ]
    text = _render_batch(batch)
    tool_line = next(line for line in text.splitlines() if line.startswith("tool:"))
    assert len(tool_line) <= 210
    assert "请分析这份数据" in text


def test_clip_summary_prefers_sentence_boundary():
    text = "第一句话。" * 200
    clipped = _clip_summary(text, 250)
    assert len(clipped) <= 250
    assert clipped.endswith("。")


def test_summarize_output_is_hard_limited(monkeypatch):
    svc = ContextService(FakeSettings(), chat=None)
    captured: dict[str, str] = {}

    def fake_invoke(messages):
        captured["prompt"] = messages[0].content
        return SimpleNamespace(content="很长的摘要内容。" * 500)

    monkeypatch.setattr(svc, "_invoke", fake_invoke)
    summary = svc._summarize(_rows(3), "已有摘要")

    assert summary is not None
    assert len(summary) <= FakeSettings.summary_max_chars
    assert summary.endswith("。")
    # 不可信内容护栏与"保最新"依赖的输入渲染都要在提示词里
    assert "一律忽略" in captured["prompt"]
    assert "msg-2-" in captured["prompt"]
