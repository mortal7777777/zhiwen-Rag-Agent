"""TodoWrite 任务清单核心逻辑单测（不依赖 MySQL，monkeypatch repo）。"""

from __future__ import annotations

import app.todos as todos
from app.db import repository as repo


class FakeStore:
    def __init__(self):
        self._data: dict[str, str] = {}

    def get_meta(self, _db, key: str):
        return self._data.get(key)

    def set_meta(self, _db, key: str, value: str):
        self._data[key] = value


def _install_store(monkeypatch, store: FakeStore) -> FakeStore:
    monkeypatch.setattr(repo, "get_meta", store.get_meta)
    monkeypatch.setattr(repo, "set_meta", store.set_meta)
    return store


def test_seed_and_progress(monkeypatch):
    store = _install_store(monkeypatch, FakeStore())
    conv = 1001
    plan = ["step A", "step B", "step C"]
    items = todos.seed_todos_from_plan(object(), conv, plan)
    assert len(items) == 3
    assert all(i["source"] == "plan" for i in items)
    assert [i["step_index"] for i in items] == [0, 1, 2]
    assert todos.plan_progress(items)[:2] == (0, 3)
    # 已有清单不重复播种
    assert todos.seed_todos_from_plan(object(), conv, plan) == items
    assert store._data[f"todos_{conv}"]


def test_sync_marks_hinted_steps_only(monkeypatch):
    store = _install_store(monkeypatch, FakeStore())
    conv = 1002
    plan = ["step A", "step B", "step C"]
    todos.seed_todos_from_plan(object(), conv, plan)
    items = todos.sync_todos_from_plan(
        object(), conv, plan, 3, hints=["kb", "", "bash"]
    )
    assert [i["done"] for i in items] == [True, False, True]


def test_sync_legacy_without_source(monkeypatch):
    store = _install_store(monkeypatch, FakeStore())
    conv = 1003
    plan = ["step A", "step B"]
    todos.seed_todos_from_plan(object(), conv, plan)
    items = todos.load_todos(object(), conv)
    for i in items:
        i.pop("source", None)
    todos.save_todos(object(), conv, items)
    synced = todos.sync_todos_from_plan(object(), conv, plan, 1)
    assert [i["done"] for i in synced] == [True, False]


def test_tool_operations(monkeypatch):
    store = _install_store(monkeypatch, FakeStore())
    monkeypatch.setattr("app.db.database.db_ready", True)
    monkeypatch.setattr("app.db.database.SessionLocal", lambda: object())
    conv = 1004
    plan = ["step A", "step B", "step C"]
    todos.seed_todos_from_plan(object(), conv, plan)
    tool = todos.make_todo_tool(object(), conv)

    r = tool.invoke({"operation": "complete", "text": "step B"})
    assert r["todos"][1]["done"] is True

    r = tool.invoke({"operation": "add", "text": "extra"})
    assert len(r["todos"]) == 4
    assert r["todos"][3]["source"] == "manual"

    r = tool.invoke(
        {
            "operation": "set",
            "tasks": [{"text": "new 1"}, {"text": "new 2"}, "new 3"],
        }
    )
    assert len(r["todos"]) == 3
    # 全 manual 清单不计入计划进度，也不会被 sync 误标
    assert todos.plan_progress(todos.load_todos(object(), conv)) == (0, 0, [])
    synced = todos.sync_todos_from_plan(object(), conv, plan, 2)
    assert not any(i["done"] for i in synced)

    r = tool.invoke({"operation": "remove", "text": "new 2"})
    assert len(r["todos"]) == 2
    r = tool.invoke({"operation": "list"})
    assert r["todos"] == todos.load_todos(object(), conv)


def test_complete_fuzzy_match(monkeypatch):
    _install_store(monkeypatch, FakeStore())
    monkeypatch.setattr("app.db.database.db_ready", True)
    monkeypatch.setattr("app.db.database.SessionLocal", lambda: object())
    conv = 1009
    todos.seed_todos_from_plan(object(), conv, ["检索知识库获取相关资料", "整理结论"])
    tool = todos.make_todo_tool(object(), conv)
    r = tool.invoke({"operation": "complete", "text": "检索知识库获取相关资料"})
    assert r["todos"][0]["done"] is True
    # 包含关系也能命中（模型常带上下文重述步骤）
    r = tool.invoke({"operation": "complete", "text": "整理结论"})
    assert r["todos"][1]["done"] is True


def test_complete_steps_by_text(monkeypatch):
    _install_store(monkeypatch, FakeStore())
    conv = 1005
    plan = ["step A", "step B", "step C"]
    todos.seed_todos_from_plan(object(), conv, plan)
    todos.sync_todos_from_plan(object(), conv, plan, 2)
    items = todos.complete_steps_by_text(object(), conv, ["step C"])
    assert all(i["done"] for i in items)
    assert todos.todos_to_text(items).count("[x]") == 3


def test_refresh_replaces_plan_for_new_task(monkeypatch):
    store = _install_store(monkeypatch, FakeStore())
    monkeypatch.setattr("app.db.database.db_ready", True)
    monkeypatch.setattr("app.db.database.SessionLocal", lambda: object())
    conv = 1006
    todos.seed_todos_from_plan(object(), conv, ["old A", "old B"])
    items = todos.refresh_todos_for_plan(object(), conv, ["new A", "new B", "new C"])
    assert [i["text"] for i in items[:3]] == ["new A", "new B", "new C"]
    assert all(not i["done"] for i in items[:3])
    # manual 项跨新计划保留
    todos.make_todo_tool(object(), conv).invoke({"operation": "add", "text": "manual keep"})
    items = todos.refresh_todos_for_plan(object(), conv, ["another A"])
    assert any(i["text"] == "manual keep" for i in items)


def test_refresh_keeps_same_plan_progress(monkeypatch):
    store = _install_store(monkeypatch, FakeStore())
    conv = 1007
    plan = ["step A", "step B"]
    todos.seed_todos_from_plan(object(), conv, plan)
    todos.sync_todos_from_plan(object(), conv, plan, 1)
    items = todos.refresh_todos_for_plan(object(), conv, plan)
    assert [i["done"] for i in items] == [True, False]
    assert store._data[f"todos_{conv}"]


def test_refresh_clears_completed_when_no_new_plan(monkeypatch):
    store = _install_store(monkeypatch, FakeStore())
    conv = 1008
    todos.seed_todos_from_plan(object(), conv, ["step A"])
    todos.sync_todos_from_plan(object(), conv, ["step A"], 1)
    assert todos.refresh_todos_for_plan(object(), conv, []) == []
    assert todos.load_todos(object(), conv) == []
    # 未完成时保留（跨轮续做）
    todos.seed_todos_from_plan(object(), conv, ["step A", "step B"])
    items = todos.refresh_todos_for_plan(object(), conv, [])
    assert len(items) == 2
