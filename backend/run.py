"""开发启动脚本：在 backend 目录下执行 python run.py。

启动前依次读取：
1. 当前进程已存在的环境变量；
2. backend/.env.local（gitignored，用于存放本机 MySQL 凭据等）。
"""

import os
from pathlib import Path

import uvicorn


def load_local_env() -> None:
    """从 .env.local 加载本机配置（不覆盖已存在的环境变量）。"""
    env_file = Path(__file__).resolve().parent / ".env.local"
    if not env_file.exists():
        return
    for raw in env_file.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


if __name__ == "__main__":
    load_local_env()
    uvicorn.run(
        "app.main:app",
        host="127.0.0.1",
        port=8000,
        reload=True,
        # 只对服务端代码热重载：CLI 客户端与日志在 backend 目录内，
        # 保存不应触发服务重启（重启会卸载 CUDA 模型，首轮检索慢 30~60s）
        reload_excludes=[
            "cli_agent.py",
            "*.log",
            ".env.local",
            "tests/*",
            "__pycache__/*",
        ],
    )
