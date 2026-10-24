"""维护脚本：归档过期自创技能（只移动到 .archive/，不删除）。

规则：只处理 frontmatter `origin: agent` 的自创技能；pinned 豁免；
最近活动时间取 usage 账本里的 last_used/last_viewed/last_patched，
没有记录则用 SKILL.md 的 mtime。内置/第三方技能目录不在处理范围内。

用法（在 backend/ 下执行）：
    python scripts/dev/prune_skills.py                  # dry-run，只打印
    python scripts/dev/prune_skills.py --stale-days 90  # 自定义阈值
    python scripts/dev/prune_skills.py --apply          # 真正移动
"""

from __future__ import annotations

import argparse
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.skills import AGENT_SKILLS_DIR, skill_usage_snapshot  # noqa: E402
from app.skills.scan import _parse_frontmatter  # noqa: E402


def _iter_skill_dirs(root: Path):
    if not root.is_dir():
        return
    for skill_md in sorted(root.glob("*/*/SKILL.md")):
        yield skill_md.parent, skill_md


def _parse_time(value) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _last_activity(row: dict, skill_md: Path, created_at: str) -> datetime:
    stamps = [
        _parse_time(row.get(key))
        for key in ("last_used_at", "last_viewed_at", "last_patched_at")
    ]
    stamps = [s for s in stamps if s is not None]
    if stamps:
        return max(stamps)
    created = _parse_time(created_at)
    if created is not None:
        return created
    return datetime.fromtimestamp(skill_md.stat().st_mtime, tz=timezone.utc)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--stale-days", type=int, default=90, help="超过多少天未活动视为过期")
    parser.add_argument("--apply", action="store_true", help="真正移动（默认只打印）")
    parser.add_argument("--root", default="", help="自创技能根目录（默认 data/agent_skills）")
    args = parser.parse_args()

    root = Path(args.root).expanduser().resolve() if args.root else Path(AGENT_SKILLS_DIR)
    print(f"自创技能根目录：{root}")
    if not root.is_dir():
        print("目录不存在，无事可做")
        return 0

    usage = skill_usage_snapshot()
    cutoff = datetime.now(timezone.utc) - timedelta(days=args.stale_days)
    stale: list[tuple[Path, str, datetime]] = []
    kept_pinned = 0
    skipped = 0

    for skill_dir, skill_md in _iter_skill_dirs(root):
        try:
            text = skill_md.read_text(encoding="utf-8", errors="ignore")[:4000]
        except OSError as exc:
            print(f"跳过（读取失败）：{skill_dir}（{exc}）")
            skipped += 1
            continue
        name, _, _, extra = _parse_frontmatter(text)
        name = name or skill_dir.name
        if str(extra.get("origin") or "") != "agent":
            skipped += 1
            continue
        row = usage.get(name) or {}
        if row.get("pinned"):
            kept_pinned += 1
            continue
        last = _last_activity(row, skill_md, extra.get("created_at", ""))
        if last < cutoff:
            stale.append((skill_dir, name, last))

    print(
        f"候选 {len(stale)} 个（阈值 {args.stale_days} 天，"
        f"pinned 豁免 {kept_pinned} 个，非自创跳过 {skipped} 个）"
    )
    for skill_dir, name, last in stale:
        print(f"  - {name}（最后活动 {last.isoformat(timespec='seconds')}）：{skill_dir}")

    if not stale:
        return 0
    if not args.apply:
        print("dry-run：未修改磁盘；加 --apply 执行归档")
        return 0

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    archive = root / ".archive" / stamp
    for skill_dir, name, _last in stale:
        dest = archive / skill_dir.relative_to(root)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(skill_dir), str(dest))
        print(f"已归档：{skill_dir} -> {dest}")

    from app.skills import scan_skills

    scan_skills(force=True)
    print(f"完成：归档 {len(stale)} 个技能到 {archive}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
