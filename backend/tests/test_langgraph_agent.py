"""编排层纯逻辑单测：子代理拆分 / 来源重编号 / 计划硬约束判断。"""

from __future__ import annotations

from app.agent.langgraph_agent import (
    _build_subagent_tasks,
    _failure_limit,
    _is_project_task,
    _iteration_limit,
    _pick_verify_command,
    _plan_hint,
    _remaining_needs_tools,
    _renumber_subagent_sources,
    _run_sensitive_subagent_tool,
    _run_verify,
    _subagent_tools,
)


class TaskSettings:
    task_mode_detect = True
    agent_max_iterations = 6
    agent_max_failures = 3
    agent_task_max_iterations = 24
    agent_task_max_failures = 6


def test_is_project_task():
    assert _is_project_task(TaskSettings(), "请帮我完成整个项目", [])
    assert _is_project_task(TaskSettings(), "短问题", ["1", "2", "3", "4"])
    assert not _is_project_task(TaskSettings(), "什么是实事求是", [])


def test_task_mode_budgets():
    assert _iteration_limit({"task_mode": True}, TaskSettings()) == 24
    assert _failure_limit({"task_mode": True}, TaskSettings()) == 6
    assert _iteration_limit({"task_mode": False}, TaskSettings()) == 6
    assert _failure_limit({"task_mode": False}, TaskSettings()) == 3


def test_build_subagent_tasks_filters_and_dedups():
    state = {
        "plan_map": [
            {"step": "检索知识库资料", "tool_hint": "knowledge_base_search"},
            {"step": "联网搜索最新信息", "tool_hint": "web_search"},
            {"step": "推理总结", "tool_hint": ""},
            {"step": "检索知识库资料", "tool_hint": "knowledge_base_search"},
        ]
    }
    tasks = _build_subagent_tasks(state)
    assert [t["step"] for t in tasks] == ["检索知识库资料", "联网搜索最新信息"]


def test_build_subagent_tasks_cap_four():
    state = {
        "plan_map": [
            {"step": f"步骤{i}", "tool_hint": "web_search"} for i in range(6)
        ]
    }
    assert len(_build_subagent_tasks(state)) == 4


def test_plan_hint_mapping():
    assert _plan_hint("检索知识库中的文档")["tool_hint"] == "knowledge_base_search"
    assert _plan_hint("联网搜索最新新闻")["tool_hint"] == "web_search"
    assert _plan_hint("写一个脚本并运行")["tool_hint"] == "file_tool/bash"
    assert _plan_hint("整理总结")["tool_hint"] == ""


def test_renumber_subagent_sources():
    counter = [5]
    results = [
        {
            "sources": [
                {"index": 1, "type": "kb", "content": "a"},
                {"index": 2, "type": "web", "content": "b"},
            ]
        },
        {"sources": [{"index": 1, "type": "kb", "content": "c"}]},
    ]
    merged = _renumber_subagent_sources(results, counter)
    assert [s["index"] for s in merged] == [6, 7, 8]
    assert counter[0] == 8


def test_remaining_needs_tools():
    assert _remaining_needs_tools(
        [{"text": "检索知识库", "tool_hint": "knowledge_base_search"}]
    )
    assert _remaining_needs_tools(
        [{"step": "运行脚本", "tool_hint": "file_tool/bash"}]
    )
    assert not _remaining_needs_tools([{"text": "整理总结"}])


def test_subagent_tools_for_file_hint_include_write():
    class Settings:
        tool_workspace = ""
        command_allowlist = ""
        command_timeout = 30
        command_sandbox = "subprocess"
        sandbox_image = "python:3.11-slim"
        sandbox_workspace_readonly = False

    class Service:
        settings = Settings()

    names = {t.name for t in _subagent_tools(Service(), "file_tool/bash", [0])}
    assert {"list_dir", "read_file", "grep_search", "write_file", "edit_file", "bash"} <= names


def test_sensitive_subagent_tool_allow_mode():
    class Settings:
        tool_permission_mode = "allow"
        permission_timeout = 300
        command_allowlist = ""

    class Service:
        settings = Settings()

    class FakeTool:
        def invoke(self, args):
            return {"summary": "ok"}

    res = _run_sensitive_subagent_tool(
        Service(),
        None,
        "write_file",
        {"path": "x.txt"},
        lambda t, a: t.invoke(a),
        FakeTool(),
        None,
        {"conv_id": 1},
        None,
    )
    assert res["summary"] == "ok"


# ---------------- 写后验证：类型自适应 + 重试上限 ----------------

class VSettings:
    verify_command = ""
    verify_auto_detect = True
    verify_max_retries = 1
    command_timeout = 30
    command_sandbox = "subprocess"
    sandbox_image = "python:3.11-slim"
    sandbox_workspace_readonly = False


def test_pick_verify_command_by_extension():
    assert "py_compile" in _pick_verify_command(VSettings(), "scripts/app.py")
    assert "node --check" in _pick_verify_command(VSettings(), "ui/index.js")
    assert "json" in _pick_verify_command(VSettings(), "data/cfg.json")
    assert "yaml" in _pick_verify_command(VSettings(), "conf/dev.yml")
    assert _pick_verify_command(VSettings(), "README.md") is None
    assert _pick_verify_command(VSettings(), "noext") is None


def test_pick_verify_command_explicit_wins():
    class S(VSettings):
        verify_command = "python -m pytest -q"

    assert _pick_verify_command(S(), "anything.py") == "python -m pytest -q"


def test_pick_verify_command_auto_disabled():
    class S(VSettings):
        verify_auto_detect = False

    assert _pick_verify_command(S(), "scripts/app.py") is None


def test_run_verify_py_compile(tmp_path):
    good = tmp_path / "ok.py"
    good.write_text("def f():\n    return 1\n", encoding="utf-8")
    bad = tmp_path / "bad.py"
    bad.write_text("def f(:\n", encoding="utf-8")
    results = _run_verify(VSettings(), [str(good), str(bad)])
    by_path = {r["path"]: r for r in results}
    assert by_path[str(good)]["exit_code"] == 0
    assert by_path[str(bad)]["exit_code"] != 0


def test_run_verify_skips_unknown_ext(tmp_path):
    f = tmp_path / "notes.md"
    f.write_text("# hi", encoding="utf-8")
    assert _run_verify(VSettings(), [str(f)]) == []


def test_run_verify_docker_translates_path(tmp_path, monkeypatch):
    from app.agent import langgraph_agent as la
    import app.tools_extra as te

    f = tmp_path / "app.py"
    f.write_text("x = 1\n", encoding="utf-8")
    calls = []
    monkeypatch.setattr(
        te,
        "_run_command",
        lambda settings, cmd: calls.append(cmd)
        or {"exit_code": 0, "output": "ok"},
    )
    monkeypatch.setattr(te, "_resolve_workspace", lambda settings: str(tmp_path))

    class S(VSettings):
        command_sandbox = "docker"

    results = la._run_verify(S(), [str(f)])
    assert results and results[0]["exit_code"] == 0
    assert calls and "/workspace/app.py" in calls[0]


def test_run_verify_json_and_yaml(tmp_path):
    bad_json = tmp_path / "bad.json"
    bad_json.write_text("{not valid json", encoding="utf-8")
    good_json = tmp_path / "good.json"
    good_json.write_text('{"a": 1}', encoding="utf-8")
    bad_yaml = tmp_path / "bad.yaml"
    bad_yaml.write_text("a: [unclosed", encoding="utf-8")
    good_yaml = tmp_path / "good.yaml"
    good_yaml.write_text("a: 1\n", encoding="utf-8")

    results = _run_verify(
        VSettings(),
        [str(bad_json), str(good_json), str(bad_yaml), str(good_yaml)],
    )
    by_path = {r["path"]: r for r in results}
    assert by_path[str(good_json)]["exit_code"] == 0
    assert by_path[str(bad_json)]["exit_code"] != 0
    assert by_path[str(good_yaml)]["exit_code"] == 0
    assert by_path[str(bad_yaml)]["exit_code"] != 0


# ---------------- 系统提示词静态/动态拆分（prompt caching） ----------------

def test_compose_system_prompt_static_dynamic_split():
    from app.agent.prompts import compose_system_prompt

    kwargs = dict(
        template_content="你是助手。",
        use_knowledge_base=True,
        use_web_search=True,
        kb_documents=["doc1.md"],
        todos_text="[ ] 步骤A",
        skills_catalog="skill: x",
    )
    full = compose_system_prompt(**kwargs)
    static = compose_system_prompt(**kwargs, static_only=True)
    dynamic = compose_system_prompt(**kwargs, dynamic_only=True)

    # 静态核心：模板 + 工具规则，不含动态部分
    assert "你是助手。" in static
    assert "doc1.md" not in static and "步骤A" not in static and "skill: x" not in static
    # 动态部分：只含清单/文档/技能
    assert "doc1.md" in dynamic and "步骤A" in dynamic and "skill: x" in dynamic
    assert "你是助手。" not in dynamic
    # 完整 = 静态 + 动态 的信息覆盖（内容不重复，可拼回）
    assert full.count("步骤A") == 1 and static.count("步骤A") == 0
