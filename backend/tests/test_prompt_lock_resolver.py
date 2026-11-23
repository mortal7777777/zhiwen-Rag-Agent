"""会话提示词锁定与升级（SQLite 内存）。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, delete, select
from sqlalchemy.orm import sessionmaker

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
from app.prompting import (
    apply_latest,
    conversation_prompt_status,
    resolve_conversation_prompt,
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
        repo.seed_templates(session)
        repo.seed_prompt_fragments(session)
        yield session


SETTINGS = SimpleNamespace(prompt_versioning_enabled=True)
DISABLED = SimpleNamespace(prompt_versioning_enabled=False)


def _conv_with_template(db, content: str = "v1") -> Conversation:
    tpl = repo.create_template(db, "锁定测试", content, category="coding")
    return repo.create_conversation(db, title="t", template_id=tpl.id)


def test_lock_freezes_template_version_until_apply_latest(db):
    conv = _conv_with_template(db, "v1")
    first = resolve_conversation_prompt(db, conv.id, None, None, SETTINGS)
    assert first.source == "new_lock"
    assert first.template_content == "v1"
    assert first.lock_written is True
    tpl = repo.get_template(db, conv.template_id)
    assert repo.get_conversation_prompt_lock(conv)["template_version_no"] == 1

    repo.update_template(db, tpl.id, content="v2", version_note="编辑")

    again = resolve_conversation_prompt(db, conv.id, None, None, SETTINGS)
    assert again.template_content == "v1"  # 旧会话不漂移
    assert again.source == "locked"

    # 新会话使用最新版本
    conv2 = repo.create_conversation(db, template_id=tpl.id)
    fresh = resolve_conversation_prompt(db, conv2.id, None, None, SETTINGS)
    assert fresh.template_content == "v2"

    upgraded = apply_latest(db, conv.id)
    assert upgraded["ok"] is True
    assert upgraded["cache_break"] is True
    after = resolve_conversation_prompt(db, conv.id, None, None, SETTINGS)
    assert after.template_content == "v2"


def test_fragment_versions_are_locked_per_conversation(db):
    conv = _conv_with_template(db)
    resolve_conversation_prompt(db, conv.id, None, None, SETTINGS)

    repo.publish_prompt_fragment(db, "tool.knowledge", "自定义规则")
    old = resolve_conversation_prompt(db, conv.id, None, None, SETTINGS)
    assert old.fragments["tool.knowledge"] != "自定义规则"

    conv2 = _conv_with_template(db)
    fresh = resolve_conversation_prompt(db, conv2.id, None, None, SETTINGS)
    assert fresh.fragments["tool.knowledge"] == "自定义规则"

    status = conversation_prompt_status(db, conv.id)
    assert status["upgradable"] is True
    assert "tool.knowledge" in status["outdated_units"]

    apply_latest(db, conv.id)
    upgraded = resolve_conversation_prompt(db, conv.id, None, None, SETTINGS)
    assert upgraded.fragments["tool.knowledge"] == "自定义规则"


def test_system_prompt_override_does_not_change_lock(db):
    conv = _conv_with_template(db)
    base = resolve_conversation_prompt(db, conv.id, None, None, SETTINGS)
    lock_before = repo.get_conversation_prompt_lock(conv)

    overridden = resolve_conversation_prompt(
        db, conv.id, None, "临时覆盖提示词", SETTINGS
    )
    assert overridden.template_content == "临时覆盖提示词"
    assert overridden.source == "override"
    assert overridden.telemetry["system_prompt_override"] is True
    assert repo.get_conversation_prompt_lock(conv) == lock_before

    restored = resolve_conversation_prompt(db, conv.id, None, None, SETTINGS)
    assert restored.template_content == base.template_content


def test_template_change_relocks_latest(db):
    conv = _conv_with_template(db, "keep")
    resolve_conversation_prompt(db, conv.id, None, None, SETTINGS)
    other = repo.create_template(db, "另一个", "other-v1", category="general")

    switched = resolve_conversation_prompt(db, conv.id, other.id, None, SETTINGS)
    assert switched.template_content == "other-v1"
    db.refresh(conv)
    assert conv.template_id == other.id
    assert repo.get_conversation_prompt_lock(conv)["template_id"] == other.id


def test_missing_version_falls_back_without_rewriting_lock(db):
    conv = _conv_with_template(db)
    first = resolve_conversation_prompt(db, conv.id, None, None, SETTINGS)
    lock = repo.get_conversation_prompt_lock(conv)
    db.execute(
        delete(PromptTemplateVersion).where(
            PromptTemplateVersion.id == lock["template_version_id"]
        )
    )
    db.commit()

    fallback = resolve_conversation_prompt(db, conv.id, None, None, SETTINGS)
    assert fallback.template_content == first.template_content
    assert fallback.telemetry["fallback"] is True
    # 锁不被自动改写，保证可观测/可人工修复
    assert repo.get_conversation_prompt_lock(conv) == lock


def test_disabled_uses_old_read_through_semantics(db):
    conv = _conv_with_template(db, "v1")
    tpl = repo.get_template(db, conv.template_id)
    repo.update_template(db, tpl.id, content="v2")

    result = resolve_conversation_prompt(db, conv.id, tpl.id, None, DISABLED)
    assert result.template_content == "v2"
    assert result.source == "disabled"
    assert repo.get_conversation_prompt_lock(conv) is None


def test_default_path_uses_hidden_template(db):
    conv = repo.create_conversation(db, title="无模板")
    result = resolve_conversation_prompt(db, conv.id, None, None, SETTINGS)
    default = repo.get_template_by_system_key(db, "default")
    assert result.template_content == default.content
    assert result.telemetry["template_key"] == "default"
    assert repo.get_conversation_prompt_lock(conv)["template_id"] == default.id
