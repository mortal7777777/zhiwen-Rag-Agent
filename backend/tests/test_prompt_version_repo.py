"""提示词版本表仓库层单测（SQLite 内存，不依赖 MySQL）。"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app.agent.prompts import ALL_SYSTEM_TEMPLATES, FRAGMENT_SPECS
from app.db import repository as repo
from app.db.database import Base
from app.db.models import (
    Conversation,
    Message,
    PromptFragment,
    PromptFragmentVersion,
    PromptTemplate,
    PromptTemplateVersion,
)


@pytest.fixture()
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(
        engine,
        tables=[
            Conversation.__table__,
            Message.__table__,
            PromptTemplate.__table__,
            PromptTemplateVersion.__table__,
            PromptFragment.__table__,
            PromptFragmentVersion.__table__,
        ],
    )
    Session = sessionmaker(bind=engine)
    with Session() as session:
        yield session


def _count(db, model) -> int:
    return int(db.scalar(select(func.count()).select_from(model)) or 0)


def test_seed_templates_idempotent_and_hides_default(db):
    repo.seed_templates(db)
    repo.seed_templates(db)

    rows = db.execute(select(PromptTemplate)).scalars().all()
    assert len(rows) == len(ALL_SYSTEM_TEMPLATES)
    assert _count(db, PromptTemplateVersion) == len(ALL_SYSTEM_TEMPLATES)

    visible = repo.list_templates(db)
    assert len(visible) == len(ALL_SYSTEM_TEMPLATES) - 1
    assert "默认助手（内置）" not in {t["name"] for t in visible}
    assert all(t["latest_version_no"] == 1 for t in visible)

    default = repo.get_template_by_system_key(db, "default")
    assert default is not None
    assert default.content == ALL_SYSTEM_TEMPLATES[-1]["content"]


def test_seed_only_bumps_version_when_content_changes(db, monkeypatch):
    import app.agent.prompts as prompts

    spec = dict(ALL_SYSTEM_TEMPLATES[0])
    monkeypatch.setattr(prompts, "ALL_SYSTEM_TEMPLATES", [spec])
    repo.seed_templates(db)
    repo.seed_templates(db)
    row = repo.get_template_by_system_key(db, spec["system_key"])
    assert repo.list_template_versions(db, row.id)[0]["version_no"] == 1

    changed = dict(spec, content=spec["content"] + "\n补充规则")
    monkeypatch.setattr(prompts, "ALL_SYSTEM_TEMPLATES", [changed])
    repo.seed_templates(db)
    assert repo.list_template_versions(db, row.id)[0]["version_no"] == 2

    # 只改描述：不产生新版本
    changed_desc = dict(changed, description="新描述")
    monkeypatch.setattr(prompts, "ALL_SYSTEM_TEMPLATES", [changed_desc])
    repo.seed_templates(db)
    assert repo.list_template_versions(db, row.id)[0]["version_no"] == 2


def test_seed_backfills_version_for_existing_custom_template(db):
    """上线前创建的自定义模板没有版本行：启动 seed 补 v1，且不重复补。"""
    legacy = PromptTemplate(name="老模板", content="旧内容", is_system=False)
    db.add(legacy)
    db.commit()
    db.refresh(legacy)

    repo.seed_templates(db)
    repo.seed_templates(db)
    versions = repo.list_template_versions(db, legacy.id)
    assert [v["version_no"] for v in versions] == [1]
    assert versions[0]["content"] == "旧内容"


def test_custom_template_crud_and_restore(db):
    tpl = repo.create_template(db, "自定义", "v1")
    assert [v["version_no"] for v in repo.list_template_versions(db, tpl.id)] == [1]

    repo.update_template(db, tpl.id, content="v2")
    versions = repo.list_template_versions(db, tpl.id)
    assert [v["version_no"] for v in versions] == [2, 1]

    repo.update_template(db, tpl.id, description="只改描述")
    assert len(repo.list_template_versions(db, tpl.id)) == 2

    v1 = next(v for v in versions if v["version_no"] == 1)
    repo.restore_template_version(db, tpl.id, v1["id"])
    latest = repo.list_template_versions(db, tpl.id)[0]
    assert latest["version_no"] == 3
    assert latest["content"] == "v1"
    assert "恢复自 v1" in latest["note"]

    assert repo.delete_template(db, tpl.id) is True
    assert _count(db, PromptTemplateVersion) == 0


def test_fragments_seed_publish_reset_restore(db):
    repo.seed_prompt_fragments(db)
    repo.seed_prompt_fragments(db)
    fragments = repo.list_prompt_fragments(db)
    assert len(fragments) == len(FRAGMENT_SPECS)
    assert all(f["version_no"] == 1 for f in fragments)
    assert not any(f["is_customized"] for f in fragments)

    key = "tool.knowledge"
    repo.publish_prompt_fragment(db, key, "自定义知识库规则")
    frag = next(f for f in repo.list_prompt_fragments(db) if f["key"] == key)
    assert frag["is_customized"] is True
    assert frag["version_no"] == 2

    # seed 不覆盖自定义内容
    repo.seed_prompt_fragments(db)
    frag = next(f for f in repo.list_prompt_fragments(db) if f["key"] == key)
    assert frag["content"] == "自定义知识库规则"
    assert frag["version_no"] == 2

    repo.reset_prompt_fragment(db, key)
    frag = next(f for f in repo.list_prompt_fragments(db) if f["key"] == key)
    assert frag["is_customized"] is False
    assert frag["version_no"] == 3

    versions = repo.list_prompt_fragment_versions(db, key)
    custom_v2 = next(v for v in versions if v["version_no"] == 2)
    repo.restore_prompt_fragment_version(db, key, custom_v2["id"])
    frag = next(f for f in repo.list_prompt_fragments(db) if f["key"] == key)
    assert frag["version_no"] == 4
    assert frag["is_customized"] is True

    # 恢复到与当前内容相同的版本：append-only 语义下是 no-op，不产生新版本
    repo.restore_prompt_fragment_version(db, key, custom_v2["id"])
    frag = next(f for f in repo.list_prompt_fragments(db) if f["key"] == key)
    assert frag["version_no"] == 4
    assert frag["is_customized"] is True

    default_v3 = next(v for v in versions if v["version_no"] == 3)
    repo.restore_prompt_fragment_version(db, key, default_v3["id"])
    frag = next(f for f in repo.list_prompt_fragments(db) if f["key"] == key)
    assert frag["version_no"] == 5
    assert frag["is_customized"] is False


def test_conversation_prompt_lock_roundtrip_and_clear_on_template_change(db):
    conv = repo.create_conversation(db, title="t")
    repo.set_conversation_prompt_lock(
        db,
        conv,
        {"template_id": 1, "template_version_no": 3, "bundle_hash": "abc"},
    )
    lock = repo.get_conversation_prompt_lock(conv)
    assert lock["template_version_no"] == 3
    assert conv.prompt_bundle_hash == "abc"
    assert conv.prompt_locked_at is not None

    listed = repo.list_conversations(db)[0]
    assert listed["prompt_bundle_hash"] == "abc"

    repo.update_conversation(db, conv.id, template_id=2)
    db.refresh(conv)
    assert conv.template_id == 2
    assert repo.get_conversation_prompt_lock(conv) is None
    assert conv.prompt_bundle_hash is None
