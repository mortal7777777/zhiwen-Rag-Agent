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

# 导出分节：(小节标题, 归入的 category, 该节条数配额)
# 配额制替代"按更新时间全局截断 30 条"——记忆多起来时，重要的旧决策
# 不会被新加入的琐事挤出文件。
_EXPORT_SECTIONS: list[tuple[str, tuple[str, ...], int]] = [
    ("项目与工作", ("project", "decision"), 16),
    ("偏好与习惯", ("preference", "profile"), 8),
    ("经验与教训", ("lesson",), 6),
    ("其他", ("other",), 4),
]
# 单条记忆在文件里的显示上限（超长截断，保持"一行一条"的可读性）
_LINE_MAX = 200
# 人工维护区：该区由人写，导出时原样保留（起止标记成对出现）
_MANUAL_MARKER = "## 人工维护"
_MANUAL_END = "<!-- /人工维护（以上内容导出时原样保留，以下为自动生成） -->"
_MANUAL_HINT = (
    "（这一节由你手工维护，导出时不会被覆盖。适合写常驻规则、项目约定、"
    "外部约束这类不需要语义检索的说明；会被完整注入每次会话的上下文。）"
)


def _find_line(text: str, needle: str, start: int = 0) -> int:
    """找 needle 作为整行出现的位置（避免被正文里顺带提及而误匹配）。"""
    pos = start
    while True:
        pos = text.find(needle, pos)
        if pos < 0:
            return -1
        if pos == 0 or text[pos - 1] == "\n":
            return pos
        pos += len(needle)


def _manual_section(existing: str | None) -> str:
    """取出既有文件里的人工维护区（含起止标记）；没有则给出带说明的空节。

    人工维护区排在文件靠前位置（加载端有字符截断，手写规则最不该被切掉），
    因此不能用"标记到文件末尾"来界定——必须靠结束标记，或在旧文件里
    退化为"到下一个二级标题为止"。匹配要求标记独占一行。
    """
    default = f"{_MANUAL_MARKER}\n{_MANUAL_HINT}\n\n{_MANUAL_END}"
    if not existing:
        return default
    idx = _find_line(existing, _MANUAL_MARKER)
    if idx < 0:
        return default
    rest = existing[idx:]
    end = _find_line(rest, _MANUAL_END)
    if end >= 0:
        return rest[: end + len(_MANUAL_END)].rstrip()
    # 旧格式（无结束标记）：截到下一个二级标题
    lines = rest.splitlines()
    keep = [lines[0]]
    for line in lines[1:]:
        if line.startswith("## "):
            break
        keep.append(line)
    return "\n".join(keep).rstrip() + "\n\n" + _MANUAL_END


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
    再追加用户级 ~/.zhiwen/AGENTS.md；否则用项目根 AGENTS.md + 用户级。
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
    user_global = Path.home() / ".zhiwen" / "AGENTS.md"
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
            # 文件是完整文档，注入的是节选：按行边界截断并标注（硬切会切断
            # 条目，也让模型以为内容到此为止），控制在 project_memory_max_chars
            limit = int(getattr(settings, "project_memory_max_chars", 0) or 0) or 4000
            if len(text) > limit:
                cut = text.rfind("\n", 0, limit)
                text = text[: cut if cut > 0 else limit].rstrip()
                text += "\n…（项目记忆较长，此处按行截断；完整内容见该文件）"
            blocks.append(f"[项目记忆：{path}]\n{text}")
    if not blocks:
        return None
    return ("\n\n".join(blocks))[:max_chars] or None


def export_project_memory(settings, db) -> dict:
    """导出 AGENTS.md：人工维护区 + 用户画像 + 分节记忆。

    结构对齐 Claude Code 的 CLAUDE.md 语义——文件即文档、人工可维护：
    - `## 人工维护` 一节由人写，导出时**原样保留**（排在靠前，避免被
      加载端的字符截断切掉）；适合写常驻规则、项目约定这类不需要检索的说明；
    - 其余部分由记忆库生成：画像 + 按类别分节的记忆条目（每节有配额，
      不再"按时间截断 30 条"，重要的旧决策不会被新琐事挤掉）。

    注意：曾导出"最近任务经验（最近 5 个 run）"，那段每次 run 后必变，
    让项目记忆每轮都不同 → 前缀缓存每轮失效、文件频繁重写，已移除。
    """
    summary = None
    try:
        summary = repo.get_meta(db, "memory_summary")
    except Exception:
        pass
    try:
        memories = repo.list_memories(db, limit=300)
    except Exception as exc:
        logger.warning("读取长期记忆失败：%s", exc)
        memories = []

    path = project_memory_path(settings)
    existing: str | None = None
    if path.exists():
        try:
            existing = path.read_text(encoding="utf-8")
        except Exception:
            existing = None
    manual = _manual_section(existing)

    parts: list[str] = [
        "# AGENTS.md",
        "",
        "> 本文件由 AI 助手自动维护；「人工维护」一节由人维护、导出时原样保留。"
        "每次会话会自动加载进上下文。",
        "",
        manual,
        "",
        "## 用户画像",
        summary or "（暂无画像摘要，可先通过对话积累）",
    ]
    by_category: dict[str, list[dict]] = {}
    for m in memories:
        by_category.setdefault(str(m.get("category") or "other"), []).append(m)
    for title, categories, quota in _EXPORT_SECTIONS:
        picked: list[dict] = []
        for category in categories:
            picked.extend(by_category.get(category, []))
        picked.sort(key=lambda m: str(m.get("updated_at") or ""), reverse=True)
        picked = picked[:quota]
        parts += ["", f"## {title}"]
        if not picked:
            parts.append("（暂无）")
            continue
        for m in picked:
            text = " ".join(str(m.get("content") or "").split())
            if len(text) > _LINE_MAX:
                text = text[:_LINE_MAX] + "…"
            parts.append(f"- {text}")

    content = "\n".join(parts)
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
