"""prepare 的提示词解析接线：锁定内容进消息、版本只进遥测。"""

from __future__ import annotations

from langchain_core.messages import SystemMessage

from app.agent.nodes import prepare as prepare_module
from app.agent.nodes.prepare import _prepare_node
from app.agent.prompts import default_fragments
from app.prompting import PromptResolution
from _node_rt import runtime_from_state  # noqa: E402

from test_dblock_versions import (  # noqa: E402
    _FakeBus,
    _FakeContext,
    _FakeRepo,
    _FakeService,
    _FakeSettings,
    _state,
)


def test_prepare_uses_locked_fragments_and_records_telemetry(monkeypatch, tmp_path):
    fragments = default_fragments()
    fragments["tool.knowledge"] = "自定义知识库规则"
    resolution = PromptResolution(
        template_content="锁定模板",
        fragments=fragments,
        telemetry={"bundle_hash": "bundle-abc", "template_version_no": 3},
        source="locked",
    )
    monkeypatch.setattr(prepare_module, "repo", _FakeRepo([]))
    monkeypatch.setattr(
        prepare_module,
        "resolve_conversation_prompt",
        lambda *args, **kwargs: resolution,
    )
    monkeypatch.setattr(
        "app.project_memory.load_project_memory", lambda settings, path=None: None
    )

    settings = _FakeSettings()
    state = _state(
        _service=_FakeService(_FakeContext([]), settings),
        _bus=_FakeBus(),
        project_dir=str(tmp_path),
        use_knowledge_base=True,
    )
    result = _prepare_node(state, runtime_from_state(state))

    first_system = next(
        m for m in result["messages"] if isinstance(m, SystemMessage)
    )
    assert first_system.content.startswith("锁定模板")
    assert "自定义知识库规则" in first_system.content
    assert state["runtime"]["prompt"]["bundle_hash"] == "bundle-abc"
    assert "prompt_ms" in state["runtime"]["timings"]

    all_text = "".join(
        str(m.content)
        for m in result["messages"]
        if isinstance(m, SystemMessage)
    )
    assert "bundle-abc" not in all_text
    assert "template_version_no" not in all_text
