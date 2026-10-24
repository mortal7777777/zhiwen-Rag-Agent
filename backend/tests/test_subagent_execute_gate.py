"""子代理 execute 模式的敏感操作闸门：调用点不得因运行时对象类型错误而崩溃。

回归背景（2026-10-22 审查）：`_subagent_node` 调 `_subagent_sensitive_gate`
时传了 LangGraph 的 Runtime 对象，而函数按 state 里的 runtime 字典使用
`rt.get("conv_id")`——execute 子代理执行任何非白名单命令都会
AttributeError，整轮 run 直接失败。单测此前只直测了 gate 函数本身（传的是
真字典），没有覆盖调用点。
"""

from __future__ import annotations

from queue import Queue
from types import SimpleNamespace

from langchain_core.messages import AIMessage, ToolMessage

from app.agent.nodes import subagent as subagent_module
from app.agent.nodes.subagent import _subagent_node
from app.agent.state import EventBus


class _Settings:
    tool_permission_mode = "ask"
    command_allowlist = ""
    agent_subagent_exec_max_rounds = 2
    agent_subagent_max_rounds = 1


class _Rag:
    def acquire_llm(self):
        pass

    def release_llm(self):
        pass


class _FakeBash:
    name = "bash"
    description = "run command"

    def __init__(self):
        self.calls: list[dict] = []

    def invoke(self, args):
        self.calls.append(args)
        return {"summary": "不应被执行", "exit_code": 0}


class _Chat:
    def __init__(self):
        self.calls = 0
        self.seen_messages: list = []

    def bind_tools(self, tools):
        return self

    def invoke(self, messages, config=None):
        self.calls += 1
        self.seen_messages = list(messages)
        if self.calls == 1:
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "bash",
                        "args": {"command": "echo hi"},
                        "id": "t1",
                        "type": "tool_call",
                    }
                ],
            )
        return AIMessage(content="结论：命令需要人工确认，未执行")


def test_execute_subagent_bash_without_allowlist_does_not_crash(monkeypatch):
    bash = _FakeBash()
    monkeypatch.setattr(
        subagent_module, "_subagent_tools", lambda *args, **kwargs: [bash]
    )

    chat = _Chat()
    deps = SimpleNamespace(
        service=SimpleNamespace(settings=_Settings(), rag=_Rag(), chat=chat),
        bus=EventBus(Queue()),
        db=None,
        stop_event=None,
    )
    state = {
        "runtime": {"conv_id": 7},
        "question": "跑个命令",
        "sub_task": {"task": "跑命令", "mode": "execute", "call_id": "c1"},
        "project_dir": None,
        "conversation_id": 7,
    }

    out = _subagent_node(state, SimpleNamespace(context=deps))

    # 关键：不抛 AttributeError；命令没有真正执行，而是交回主代理
    assert bash.calls == []
    tool_rows = [m for m in chat.seen_messages if isinstance(m, ToolMessage)]
    assert tool_rows and "subagent_denied" in str(tool_rows[0].content)
    assert out["subagent_results"][0]["summary"].startswith("结论")
