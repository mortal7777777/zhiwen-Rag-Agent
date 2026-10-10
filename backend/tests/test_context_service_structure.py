"""`ContextService` 包化拆分契约（2026-10-10，原 app/agent/context.py 931 行 → context/ 包）。

镜像 rag/agent 两侧结构测试的契约：
1. 相对导入（含方法体里的局部导入）必须真能解析——本次包化踩过
   compact.py 使用 estimate_tokens 未从 base 导入的 NameError；
2. mixin 之间不得互相 import，只允许依赖 base；
3. **旧模块命名空间兼容**：历史测试用 `context_mod.repo`（monkeypatch 锚点）与
   `context_mod._memory_vec_cache`（夹具清理锚点），包必须继续暴露这两个属性；
4. 对外契约方法名保持 + 装配文件保持薄（行数棘轮）。
"""

from __future__ import annotations

import ast
import importlib
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
AGENT_DIR = BACKEND / "app" / "agent"
SERVICE_DIR = AGENT_DIR / "context"

PACKAGE = "app.agent.context"
MIXINS = {
    "base": "ContextServiceBase",
    "compact": "CompactMixin",
    "memory": "MemoryMixin",
}

EXPECTED_METHODS = [
    "compact_conversation",
    "context_stats",
    "compact_now",
    "history_budget_for",
    "retrieve_memories",
    "get_memory_summary",
    "has_memory_intent",
    "extract_facts",
    "add_facts",
    "maybe_consolidate",
    "consolidate_memories",
]


def _relative_imports(path: Path) -> list[tuple[int, ast.ImportFrom]]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return [
        (node.lineno, node)
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.level
    ]


def _resolve(package: str, node: ast.ImportFrom) -> str:
    parts = package.split(".")
    base = parts[: len(parts) - (node.level - 1)] if node.level > 1 else parts
    if node.module:
        base = base + node.module.split(".")
    return ".".join(base)


def test_modules_exist_with_expected_mixins():
    for module, cls_name in MIXINS.items():
        path = SERVICE_DIR / f"{module}.py"
        assert path.exists(), f"缺少 context/{module}.py"
        module_obj = importlib.import_module(f"{PACKAGE}.{module}")
        assert hasattr(module_obj, cls_name), f"context/{module}.py 缺少 {cls_name}"


def test_old_module_is_gone():
    assert not (AGENT_DIR / "context.py").exists(), (
        "app/agent/context.py 又出现了：包与模块并存会让导入来源随环境漂移"
    )


def test_relative_imports_inside_modules_resolve():
    broken: list[str] = []
    for path in sorted(SERVICE_DIR.glob("*.py")):
        if path.name == "__init__.py":
            continue
        for lineno, node in _relative_imports(path):
            target = _resolve(PACKAGE, node)
            try:
                importlib.import_module(target)
            except Exception as exc:  # noqa: BLE001
                broken.append(
                    f"{path.name}:{lineno} {'.' * node.level}{node.module or ''} -> {target} ({exc})"
                )
    assert not broken, "相对导入无法解析：\n" + "\n".join(broken)


def test_mixins_do_not_import_each_other():
    problems: list[str] = []
    for path in sorted(SERVICE_DIR.glob("*.py")):
        name = path.stem
        if name in ("__init__", "base"):
            continue
        for lineno, node in _relative_imports(path):
            if node.level != 1:
                continue
            target = (node.module or "").split(".")[0]
            if target in MIXINS and target not in (name, "base"):
                problems.append(f"{path.name}:{lineno} 导入了兄弟 mixin {target}")
    assert not problems, "\n".join(problems)


def test_legacy_namespace_anchors_survive():
    """历史测试的补丁锚点：context_mod.repo 与 context_mod._memory_vec_cache。"""
    pkg = importlib.import_module(PACKAGE)
    assert hasattr(pkg, "repo"), "包缺少 repo 兼容锚点（tests monkeypatch context_mod.repo）"
    assert hasattr(pkg, "_memory_vec_cache"), "包缺少 _memory_vec_cache 兼容锚点（测试夹具清理用）"
    assert hasattr(pkg, "ContextService") and hasattr(pkg, "ConsolidateOutput")
    assert hasattr(pkg, "context_window_for") and hasattr(pkg, "trim_history_for_budget")


def test_composed_service_keeps_public_methods():
    from app.agent.context import ContextService

    missing = [m for m in EXPECTED_METHODS if not hasattr(ContextService, m)]
    assert not missing, f"拆分后丢失了这些方法：{missing}"
    names = {c.__name__ for c in ContextService.__mro__}
    for cls_name in MIXINS.values():
        assert cls_name in names, f"{cls_name} 未进入 MRO"


def test_assembly_stays_thin():
    lines = len((SERVICE_DIR / "__init__.py").read_text(encoding="utf-8").splitlines())
    assert lines <= 80, (
        f"context/__init__.py 已 {lines} 行（上限 80）：新增逻辑请放进对应 mixin"
        "（compact / memory，或共享基座 base）"
    )
