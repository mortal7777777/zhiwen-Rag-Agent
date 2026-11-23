"""TUI 命令层：@文件解析、导出、文件补全（批次 2）。"""

from __future__ import annotations

from pathlib import Path

from cli import commands
from cli import http as cli_http
from cli import input as input_mod
from cli import term as term_mod
from cli.input import (
    COMMANDS,
    COMMAND_META,
    MENU,
    _at_fragment,
    _command_matches,
    completion_candidates,
    file_completions,
    menu_move,
    menu_selected,
)


def _meta(path: Path) -> dict:
    return {
        "id": "a" * 32,
        "name": path.name,
        "chars": 5,
        "truncated": False,
    }


def test_expand_document_uploads_attachment(monkeypatch, tmp_path):
    doc = tmp_path / "note.md"
    doc.write_text("hello", encoding="utf-8")
    calls = []

    def fake_upload(base, path):
        calls.append(path)
        return _meta(path)

    monkeypatch.setattr(commands, "upload_attachment", fake_upload)
    question, ids, images = commands.expand_file_mentions(
        "http://x", f"请总结 @{doc}", str(tmp_path)
    )
    assert question == f"请总结 @{doc}"
    assert ids == ["a" * 32]
    assert images == []
    assert calls == [doc]


def test_expand_image_encodes_data_url(monkeypatch, tmp_path):
    img = tmp_path / "shot.png"
    img.write_bytes(b"\x89PNG\r\n")
    monkeypatch.setattr(
        commands,
        "upload_attachment",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("图片不应上传")),
    )
    _question, ids, images = commands.expand_file_mentions(
        "http://x", f"看图 @{img}", str(tmp_path)
    )
    assert ids == []
    assert len(images) == 1
    assert images[0].startswith("data:image/png;base64,")


def test_expand_quoted_path_with_spaces(monkeypatch, tmp_path):
    folder = tmp_path / "my docs"
    folder.mkdir()
    doc = folder / "note.txt"
    doc.write_text("hello", encoding="utf-8")
    monkeypatch.setattr(
        commands, "upload_attachment", lambda base, path: _meta(path)
    )
    _question, ids, _images = commands.expand_file_mentions(
        "http://x", '看 @"my docs/note.txt"', str(tmp_path)
    )
    assert ids == ["a" * 32]


def test_expand_missing_file_is_ignored(monkeypatch, tmp_path):
    monkeypatch.setattr(
        commands,
        "upload_attachment",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("不应上传")),
    )
    _question, ids, images = commands.expand_file_mentions(
        "http://x", "看 @missing.md", str(tmp_path)
    )
    assert ids == [] and images == []


def test_command_menu_replaces_whole_token_with_meta():
    candidates = completion_candidates("/mo")
    match = next(item for item in candidates if item[0] == "/model")
    value, start, meta = match
    assert meta == "切换对话供应商"
    assert "/mo"[:start] + value == "/model"  # 不会补成 //model


def test_command_menu_argument_completion_has_no_meta():
    candidates = completion_candidates("/tools a")
    assert ("auto", -1, "") in candidates


def test_command_menu_closed_for_plain_text():
    assert completion_candidates("hello") == []


def test_command_menu_cycles_and_selects():
    MENU["prefix"], MENU["index"] = "", 0
    assert _command_matches("/") == COMMANDS
    assert menu_selected("/") == "/help"  # 首项是安全命令，避免 / + 回车误退出
    menu_move("/", 1)
    assert menu_selected("/") == COMMANDS[1]
    menu_move("/", -1)
    assert menu_selected("/") == "/help"
    assert menu_selected("hello") is None


def test_command_menu_closes_when_args_start():
    assert _command_matches("/tools ") == []
    assert _command_matches("/tools a") == []
    assert _command_matches("/tools") == ["/tools"]


def test_every_command_has_menu_description():
    assert set(COMMANDS) == set(COMMAND_META)


def test_toolbar_menu_lists_commands_with_hint(monkeypatch):
    import prompt_toolkit.application as pt_app

    class _Buf:
        text = "/"

    class _App:
        current_buffer = _Buf()

    MENU["prefix"], MENU["index"] = "", 0
    monkeypatch.setattr(pt_app, "get_app", lambda: _App())
    lines = input_mod._toolbar_text()
    styles = [style for style, _text in lines]
    text = "".join(_text for _style, _text in lines)
    assert "class:menu.current" in styles
    assert "/help" in text and "回车执行" in text
    assert f"1/{len(COMMANDS)}" in text  # 滚动窗口，总数可见


def test_legacy_input_renders_command_menu(monkeypatch):
    import io

    MENU["prefix"], MENU["index"] = "", 0
    input_mod._INPUT_ROWS[0] = 1
    input_mod._CURSOR_ROW[0] = 0
    monkeypatch.setattr(input_mod, "USE_COLOR", True)
    monkeypatch.setattr(input_mod, "term_width", lambda: 80)
    monkeypatch.setattr(term_mod, "USE_COLOR", True)
    buf = io.StringIO()
    monkeypatch.setattr(input_mod.sys, "stdout", buf)
    input_mod._render_input("/", 1)
    out = buf.getvalue()
    assert "/help" in out and "回车执行" in out
    # 下移后重绘应显示下一条
    input_mod.menu_move("/", 1)
    input_mod._render_input("/", 1)
    assert "/new" in buf.getvalue()


def test_file_completions_lists_matching_names(monkeypatch, tmp_path):
    (tmp_path / "alpha.txt").write_text("x", encoding="utf-8")
    (tmp_path / "beta").mkdir()
    (tmp_path / ".hidden").write_text("x", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    assert file_completions("a") == ["alpha.txt"]
    assert file_completions("be") == ["beta/"]
    assert file_completions("") == ["beta/", "alpha.txt"]
    assert ".hidden" in file_completions(".")


def test_at_fragment_detection():
    assert _at_fragment("请 @doc") == ("doc", -3)
    assert _at_fragment("@") == ("", 0)
    assert _at_fragment("mail foo@bar") is None
    assert _at_fragment('@"quoted') is None


def test_legacy_empty_enter_ignored_for_main_input(monkeypatch):
    from collections import deque

    from cli.input import Key

    class _Stdin:
        def isatty(self):
            return True

        def readline(self):
            return ""

    class _Reader:
        def __init__(self, keys):
            self.keys = deque(keys)

        def get(self, timeout=None):
            return self.keys.popleft()

    monkeypatch.setattr(input_mod.sys, "stdin", _Stdin())
    reader = _Reader([Key("enter"), Key("char", "x"), Key("enter")])
    assert input_mod._read_line_legacy(reader, [], submit_empty=False) == "x"
    # 选择/确认类输入仍允许空回车取消
    assert input_mod._read_line_legacy(_Reader([Key("enter")]), []) == ""


def test_ctrl_y_maps_to_retry_key():
    from cli.input import KeyReader

    class _Recorder:
        def __init__(self):
            self.items = []

        def _push(self, key):
            self.items.append(key)

    rec = _Recorder()
    KeyReader._push_key(rec, "\x19")
    assert rec.items[0].name == "ctrl-y"


def test_doctor_reports_checks(monkeypatch, capsys):
    def fake_http(base, method, path, body=None, timeout=120):
        if path == "/api/health":
            return {
                "status": "ok",
                "version": "0.2.0",
                "uptime_seconds": 12,
                "instance_lock": {"held": True},
            }
        if path == "/api/index/status":
            return {"index_name": "kb", "doc_count": 10, "error": None}
        if path == "/api/conversations":
            return [{"id": 1}]
        if path == "/api/vision/status":
            return {"configured": True, "model": "v", "auto_describe": True}
        if path == "/api/mcp":
            return {
                "servers": [
                    {"id": "a", "name": "A", "enabled": True},
                    {"id": "b", "name": "B", "enabled": False},
                ]
            }
        if path == "/api/skills":
            return {"total": 3, "items": []}
        raise AssertionError(path)

    monkeypatch.setattr(commands, "http_json", fake_http)
    commands.doctor("http://x")
    out = capsys.readouterr().out
    assert "✔ 后端" in out and "✔ 索引" in out
    assert "✔ MCP" in out and "1/2 启用" in out
    assert "✔ 技能" in out and "3 个" in out


def test_show_skills_filters(monkeypatch, capsys):
    monkeypatch.setattr(
        commands,
        "http_json",
        lambda *a, **k: {
            "total": 2,
            "items": [
                {"name": "accessibility", "description": "a11y"},
                {"name": "pdf", "description": "pdf tools"},
            ],
        },
    )
    commands.show_skills("http://x", "pdf")
    out = capsys.readouterr().out
    assert "1/2" in out and "pdf" in out


def test_timeline_rollback_requires_confirmation(monkeypatch, capsys):
    calls = []

    def fake_http(base, method, path, body=None, timeout=120):
        calls.append((method, path))
        if method == "GET":
            return {
                "items": [
                    {
                        "checkpoint_id": "a" * 32,
                        "created_at": "2026-11-09T07:12:37+00:00",
                        "messages": 366,
                        "plan": [],
                        "plan_done_count": 0,
                    }
                ]
            }
        return {"ok": True}

    monkeypatch.setattr(commands, "http_json", fake_http)
    monkeypatch.setattr(commands, "read_line", lambda *a, **k: "y")
    assert commands.timeline("http://x", 390, None, "1") is True
    assert any(m == "POST" and p.endswith("/rollback") for m, p in calls)
    out = capsys.readouterr().out
    assert "已回滚" in out


def test_context_pct_prefers_sent_estimate():
    assert (
        cli_http.context_pct(
            {
                "sent_estimate_tokens": 40000,
                "estimated_tokens": 180000,
                "token_budget": 400000,
            }
        )
        == 10
    )
    assert (
        cli_http.context_pct(
            {"estimated_tokens": 180000, "token_budget": 400000}
        )
        == 45
    )


def test_show_cost_aggregates_cache_and_cost(monkeypatch, capsys):
    runs = [
        {
            "token_usage": {
                "prompt_tokens": 100,
                "completion_tokens": 10,
                "cache_hit_tokens": 80,
                "cache_miss_tokens": 20,
                "llm_calls": 1,
            },
            "estimated_cost_yuan": 0.0012,
        },
        {
            "token_usage": {
                "prompt_tokens": 50,
                "completion_tokens": 5,
                "cache_hit_tokens": 40,
                "cache_miss_tokens": 10,
                "llm_calls": 1,
            },
            "estimated_cost_yuan": 0.0008,
        },
    ]
    monkeypatch.setattr(cli_http, "fetch_runs", lambda *a, **k: runs)
    cli_http.show_cost("http://x", 1)
    out = capsys.readouterr().out
    assert "最近 2 次运行" in out
    assert "缓存命中 80%" in out
    assert "0.0020 元" in out


def test_export_conversation_writes_markdown(monkeypatch, tmp_path):
    messages = [
        {"role": "user", "content": "问一下"},
        {
            "role": "assistant",
            "content": "答一下",
            "sources": [
                {"type": "kb", "relative_path": "docs/a.md", "page": 2},
            ],
        },
        {"role": "tool", "content": "隐藏"},
    ]
    monkeypatch.setattr(
        commands, "http_json", lambda *a, **k: messages
    )
    monkeypatch.setattr(
        commands, "fetch_conversation_titles", lambda base: {1: "测试会话"}
    )

    commands.export_conversation("http://x", 1, str(tmp_path), "md")
    files = list(tmp_path.glob("zhiwen_conv_1_*.md"))
    assert len(files) == 1
    text = files[0].read_text(encoding="utf-8")
    assert "# 测试会话" in text
    assert "## 用户\n\n问一下" in text
    assert "## 助手\n\n答一下" in text
    assert "docs/a.md · p.2" in text
    assert "隐藏" not in text
