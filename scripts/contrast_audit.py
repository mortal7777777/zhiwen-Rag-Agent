#!/usr/bin/env python3
"""对比度审计：对"文本色 × 背景色"出 WCAG 对比度并判定是否达标。

知问前端令牌基线（2026-09-30，随 docs/FRONTEND_REDESIGN_PLAN.md 阶段 0 入库）。
不带参数运行 = 跑项目内置的浅/深两套关键组合；改动主题令牌后重跑本脚本核对。

用法：
    python contrast_audit.py                      # 跑内置的项目组合
    python contrast_audit.py "#fff" "#8b909b"     # 单对（背景 文本）
    python contrast_audit.py --file pairs.txt     # 每行 "背景 文本 [标签]"（# 开头的注释行）

阈值（WCAG 2.1 AA）：正文 <18.66px 需 4.5:1；大字号（≥24px，或 ≥18.66px 粗体）需 3.0:1。
"""

from __future__ import annotations

import argparse
import sys


def _srgb_to_linear(c: float) -> float:
    return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4


def relative_luminance(hex_color: str) -> float:
    h = hex_color.strip().lstrip("#")
    if len(h) == 3:
        h = "".join(ch * 2 for ch in h)
    if len(h) != 6:
        raise ValueError(f"无法解析色值：{hex_color!r}")
    r, g, b = (int(h[i : i + 2], 16) / 255 for i in (0, 2, 4))
    return (
        0.2126 * _srgb_to_linear(r)
        + 0.7152 * _srgb_to_linear(g)
        + 0.0722 * _srgb_to_linear(b)
    )


def contrast_ratio(a: str, b: str) -> float:
    la, lb = relative_luminance(a), relative_luminance(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def verdict(ratio: float) -> str:
    if ratio >= 4.5:
        return "正文 OK"
    if ratio >= 3.0:
        return "仅大字号(>=18.66px 粗体/24px)"
    return "不达标"


# 知问令牌的实际组合（2026-09-30 改造后基线；改令牌后重跑核对）
def _project_pairs() -> list[tuple[str, str, str]]:
    return [
        # ---- 浅色 ----
        ("#ffffff", "#23262d", "浅-正文 text-1 on 主区"),
        ("#ffffff", "#4c515b", "浅-正文 text-2 on 主区"),
        ("#ffffff", "#6b7280", "浅-次要文本 text-3 on 主区(12px 小字)"),
        ("#fbfcfe", "#6b7280", "浅-次要文本 text-3 on 侧栏"),
        ("#f7f8fc", "#6b7280", "浅-次要文本 text-3 on 卡片"),
        ("#ffffff", "#0958d9", "浅-主色文字 primary-text on 白"),
        ("#ecf4ff", "#0958d9", "浅-主色文字 on 主色软底"),
        ("#ffffff", "#0f7a4a", "浅-成功文字 success-text"),
        ("#ffffff", "#d4380d", "浅-危险文字 danger-text"),
        ("#fff3e2", "#8a5210", "浅-联网标签"),
        ("#fff7e8", "#8a5210", "浅-警告标签"),
        ("#e8f1ff", "#1f2329", "浅-用户气泡文字"),
        ("#f7f9ff", "#4c515b", "浅-计划块文字"),
        # ---- 深色 ----
        ("#1a1d24", "#e6e8ec", "深-正文 text-1 on 主区"),
        ("#14161b", "#e6e8ec", "深-正文 text-1 on 侧栏"),
        ("#1a1d24", "#c4c9d2", "深-正文 text-2 on 主区"),
        ("#20242d", "#c4c9d2", "深-正文 text-2 on 卡片"),
        ("#1a1d24", "#858c98", "深-次要文本 text-3 on 主区"),
        ("#171a20", "#858c98", "深-次要文本 text-3 on 侧栏"),
        ("#20242d", "#858c98", "深-次要文本 text-3 on 卡片"),
        ("#1a1d24", "#4a94ff", "深-主色文字 on 主区"),
        ("#1b2942", "#4a94ff", "深-主色文字 on 选中底"),
        ("#1a1d24", "#4bd08a", "深-成功文字"),
        ("#1a1d24", "#f78989", "深-危险文字"),
        ("#3a2f1f", "#e8a33d", "深-联网标签"),
        ("#1e3a5f", "#dbe6f5", "深-用户气泡文字"),
        ("#1b2233", "#c4c9d2", "深-计划块文字"),
    ]


def _looks_like_hex(token: str) -> bool:
    """首字段是否像色值（#abc / #aabbcc / 无 # 的 6 位十六进制）。

    不能简单用 line.startswith("#") 判注释：色值行本身就以 # 开头
    （#ffffff #23262d 标签），会被整行误跳过。
    """
    t = token.lstrip("#")
    return len(t) in (3, 4, 6, 8) and all(c in "0123456789abcdefABCDEF" for c in t)


def _load_file(path: str) -> list[tuple[str, str, str]]:
    pairs: list[tuple[str, str, str]] = []
    with open(path, encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            # "# 注释" 与 "#ffffff 色值行" 要区分开
            if line.startswith("#") and not _looks_like_hex(line.split()[0]):
                continue
            parts = line.split()
            if len(parts) < 2:
                print(f"跳过第 {lineno} 行（需要 '背景 文本'）：{line}", file=sys.stderr)
                continue
            pairs.append((parts[0], parts[1], " ".join(parts[2:]) or f"line {lineno}"))
    return pairs


def main() -> int:
    ap = argparse.ArgumentParser(description="WCAG 对比度审计")
    ap.add_argument("bg", nargs="?", help="背景色（与 fg 同时给出）")
    ap.add_argument("fg", nargs="?", help="文本色（与 bg 同时给出）")
    ap.add_argument("--file", help="批量文件：每行 '背景 文本 [标签]'")
    args = ap.parse_args()

    if args.file:
        pairs = _load_file(args.file)
        if not pairs:
            print("警告：pairs 文件未解析出任何组合，请检查格式", file=sys.stderr)
            return 2
    elif args.bg and args.fg:
        pairs = [(args.bg, args.fg, f"{args.fg} on {args.bg}")]
    else:
        pairs = _project_pairs()
        print("(未给参数，跑项目内置组合)")

    width = max(len(label) for _, _, label in pairs)
    failures = 0
    print(f"{'组合':<{width}}  {'对比度':>7}  判定")
    for bg, fg, label in pairs:
        ratio = contrast_ratio(bg, fg)
        note = verdict(ratio)
        if ratio < 4.5:
            failures += 1
        print(f"{label:<{width}}  {ratio:>7.2f}  {note}")
    print(f"\n{len(pairs)} 组中 {failures} 组不足以用作正文（<4.5:1）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
