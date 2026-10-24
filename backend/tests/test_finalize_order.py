"""finalize 收尾顺序：轨迹压缩（一次 LLM 调用）必须排在 done 之后。

否则用户已经看到正文，却要等压缩跑完才收到"回答结束"；与 run.py 里
长期记忆提取一样，它属于收尾后的后台工作。
"""

from __future__ import annotations

import time
from types import SimpleNamespace

from app.agent.nodes import finalize as finalize_module
from app.agent.nodes.finalize import _finalize_node
from _node_rt import runtime_from_state


class _OrderBus:
    def __init__(self, order: list[str]):
        self.order = order

    def emit(self, event: str, data) -> None:
        self.order.append(event)


class _Repo:
    def add_message(self, *args, **kwargs):
        pass

    def create_agent_run(self, *args, **kwargs):
        return SimpleNamespace(id=1)


def test_trajectory_compression_runs_after_done(monkeypatch):
    order: list[str] = []
    service = SimpleNamespace(
        settings=SimpleNamespace(
            trajectory_compress_enabled=True,
            checkpoint_keep_per_thread=20,
        ),
        context=SimpleNamespace(_invoke=lambda *a, **k: None),
        checkpoint_saver=None,
    )

    monkeypatch.setattr(finalize_module, "repo", _Repo())
    monkeypatch.setattr(finalize_module, "write_trace", lambda *a, **k: None)

    def fake_compress(*args, **kwargs):
        order.append("compress")

    monkeypatch.setattr("app.trajectory.compress_trajectory", fake_compress)

    state = {
        "service": service,
        "bus": _OrderBus(order),
        "db": SimpleNamespace(rollback=lambda: None),
        "runtime": {
            "started": time.perf_counter(),
            "final_text": "回答正文",
            "status": "ok",
            "conv_id": 1,
        },
        "sources": [],
        "tool_trace": [{"name": "a"}, {"name": "b"}, {"name": "c"}],
        "question": "测试",
        "messages": [],
        "plan_steps": [],
        "todos": [],
        "plan_done_count": 0,
        "memory_hits": [],
        "subagent_round_count": 0,
        "persist_start": 0,
    }

    _finalize_node(state, runtime_from_state(state))

    assert "done" in order
    assert order.count("compress") == 1
    assert order.index("done") < order.index("compress")
    assert order[-1] == "compress"
