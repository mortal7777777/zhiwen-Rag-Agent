"""维护脚本：裁剪原生检查点（每个线程只保留最近 N 条快照）并 VACUUM 回收空间。

背景：原生 saver 每个 superstep 存一条含完整 messages 的快照，长会话会让
opensearch_meta/checkpoints.sqlite 单调增长（实测 700MB+）。finalize 收尾时
会自动裁剪单个线程；本脚本用于对历史数据做一次全库清理。

用法（在 backend/ 下执行）：
    python scripts/dev/prune_checkpoints.py            # 默认保留最近 20 条/线程
    python scripts/dev/prune_checkpoints.py --keep 50  # 自定义保留数
注意：脚本直接写 sqlite，请先停掉后端（避免与运行中的 saver 争锁）。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.config import get_settings  # noqa: E402
from app.native_checkpoint import build_saver, prune_all, vacuum  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--keep", type=int, default=20, help="每线程保留的快照数")
    parser.add_argument("--no-vacuum", action="store_true", help="不执行 VACUUM")
    args = parser.parse_args()

    settings = get_settings()
    db_path = settings.meta_dir / "checkpoints.sqlite"
    print(f"库文件：{db_path}")
    print(f"清理前大小：{db_path.stat().st_size / 1e6:.1f} MB")

    saver, conn = build_saver(settings.meta_dir)
    stats = prune_all(saver, keep=args.keep)
    print(f"线程数 {stats['threads']}，删除快照 {stats['deleted']} 条")

    if not args.no_vacuum:
        print("VACUUM 回收空间（大库可能耗时数十秒）…")
        vacuum(saver)
    conn.close()
    print(f"清理后大小：{db_path.stat().st_size / 1e6:.1f} MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
