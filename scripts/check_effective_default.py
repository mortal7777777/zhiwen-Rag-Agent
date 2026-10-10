#!/usr/bin/env python3
"""effective() 误用守门：禁止传第三个参数（default）。

背景（P1-2，2026-10-08）：`effective(settings, key, default)` 里传 default 会
**静默忽略 settings 属性**（即环境变量与 config.py 默认值全部失效，只剩设置页
的 DB 覆盖能生效）。实测有 14 处这样写，其中 9 个键的部署配置通道是断的，还
造成 `/api/advanced` 的受控执行开关读不到默认值 True。

规则：需要兜底常量时用 `effective_or(settings, key, fallback)`（顺序：DB 覆盖
> settings 属性 > fallback），语义写在名字里。

用法：python scripts/check_effective_default.py
退出码：0 = 通过；1 = 发现误用（CI / pre-commit 拦截）。
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent / "backend" / "app"


def _find_violations() -> list[tuple[str, int, str]]:
    """扫描 backend/app 下带 default 的 effective() 调用。"""
    bad: list[tuple[str, int, str]] = []
    for path in sorted(ROOT.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError):
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "id", "") or getattr(node.func, "attr", "")
            if name != "effective":  # effective_or 是正确写法，不拦
                continue
            if len(node.args) >= 3 or any(k.arg == "default" for k in node.keywords):
                bad.append(
                    (
                        str(path.relative_to(ROOT.parent.parent)),
                        node.lineno,
                        ast.unparse(node)[:90],
                    )
                )
    return bad


def main() -> int:
    bad = _find_violations()
    if not bad:
        print("effective() 用法检查通过：未发现带 default 的调用。")
        return 0
    print("失败：以下 effective() 调用传了 default，会忽略环境变量与 config 默认值；")
    print("请改用 effective_or(settings, key, fallback)（兜底常量放第三个参数）：")
    for path, lineno, snippet in bad:
        print(f"  {path}:{lineno}  {snippet}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
