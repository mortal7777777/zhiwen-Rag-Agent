"""一次性维护脚本：把被误归档的长期记忆恢复为 active。

背景：2026-09-30 之前，整合提示词含"删除琐碎无用的"+ 按命中次数优先归档，
实测把 46 条压成 2 条（44 条归档，其中 33 条曾被召回），召回层只剩两个混着
多主题的大块。提示词与时间字段语义已修复（见 e505904），这里把历史误伤恢复。

安全：先写变更前快照到 opensearch_meta/memory_audit/restore-YYYY-MM-DD.jsonl，
再改状态；只动 status=archived 的行，不碰内容。

用法：
    cd backend
    python scripts/dev/restore_archived_memories.py --dry-run
    python scripts/dev/restore_archived_memories.py --apply
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BACKEND))

# 与 run.py 一致：backend/.env.local 提供 MYSQL_URL 等（不打印值）
_envf = BACKEND / ".env.local"
if _envf.exists():
    for line in _envf.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))

import logging  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

from sqlalchemy import select  # noqa: E402

from app.db import database  # noqa: E402
from app.db.models import Memory  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="恢复被误归档的长期记忆")
    parser.add_argument("--apply", action="store_true", help="真正写库（默认只预览）")
    parser.add_argument("--dry-run", action="store_true", help="只预览，不写库")
    args = parser.parse_args()
    apply = args.apply and not args.dry_run

    database.init_db()
    if not database.db_ready:
        print("MySQL 未连接，检查 backend/.env.local 的 MYSQL_URL")
        return 2
    db = database.SessionLocal()
    try:
        archived = (
            db.execute(select(Memory).where(Memory.status == "archived").order_by(Memory.id))
            .scalars()
            .all()
        )
        active_before = db.execute(
            select(Memory).where(Memory.status == "active")
        ).scalars().all()
        print(f"当前：活跃 {len(active_before)} 条 / 归档 {len(archived)} 条")
        if not archived:
            print("没有需要恢复的归档记忆")
            return 0

        snapshot = [
            {
                "id": m.id,
                "status": m.status,
                "category": m.category,
                "hit_count": m.hit_count or 0,
                "created_at": m.created_at.isoformat() if m.created_at else None,
                "content": m.content,
            }
            for m in archived
        ]
        with_hits = [m for m in archived if (m.hit_count or 0) > 0]
        print(f"其中曾被召回过的：{len(with_hits)} 条")
        for m in archived[:5]:
            print(f"  #{m.id} [{m.category}] hit={m.hit_count or 0} {str(m.content)[:60]}")
        if len(archived) > 5:
            print(f"  …（共 {len(archived)} 条）")

        # 快照先落盘：恢复本身可逆，但审计要有据可查
        audit_dir = Path(os.environ.get("META_DIR") or (BACKEND.parent / "opensearch_meta"))
        audit_dir = audit_dir / "memory_audit"
        audit_dir.mkdir(parents=True, exist_ok=True)
        audit_path = audit_dir / f"restore-{datetime.now():%Y-%m-%d}.jsonl"
        record = {
            "ts": datetime.now().isoformat(),
            "action": "restore_archived_memories",
            "applied": bool(apply),
            "restored_ids": [m.id for m in archived],
            "before": snapshot,
        }

        if not apply:
            print(f"\n[dry-run] 将恢复 {len(archived)} 条为 active；快照会写入 {audit_path}")
            return 0

        with open(audit_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

        for m in archived:
            m.status = "active"
            # 不写 updated_at：恢复是管理动作，不是内容编辑。写上会让 44 行并列
            # 同一时间戳，排序失去区分度（updated_at 的语义见 Memory 模型注释）。
        db.commit()

        active_after = db.execute(
            select(Memory).where(Memory.status == "active")
        ).scalars().all()
        print(f"\n已恢复：活跃 {len(active_before)} → {len(active_after)} 条")
        print(f"变更前快照：{audit_path}")

        # 同步 AGENTS.md（导出按分节配额取 active，恢复后文件才反映真实记忆）
        try:
            from app.config import get_settings
            from app.project_memory import export_project_memory

            result = export_project_memory(get_settings(), db)
            print(f"AGENTS.md 导出：{result}")
        except Exception as exc:
            print(f"AGENTS.md 导出失败（可稍后手动触发）：{exc}")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
