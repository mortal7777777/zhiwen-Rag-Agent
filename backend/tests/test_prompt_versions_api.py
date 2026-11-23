"""提示词版本 / 片段 / 会话锁定 API（真实 SQLite + TestClient，不依赖 MySQL）。"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.conversations import router as conversations_router
from app.api.deps import get_agent_service
from app.api.prompt_admin import router as prompt_admin_router
from app.db import get_db
from app.db import repository as repo
from app.db.database import Base
from app.db.models import (
    Conversation,
    Message,
    PromptFragment,
    PromptFragmentVersion,
    PromptTemplate,
    PromptTemplateVersion,
    PromptTrial,
)


class _FakeChat:
    def invoke(self, messages):
        return type(
            "FakeResponse",
            (),
            {
                "content": "模拟回答",
                "usage_metadata": {"input_tokens": 10, "output_tokens": 4},
            },
        )()


class _FakeAgent:
    def __init__(self) -> None:
        self.pending = False
        self.interrupted = False
        self.chat = _FakeChat()

    def pending_state(self, conversation_id):
        return {
            "pending": self.pending,
            "interrupted": self.interrupted,
            "ops": [],
            "plan": None,
            "_payload": None,
        }


@pytest.fixture()
def api():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(
        engine,
        tables=[
            Conversation.__table__,
            Message.__table__,
            PromptTemplate.__table__,
            PromptTemplateVersion.__table__,
            PromptFragment.__table__,
            PromptFragmentVersion.__table__,
            PromptTrial.__table__,
        ],
    )
    Session = sessionmaker(bind=engine)
    with Session() as seed:
        repo.seed_templates(seed)
        repo.seed_prompt_fragments(seed)

    def _get_db():
        db = Session()
        try:
            yield db
        finally:
            db.close()

    agent = _FakeAgent()
    app = FastAPI()
    app.include_router(prompt_admin_router, prefix="/api")
    app.include_router(conversations_router, prefix="/api")
    app.dependency_overrides[get_db] = _get_db
    app.dependency_overrides[get_agent_service] = lambda: agent
    with TestClient(app) as client:
        yield client, Session, agent


def test_template_versions_and_restore(api):
    from app.agent.prompts import PRESET_TEMPLATES

    client, Session, _ = api
    listed = client.get("/api/templates").json()
    assert len(listed) == len(PRESET_TEMPLATES) == 12
    assert all(t["latest_version_no"] == 1 for t in listed)

    created = client.post(
        "/api/templates",
        json={"name": "测试模板", "content": "v1", "category": "coding"},
    ).json()
    tid = created["id"]
    assert created["latest_version_no"] == 1

    updated = client.patch(
        f"/api/templates/{tid}",
        json={"content": "v2", "version_note": "第二版"},
    ).json()
    assert updated["latest_version_no"] == 2

    versions = client.get(f"/api/templates/{tid}/versions").json()["versions"]
    assert [v["version_no"] for v in versions] == [2, 1]
    version_meta = client.get(
        f"/api/templates/{tid}/versions?include_content=false"
    ).json()["versions"]
    assert all(v["content"] == "" and v["content_omitted"] for v in version_meta)
    v1 = next(v for v in versions if v["version_no"] == 1)

    restored = client.post(
        f"/api/templates/{tid}/restore", json={"version_id": v1["id"]}
    ).json()
    assert restored["ok"] is True
    assert restored["template"]["content"] == "v1"
    assert restored["template"]["latest_version_no"] == 3

    system_id = next(t["id"] for t in listed if t["is_system"])
    assert (
        client.patch(f"/api/templates/{system_id}", json={"content": "x"}).status_code
        == 400
    )


def test_fragment_api_validation_versions_and_reset(api):
    client, _, _ = api
    fragments = client.get("/api/prompts/fragments").json()["fragments"]
    assert len(fragments) == 7

    updated = client.patch(
        "/api/prompts/fragments/tool.knowledge",
        json={"content": "自定义知识库规则", "note": "测试"},
    )
    assert updated.status_code == 200
    assert updated.json()["fragment"]["is_customized"] is True
    assert updated.json()["fragment"]["version_no"] == 2

    bad = client.patch(
        "/api/prompts/fragments/skills.usage",
        json={"content": "缺少占位符"},
    )
    assert bad.status_code == 400

    versions = client.get(
        "/api/prompts/fragments/tool.knowledge/versions"
    ).json()["versions"]
    assert [v["version_no"] for v in versions] == [2, 1]

    reset = client.post("/api/prompts/fragments/tool.knowledge/reset")
    assert reset.status_code == 200
    assert reset.json()["fragment"]["is_customized"] is False
    assert reset.json()["fragment"]["version_no"] == 3


def test_clone_template_and_default_view(api):
    client, _, _ = api
    listed = client.get("/api/templates").json()
    source = next(item for item in listed if item["is_system"])

    cloned = client.post(f"/api/templates/{source['id']}/clone")
    assert cloned.status_code == 200
    data = cloned.json()
    assert data["is_system"] is False
    assert data["name"] == f"{source['name']} 副本"
    assert data["content"] == source["content"]

    default = client.get("/api/templates/default")
    assert default.status_code == 200
    assert default.json()["is_system"] is True
    assert default.json()["content"]


def test_conversation_prompt_status_apply_latest_and_conflict(api):
    client, Session, agent = api
    with Session() as db:
        conv = repo.create_conversation(db, title="t")
        conv_id = conv.id

    status = client.get(f"/api/conversations/{conv_id}/prompt").json()
    assert status["locked"] is None
    assert status["upgradable"] is True

    applied = client.post(f"/api/conversations/{conv_id}/prompt/apply-latest")
    assert applied.status_code == 200
    assert applied.json()["first_lock"] is True
    assert applied.json()["cache_break"] is False

    status = client.get(f"/api/conversations/{conv_id}/prompt").json()
    assert status["locked"] is not None
    assert status["upgradable"] is False

    client.patch(
        "/api/prompts/fragments/tool.knowledge",
        json={"content": "新规则"},
    )
    status = client.get(f"/api/conversations/{conv_id}/prompt").json()
    assert status["upgradable"] is True
    assert "tool.knowledge" in status["outdated_units"]
    detail = next(
        item
        for item in status["outdated_details"]
        if item["key"] == "tool.knowledge"
    )
    assert detail["title"] == "知识库工具规则"
    assert detail["locked_version_no"] == 1
    assert detail["latest_version_no"] == 2

    upgraded = client.post(f"/api/conversations/{conv_id}/prompt/apply-latest")
    assert upgraded.status_code == 200
    assert upgraded.json()["cache_break"] is True

    agent.pending = True
    conflict = client.post(f"/api/conversations/{conv_id}/prompt/apply-latest")
    assert conflict.status_code == 409


def test_fragment_lint_and_preview_are_read_only(api):
    client, Session, _ = api
    lint = client.post(
        "/api/prompts/fragments/plan.mode/lint",
        json={"content": "只写文字计划"},
    ).json()
    assert lint["ok"] is True
    assert any("exit_plan_mode" in item for item in lint["warnings"])

    bad = client.post(
        "/api/prompts/fragments/skills.usage/lint",
        json={"content": "缺少占位符"},
    ).json()
    assert bad["ok"] is False
    assert bad["errors"]

    template_lint = client.post(
        "/api/templates/lint",
        json={"content": "一句话被拆成两行，\n  第二行是续句。"},
    ).json()
    assert any("句中硬换行" in item for item in template_lint["warnings"])

    normalized = client.post(
        "/api/prompts/normalize",
        json={"content": "一句话被拆成两行，\n  第二行是续句。"},
    ).json()
    assert normalized["changed"] is True
    assert normalized["content"] == "一句话被拆成两行，第二行是续句。"
    assert normalized["remaining_breaks"] == []

    audit = client.get("/api/prompts/audit").json()
    assert audit["ok"] is True
    assert audit["issue_count"] == 0

    preview = client.post(
        "/api/prompts/preview",
        json={
            "template_content": "自定义预览模板",
            "fragment_key": "tool.knowledge",
            "fragment_content": "预览用知识库规则",
            "use_knowledge_base": True,
            "include_live_context": False,
        },
    )
    assert preview.status_code == 200
    data = preview.json()
    assert data["static_core"].startswith("自定义预览模板")
    assert "全局约束" in data["static_core"]
    assert "预览用知识库规则" in data["static_core"]
    assert data["source"] == "override"

    with Session() as db:
        conv = repo.create_conversation(db, title="preview-lock")
        conv_id = conv.id
    client.post(
        "/api/prompts/preview",
        json={"conversation_id": conv_id, "include_live_context": False},
    )
    status = client.get(f"/api/conversations/{conv_id}/prompt").json()
    assert status["locked"] is None


def test_template_conversation_count_and_pending_switch_guard(api):
    client, Session, agent = api
    tpl = client.post(
        "/api/templates", json={"name": "被引用模板", "content": "v1"}
    ).json()
    with Session() as db:
        conv = repo.create_conversation(db, title="使用模板", template_id=tpl["id"])
        conv_id = conv.id
    listed = client.get("/api/templates").json()
    used = next(item for item in listed if item["id"] == tpl["id"])
    assert used["conversation_count"] == 1

    agent.pending = True
    blocked = client.patch(
        f"/api/conversations/{conv_id}",
        json={"title": "使用模板", "template_id": 0},
    )
    assert blocked.status_code == 409
    # 只改标题、不换模板时仍然允许
    renamed = client.patch(
        f"/api/conversations/{conv_id}", json={"title": "新标题"}
    )
    assert renamed.status_code == 200
    agent.pending = False
    switched = client.patch(
        f"/api/conversations/{conv_id}",
        json={"title": "新标题", "template_id": 0},
    )
    assert switched.status_code == 200


def test_archive_draft_impact_batch_and_pin(api):
    client, Session, _ = api
    tpl = client.post(
        "/api/templates", json={"name": "归档模板", "content": "v1"}
    ).json()
    tid = tpl["id"]

    listed = client.get("/api/templates").json()
    item = next(it for it in listed if it["id"] == tid)
    assert item["archived"] is False

    archived = client.post(f"/api/templates/{tid}/archive").json()
    assert archived["archived"] is True
    with Session() as db:
        conv = repo.create_conversation(db, title="归档测试")
        db.add(
            Message(
                id=1, conversation_id=conv.id, role="user", content="hi"
            )
        )
        db.commit()
        conv_id = conv.id
    blocked = client.patch(
        f"/api/conversations/{conv_id}",
        json={"title": "归档测试", "template_id": tid},
    )
    assert blocked.status_code == 409
    client.post(f"/api/templates/{tid}/unarchive")

    draft = client.put(
        f"/api/templates/{tid}/draft",
        json={"content": "草稿内容", "note": "草稿"},
    ).json()
    assert draft["has_draft"] is True
    published = client.post(
        f"/api/templates/{tid}/publish-draft", json={"note": "发布"}
    ).json()
    assert published["latest_version_no"] == 2
    assert published["has_draft"] is False

    fragment_draft = client.put(
        "/api/prompts/fragments/tool.knowledge/draft",
        json={"content": "片段草稿", "note": "草稿"},
    ).json()
    assert fragment_draft["fragment"]["has_draft"] is True
    fragment_pub = client.post(
        "/api/prompts/fragments/tool.knowledge/publish-draft"
    ).json()
    assert fragment_pub["fragment"]["version_no"] == 2
    assert fragment_pub["fragment"]["has_draft"] is False

    impact = client.get("/api/prompts/impact").json()
    assert any(item["id"] == conv_id for item in impact["conversations"])
    batch = client.post("/api/prompts/apply-latest-batch", json={}).json()
    assert batch["applied_count"] >= 1
    impact_after = client.get("/api/prompts/impact").json()
    assert not any(item["id"] == conv_id for item in impact_after["conversations"])

    with Session() as db:
        conv2 = repo.create_conversation(db, title="pin", template_id=tid)
        db.add(
            Message(
                id=2, conversation_id=conv2.id, role="user", content="hi"
            )
        )
        db.commit()
        conv2_id = conv2.id
    client.patch(f"/api/templates/{tid}", json={"content": "v3"})
    versions = client.get(f"/api/templates/{tid}/versions").json()["versions"]
    v1 = next(v for v in versions if v["version_no"] == 1)
    pinned = client.post(
        f"/api/conversations/{conv2_id}/prompt/pin",
        json={"version_id": v1["id"]},
    )
    assert pinned.status_code == 200
    status = client.get(f"/api/conversations/{conv2_id}/prompt").json()
    assert status["locked"]["template_version_no"] == 1


def test_fragment_default_drift_and_admin_token(api, monkeypatch):
    client, Session, _ = api
    client.patch(
        "/api/prompts/fragments/tool.knowledge",
        json={"content": "自定义规则"},
    )
    with Session() as db:
        row = repo.get_prompt_fragment(db, "tool.knowledge")
        row.base_default_hash = "old-default-hash"
        db.commit()
    fragments = client.get("/api/prompts/fragments").json()["fragments"]
    item = next(f for f in fragments if f["key"] == "tool.knowledge")
    assert item["default_changed"] is True

    monkeypatch.setenv("PROMPT_ADMIN_TOKEN", "secret")
    assert client.get("/api/prompts/fragments").status_code == 401
    ok = client.get(
        "/api/prompts/fragments",
        headers={"X-Prompt-Admin-Token": "secret"},
    )
    assert ok.status_code == 200


def test_export_import_and_trials(api):
    client, _, _ = api
    exported = client.get("/api/prompts/export").json()
    assert exported["templates"]
    assert exported["fragments"]
    imported = client.post("/api/prompts/import", json={"data": exported}).json()
    assert imported["ok"] is True
    assert imported["templates"]
    assert imported["fragments"]

    tpl = client.post(
        "/api/templates", json={"name": "试跑模板", "content": "v1"}
    ).json()
    tid = tpl["id"]
    client.patch(f"/api/templates/{tid}", json={"content": "v2"})
    versions = client.get(f"/api/templates/{tid}/versions").json()["versions"]
    v1 = next(v for v in versions if v["version_no"] == 1)
    v2 = next(v for v in versions if v["version_no"] == 2)

    ab = client.post(
        "/api/prompts/trials/ab",
        json={
            "template_id": tid,
            "version_a_id": v1["id"],
            "version_b_id": v2["id"],
            "question": "测试问题",
        },
    )
    assert ab.status_code == 200
    assert ab.json()["trial"]["result"]["a"]["text"] == "模拟回答"

    ev = client.post(
        "/api/prompts/trials/eval",
        json={"template_id": tid, "version_id": v1["id"], "limit": 1},
    )
    assert ev.status_code == 200
    assert ev.json()["trial"]["result"]["items"]

    trials = client.get("/api/prompts/trials").json()["trials"]
    assert trials
