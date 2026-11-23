"""finalize 收尾顺序：轨迹压缩（一次 LLM 调用）必须排在 done 之后。

否则用户已经看到正文，却要等压缩跑完才收到"回答结束"；与 run.py 里
长期记忆提取一样，它属于收尾后的后台工作。
"""

from __future__ import annotations

import time
from types import SimpleNamespace

from app.agent.nodes import finalize as finalize_module
from app.agent.nodes.finalize import _compute_latency_ms, _finalize_node
from app.agent.service.run import RunMixin
from app.db import database as db_module
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
        rag=SimpleNamespace(acquire_llm=lambda: None, release_llm=lambda: None),
        checkpoint_saver=None,
    )

    monkeypatch.setattr(finalize_module, "repo", _Repo())
    monkeypatch.setattr(finalize_module, "write_trace", lambda *a, **k: None)
    monkeypatch.setattr(db_module, "db_ready", True)
    monkeypatch.setattr(
        db_module,
        "SessionLocal",
        lambda: SimpleNamespace(rollback=lambda: None, close=lambda: None),
    )

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

    # 压缩已改为 done 之后的后台线程：等它完成再断言顺序。
    deadline = time.time() + 2.0
    while "compress" not in order and time.time() < deadline:
        time.sleep(0.01)

    assert "done" in order
    assert order.count("compress") == 1
    assert order.index("done") < order.index("compress")


def test_latency_accumulates_segments_and_ignores_suspension():
    deps = SimpleNamespace(elapsed_ms=1200, started=time.perf_counter())
    # rt["started"] 若是另一进程/挂起前的 perf_counter，不能参与计算
    assert 1200 <= _compute_latency_ms(deps, {"started": -1e9}) < 1500
    # 本段起点缺失时只返回已累计值，不产生负数/异常巨值
    assert _compute_latency_ms(SimpleNamespace(elapsed_ms=500, started=0.0), {}) == 500


def test_suspended_elapsed_accumulates_without_approval_wait():
    service = SimpleNamespace(
        _suspended_usage={},
        _suspended_elapsed={},
        settings=SimpleNamespace(permission_timeout=0),
    )
    deps = SimpleNamespace(elapsed_ms=1000, started=time.perf_counter() - 0.05)
    bus = _OrderBus([])

    RunMixin._emit_suspended(
        service, bus, {"__interrupt__": []}, 7, deps
    )

    assert 1040 <= service._suspended_elapsed[7] < 1200
