"""D 块版本账本：状态回退（A→B→A）不再注入陈旧版本。

背景（P0）：D 块的旧判据只问"发送窗口里有没有逐字相同的行"。状态回退时
（例如长期记忆摘要 北京 → 上海 → 北京，或项目记忆被改回来），窗口里仍有
旧的 A 副本 → 跳过注入 → 链上最后一条仍是 B，模型会一直带着过期状态回答，
直到该行被压缩出窗口才自愈。

新判据是两条同时成立才跳过：① 该键最后一次注入的版本 == 当前版本；
② 该版本仍在发送窗口里（压缩 / 时间线回滚会把它踢出去）。
"""

from __future__ import annotations

from types import SimpleNamespace

from langchain_core.messages import SystemMessage

from app.agent.nodes import prepare as prepare_module
from app.agent.nodes.prepare import _prepare_node
from app.agent.state import AgentState
from app.agent.utils import (
    _dblock_needs_injection,
    _dblock_version,
    _window_has_same,
)
from _node_rt import runtime_from_state  # noqa: E402

BLOCK_A = (
    "关于用户（长期记忆摘要，通常应作为默认背景）：\n用户住北京。"
)
BLOCK_B = (
    "关于用户（长期记忆摘要，通常应作为默认背景）：\n用户住上海。"
)


def _row(role: str, content: str) -> dict:
    return {"role": role, "content": content}


# ---------------- 纯函数：版本与双判据 ----------------


def test_version_is_content_addressed():
    assert _dblock_version(BLOCK_A) == _dblock_version(BLOCK_A)
    assert _dblock_version(BLOCK_A) != _dblock_version(BLOCK_B)
    assert len(_dblock_version(BLOCK_A)) == 12
    assert _dblock_version("") == _dblock_version("")


def test_skip_when_same_version_and_present():
    """当前版本就是最后注入的版本，且还在窗口里 → 维持纯追加，不重复注入。"""
    rows = [_row("system", BLOCK_A)]
    ledger = {"memory_profile": _dblock_version(BLOCK_A)}
    assert _dblock_needs_injection("memory_profile", BLOCK_A, ledger, rows) is False


def test_reinject_when_version_reverted():
    """核心回归：A→B→A。窗口里有旧的 A，但该键最后注入的是 B → 必须重新注入。"""
    rows = [_row("system", BLOCK_A)]          # 旧的 A 副本仍在窗口内
    ledger = {"memory_profile": _dblock_version(BLOCK_B)}   # 期间注入过 B
    assert _dblock_needs_injection("memory_profile", BLOCK_A, ledger, rows) is True
    # 对照：只看"窗口内有没有同内容"的旧判据会跳过（这就是缺陷本身）
    assert _window_has_same(rows, BLOCK_A) is True


def test_reinject_after_compaction():
    """账本说"最后注入的就是当前版本"，但它已被压缩出窗口 → 必须补注入。"""
    ledger = {"memory_profile": _dblock_version(BLOCK_A)}
    assert _dblock_needs_injection("memory_profile", BLOCK_A, ledger, []) is True


def test_missing_ledger_falls_back_to_injection():
    """账本丢失（内存版 checkpointer 重启 / 删线程）：宁可多注入，不可漏。"""
    rows = [_row("system", BLOCK_A)]
    assert _dblock_needs_injection("memory_profile", BLOCK_A, None, rows) is True
    assert _dblock_needs_injection("memory_profile", BLOCK_A, {}, rows) is True


def test_empty_content_never_injected():
    assert _dblock_needs_injection("todos", "", {}, []) is False
    assert _dblock_needs_injection("todos", "", {"todos": "x"}, []) is False


def test_same_text_in_user_role_does_not_count():
    """用户恰好复述了同样文本，不构成"已注入"。"""
    rows = [_row("user", BLOCK_A)]
    ledger = {"memory_profile": _dblock_version(BLOCK_A)}
    assert _dblock_needs_injection("memory_profile", BLOCK_A, ledger, rows) is True


def test_state_declares_ledger_field():
    """契约：账本必须是 AgentState 的字段（随检查点持久化的前提）。"""
    assert "dblock_versions" in AgentState.__annotations__


# ---------------- 节点级：接线与账本回归 ----------------


class _FakeBus:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def emit(self, event: str, data) -> None:
        self.events.append((event, data))


class _FakeSettings:
    task_mode_detect = True
    advanced_tools_enabled = True
    skills_enabled = False
    project_memory_file = ""
    vision_auto_describe = False
    main_model_vision = False
    vision_max_images = 6


class _FakeContext:
    """上下文服务替身：只提供 prepare 用到的四个方法。"""

    def __init__(self, window_rows: list[dict], memory_summary: str | None = None):
        self._window_rows = window_rows
        self._memory_summary = memory_summary

    def compact_conversation(self, db, conversation_id):
        return None, list(self._window_rows)

    def history_budget_for(self, db, conversation_id) -> int:
        return 64_000

    def retrieve_memories(self, db, embeddings, question) -> list[str]:
        return []

    def get_memory_summary(self, db) -> str | None:
        return self._memory_summary


class _FakeRepo:
    """repository 替身：prepare 在 db 非空时只读这几件事。"""

    def __init__(self, window_rows: list[dict]):
        self._window_rows = window_rows

    def get_conversation(self, db, conversation_id):
        return SimpleNamespace(id=1, template_id=None, title="测试会话")

    def list_agent_runs(self, db, limit=1, conversation_id=None):
        return []

    def get_meta(self, db, key):
        return None


class _FakeRag:
    embeddings = None

    def acquire_gpu(self):
        pass

    def release_gpu(self):
        pass


class _FakeService:
    def __init__(self, context, settings):
        self.settings = settings
        self.rag = _FakeRag()
        self.context = context

    def _get_template_content(self, db, template_id, system_prompt):
        return ""

    def build_tool_list(self, db, **kwargs):
        return []


def _state(**overrides) -> dict:
    state = {
        "runtime": {},
        "question": "测试问题",
        "images": [],
        "use_web_search": False,
        "use_knowledge_base": False,
        "conversation_id": 1,
        "template_id": None,
        "system_prompt": None,
        "tool_mode": "auto",
        "plan_only": False,
        "project_dir": None,
        "messages": [],
        "pending_tool_calls": [],
        "pending_subtasks": [],
        "sources": [],
        "tool_trace": [],
        "counter": [0],
        "tool_calls_used": 0,
        "failure_count": 0,
        "forced_final": False,
        "last_call_warned": False,
        "plan_steps": [],
        "plan_map": [],
        "kb_documents": [],
        "memory_hits": [],
        "memory_summary": None,
        "vision_descriptions": [],
        "summary_text": None,
        "todos": [],
        "plan_done_count": 0,
        "force_continue": False,
        "plan_push_count": 0,
        "xml_retry_count": 0,
        "empty_retry_count": 0,
        "dispatch_rounds": 0,
        "task_mode": False,
        "subagent_results": [],
        "sub_task": {},
        "persist_start": 0,
        "dblock_versions": {},
        # 非 None 才会走"读历史窗口"的分支；真实的 repo 调用已被替身接掉，
        # 少数内部调用（todos/trajectory）落到这个空对象上，异常被上层吞掉。
        "db": SimpleNamespace(),
    }
    state.update(overrides)
    state["service"] = state.pop("_service", None)
    state["bus"] = state.pop("_bus", None)
    return state


def _run_prepare(
    monkeypatch, tmp_path, window_rows, memory_summary, ledger, **overrides
):
    """跑一次真实的 _prepare_node（repo / 项目记忆用替身，不碰真库与真文件）。"""
    monkeypatch.setattr(prepare_module, "repo", _FakeRepo(window_rows))
    monkeypatch.setattr(
        "app.project_memory.load_project_memory", lambda settings, path=None: None
    )

    settings = _FakeSettings()
    context = _FakeContext(window_rows, memory_summary)
    bus = _FakeBus()
    state = _state(
        _service=_FakeService(context, settings),
        _bus=bus,
        project_dir=str(tmp_path),
        dblock_versions=dict(ledger),
        **overrides,
    )
    result = _prepare_node(state, runtime_from_state(state))
    return state, result


def test_prepare_records_stage_timings(monkeypatch, tmp_path):
    """prepare 的分段耗时随运行记录落库（先量化再决定拆并发）。"""
    state, _ = _run_prepare(
        monkeypatch, tmp_path, window_rows=[], memory_summary=None, ledger={}
    )
    timings = state["runtime"]["timings"]
    assert {"compact_ms", "memory_ms", "tools_ms"} <= set(timings)
    assert all(isinstance(v, int) and v >= 0 for v in timings.values())


def test_prepare_resets_per_turn_retry_counters(monkeypatch, tmp_path):
    """空正文重试与写后复验计数必须每轮从 0 开始。

    检查点按 conv:<id> 携带 state；只靠"新会话初值"会让计数在整个会话里
    单调累积——空正文兜底用一次就失效（已修），复验机会同样会被永久耗掉。
    """
    _, result = _run_prepare(
        monkeypatch,
        tmp_path,
        window_rows=[],
        memory_summary=None,
        ledger={},
        empty_retry_count=2,
        empty_retry_pending=True,
        verify_fail_count=3,
    )
    assert result["empty_retry_count"] == 0
    assert result["empty_retry_pending"] is False
    assert result["verify_fail_count"] == 0


def test_prepare_returns_ledger_and_renders_time_block(monkeypatch, tmp_path):
    """接线测试：账本必须随返回值出节点（节点内直接改 state 不生效）。"""
    state, result = _run_prepare(
        monkeypatch, tmp_path, window_rows=[], memory_summary=None, ledger={}
    )
    ledger = result["dblock_versions"]
    assert isinstance(ledger, dict) and "time" in ledger
    time_msgs = [
        m
        for m in result["messages"]
        if isinstance(m, SystemMessage) and str(m.content).startswith("当前时间：")
    ]
    assert len(time_msgs) == 1
    assert ledger["time"] == _dblock_version(str(time_msgs[0].content))
    # state 里的旧账本不应被原地改写（改动只走返回值）
    assert state["dblock_versions"] == {}


def test_prepare_reinjects_reverted_memory_block(monkeypatch, tmp_path):
    """端到端回归：窗口里是旧的 A、账本记录最近注入 B → 本轮必须重新注入 A。"""
    window_rows = [_row("system", BLOCK_A)]
    state, result = _run_prepare(
        monkeypatch,
        tmp_path,
        window_rows=window_rows,
        memory_summary="用户住北京。",
        ledger={"memory_profile": _dblock_version(BLOCK_B)},
    )
    contents = [str(m.content) for m in result["messages"]]
    # 一份来自历史窗口回放，一份是本轮重新注入的 D 块——两份缺一不可
    assert contents.count(BLOCK_A) == 2, "状态回退后必须重新注入当前版本"
    assert result["dblock_versions"]["memory_profile"] == _dblock_version(BLOCK_A)


def test_prepare_skips_when_version_matches_and_present(monkeypatch, tmp_path):
    """同一版本且仍在窗口内 → 不重复注入，维持纯追加链。"""
    window_rows = [_row("system", BLOCK_A)]
    _state_out, result = _run_prepare(
        monkeypatch,
        tmp_path,
        window_rows=window_rows,
        memory_summary="用户住北京。",
        ledger={"memory_profile": _dblock_version(BLOCK_A)},
    )
    contents = [str(m.content) for m in result["messages"]]
    # 只有历史窗口回放的那一份，本轮不再追加
    assert contents.count(BLOCK_A) == 1
