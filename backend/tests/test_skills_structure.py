"""`app.skills` 包化拆分契约（2026-10-10，原 app/skills.py 808 行 → skills/ 包）。

镜像 rag/agent/context 结构测试，另加一条本包特有的血泪教训：
**路径常量的单一权威位置**——包化初版让 agent_skills.py 值导入 AGENT_SKILLS_DIR /
scan_skills 等，导致测试 `monkeypatch.setattr(S.scan, "AGENT_SKILLS_DIR", ...)`
无法穿透到 agent_skills 的旧绑定（4 个用例失败）。修复方式是 agent_skills.py
经 `from . import scan as scan_mod` 动态引用；本文件锁住这个模式。
"""

from __future__ import annotations

import ast
import importlib
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
APP_DIR = BACKEND / "app"
SKILLS_DIR = APP_DIR / "skills"

PACKAGE = "app.skills"
MODULES = ["prefs", "scan", "agent_skills", "structure", "sanitize", "catalog"]

# 历史导入面（api/skills.py、agent/tools.py、tests 用）：必须仍可从此包导入
EXPECTED_EXPORTS = [
    "scan_skills", "search_skills", "load_prefs", "save_prefs", "reset_prefs",
    "share_agent_skill", "unshare_agent_skill", "delete_agent_skill", "agent_skill_content",
    "sanitize_skill_text", "skill_structure", "build_skill_catalog",
    "matching_skills_for_injection", "AGENT_SKILLS_DIR", "HERMES_SKILLS_DIR", "AGENT_SOURCE",
    "DEFAULT_ROOTS", "DEFAULT_ENABLED", "_cache", "_scanned_at", "_parse_frontmatter",
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


def test_modules_exist():
    for module in MODULES:
        path = SKILLS_DIR / f"{module}.py"
        assert path.exists(), f"缺少 skills/{module}.py"
        importlib.import_module(f"{PACKAGE}.{module}")


def test_old_module_is_gone():
    assert not (APP_DIR / "skills.py").exists(), (
        "app/skills.py 又出现了：包与模块并存会让导入来源随环境漂移"
    )


def test_relative_imports_resolve():
    """prefs.py 的 load/save 在函数体内有 `from ..db import repository`（层级 +1 的坑）。"""
    broken: list[str] = []
    for path in sorted(SKILLS_DIR.glob("*.py")):
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


def test_path_constants_have_single_canonical_home():
    """agent_skills 必须经 scan_mod 动态引用（否则 monkeypatch S.scan.* 穿不透）。"""
    src = (SKILLS_DIR / "agent_skills.py").read_text(encoding="utf-8")
    assert "from . import scan as scan_mod" in src, "agent_skills.py 应经 scan_mod 引用 scan"
    for banned in ["from .scan import"]:
        assert banned not in src, (
            f"agent_skills.py 出现值导入 `{banned}`：路径/函数会形成第二份绑定，"
            "测试对 S.scan.* 的 monkeypatch 将无法穿透"
        )
    assert "scan_mod.AGENT_SKILLS_DIR" in src and "scan_mod.scan_skills" in src


def test_namespace_exports_survive():
    pkg = importlib.import_module(PACKAGE)
    missing = [n for n in EXPECTED_EXPORTS if not hasattr(pkg, n)]
    assert not missing, f"包化后丢失命名空间导出：{missing}"


def test_assembly_stays_thin():
    lines = len((SKILLS_DIR / "__init__.py").read_text(encoding="utf-8").splitlines())
    assert lines <= 140, (
        f"skills/__init__.py 已 {lines} 行（上限 140，含 re-export 块）："
        "新增逻辑请放进对应子模块（prefs/scan/agent_skills/structure/sanitize/catalog）"
    )
