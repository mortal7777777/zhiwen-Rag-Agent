"""技能 · 扫描与检索（从 app/skills.py 拆出，2026-10-10）。

多来源目录发现（codex/agent/hermes/claude）、frontmatter 解析、分组归类、
TTL 扫描缓存与中英检索。"""


from __future__ import annotations

import json
import logging
import os
import re
import shutil
import sys
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)

from .prefs import _get_cached_prefs

_PROJECT_ROOT = Path(__file__).resolve().parents[2]


AGENT_SKILLS_DIR = Path(os.environ.get("DATA_DIR") or (_PROJECT_ROOT / "data")) / "agent_skills"


HERMES_SKILLS_DIR = Path(os.path.expanduser("~/.hermes/skills"))


AGENT_SOURCE = "agent"


def _version_sort_key(path: str) -> tuple:
    """按数字版本排序（2.10.0 > 2.2.3），非数字版本（unknown）用 mtime 兜底。

    不能用字符串排序：`2.2.3` > `2.10.0`，升级后反而会扫到旧版缓存。
    """
    version = Path(path).parent.name
    numbers = tuple(int(part) for part in re.findall(r"\d+", version))
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        mtime = 0.0
    return numbers, mtime


def _expand_plugin_skill_root(rel_glob: str) -> str | None:
    """把 ~/.claude/plugins/cache/<glob> 展开为实际存在的技能根目录。

    插件缓存路径含版本号（如 ecc/ecc/2.0.0-rc.1），版本升级会变；
    按数字版本取最新，返回 None 表示未找到（扫描时跳过）。
    """
    import glob

    base = os.path.expanduser("~/.claude/plugins/cache")
    matches = glob.glob(os.path.join(base, rel_glob))
    if not matches:
        return None
    return max(matches, key=_version_sort_key)


DEFAULT_ROOTS = [
    # 知问自创技能（write_skill 写入；用户可共享到 Hermes 个人层）
    (AGENT_SOURCE, str(AGENT_SKILLS_DIR), "nested"),
    ("codex", os.path.expanduser("~/.codex/skills"), "flat"),
    ("claude", os.path.expanduser("~/.claude/skills"), "flat"),
    # Claude Code 插件市场的技能（ecc=Everything Claude Code 等，装成插件时
    # 技能在插件缓存目录里，不在 ~/.claude/skills）。版本号会随插件升级变化，
    # 用 glob 在扫描时动态展开。
    ("claude", _expand_plugin_skill_root("ecc/*/*/skills"), "flat"),
    ("claude", _expand_plugin_skill_root("anthropic-agent-skills/*/*/skills"), "flat"),
    # Hermes 个人技能层（~/.hermes/skills）：用户 skill_manage 创建的技能，
    # 放发行版层之前，同名时个人版优先（用户显式创建/安装的内容覆盖官方版）
    ("hermes", str(HERMES_SKILLS_DIR), "nested"),
    ("hermes", r"D:\agents\hermes\skills", "nested"),
    # Hermes 的可选技能与主技能统一归为 "hermes" 来源，避免界面出现两个 Hermes 选项
    ("hermes", r"D:\agents\hermes\optional-skills", "nested"),
]


_SOURCE_PRIORITY = {"codex": 0, "agent": 0.5, "hermes": 1, "claude": 3}


SKIP_NAMES = {
    # Codex 系统技能与元技能
    "openai-docs", "plugin-creator", "skill-creator", "skill-installer",
    "imagegen", "create-plan-revise", "codex-task",
    # Hermes 元技能/示例/内部技能
    "hermes-agent-skill-authoring", "dogfood", "yuanbao", "index-cache",
    "claude-code", "codex", "hermes-agent", "opencode", "computer-use",
    "macos-computer-use", "inspecting-hermes-desktop-dom",
    # Claude 本地目录
    "learned",
}


SKIP_CATEGORIES = {
    "apple", "smart-home", "blockchain", "finance", "gaming", "health",
    "payments", "security", "migration", "dogfood", "yuanbao", "index-cache",
    "evaluation", "inference", "models",
}


SKILL_GROUPS = {
    "documents": "文档处理",
    "writing": "写作内容",
    "research": "研究资料",
    "productivity": "效率办公",
    "design": "设计创意",
    "coding": "编程调试",
    "github": "GitHub 协作",
    "data-ml": "数据与 AI",
    "media": "媒体处理",
    "email": "邮件沟通",
    "other": "其他",
}


_GROUP_RULES = [
    (("github",), "github"),
    (("youtube", "gif", "video", "audio", "song", "media", "manim"), "media"),
    (("canvas", "diagram", "infographic", "frontend", "sketch", "excalidraw", "pixel",
      "poster", "web-design", "claude-design", "design-md", "p5js", "popular-web-designs",
      "humanizer"), "design"),
    (("gmail", "outlook", "mailchimp"), "email"),
    (("debug", "refactor", "tdd", "spike"), "coding"),
    (("dataset", "kaggle", "chroma", "vectordb"), "data-ml"),
    (("write", "draft", "polish", "resume", "paper-writing", "songwriting"), "writing"),
    (("excel", "word", "powerpoint", "nano-pdf", "ocr", "docx", "xlsx"), "documents"),
    (("notion", "obsidian", "airtable", "google-workspace", "maps", "teams", "calendar",
      "file-organizer", "templates"), "productivity"),
]


_NAME_OVERRIDES = {
    "pdf": "documents",
    "docx": "documents",
    "xlsx": "documents",
    "powerpoint": "documents",
    "pptx-author": "documents",
    "excel-author": "documents",
    "nano-pdf": "documents",
    "ocr-and-documents": "documents",
    "arxiv": "research",
    "llm-wiki": "research",
    "grounded-citations": "research",
    "blogwatcher": "research",
    "youtube-content": "media",
    "gif-search": "media",
    "songsee": "media",
    "content-research-writer": "writing",
    "email-draft-polish": "writing",
    "humanizer": "writing",
    "tailored-resume-generator": "writing",
    "meeting-notes-and-actions": "writing",
    "research-paper-writing": "writing",
    "himalaya": "email",
    "agentmail": "email",
    "canvas-design": "design",
    "design-taste-frontend": "design",
    "architecture-diagram": "design",
    "baoyu-infographic": "design",
    "ascii-art": "design",
    "ascii-video": "media",
    "systematic-debugging": "coding",
    "simplify-code": "coding",
    "spike": "coding",
    "test-driven-development": "coding",
    "python-debugpy": "coding",
    "node-inspect-debugger": "coding",
    "requesting-code-review": "coding",
    "codebase-inspection": "coding",
    "create-plan": "coding",
    "plan": "coding",
    "file-organizer": "productivity",
    "notion": "productivity",
    "obsidian": "productivity",
    "google-workspace": "productivity",
    "airtable": "productivity",
    "maps": "productivity",
    "teams-meeting-pipeline": "productivity",
    "jupyter-live-kernel": "data-ml",
    "huggingface-hub": "data-ml",
    "chroma": "data-ml",
}


def _group_for(skill: dict) -> str:
    """根据名称/标签/描述把技能归类到功能组。"""
    name = skill["name"].lower()
    if name in _NAME_OVERRIDES:
        return _NAME_OVERRIDES[name]
    hay = (
        f"{skill['name']} {' '.join(skill.get('tags', []))} "
        f"{skill['description'][:200]}"
    ).lower()
    for keywords, group in _GROUP_RULES:
        if any(kw in hay for kw in keywords):
            return group
    return "other"


TRANSLATE = {
    "写作": ["write", "writing", "author", "draft", "polish", "content"],
    "润色": ["polish", "refine", "writing", "edit"],
    "设计": ["design", "ui", "frontend", "visual", "canvas", "diagram"],
    "部署": ["deploy", "pipeline", "vercel", "publish"],
    "计划": ["plan", "roadmap", "create-plan"],
    "简历": ["resume", "tailored"],
    "邮件": ["email", "draft"],
    "会议": ["meeting", "notes", "actions"],
    "翻译": ["translate", "translation"],
    "研究": ["research", "content-research", "arxiv"],
    "图片": ["image", "canvas", "visual", "infographic"],
    "文件": ["file", "organize", "organizer", "pdf", "docx", "xlsx"],
    "分析": ["analyze", "analysis", "research"],
    "前端": ["frontend", "design", "taste", "web"],
    "知识库": ["knowledge", "document", "pdf", "ocr"],
    "pdf": ["pdf"],
    "文档": ["pdf", "document", "docx", "ocr"],
    "调试": ["debug", "debugging", "debugpy", "systematic-debugging"],
    "代码": ["code", "github", "pull", "review", "refactor"],
    "表格": ["xlsx", "excel", "spreadsheet"],
    "幻灯片": ["powerpoint", "ppt", "slides"],
    "笔记": ["notion", "obsidian", "notes"],
    "视频": ["youtube", "video"],
    "数据库": ["database", "sql", "query"],
}


_cache: list[dict] | None = None


_scanned_at = 0.0


_SCAN_TTL = 300  # 5 分钟重新扫描


def _iter_skill_dirs(root: Path, layout: str):
    """按布局产出 (技能目录, 分类名)。"""
    if not root.is_dir():
        return
    if layout == "flat":
        for skill_dir in sorted(root.iterdir()):
            if skill_dir.is_dir() and not skill_dir.name.startswith("."):
                yield skill_dir, ""
    else:
        for cat in sorted(root.iterdir()):
            if not cat.is_dir() or cat.name.startswith("."):
                continue
            if cat.name in SKIP_CATEGORIES:
                continue
            for skill_dir in sorted(cat.iterdir()):
                if skill_dir.is_dir() and not skill_dir.name.startswith("."):
                    yield skill_dir, cat.name


def _parse_list_field(value: str) -> list[str]:
    """解析 frontmatter 里的内联列表（[a, b] 或 a, b）。"""
    raw = (value or "").strip()
    if raw.startswith("[") and raw.endswith("]"):
        raw = raw[1:-1]
    return [p.strip().strip("\"'") for p in raw.split(",") if p.strip()]


# 平台门控只影响"是否进入模型索引"，不影响扫描结果（设置页仍能看到被过滤原因）
_PLATFORM_ALIASES = {
    "win32": {"windows", "win", "win32"},
    "darwin": {"darwin", "macos", "mac", "osx"},
    "linux": {"linux"},
}


def current_platform() -> str:
    return sys.platform


def platform_supported(platforms: list[str] | None) -> bool:
    if not platforms:
        return True
    normalized = {str(p).strip().lower() for p in platforms if str(p).strip()}
    aliases = _PLATFORM_ALIASES.get(sys.platform, {sys.platform})
    return bool(normalized & (aliases | {sys.platform}))


_LINKED_DIRS = ("references", "templates", "scripts", "assets")


def _scan_linked_files(skill_dir: Path) -> dict[str, list[str]]:
    """列出技能附件（相对技能目录的 POSIX 路径），供 skill_view 按需读取。"""
    out: dict[str, list[str]] = {}
    for sub in _LINKED_DIRS:
        base = skill_dir / sub
        files: list[str] = []
        if base.is_dir():
            for path in sorted(base.rglob("*")):
                if not path.is_file():
                    continue
                rel = path.relative_to(skill_dir)
                if any(part.startswith(".") for part in rel.parts):
                    continue
                files.append(rel.as_posix())
                if len(files) >= 100:
                    break
        out[sub] = files
    return out


def _parse_frontmatter(text: str) -> tuple[str, str, list[str], dict]:
    """解析 SKILL.md 的 name/description/tags + 扩展字段。"""
    name = ""
    description = ""
    tags: list[str] = []
    extra: dict[str, str] = {}
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            block = text[3:end]
            for line in block.splitlines():
                if ":" in line:
                    key, _, value = line.partition(":")
                    value = value.strip().strip('"\'')
                    key = key.strip().lower()
                    if key == "name" and not name:
                        name = value
                    elif key in ("description", "desc") and not description:
                        description = value
                    elif key in (
                        "origin",
                        "created_at",
                        "when_to_use",
                        "when_not_to_use",
                        "platforms",
                    ) and value:
                        extra[key] = value
            tag_match = re.search(r"tags:\s*\[([^\]]*)\]", block)
            if tag_match:
                tags = [
                    t.strip().strip('"\'')
                    for t in tag_match.group(1).split(",")
                    if t.strip()
                ][:8]
            if name or description:
                return name, description, tags, extra
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if lines:
        name = lines[0].lstrip("# ").strip()
        description = " ".join(lines[1:3])[:200]
    return name, description, tags, extra


def _norm_name(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", name.lower())


def _visible(skill: dict) -> bool:
    """技能是否进入目录（屏蔽元技能/无关技能）。"""
    # 自创技能始终可见：由用户/知问 Agent 创建，在技能页"自创"板块集中管理
    if skill.get("source") == AGENT_SOURCE:
        return True
    if skill["name"] in SKIP_NAMES or skill["dir_name"] in SKIP_NAMES:
        return False
    if skill["category"] in SKIP_CATEGORIES:
        return False
    return True


def scan_skills(force: bool = False) -> list[dict]:
    """扫描全部技能目录并去重，返回 [{id, name, description, source, ...}]。"""
    global _cache, _scanned_at
    now = time.time()
    if not force and _cache is not None and now - _scanned_at < _SCAN_TTL:
        skills = _cache
    else:
        by_name: dict[str, dict] = {}
        for source, root_raw, layout in DEFAULT_ROOTS:
            if not root_raw:
                continue  # 插件 glob 未命中（目录不存在/版本变化）
            root = Path(root_raw)
            if not root.is_dir():
                continue
            for skill_dir, category in _iter_skill_dirs(root, layout):
                skill_md = skill_dir / "SKILL.md"
                if not skill_md.exists():
                    continue
                try:
                    text = skill_md.read_text(encoding="utf-8", errors="ignore")
                    name, description, tags, extra = _parse_frontmatter(text)
                    name = name or skill_dir.name
                    platforms = _parse_list_field(extra.get("platforms", ""))
                    item = {
                        "name": name,
                        "dir_name": skill_dir.name,
                        "description": description or "暂无描述",
                        "source": source,
                        "category": category or source,
                        "group": "",
                        "path": str(skill_md),
                        "dir_path": str(skill_dir),
                        "excerpt": text[:1500],
                        "content_chars": len(text),
                        "when_to_use": extra.get("when_to_use", ""),
                        "when_not_to_use": extra.get("when_not_to_use", ""),
                        "platforms": platforms,
                        "platform_ok": platform_supported(platforms),
                        "linked_files": _scan_linked_files(skill_dir),
                        "tags": tags,
                        "origin": extra.get("origin", ""),
                        "created_at": extra.get("created_at", ""),
                        "sources": [source],
                        "has_commands": bool(
                            re.search(r"```(?:bash|sh|powershell|cmd)", text)
                        ),
                        "shared": False,
                    }
                    if source == AGENT_SOURCE:
                        # 共享状态：Hermes 个人层里是否存在同名副本
                        item["shared"] = (
                            HERMES_SKILLS_DIR
                            / (category or source)
                            / skill_dir.name
                            / "SKILL.md"
                        ).exists()
                    if not _visible(item):
                        continue
                    item["group"] = _group_for(item)
                    key = _norm_name(name)
                    prev = by_name.get(key)
                    if prev is None or _SOURCE_PRIORITY.get(source, 9) < _SOURCE_PRIORITY.get(
                        prev["source"], 9
                    ):
                        by_name[key] = item
                    else:
                        # 同名技能：合并来源标签（如 codex + hermes），保留高优先级副本
                        if source not in prev["sources"]:
                            prev["sources"].append(source)
                            prev["source"] = " + ".join(
                                s for s in ["codex", "agent", "hermes", "claude"]
                                if s in prev["sources"]
                            )
                except Exception as exc:
                    logger.warning("解析技能 %s 失败：%s", skill_dir, exc)

        skills = sorted(
            by_name.values(),
            key=lambda s: (s["source"], s["category"], s["name"]),
        )
        _cache = skills
        _scanned_at = now
        logger.info(
            "已扫描 %d 个技能（来源：%s）",
            len(skills),
            ", ".join(sorted({s["source"] for s in skills})),
        )

    # 每次返回前按当前偏好重新打标（偏好可能在两次扫描之间变化）
    prefs = _get_cached_prefs()
    for s in skills:
        s["enabled"] = s["name"] in prefs["enabled"]
        s["hidden"] = s["name"] in prefs["hidden"]
    return skills


def search_skills(
    query: str,
    embeddings=None,
    top_k: int = 3,
    enabled_ids: set[str] | None = None,
) -> list[dict]:
    """按语义/关键词检索技能。有 embedding 时优先语义匹配，否则关键词。"""
    skills = scan_skills()
    if enabled_ids is not None:
        skills = [s for s in skills if s["name"] in enabled_ids]
    if not skills:
        return []

    def expanded_keywords() -> list[str]:
        kws = [w for w in query.replace("？", " ").split() if w]
        for zh, en_list in TRANSLATE.items():
            if zh in query.lower():
                kws.extend(en_list)
        return kws

    if embeddings is not None:
        try:
            q_vec = embeddings.embed_query(query)
            texts = [f"{s['name']} {s['description']} {' '.join(s.get('tags', []))}" for s in skills]
            doc_vecs = embeddings.embed_documents(texts, batch_size=64)
            keywords = expanded_keywords()
            scored = []
            for skill, vec in zip(skills, doc_vecs):
                sim = sum(a * b for a, b in zip(q_vec, vec))
                kw_score = sum(1 for kw in keywords if kw in skill["name"].lower())
                kw_score += sum(
                    0.5 for kw in keywords if kw in skill["description"].lower()
                )
                scored.append((sim + min(0.6, kw_score * 0.12), skill))
            scored.sort(key=lambda x: x[0], reverse=True)
            return [s for _, s in scored[:top_k]]
        except Exception as exc:
            logger.warning("技能语义检索失败，回退关键词：%s", exc)
    keywords = expanded_keywords()
    scored = []
    for skill in skills:
        hay = (
            f"{skill['name']} {skill['description']} {' '.join(skill.get('tags', []))}"
        ).lower()
        score = sum(1 for kw in keywords if kw.lower() in hay)
        if score:
            scored.append((score, skill))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [s for _, s in scored[:top_k]]
