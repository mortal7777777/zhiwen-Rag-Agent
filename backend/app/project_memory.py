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


def load_project_memory(
    settings,
    override_path: str | None = None,
    max_chars: int = 8000,
) -> str | None:
    """按 CLAUDE.md 语义加载项目记忆：

    override_path 存在时（CLI 启动目录）：从该目录逐级向上找 AGENTS.md，
    再追加用户级 ~/.myragagent/AGENTS.md；否则用项目根 AGENTS.md + 用户级。
    """
    paths: list[Path] = []
    if override_path:
        directory = Path(override_path).expanduser().resolve()
        if directory.is_file():
            directory = directory.parent
        current = directory
        while True:
            candidate = current / "AGENTS.md"
            if candidate.exists():
                paths.append(candidate)
            if current.parent == current:
                break
            current = current.parent
    else:
        default = project_memory_path(settings)
        if default.exists():
            paths.append(default)
    user_global = Path.home() / ".myragagent" / "AGENTS.md"
    if user_global.exists():
        paths.append(user_global)

    blocks: list[str] = []
    seen: set[str] = set()
    for path in paths:
        key = str(path.resolve())
        if key in seen:
            continue
        seen.add(key)
        try:
            text = path.read_text(encoding="utf-8", errors="ignore").strip()
        except Exception as exc:
            logger.warning("读取项目记忆失败 %s：%s", path, exc)
            continue
        if text:
            blocks.append(f"[项目记忆：{path}]\n{text[:4000]}")
    if not blocks:
        return None
    return ("\n\n".join(blocks))[:max_chars] or None


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
        # 内容无变化不写盘：AGENTS.md 每轮都会导出，重写相同字节
        # 会改变项目记忆 SystemMessage（缓存前缀的一部分），导致
        # 跨 run 首调整个历史前缀失效
        if path.exists():
            try:
                existing = path.read_text(encoding="utf-8")
            except Exception:
                existing = None
            if existing == content:
                return {"ok": True, "path": str(path), "unchanged": True}
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        logger.info("项目记忆已导出：%s", path)
        return {"ok": True, "path": str(path), "content": content}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
