"""共享安全边界与轨迹摘要接入（2026-10-23）。"""

from __future__ import annotations

from types import SimpleNamespace

from app import trajectory
from app.agent.nodes.prompts import hook_context_notice
from app.agent.nodes.subagent import SUBAGENT_SYSTEM_PROMPT
from app.prompt_safety import UNTRUSTED_NOTE, wrap_untrusted


def test_wrap_untrusted_uses_stable_tag_and_sanitizes_kind():
    text = wrap_untrusted('web"x', "内容")
    assert text.startswith('<untrusted_data kind="webx">')
    assert text.endswith("</untrusted_data>")


def test_subagent_prompt_has_untrusted_and_citation_rules():
    assert UNTRUSTED_NOTE in SUBAGENT_SYSTEM_PROMPT
    assert "不可信数据" in SUBAGENT_SYSTEM_PROMPT
    assert "文件名/页码/链接" in SUBAGENT_SYSTEM_PROMPT


def test_hook_context_is_labeled_as_external_data():
    assert UNTRUSTED_NOTE in hook_context_notice(["[hook] ctx"])


def test_trajectory_keeps_head_and_tail_and_marks_untrusted(monkeypatch):
    captured: dict[str, str] = {}

    def fake_chat(messages):
        captured["prompt"] = messages[0].content
        return SimpleNamespace(content="过程摘要")

    monkeypatch.setattr(trajectory.repo, "set_meta", lambda db, k, v: None)
    trace = [
        {"name": f"t{i}", "arguments": {"i": i}, "summary": f"结果{i}"}
        for i in range(12)
    ]
    out = trajectory.compress_trajectory(
        fake_chat, None, 1, question="做一个长任务", plan=["a", "b"], tool_trace=trace
    )

    assert out == "过程摘要"
    prompt = captured["prompt"]
    assert "t0(" in prompt and "t11(" in prompt   # 前 4 + 后 6
    assert "t5(" not in prompt                     # 中段被省略
    assert UNTRUSTED_NOTE in prompt
