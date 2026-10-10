"""循环收敛与预算耦合（P1-3）：预算、失败上限、图步数上限必须互相对得上。

历史坑：设置页允许把 `agent_task_max_iterations` 调到 500，而 `config.py` 的
`agent_recursion_limit` 固定 110（≈50 次迭代）——预算还没用完，LangGraph 先报
"Recursion limit"，表现为"任务做一半被图步数掐断"。这里锁三件事：
1. 预算/失败上限随任务态单调（task_mode ≥ normal）；
2. 循环模拟必须在预算内到达强制收尾（不会无限转）；
3. 图步数上限 ≥ 2×预算 + 余量（含把预算调到上限的情况）。
"""

from __future__ import annotations

from types import SimpleNamespace

from app import runtime_config
from app.agent.langgraph_agent import LangGraphAgentService
from app.agent.nodes.common import (
    _bump_patch,
    _failure_limit,
    _iteration_limit,
)
from app.config import Settings


class _FakeRag:
    def __init__(self, settings) -> None:
        self.settings = settings

    def acquire_gpu(self) -> None: ...

    def release_gpu(self) -> None: ...

    def retrieve(self, question, history=None):
        return []


def _settings() -> SimpleNamespace:
    return SimpleNamespace(
        agent_max_iterations=4,
        agent_task_max_iterations=6,
        agent_max_failures=2,
        agent_task_max_failures=3,
        agent_recursion_limit=110,
    )


def _state(*, task_mode: bool) -> dict:
    return {
        "task_mode": task_mode,
        "tool_calls_used": 0,
        "failure_count": 0,
        "forced_final": False,
    }


def test_limits_are_monotonic_in_task_mode(monkeypatch):
    monkeypatch.setattr(runtime_config, "_overrides", {})
    settings = _settings()
    normal, task = _state(task_mode=False), _state(task_mode=True)
    assert _iteration_limit(task, settings) >= _iteration_limit(normal, settings)
    assert _failure_limit(task, settings) >= _failure_limit(normal, settings)


def test_loop_reaches_forced_final_within_iteration_budget(monkeypatch):
    """模拟 tools 节点的规则：成功工具调用累计到上限 → 强制收尾（不会无限转）。"""
    monkeypatch.setattr(runtime_config, "_overrides", {})
    settings = _settings()
    for task_mode in (False, True):
        state = _state(task_mode=task_mode)
        limit = _iteration_limit(state, settings)
        rounds = 0
        while not state["forced_final"] and rounds < 1000:
            rounds += 1
            state["tool_calls_used"] += 1
            if (
                state["tool_calls_used"] >= _iteration_limit(state, settings)
                and not state["forced_final"]
            ):
                state["forced_final"] = True
        assert state["forced_final"], f"task_mode={task_mode} 未收敛"
        assert rounds == limit, "应在第 limit 轮强制收尾"


def test_loop_reaches_forced_final_within_failure_budget(monkeypatch):
    """失败不占迭代预算，但连续失败到上限必须收尾（避免无意义空转）。"""
    monkeypatch.setattr(runtime_config, "_overrides", {})
    settings = _settings()
    for task_mode in (False, True):
        state = _state(task_mode=task_mode)
        limit = _failure_limit(state, settings)
        rounds = 0
        while not state["forced_final"] and rounds < 1000:
            rounds += 1
            state["failure_count"] += 1
            if (
                state["failure_count"] >= _failure_limit(state, settings)
                and not state["forced_final"]
            ):
                state["forced_final"] = True
        assert state["forced_final"] and rounds == limit


def test_recursion_limit_covers_editable_budget(monkeypatch):
    """预算调到设置页允许的上限（500）时，图步数上限必须跟着抬起来。"""
    settings = Settings()
    service = LangGraphAgentService(settings, _FakeRag(settings))

    monkeypatch.setattr(runtime_config, "_overrides", {})
    assert service._recursion_limit() == 110, "默认配置下保持 110（不改变现有行为）"

    monkeypatch.setattr(
        runtime_config, "_overrides", {"agent_task_max_iterations": 500}
    )
    assert service._recursion_limit() >= 500 * 2 + 10

    # 服务级的线程配置也要用推导后的值（resume/continue 同源）
    assert service._thread_config(1)["recursion_limit"] == service._recursion_limit()


def test_patch_telemetry_aggregates():
    """补丁计数可聚合（写进 agent_runs.token_usage.patches，用于按数据淘汰补丁）。"""
    rt: dict = {}
    _bump_patch(rt, "empty_retry")
    _bump_patch(rt, "empty_retry")
    _bump_patch(rt, "verify_fail")
    assert rt["patches"] == {"empty_retry": 2, "verify_fail": 1}
    _bump_patch(None, "x")  # 不应抛错
