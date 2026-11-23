"""命令 shell 后端：auto 优先 Git Bash，绝不静默选 WSL。"""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

from app.tools_extra import common

pytestmark = pytest.mark.skipif(
    os.name != "nt", reason="Windows-only shell backend detection"
)


def test_auto_prefers_git_bash(monkeypatch):
    monkeypatch.setattr(common, "_find_git_bash", lambda: r"D:\Git\bin\bash.exe")
    monkeypatch.setattr(common, "_find_wsl_bash", lambda: r"C:\Windows\System32\wsl.exe")
    backend = common.detect_bash_backend(SimpleNamespace(command_shell="auto"))
    assert backend["backend"] == "gitbash"
    assert backend["path"].endswith("bash.exe")


def test_auto_never_silently_selects_wsl(monkeypatch):
    monkeypatch.setattr(common, "_find_git_bash", lambda: None)
    monkeypatch.setattr(common, "_find_wsl_bash", lambda: r"C:\Windows\System32\wsl.exe")
    backend = common.detect_bash_backend(SimpleNamespace(command_shell="auto"))
    assert backend["backend"] == "default", "auto 不允许静默切到 WSL"


def test_explicit_wsl_is_honored(monkeypatch):
    monkeypatch.setattr(common, "_find_git_bash", lambda: r"D:\Git\bin\bash.exe")
    monkeypatch.setattr(
        common, "_find_wsl_bash", lambda: r"C:\Windows\System32\wsl.exe"
    )
    backend = common.detect_bash_backend(SimpleNamespace(command_shell="wsl"))
    assert backend["backend"] == "wsl" and backend["is_wsl"] is True


def test_missing_gitbash_reports_missing(monkeypatch):
    monkeypatch.setattr(common, "_find_git_bash", lambda: None)
    backend = common.detect_bash_backend(SimpleNamespace(command_shell="gitbash"))
    assert backend["backend"] == "missing"


def test_shell_path_translation():
    assert common.shell_path(r"D:\conda\python.exe", "gitbash") == (
        "/d/conda/python.exe"
    )
    assert common.shell_path(r"D:\conda\python.exe", "wsl") == (
        "/mnt/d/conda/python.exe"
    )
    assert common.shell_path("scratch/a.py", "wsl") == "scratch/a.py"
