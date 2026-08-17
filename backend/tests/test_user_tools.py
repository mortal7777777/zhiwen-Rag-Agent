"""用户自写工具加载器单测：加载/敏感标记/失败跳过（monkeypatch 目录到 tmp）。"""

from __future__ import annotations

from app import permissions
from app import user_tool_loader as loader


def _write_tool_module(tmp_path, name: str, source: str):
    py = tmp_path / f"{name}.py"
    py.write_text(source, encoding="utf-8")
    return py


def test_load_and_sensitive_declared(tmp_path, monkeypatch):
    monkeypatch.setattr(loader, "USER_TOOLS_DIR", tmp_path)
    _write_tool_module(
        tmp_path,
        "hello",
        """
from langchain_core.tools import tool

SENSITIVE = set()

@tool
def hello_tool(name: str = "world") -> dict:
    \"\"\"打招呼。\"\"\"
    return {"summary": f"hello {name}"}
""",
    )
    tools = loader.load_user_tools()
    assert [t.name for t in tools] == ["hello_tool"]
    assert tools[0].invoke({"name": "mortal"})["summary"] == "hello mortal"
    # 声明 SENSITIVE = set() → 不敏感
    assert loader.is_user_tool_sensitive("hello_tool") is False
    # 非用户工具 → None（走默认规则）
    assert loader.is_user_tool_sensitive("write_file") is None
    # permissions.is_sensitive_tool 接入：用户工具按标记返回
    assert permissions.is_sensitive_tool("hello_tool") is False


def test_undeclared_sensitive_defaults_true(tmp_path, monkeypatch):
    monkeypatch.setattr(loader, "USER_TOOLS_DIR", tmp_path)
    _write_tool_module(
        tmp_path,
        "raw",
        """
from langchain_core.tools import tool

@tool
def raw_tool(x: int = 1) -> dict:
    \"\"\"未声明敏感标记。\"\"\"
    return {"summary": str(x)}
""",
    )
    tools = loader.load_user_tools()
    assert [t.name for t in tools] == ["raw_tool"]
    # 未声明 SENSITIVE → 默认敏感（安全默认）
    assert loader.is_user_tool_sensitive("raw_tool") is True
    assert permissions.is_sensitive_tool("raw_tool") is True


def test_declared_sensitive_tool(tmp_path, monkeypatch):
    monkeypatch.setattr(loader, "USER_TOOLS_DIR", tmp_path)
    _write_tool_module(
        tmp_path,
        "writer",
        """
from langchain_core.tools import tool

SENSITIVE = {"my_writer"}

@tool
def my_writer(path: str, content: str) -> dict:
    \"\"\"写文件。\"\"\"
    return {"summary": f"wrote {path}"}
""",
    )
    tools = loader.load_user_tools()
    assert loader.is_user_tool_sensitive("my_writer") is True
    assert permissions.is_sensitive_tool("my_writer") is True


def test_failed_module_skipped_others_ok(tmp_path, monkeypatch):
    monkeypatch.setattr(loader, "USER_TOOLS_DIR", tmp_path)
    _write_tool_module(tmp_path, "broken", "raise RuntimeError('boom')\n")
    _write_tool_module(
        tmp_path,
        "good",
        """
from langchain_core.tools import tool

SENSITIVE = set()

@tool
def good_tool() -> dict:
    \"\"\"正常工具。\"\"\"
    return {"summary": "ok"}
""",
    )
    tools = loader.load_user_tools()
    # 坏模块被跳过，好模块正常加载
    assert [t.name for t in tools] == ["good_tool"]


def test_underscore_prefix_module_ignored(tmp_path, monkeypatch):
    monkeypatch.setattr(loader, "USER_TOOLS_DIR", tmp_path)
    _write_tool_module(
        tmp_path,
        "_helper",
        """
from langchain_core.tools import tool

@tool
def helper_tool() -> dict:
    \"\"\"内部辅助，不应被加载。\"\"\"
    return {"summary": "helper"}
""",
    )
    assert loader.load_user_tools() == []
