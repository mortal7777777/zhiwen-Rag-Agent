"""文件/命令工具的安全边界与白名单逻辑单测（临时目录，不碰真实工作区）。"""

from __future__ import annotations

import pytest

import app.tools_extra as tools_extra


class FakeSettings:
    def __init__(self, workspace, allowlist="", timeout=60):
        self.tool_workspace = workspace
        self.command_allowlist = allowlist
        self.command_timeout = timeout


def test_safe_path_relative_and_absolute(tmp_path):
    ws = tmp_path
    target = ws / "sub" / "notes.md"
    assert tools_extra._safe_path(ws, "sub/notes.md") == target.resolve()
    assert tools_extra._safe_path(ws, str(target)) == target.resolve()


def test_safe_path_rejects_escape(tmp_path):
    ws = tmp_path
    with pytest.raises(ValueError):
        tools_extra._safe_path(ws, "../outside.txt")


def test_command_allowlist():
    settings = FakeSettings(".", allowlist="python, ls, echo")
    assert tools_extra.command_allowed(settings, "python run.py") == (True, "")
    assert tools_extra.command_allowed(settings, "ls -la") == (True, "")
    ok, reason = tools_extra.command_allowed(settings, "rm -rf x")
    assert ok is False and "白名单" in reason
    assert tools_extra.command_allowed(settings, "") == (False, "命令为空")


def test_allowlist_rejects_compound_commands():
    """前缀匹配挡不住复合命令：以白名单前缀开头但会写文件/串联破坏性操作。"""
    settings = FakeSettings(".", allowlist="python, ls, echo")

    # 重定向写文件
    for cmd in ("ls > files.txt", "ls >> log.txt", "echo hi > important.txt"):
        ok, reason = tools_extra.command_allowed(settings, cmd)
        assert ok is False, cmd
        assert "重定向" in reason, cmd

    # 管道 / 串联 / 命令替换
    for cmd in ("ls | xargs rm -rf", "ls; rm -rf x", "echo $(rm -rf x)", "ls && rm x"):
        ok, reason = tools_extra.command_allowed(settings, cmd)
        assert ok is False, cmd

    # 引号内的操作符是普通字符，不能误伤
    assert tools_extra.command_allowed(
        settings, 'python -c "import a; print(b)"'
    ) == (True, "")

    # 无害重定向放行
    assert tools_extra.command_allowed(settings, "ls 2>/dev/null") == (True, "")
    assert tools_extra.command_allowed(settings, "ls > /dev/null") == (True, "")
    assert tools_extra.command_allowed(settings, "ls 2>&1") == (True, "")


def test_atomic_write(tmp_path):
    target = tmp_path / "a" / "b.txt"
    tools_extra._atomic_write(target, "hello")
    assert target.read_text(encoding="utf-8") == "hello"
    leftovers = [p for p in target.parent.iterdir() if p.name.endswith(".tmp")]
    assert leftovers == []


def test_diff_context():
    text = "第一行\n需要替换的旧内容\n第三行"
    before, after = tools_extra._diff_context(text, "旧内容", "新内容", False)
    assert "旧内容" in before and "新内容" in after


def test_run_command_echo(tmp_path):
    settings = FakeSettings(str(tmp_path), allowlist="echo")
    result = tools_extra._run_command(settings, "echo hello")
    assert result["exit_code"] == 0
    assert "hello" in result["output"]


def test_docker_run_cmd_isolation_flags(tmp_path):
    settings = FakeSettings(str(tmp_path))
    settings.command_sandbox = "docker"
    settings.sandbox_image = "python:3.11-slim"
    settings.sandbox_workspace_readonly = False
    cmd = tools_extra._docker_run_cmd(settings, "echo hi", tmp_path)
    assert "--read-only" in cmd
    assert "--network=none" in cmd
    assert "--cap-drop" in cmd
    assert "--pids-limit=256" in cmd
    assert f"{str(tmp_path.resolve())}:/workspace" in cmd
    assert cmd[cmd.index("-w") + 1] == "/workspace"


def test_docker_run_cmd_readonly_workspace(tmp_path):
    settings = FakeSettings(str(tmp_path))
    settings.command_sandbox = "docker"
    settings.sandbox_image = "python:3.11-slim"
    settings.sandbox_workspace_readonly = True
    cmd = tools_extra._docker_run_cmd(settings, "echo hi", tmp_path)
    assert f"{str(tmp_path.resolve())}:/workspace:ro" in cmd
    assert cmd[cmd.index("-w") + 1] == "/scratch"
    assert "/scratch:rw,size=256m" in cmd


def test_list_dir_read_grep(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("def foo():\n    return 42\n", encoding="utf-8")
    settings = FakeSettings(str(tmp_path))
    ls = tools_extra.make_list_dir_tool(settings).invoke({"path": "."})
    assert any(e["name"] == "src" and e["is_dir"] for e in ls["entries"])
    read = tools_extra.make_read_file_tool(settings).invoke({"path": "src/app.py"})
    assert "def foo" in read["content"]
    grep = tools_extra.make_grep_search_tool(settings).invoke({"pattern": "foo", "path": "."})
    assert grep["matches"][0]["file"] == "src/app.py"


# ---------------- MCP 失败提示（mcp_manager）----------------


def test_mcp_error_hint_for_blocked_file_protocol():
    from app.mcp_manager import _append_error_hint

    blocked = 'Error: Access to "file:" protocol is blocked. Attempted URL: "file:///x.html"'
    out = _append_error_hint(blocked)
    assert "python -m http.server" in out
    assert "不要再尝试 file://" in out
    # 幂等：重复处理不叠加提示
    assert _append_error_hint(out).count("python -m http.server") == 1


def test_mcp_error_hint_for_node_require():
    from app.mcp_manager import _append_error_hint

    out = _append_error_hint("ReferenceError: require is not defined")
    assert "read_file" in out
    assert "require/import 不可用" in out


def test_mcp_error_hint_ignores_normal_output():
    from app.mcp_manager import _append_error_hint

    plain = "### Result\n- Page URL: http://127.0.0.1:8123/x.html"
    assert _append_error_hint(plain) == plain


# ---------------- bash 工具：后台子进程不得挂住整轮 ----------------


def test_bash_tool_not_blocked_by_background_child(tmp_path):
    """`cmd &` 起的后台进程会继承管道；旧实现（capture_output）等管道 EOF 永久挂住。

    2026-09-26 实测：模型按提示 `python -m http.server 8123 &` 预览页面，
    bash 工具卡死 45 分钟、整轮对话僵死（超时 kill 也救不了）。
    """
    import time

    from app import tools_extra

    settings = FakeSettings(str(tmp_path))
    settings.command_timeout = 10
    bash = tools_extra.make_bash_tool(settings)

    started = time.perf_counter()
    res = bash.invoke(
        {"command": 'python -c "import time; time.sleep(20)" & echo started'}
    )
    elapsed = time.perf_counter() - started
    assert elapsed < 5, f"后台子进程不应挂住工具（耗时 {elapsed:.1f}s）"
    assert "started" in str(res.get("output") or "")
