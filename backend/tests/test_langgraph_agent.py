"""编排层纯逻辑单测：子代理拆分 / 来源重编号 / 计划硬约束判断。"""

from __future__ import annotations

from app.agent.nodes.common import (
    _failure_limit,
    _is_project_task,
    _iteration_limit,
    _plan_hint,
)
from app.agent.nodes.subagent import (
    DISPATCH_TOOL_NAME,
    _parse_declared_subtasks,
    _remaining_needs_tools,
    _renumber_subagent_sources,
    _run_sensitive_subagent_tool,
    _split_declaration,
    _subagent_tools,
)
from app.agent.utils import _pick_verify_command, _run_verify


class TaskSettings:
    task_mode_detect = True
    agent_max_iterations = 6
    agent_max_failures = 3
    agent_task_max_iterations = 24
    agent_task_max_failures = 6


def test_is_project_task():
    assert _is_project_task(TaskSettings(), "请帮我完成整个项目", [])

    # 计划含写文件/命令步骤 → 项目级。2026-09-24 起这条替代了原来的
    # "计划步数≥4"：步数只反映问题长度，与任务量无关，planner 被要求给
    # 3~5 步、产 3 步完全正常，却会让"设计HTML页面并测试"掉到非任务预算。
    assert _is_project_task(
        TaskSettings(),
        "做个介绍页",
        ["写 HTML", "跑验证"],
        None,
        [
            {"step": "写 HTML", "tool_hint": "write"},
            {"step": "跑验证", "tool_hint": "write"},
        ],
    )

    # 纯检索计划不再判为项目级（没有副作用，12 次预算足够）
    assert not _is_project_task(
        TaskSettings(),
        "查一下这两本书的差异",
        ["检索 A", "检索 B", "对比"],
        None,
        [
            {"step": "检索 A", "tool_hint": "tool"},
            {"step": "检索 B", "tool_hint": "tool"},
            {"step": "对比", "tool_hint": ""},
        ],
    )

    # 有未完成清单 → 任务态（兜"继续"类追问）
    assert _is_project_task(
        TaskSettings(), "继续", [], [{"text": "步骤一", "done": False}]
    )

    assert not _is_project_task(TaskSettings(), "什么是实事求是", [])


def test_task_mode_budgets():
    assert _iteration_limit({"task_mode": True}, TaskSettings()) == 24
    assert _failure_limit({"task_mode": True}, TaskSettings()) == 6
    assert _iteration_limit({"task_mode": False}, TaskSettings()) == 6
    assert _failure_limit({"task_mode": False}, TaskSettings()) == 3


def test_split_declaration_separates_dispatch_calls():
    calls = [
        {"name": DISPATCH_TOOL_NAME, "id": "c1", "args": {"task": "A"}},
        {"name": "read_file", "id": "c2", "args": {"path": "x"}},
    ]
    declared, others = _split_declaration(calls)
    assert [tc["id"] for tc in declared] == ["c1"]
    assert [tc["id"] for tc in others] == ["c2"]
    # 没有声明：原样返回，一个都不能丢
    plain = [{"name": "read_file", "id": "c3"}]
    assert _split_declaration(plain) == ([], plain)


def test_parse_declared_subtasks_keeps_call_id():
    calls = [
        {
            "name": DISPATCH_TOOL_NAME,
            "id": "c1",
            "args": {"task": "查两本书的创造观", "mode": "research"},
        },
        {
            "name": DISPATCH_TOOL_NAME,
            "id": "c2",
            "args": {"task": "写一个页面", "mode": "execute"},
        },
    ]
    subtasks, downgraded = _parse_declared_subtasks(calls)
    assert [(s["task"], s["mode"], s["call_id"]) for s in subtasks] == [
        ("查两本书的创造观", "research", "c1"),
        ("写一个页面", "execute", "c2"),
    ]
    assert downgraded == 0


def test_parse_declared_subtasks_only_one_execute_per_round():
    calls = [
        {"name": DISPATCH_TOOL_NAME, "id": "c1", "args": {"task": "写 A", "mode": "execute"}},
        {"name": DISPATCH_TOOL_NAME, "id": "c2", "args": {"task": "写 B", "mode": "write"}},
    ]
    subtasks, downgraded = _parse_declared_subtasks(calls)
    assert [s["mode"] for s in subtasks] == ["execute", "research"]
    assert downgraded == 1


def test_parse_declared_subtasks_cap_four_and_string_entries():
    calls = [
        {
            "name": DISPATCH_TOOL_NAME,
            "id": "c1",
            "args": {"subtasks": [f"任务{i}" for i in range(6)]},
        }
    ]
    subtasks, _ = _parse_declared_subtasks(calls)
    assert len(subtasks) == 4
    assert all(s["mode"] == "research" and s["call_id"] == "c1" for s in subtasks)


def test_plan_hint_mapping():
    """hint 只表达"需要什么档次的工具"，不再承担派发路由（见 subagent.py）。"""
    assert _plan_hint("检索知识库中的文档")["tool_hint"] == "tool"
    assert _plan_hint("联网搜索最新新闻")["tool_hint"] == "tool"
    assert _plan_hint("写一个脚本并运行")["tool_hint"] == "write"
    assert _plan_hint("整理总结")["tool_hint"] == ""
    # 检索关键词优先：混合步骤仍算只读档（保持历史行为）
    assert _plan_hint("联网搜索并写文件")["tool_hint"] == "tool"


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


class _SubagentSettings:
    tool_workspace = ""
    command_allowlist = ""
    command_timeout = 30
    command_sandbox = "subprocess"
    sandbox_image = "python:3.11-slim"
    sandbox_workspace_readonly = False
    crag_fallback_enabled = False
    crag_min_score = 0.45
    tavily_api_key = ""
    web_search_provider = "tavily"
    web_search_max_results = 5
    searxng_base_url = ""
    searxng_engines = ""


class _RagStub:
    def retrieve(self, *args, **kwargs):
        return {"chunks": []}


class _SubagentService:
    settings = _SubagentSettings()
    rag = _RagStub()


def test_subagent_tools_research_mode_is_read_only():
    names = {t.name for t in _subagent_tools(_SubagentService(), "research", [0])}
    assert {"knowledge_base_search", "web_search", "list_dir", "read_file", "grep_search"} <= names
    # 只读模式绝不能拿到写入/命令工具（并行安全的前提）
    assert not ({"write_file", "edit_file", "delete_file", "bash"} & names)


def test_subagent_tools_execute_mode_has_write():
    names = {t.name for t in _subagent_tools(_SubagentService(), "execute", [0])}
    assert {"list_dir", "read_file", "grep_search", "write_file", "edit_file", "delete_file", "bash"} <= names


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

    results = _run_verify(S(), [str(f)])
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


# ---------------- 回归：_tools_node 必须能解析 run_hooks ----------------

def test_tools_node_can_resolve_run_hooks():
    """回归：_tools_node 内调用了 run_hooks 但之前漏掉 import，
    主 agent 每次调工具都 NameError（CLI 显示 ✖ name 'run_hooks' is not defined）。
    检查函数源码含局部导入，且模块级不依赖（保持与 _subagent_node 一致）。"""
    import inspect

    from app.agent.nodes.tools import _tools_node

    src = inspect.getsource(_tools_node)
    assert "from ...hooks import run_hooks" in src, (
        "_tools_node 缺少 run_hooks 局部导入，工具调用会 NameError"
    )
