"""写后验证：解释器路径按 shell 后端翻译；解释器缺失归为环境错误。"""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

from app.agent import utils as agent_utils
from app.agent.utils import _pick_verify_command, _run_verify


def _settings(**over):
    base = {
        "verify_command": "",
        "verify_auto_detect": True,
        "command_sandbox": "subprocess",
        "command_shell": "auto",
        "tool_workspace": "",
    }
    base.update(over)
    return SimpleNamespace(**base)


def test_verify_command_translates_interpreter_and_path(monkeypatch):
    import app.tools_extra as te

    monkeypatch.setattr(te, "shell_python", lambda settings=None: "/d/py/python.exe")
    monkeypatch.setattr(
        te,
        "detect_bash_backend",
        lambda settings=None: {"backend": "gitbash", "path": "", "is_wsl": False},
    )
    cmd = _pick_verify_command(_settings(), "scratch/a.py")
    assert cmd.startswith('"/d/py/python.exe" -m py_compile')
    assert "scratch/a.py" in cmd


@pytest.mark.skipif(os.name != "nt", reason="Windows path translation")
def test_verify_command_translates_absolute_path_for_wsl(monkeypatch):
    import app.tools_extra as te

    monkeypatch.setattr(
        te, "shell_python", lambda settings=None: "/mnt/d/py/python.exe"
    )
    monkeypatch.setattr(
        te,
        "detect_bash_backend",
        lambda settings=None: {"backend": "wsl", "path": "", "is_wsl": True},
    )
    cmd = _pick_verify_command(_settings(command_shell="wsl"), r"D:\x\a.py")
    assert "/mnt/d/x/a.py" in cmd


def test_docker_verify_uses_container_python(monkeypatch):
    cmd = _pick_verify_command(
        _settings(command_sandbox="docker"), "scratch/a.py"
    )
    assert cmd.startswith('"python" -m py_compile')


def test_verify_env_failure_is_classified(monkeypatch):
    import app.tools_extra as te

    agent_utils._VERIFY_PROBE_CACHE.clear()
    monkeypatch.setattr(
        te,
        "_run_command",
        lambda settings, cmd, **kw: {
            "exit_code": 127,
            "error": "No such file or directory",
            "output": "D:/py/python.exe: No such file or directory",
        },
    )
    results = _run_verify(_settings(), ["scratch/a.py"])
    assert results and results[0]["environment_error"] is True
    assert "验证环境不可用" in results[0]["error"]


def test_real_syntax_failure_is_not_environment_error(monkeypatch):
    import app.tools_extra as te

    agent_utils._VERIFY_PROBE_CACHE.clear()
    calls = {"n": 0}

    def fake_run(settings, cmd, **kw):
        calls["n"] += 1
        if "verify-ok" in cmd:
            return {"exit_code": 0, "error": None, "output": "verify-ok"}
        return {"exit_code": 1, "error": None, "output": "SyntaxError"}

    monkeypatch.setattr(te, "_run_command", fake_run)
    results = _run_verify(_settings(), ["scratch/a.py"])
    assert results and results[0]["exit_code"] == 1
    assert not results[0].get("environment_error")
    assert calls["n"] == 2


def test_run_verify_forwards_project_dir_to_run_command(monkeypatch, tmp_path):
    import app.tools_extra as te

    agent_utils._VERIFY_PROBE_CACHE.clear()
    seen_dirs: list[str | None] = []

    def fake_run(settings, cmd, cwd="", project_dir=None, **kw):
        seen_dirs.append(project_dir)
        return {"exit_code": 0, "error": None, "output": "ok"}

    monkeypatch.setattr(te, "_run_command", fake_run)
    custom_dir = str(tmp_path / "proj")
    results = _run_verify(_settings(), ["foo.py"], project_dir=custom_dir)
    assert results and results[0]["exit_code"] == 0
    assert custom_dir in seen_dirs

