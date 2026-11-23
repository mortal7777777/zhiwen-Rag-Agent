"""对话附件：文本提取、大小限制、临时存储与读取。"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.attachments import (
    AttachmentError,
    delete_attachment,
    load_attachments,
    save_bytes,
)


class FakeSettings:
    def __init__(self, meta_dir: Path, **overrides):
        self.meta_dir = meta_dir
        self.attachment_max_files = 5
        self.attachment_max_mb = 20
        self.attachment_max_chars = 30000
        self.attachment_total_chars = 60000
        self.attachment_retention_hours = 24
        for key, value in overrides.items():
            setattr(self, key, value)


def test_save_and_load_text_file(tmp_path):
    settings = FakeSettings(tmp_path)
    meta = save_bytes(settings, "notes.md", "# 标题\n\n正文".encode("utf-8"))
    assert meta["name"] == "notes.md"
    assert meta["chars"] > 0
    assert meta["truncated"] is False

    loaded = load_attachments(settings, [meta["id"]])
    assert len(loaded) == 1
    assert "标题" in loaded[0]["text"]


def test_rejects_unsupported_suffix(tmp_path):
    settings = FakeSettings(tmp_path)
    with pytest.raises(AttachmentError):
        save_bytes(settings, "evil.exe", b"MZ")


def test_rejects_oversize(tmp_path):
    settings = FakeSettings(tmp_path, attachment_max_mb=1)
    with pytest.raises(AttachmentError):
        save_bytes(settings, "big.txt", b"x" * (1024 * 1024 + 1))


def test_truncates_long_text(tmp_path):
    settings = FakeSettings(tmp_path, attachment_max_chars=5)
    meta = save_bytes(settings, "a.txt", b"abcdefgh")
    assert meta["truncated"] is True
    loaded = load_attachments(settings, [meta["id"]])
    assert loaded[0]["text"] == "abcde"


def test_load_caps_total_chars(tmp_path):
    settings = FakeSettings(tmp_path, attachment_total_chars=3)
    first = save_bytes(settings, "a.txt", b"aaaa")
    second = save_bytes(settings, "b.txt", b"bbbb")
    loaded = load_attachments(settings, [first["id"], second["id"]])
    assert loaded[0]["text"] == "aaa"
    assert loaded[1]["text"] == ""


def test_invalid_or_traversal_id_is_ignored(tmp_path):
    settings = FakeSettings(tmp_path)
    assert load_attachments(settings, ["../../etc/passwd", "not-an-id"]) == []


def test_delete_attachment(tmp_path):
    settings = FakeSettings(tmp_path)
    meta = save_bytes(settings, "a.txt", b"hello")
    assert delete_attachment(settings, meta["id"]) is True
    assert load_attachments(settings, [meta["id"]]) == []
    assert delete_attachment(settings, meta["id"]) is False
