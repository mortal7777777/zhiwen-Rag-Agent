"""文件型项目记忆（AGENTS.md）：把画像摘要与长期记忆导出为可编辑文件。

类似 Claude Code 的 CLAUDE.md / Codex 的 AGENTS.md：
- 自动导出：记忆整合后、或用户手动触发；
- prepare 节点注入：文件存在时作为 SystemMessage 加载进上下文；
- 文件可人工编辑、可随项目迁移。
"""

from __future__ import annotations

import logging
from pathlib import Path

from .db import repository as repo

logger = logging.getLogger(__name__)


def project_memory_path(settings) -> Path:
    root = (settings.project_memory_file or "").strip()
    if root:
        return Path(root).expanduser().resolve()
    return Path(__file__).resolve().parents[2] / "AGENTS.md"  # rag_knowledge_base/AGENTS.md


def load_project_memory(settings, override_path: str | None = None) -> str | None:
    """读取项目记忆文件；override_path 指向 CLI 启动目录下的 AGENTS.md。"""
    if override_path:
        path = Path(override_path).expanduser().resolve()
        if path.is_dir():
            path = path / "AGENTS.md"
    else:
        path = project_memory_path(settings)
    try:
        if not path.exists():
            return None
        text = path.read_text(encoding="utf-8", errors="ignore").strip()
        return text[:4000] if text else None
    except Exception as exc:
        logger.warning("读取项目记忆失败：%s", exc)
        return None


def export_project_memory(settings, db) -> dict:
    """导出 AGENTS.md：用户画像 + 活跃记忆（前 30 条）+ 最近任务经验。"""
    lines: list[str] = [
        "# AGENTS.md",
        "",
        "> 本文件由 AI 助手自动维护，可人工编辑；每次会话会自动加载进上下文。",
        "",
        "## 用户画像",
    ]
    summary = None
    try:
        summary = repo.get_meta(db, "memory_summary")
    except Exception:
        pass
    lines.append(summary or "（暂无画像摘要，可先通过对话积累）")

    lines += ["", "## 长期事实记忆"]
    try:
        memories = repo.list_memories(db, limit=30)
        if memories:
            for m in memories:
                lines.append(f"- [{m.get('category', 'other')}] {m.get('content', '')}")
        else:
            lines.append("（暂无）")
    except Exception as exc:
        lines.append(f"（读取失败：{exc}）")

    lines += ["", "## 最近任务经验"]
    try:
        runs = repo.list_agent_runs(db, limit=5)
        if runs:
            for run in runs:
                question = (run.get("question") or "")[:60]
                status = run.get("status") or ""
                lines.append(f"- {question}（{status}）")
        else:
            lines.append("（暂无运行记录）")
    except Exception as exc:
        lines.append(f"（读取失败：{exc}）")

    content = "\n".join(lines)
    path = project_memory_path(settings)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        logger.info("项目记忆已导出：%s", path)
        return {"ok": True, "path": str(path), "content": content}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
