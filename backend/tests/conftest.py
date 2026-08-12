"""pytest 公共配置：把 backend 根目录加入导入路径。"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
