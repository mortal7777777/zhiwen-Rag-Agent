"""子代理与主链的行为对齐：XML 兜底、空正文重试、结果瘦身、hooks、故障隔离、预算。

这些能力主链早有、子代理原先没有。差异会让同一供应商的怪毛病在子代理里
静默失败（工具不执行 / 返回"未产出结论"），且并行分支绕过用户的 hook 约束。
"""

from __future__ import annotations

import threading
from queue import Queue
from types import SimpleNamespace

from langchain_core.messages import AIMessage, SystemMessage, ToolMessage

from app.agent.nodes import subagent as subagent_module
from app.agent.nodes.subagent import _subagent_node
from app.agent.state import EventBus


class _Settings:
    tool_permission_mode = "ask"
    command_allowlist = ""
    agent_subagent_max_rounds = 2
    agent_subagent_exec_max_rounds = 3
    agent_subagent_max_llm_calls = 24


class _Rag:
    def acquire_llm(self):
        pass

    def release_llm(self):
        pass


class _FakeTool:
    def __init__(self, name: str, result: dict):
        self.name = name
        self.description = name
        self.result = result
        self.calls: list[dict] = []

    def invoke(self, args):
        self.calls.append(args)
        return dict(self.result)


class _SeqChat:
    """按顺序吐出预设响应的假模型（异常实例会被抛出）。"""

    def __init__(self, responses: list):
        self._responses = list(responses)
        self.calls = 0
        self.seen: list = []

    def bind_tools(self, tools):
        return self

    def invoke(self, messages, config=None):
        self.calls += 1
        self.seen = list(messages)
        item = (
            self._responses.pop(0)
            if self._responses
            else AIMessage(content="（无更多响应）")
        )
        if isinstance(item, Exception):
            raise item
        return item


def _call(name: str = "read_file", args: dict | None = None, call_id: str = "t1"):
    return {
        "name": name,
        "args": args or {"path": "a.txt"},
        "id": call_id,
        "type": "tool_call",
    }


def _run(
    monkeypatch,
    *,
    chat,
    tool,
    chat_plain=None,
    mode: str = "research",
    settings=None,
    deps=None,
):
    monkeypatch.setattr(
        subagent_module, "_subagent_tools", lambda *a, **k: [tool]
    )
    settings = settings or _Settings()
    if deps is None:
        deps = SimpleNamespace(
            service=SimpleNamespace(
                settings=settings,
                chat=chat,
                chat_plain=chat_plain or _SeqChat([]),
                rag=_Rag(),
            ),
            bus=EventBus(Queue()),
            db=None,
            stop_event=None,
        )
    state = {
        "runtime": {"conv_id": 1},
        "question": "原始问题",
        "sub_task": {"task": "子任务", "mode": mode, "call_id": "c1"},
        "project_dir": None,
        "conversation_id": 1,
    }
    return _subagent_node(state, SimpleNamespace(context=deps))


def test_xml_style_tool_call_is_executed(monkeypatch):
    tool = _FakeTool("read_file", {"summary": "读到 a", "content": "hello"})
    chat = _SeqChat(
        [
            AIMessage(
                content=(
                    '<tool_calls><invoke name="read_file">'
                    '<parameter name="path">a.txt</parameter>'
                    "</invoke></tool_calls>"
                )
            ),
            AIMessage(content="结论：读到 hello"),
        ]
    )

    out = _run(monkeypatch, chat=chat, tool=tool)

    assert tool.calls == [{"path": "a.txt"}]
    assert out["subagent_results"][0]["summary"] == "结论：读到 hello"
    # 重建的 AIMessage 与 ToolMessage 必须 id 配对（协议要求）
    ai = next(m for m in chat.seen if getattr(m, "tool_calls", None))
    tool_msg = next(m for m in chat.seen if isinstance(m, ToolMessage))
    assert tool_msg.tool_call_id == ai.tool_calls[0]["id"]


def test_malformed_xml_gets_one_corrective_retry(monkeypatch):
    tool = _FakeTool("read_file", {"summary": "不该执行"})
    chat = _SeqChat(
        [
            AIMessage(
                content='<tool_calls><invoke name="read_file">未闭合'
            ),
            AIMessage(content="结论：改用正文作答"),
        ]
    )

    out = _run(monkeypatch, chat=chat, tool=tool)

    assert chat.calls == 2
    assert tool.calls == []
    assert out["subagent_results"][0]["summary"] == "结论：改用正文作答"
    assert any(
        isinstance(m, SystemMessage) and "XML 风格工具调用标记" in str(m.content)
        for m in chat.seen
    )


def test_empty_body_retries_with_plain_model_without_burning_round(monkeypatch):
    settings = _Settings()
    settings.agent_subagent_max_rounds = 1
    chat = _SeqChat(
        [
            AIMessage(
                content="",
                usage_metadata={
                    "input_tokens": 10,
                    "output_tokens": 100,
                    "total_tokens": 110,
                },
            )
        ]
    )
    plain = _SeqChat([AIMessage(content="结论：重试成功")])

    out = _run(
        monkeypatch, chat=chat, chat_plain=plain, tool=_FakeTool("read_file", {}),
        settings=settings,
    )

    assert chat.calls == 1 and plain.calls == 1
    assert out["subagent_results"][0]["summary"] == "结论：重试成功"


def test_empty_body_retries_even_when_usage_is_missing(monkeypatch):
    """部分网关不回填 usage：无法判断是否截断，但仍给一次有界重试。"""
    settings = _Settings()
    settings.agent_subagent_max_rounds = 1
    chat = _SeqChat([AIMessage(content="")])  # usage_metadata 缺失
    plain = _SeqChat([AIMessage(content="结论：兜底成功")])

    out = _run(
        monkeypatch, chat=chat, chat_plain=plain, tool=_FakeTool("read_file", {}),
        settings=settings,
    )

    assert chat.calls == 1 and plain.calls == 1
    assert out["subagent_results"][0]["summary"] == "结论：兜底成功"


def test_tool_result_is_slimmed_in_history(monkeypatch):
    tool = _FakeTool(
        "read_file", {"summary": "ok", "content": "x" * 6000}
    )
    chat = _SeqChat(
        [
            AIMessage(content="", tool_calls=[_call("read_file")]),
            AIMessage(content="结论"),
        ]
    )

    _run(monkeypatch, chat=chat, tool=tool)

    tool_msg = next(m for m in chat.seen if isinstance(m, ToolMessage))
    assert "中间省略" in tool_msg.content
    assert len(tool_msg.content) < 6000


def test_skill_view_content_is_not_slimmed(monkeypatch):
    tool = _FakeTool(
        "skill_view", {"summary": "技能全文", "content": "y" * 6000}
    )
    chat = _SeqChat(
        [
            AIMessage(content="", tool_calls=[_call("skill_view")]),
            AIMessage(content="结论"),
        ]
    )

    _run(monkeypatch, chat=chat, tool=tool)

    tool_msg = next(m for m in chat.seen if isinstance(m, ToolMessage))
    assert "中间省略" not in tool_msg.content
    assert "y" * 6000 in tool_msg.content


def test_pre_tool_hook_denies_subagent_tool(monkeypatch):
    tool = _FakeTool("read_file", {"summary": "不该执行"})
    chat = _SeqChat(
        [
            AIMessage(content="", tool_calls=[_call("read_file")]),
            AIMessage(content="结论：操作被拦截"),
        ]
    )

    import app.hooks as hooks_module

    def fake_run_hooks_from(hooks, event, tool_name, payload):
        if event == "pre_tool_use":
            return [
                {
                    "name": "guard",
                    "decision": "deny",
                    "reason": "禁止读取该路径",
                    "additional_context": "",
                }
            ]
        return []

    monkeypatch.setattr(hooks_module, "run_hooks_from", fake_run_hooks_from)

    out = _run(monkeypatch, chat=chat, tool=tool)

    assert tool.calls == []
    tool_msg = next(m for m in chat.seen if isinstance(m, ToolMessage))
    assert "hook_denied" in tool_msg.content
    assert out["subagent_results"][0]["summary"] == "结论：操作被拦截"


def test_branch_failure_is_isolated_into_error_result(monkeypatch):
    chat = _SeqChat([RuntimeError("provider down")])

    out = _run(monkeypatch, chat=chat, tool=_FakeTool("read_file", {}))

    result = out["subagent_results"][0]
    assert result["error"].startswith("RuntimeError")
    assert "执行失败" in result["summary"]


def test_subagent_llm_call_budget_is_shared_and_enforced(monkeypatch):
    settings = _Settings()
    settings.agent_subagent_exec_max_rounds = 5
    settings.agent_subagent_max_llm_calls = 1
    tool = _FakeTool("read_file", {"summary": "ok"})
    chat = _SeqChat(
        [
            AIMessage(content="", tool_calls=[_call("read_file")]),
            AIMessage(content="", tool_calls=[_call("read_file", call_id="t2")]),
        ]
    )
    deps = SimpleNamespace(
        service=SimpleNamespace(
            settings=settings,
            chat=chat,
            chat_plain=_SeqChat([]),
            rag=_Rag(),
        ),
        bus=EventBus(Queue()),
        db=None,
        stop_event=None,
        subagent_llm_calls=0,
        subagent_lock=threading.Lock(),
    )

    out = _run(monkeypatch, chat=chat, tool=tool, mode="execute", deps=deps)

    assert chat.calls == 1
    assert deps.subagent_llm_calls == 1
    assert "预算已耗尽" in out["subagent_results"][0]["summary"]
