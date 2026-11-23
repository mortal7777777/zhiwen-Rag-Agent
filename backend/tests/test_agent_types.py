"""子代理类型注册表（Phase A 6.1）：默认种子 / profile 解析 / 收窄不扩权 / API。"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.agent import agent_types
from app.agent.agent_types import (
    ALL_SUBAGENT_TOOLS,
    DEFAULT_AGENT_TYPES,
    EXECUTE_TOOLS,
    MODE_EXECUTE,
    MODE_RESEARCH,
    MODE_REVIEW,
    READ_FS_TOOLS,
    list_agent_type_specs,
    normalize_mode,
    refresh_registry,
    reset_registry,
    resolve_agent_profile,
)
from app.agent.nodes.subagent import _parse_declared_subtasks
from app.api.agent_types import router as agent_types_router
from app.db import get_db
from app.db import repository as repo
from app.db.database import Base
from app.db.models import AgentType


@pytest.fixture(autouse=True)
def _clean_registry():
    reset_registry()
    yield
    reset_registry()


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine, tables=[AgentType.__table__])
    Session = sessionmaker(bind=engine)
    with Session() as session:
        repo.seed_agent_types(session)
        yield session


@pytest.fixture()
def api(db):
    # session 已在 fixture 中复用；TestClient 在另一线程执行，需放开 SQLite 线程检查
    app = FastAPI()
    app.include_router(agent_types_router, prefix="/api")
    app.dependency_overrides[get_db] = lambda: db
    refresh_registry(db)
    return TestClient(app)


def test_defaults_resolve_all_modes():
    assert len(list_agent_type_specs()) == len(DEFAULT_AGENT_TYPES)
    for item in DEFAULT_AGENT_TYPES:
        spec = resolve_agent_profile(item["key"], None)
        assert spec["mode"] == item["mode"]
        assert spec["tools"]
        assert set(spec["tools"]) <= set(ALL_SUBAGENT_TOOLS)
    execute = resolve_agent_profile("execute", None)
    assert execute["mode"] == MODE_EXECUTE
    assert "bash" in execute["tools"]
    assert execute["read_only"] is False


def test_unknown_profile_falls_back_to_mode():
    spec = resolve_agent_profile("no-such-type", "review")
    assert spec["key"] == MODE_REVIEW
    assert spec["mode"] == MODE_REVIEW
    assert spec["profile"] == ""
    assert "write_file" not in spec["tools"]


def test_custom_profile_narrows_tools_and_forces_readonly(db):
    repo.create_agent_type(
        db,
        {
            "key": "frontend-review",
            "name": "前端审查",
            "mode": "review",
            "system_prompt": "只检查 Vue 组件。",
            "tools": ["read_file", "grep_search", "bash"],
        },
    )
    refresh_registry(db)
    spec = resolve_agent_profile("frontend-review", "execute")
    assert spec["mode"] == MODE_REVIEW
    assert spec["profile"] == "frontend-review"
    assert spec["read_only"] is True
    # review 模式不允许 bash：注册表归一化时已被丢弃
    assert "bash" not in spec["tools"]
    assert spec["system_prompt"].endswith("只检查 Vue 组件。")
    assert "不可信数据隔离" in spec["system_prompt"]


def test_execute_type_marked_readonly_degrades_to_research(db):
    row = repo.get_agent_type_row(db, MODE_EXECUTE)
    assert row is not None
    repo.update_agent_type(db, row, {"read_only": True})
    refresh_registry(db)
    spec = resolve_agent_profile("execute", None)
    assert spec["mode"] == MODE_RESEARCH
    assert spec["read_only"] is True
    assert "write_file" not in spec["tools"]


def test_seed_idempotent_update_bumps_version_and_reset(db):
    repo.seed_agent_types(db)
    repo.seed_agent_types(db)
    rows = repo.list_agent_types(db)
    assert len(rows) == len(DEFAULT_AGENT_TYPES)
    assert {r["key"] for r in rows} == {i["key"] for i in DEFAULT_AGENT_TYPES}
    execute_row = repo.get_agent_type_row(db, MODE_EXECUTE)
    assert execute_row is not None and execute_row.read_only is False

    row = repo.get_agent_type_row(db, MODE_RESEARCH)
    assert row.version == 1
    row = repo.update_agent_type(db, row, {"name": "自定义检索"})
    assert row.version == 2
    # 无实际变化不增版本
    row = repo.update_agent_type(db, row, {"name": "自定义检索"})
    assert row.version == 2
    row = repo.reset_agent_type(db, row)
    assert row.name == "检索"
    assert row.version == 3


def test_api_crud_validation_and_reset(api):
    rows = api.get("/api/agent-types").json()
    assert {r["key"] for r in rows} == {i["key"] for i in DEFAULT_AGENT_TYPES}
    assert all(r["is_builtin"] for r in rows)

    meta = api.get("/api/agent-types/meta").json()
    assert set(meta["modes"]) == {"research", "explore", "review", "execute"}
    assert "bash" in meta["modes"]["execute"]
    assert "bash" not in meta["modes"]["review"]
    assert meta["thinking"] == ["inherit", "on", "off"]
    # 图形化编辑器：工具/模式的显示标签（值仍是稳定 key）
    assert meta["tool_labels"]["read_file"] == "读取文件"
    assert meta["tool_labels"]["bash"] == "执行命令"
    assert meta["mode_labels"]["execute"] == "执行"

    created = api.post(
        "/api/agent-types",
        json={
            "key": "frontend-review",
            "name": "前端审查",
            "mode": "review",
            "tools": ["read_file"],
        },
    )
    assert created.status_code == 200, created.text
    assert created.json()["is_builtin"] is False

    patched = api.patch(
        "/api/agent-types/frontend-review",
        json={"max_rounds": 5, "model_provider_id": "p1", "model": "m1"},
    )
    assert patched.status_code == 200
    body = patched.json()
    assert body["max_rounds"] == 5
    assert body["model_provider_id"] == "p1"
    # 写入后注册表立即可解析
    spec = resolve_agent_profile("frontend-review", "research")
    assert spec["mode"] == "review"
    assert spec["max_rounds"] == 5

    assert api.post("/api/agent-types/frontend-review/reset").status_code == 400
    assert api.patch("/api/agent-types/nope", json={"name": "x"}).status_code == 404
    assert api.patch(
        "/api/agent-types/review", json={"mode": "write"}
    ).status_code == 400
    # review 模式不允许 bash（扩权）
    assert api.patch(
        "/api/agent-types/review", json={"tools": ["bash"]}
    ).status_code == 400

    # 自定义类型可删除；内置类型不可删除
    assert api.delete("/api/agent-types/review").status_code == 400
    assert api.delete("/api/agent-types/frontend-review").status_code == 200
    assert api.delete("/api/agent-types/frontend-review").status_code == 404
    assert "frontend-review" not in {
        r["key"] for r in api.get("/api/agent-types").json()
    }

    reset = api.post("/api/agent-types/explore/reset")
    assert reset.status_code == 200
    assert reset.json()["name"] == "侦察"

    # 只改 mode 时工具同步收窄（execute 的 bash 不会带进 review）
    assert api.patch(
        "/api/agent-types/execute", json={"tools": ["bash", "read_file"]}
    ).status_code == 200
    changed = api.patch("/api/agent-types/execute", json={"mode": "review"})
    assert changed.status_code == 200
    assert changed.json()["tools"] == ["read_file"]
    assert changed.json()["read_only"] is True


def test_dispatch_parses_profile_from_items_and_args():
    subtasks, _ = _parse_declared_subtasks(
        [
            {
                "id": "c1",
                "args": {
                    "task": "审查前端",
                    "mode": "review",
                    "profile": "frontend-review",
                },
            },
            {
                "id": "c2",
                "args": {
                    "profile": "explore-fast",
                    "subtasks": [
                        {"task": "扫描目录", "mode": "scan"},
                        {"task": "读配置", "mode": "research", "profile": "other"},
                    ],
                },
            },
        ]
    )
    assert [s["profile"] for s in subtasks] == [
        "frontend-review",
        "explore-fast",
        "other",
    ]
    assert [s["mode"] for s in subtasks] == ["review", "explore", "research"]
    assert normalize_mode("AUDIT") == MODE_REVIEW
