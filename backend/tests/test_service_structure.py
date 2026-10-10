"""`LangGraphAgentService` mixin 拆分契约（2026-10-09）。

拆分后容易踩的坑（实测全踩过一遍）：
1. **方法体里的相对导入**没有随文件层级加一层点（`from .tools` 变成了
   `app.agent.service.tools`）→ 运行到那行才炸。本文件用 importlib 真解析一遍；
2. **装饰器不随方法走**（AST 的 lineno 指向 `def`，`@staticmethod` 留在原文件）
   → `_load_mcp_servers` 从静态方法变成实例方法，调用直接 TypeError；
3. mixin 互相 import → 形成环，主类装配顺序变成隐式契约。

另外用行数棘轮防止主类重新长回去（拆分前 781 行）。
"""

from __future__ import annotations

import ast
import importlib
import inspect
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
AGENT_DIR = BACKEND / "app" / "agent"
SERVICE_DIR = AGENT_DIR / "service"
MAIN_FILE = AGENT_DIR / "langgraph_agent.py"

PACKAGE = "app.agent.service"
MIXINS = {
    "base": "ServiceBaseMixin",
    "run": "RunMixin",
    "resume": "ResumeMixin",
    "tool_surface": "ToolSurfaceMixin",
    "mcp": "McpMixin",
}

# 拆分后必须仍可调用的服务方法（名字 = 对外契约，别在重构里悄悄丢）
EXPECTED_METHODS = [
    "_run",
    "_emit_suspended",
    "_arm_permission_watchdog",
    "_recursion_limit",
    "_thread_config",
    "pending_state",
    "pending_approval_payload",
    "has_pending_approval",
    "has_unfinished_run",
    "abandon_run",
    "resume",
    "continue_run",
    "_continue_stream",
    "build_tool_list",
    "extra_tools",
    "mcp_tools",
    "mcp_status",
    "connect_mcp_servers",
    "graph",
    "checkpoint_saver",
    "refresh",
]


def _relative_imports(path: Path) -> list[tuple[int, ast.ImportFrom]]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return [
        (node.lineno, node)
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.level
    ]


def _resolve(package: str, node: ast.ImportFrom) -> str:
    """把相对导入还原成绝对模块名（level=1 → package 自身）。"""
    parts = package.split(".")
    base = parts[: len(parts) - (node.level - 1)] if node.level > 1 else parts
    if node.module:
        base = base + node.module.split(".")
    return ".".join(base)


def test_service_modules_exist_with_expected_mixins():
    for module, cls_name in MIXINS.items():
        path = SERVICE_DIR / f"{module}.py"
        assert path.exists(), f"缺少 service/{module}.py"
        module_obj = importlib.import_module(f"{PACKAGE}.{module}")
        assert hasattr(module_obj, cls_name), f"service/{module}.py 缺少 {cls_name}"


def test_relative_imports_inside_mixins_resolve():
    """核心回归：方法体里的相对导入必须真的能 import（层级改动最容易漏）。"""
    broken: list[str] = []
    for path in sorted(SERVICE_DIR.glob("*.py")):
        if path.name == "__init__.py":
            continue
        for lineno, node in _relative_imports(path):
            target = _resolve(PACKAGE, node)
            try:
                importlib.import_module(target)
            except Exception as exc:  # noqa: BLE001 - 只要报错就说明层级不对
                broken.append(f"{path.name}:{lineno} from {'.' * node.level}{node.module or ''} -> {target} ({exc})")
    assert not broken, "相对导入无法解析：\n" + "\n".join(broken)


def test_mixins_do_not_import_each_other():
    """mixin 之间不得互相 import（只允许依赖 base），避免隐式顺序契约与环。"""
    problems: list[str] = []
    for path in sorted(SERVICE_DIR.glob("*.py")):
        name = path.stem
        if name in ("__init__", "base"):
            continue
        for lineno, node in _relative_imports(path):
            if node.level != 1:
                continue
            target = (node.module or "").split(".")[0]
            if target in MIXINS and target != name and target != "base":
                problems.append(f"{path.name}:{lineno} 导入了兄弟 mixin {target}")
    assert not problems, "\n".join(problems)


def test_composed_service_keeps_public_methods():
    from app.agent.langgraph_agent import LangGraphAgentService

    missing = [m for m in EXPECTED_METHODS if not hasattr(LangGraphAgentService, m)]
    assert not missing, f"拆分后丢失了这些方法：{missing}"
    # MRO 里应包含全部 mixin（确认装配生效）
    names = {c.__name__ for c in LangGraphAgentService.__mro__}
    for cls_name in MIXINS.values():
        assert cls_name in names, f"{cls_name} 未进入 MRO"


def test_load_mcp_servers_stays_staticmethod():
    """回归：装饰器不随方法走，会把 @staticmethod 悄悄变成实例方法。"""
    from app.agent.service.mcp import McpMixin

    raw = inspect.getattr_static(McpMixin, "_load_mcp_servers")
    assert isinstance(raw, staticmethod), "_load_mcp_servers 必须是 @staticmethod"
    params = list(inspect.signature(McpMixin._load_mcp_servers).parameters)
    assert params == ["db"], f"期望只接受 db 参数，实际 {params}"


def test_main_service_file_stays_small():
    """行数棘轮：主类只做装配（拆分前 781 行）。"""
    lines = len(MAIN_FILE.read_text(encoding="utf-8").splitlines())
    assert lines <= 200, (
        f"langgraph_agent.py 已 {lines} 行（上限 200）：新增服务逻辑请放进 "
        "app/agent/service/ 对应 mixin（run/resume/tool_surface/mcp）"
    )
