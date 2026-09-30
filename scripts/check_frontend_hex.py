#!/usr/bin/env python3
"""裸 hex 防回退：统计 frontend/src 的 .vue 文件里的裸十六进制颜色，超过基线即失败。

背景（docs/FRONTEND_REDESIGN_PLAN.md 阶段 2）：颜色若不集中管理，会被组件反复
手抄，最终出现"两种蓝、两套灰"（改造前实测 EP 调色板被手抄 30+ 处）。主题令牌
集中在 frontend/src/styles/theme.css；组件一律用 var(--token)。

有意保留的字面色（计入基线，勿再新增）：
  - App.vue 4：主题预览小卡（要固定描绘"浅色/深色"两套界面，不能跟当前主题走）；
  - MarkdownContent.vue 12：代码块暗色主题常量（两种主题下都刻意保持暗色块）；
  - DocxViewer.vue / PdfViewer.vue 各 1："白纸"页面画布底色；
  - RunsView.vue 5：图表 JS 里读取令牌失败时的兜底色。

基线更新：确属有意例外时，在提交说明里写明原因并同步 BASELINE。
用法：python scripts/check_frontend_hex.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

BASELINE = 23  # 2026-09-30 阶段 2 完成时存量（见文件头）

HEX = re.compile(r"#[0-9a-fA-F]{3,8}\b")


def main() -> int:
    root = Path(__file__).resolve().parent.parent / "frontend" / "src"
    rows: list[tuple[str, int]] = []
    for f in sorted(root.rglob("*.vue")):
        n = len(HEX.findall(f.read_text(encoding="utf-8")))
        if n:
            rows.append((str(f.relative_to(root)).replace("\\", "/"), n))
    total = sum(n for _, n in rows)
    print(f"裸 hex 统计（不含 styles/theme.css）：{total} / 基线 {BASELINE}")
    for name, n in rows:
        print(f"  {n:>3}  {name}")
    if total > BASELINE:
        print(
            "失败：裸 hex 比基线多。新增颜色请用 var(--token)"
            "（令牌见 frontend/src/styles/theme.css）；\n"
            "若确属有意例外，同步本脚本的 BASELINE 并在提交说明里写明原因。"
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
