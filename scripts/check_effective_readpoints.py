#!/usr/bin/env python3
"""守门：EDITABLE_KEYS 的读点必须走 effective() / effective_or()。

背景：effective() 的优先级是 DB 覆盖 > settings 属性；直接 `settings.<key>`
会静默忽略设置页保存的值。`data_dir` 是唯一例外——save_overrides() 会把
它原地写回全局 settings 单例（见 runtime_config._apply_data_dir_override）。

CI 与 pre-commit 都跑；发现新违规时给出 file:line，便于直接修。
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "backend" / "app"
RUNTIME_CONFIG = APP / "runtime_config.py"

# 有意直读的键（附原因）。新增例外请写清楚为什么 effective 不适用。
ALLOW_DIRECT = {
    "data_dir": "save_overrides 会把覆盖值原地写回 settings 单例",
}


def _editable_keys() -> set[str]:
    tree = ast.parse(RUNTIME_CONFIG.read_text(encoding="utf-8"))
    keys: set[str] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.AnnAssign)
            and getattr(node.target, "id", "") == "_SPECS"
            and isinstance(node.value, ast.Dict)
        ):
            keys |= {
                k.value
                for k in node.value.keys
                if isinstance(k, ast.Constant) and isinstance(k.value, str)
            }
        if (
            isinstance(node, ast.Assign)
            and any(
                getattr(t, "id", "") == "_EDITABLE_LEGACY" for t in node.targets
            )
            and isinstance(node.value, ast.Set)
        ):
            keys |= {
                e.value
                for e in node.value.elts
                if isinstance(e, ast.Constant) and isinstance(e.value, str)
            }
    return keys


def _settings_attr(node: ast.AST) -> str | None:
    """识别 settings.key / self.settings.key / service.settings.key。"""
    if not isinstance(node, ast.Attribute):
        return None
    base = node.value
    if isinstance(base, ast.Name) and base.id in ("settings", "_settings"):
        return node.attr
    if isinstance(base, ast.Attribute) and base.attr == "settings":
        return node.attr
    return None


def _effective_keys(tree: ast.AST) -> set[str]:
    keys: set[str] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in ("effective", "effective_or")
            and len(node.args) >= 2
            and isinstance(node.args[1], ast.Constant)
            and isinstance(node.args[1].value, str)
        ):
            keys.add(node.args[1].value)
    return keys


def main() -> int:
    keys = _editable_keys()
    problems: list[tuple[str, int, str]] = []
    for path in sorted(APP.rglob("*.py")):
        if path.name in ("runtime_config.py", "config.py"):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        effective = _effective_keys(tree)
        for node in ast.walk(tree):
            key = _settings_attr(node)
            if key in keys and key not in effective and key not in ALLOW_DIRECT:
                problems.append((path.relative_to(ROOT).as_posix(), node.lineno, key))

    if problems:
        print(
            "[check_effective_readpoints] 以下读点绕过 effective()，"
            "设置页保存不会生效："
        )
        for rel, line, key in problems:
            print(f"  {rel}:{line}  settings.{key}")
        print(
            "修法：改为 effective(settings, key) / effective_or(...)；"
            "确需例外请加进 ALLOW_DIRECT。"
        )
        return 1

    print(
        f"[check_effective_readpoints] OK"
        f"（{len(keys)} 个可调键的读点均走 effective）"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
