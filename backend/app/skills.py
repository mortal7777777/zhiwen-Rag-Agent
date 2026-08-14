"""技能目录：扫描本机已安装的 Agent Skills（Codex / Claude / Hermes），
把"技能名 + 描述 + SKILL.md 摘要"索引起来，供 Agent 按需检索注入。

设计要点：
- 支持三种目录布局：Codex/Claude 平铺（skills/<name>/SKILL.md）、
  Hermes 分类嵌套（skills/<category>/<name>/SKILL.md、optional-skills/...）；
- 按技能名去重（保留 Codex > Hermes > Hermes-optional > Claude 的优先级）；
- 屏蔽与本助手无关的元技能/领域技能，避免目录里塞满无用条目；
- 启停/隐藏偏好持久化在 app_meta.skill_prefs：
  * enabled = 本助手启用的技能（Agent 的 skill_lookup 只返回这些）；
  * hidden  = 从本助手移除的技能（只影响本助手，绝不改动其他 Agent 的文件）；
- 默认启用一份"最实用必要"的精选手集，用户可在设置页调整。
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)

def _expand_plugin_skill_root(rel_glob: str) -> str | None:
    """把 ~/.claude/plugins/cache/<glob> 展开为实际存在的技能根目录。

    插件缓存路径含版本号（如 ecc/ecc/2.0.0-rc.1），版本升级会变，
    返回 None 表示未找到（扫描时跳过）。
    """
    import glob

    base = os.path.expanduser("~/.claude/plugins/cache")
    matches = sorted(glob.glob(os.path.join(base, rel_glob)))
    return matches[-1] if matches else None


# (来源标签, 根目录, 目录布局: flat=一级目录 / nested=分类/技能 两级)
DEFAULT_ROOTS = [
    ("codex", os.path.expanduser("~/.codex/skills"), "flat"),
    ("claude", os.path.expanduser("~/.claude/skills"), "flat"),
    # Claude Code 插件市场的技能（ecc=Everything Claude Code 等，装成插件时
    # 技能在插件缓存目录里，不在 ~/.claude/skills）。版本号会随插件升级变化，
    # 用 glob 在扫描时动态展开。
    ("claude", _expand_plugin_skill_root("ecc/*/*/skills"), "flat"),
    ("claude", _expand_plugin_skill_root("anthropic-agent-skills/*/skills"), "flat"),
    ("hermes", r"D:\agents\hermes\skills", "nested"),
    # Hermes 的可选技能与主技能统一归为 "hermes" 来源，避免界面出现两个 Hermes 选项
    ("hermes", r"D:\agents\hermes\optional-skills", "nested"),
]

_SOURCE_PRIORITY = {"codex": 0, "hermes": 1, "claude": 3}

# 与本助手无关/元技能：直接不进入目录
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

# 与个人知识库助手无关的领域分类（Hermes 分类目录）
SKIP_CATEGORIES = {
    "apple", "smart-home", "blockchain", "finance", "gaming", "health",
    "payments", "security", "migration", "dogfood", "yuanbao", "index-cache",
    "evaluation", "inference", "models",
}

# 默认启用：经过筛选的"最实用必要"技能（可按 id 调整，用户偏好覆盖）
DEFAULT_ENABLED = {
    # 文档与知识库处理
    "pdf", "ocr-and-documents", "docx", "xlsx", "powerpoint", "nano-pdf",
    # 写作与内容
    "content-research-writer", "email-draft-polish", "humanizer",
    "tailored-resume-generator", "meeting-notes-and-actions",
    # 研究与资料
    "arxiv", "research-paper-writing", "grounded-citations",
    # 个人效率
    "file-organizer",
    # 前端/设计
    "design-taste-frontend", "canvas-design", "architecture-diagram",
    "baoyu-infographic",
    # 编程与调试
    "create-plan", "systematic-debugging", "simplify-code",
    "test-driven-development", "requesting-code-review", "codebase-inspection",
    # 数据与机器学习
    "jupyter-live-kernel",
}

# 功能分组（用于设置页按功能筛选）
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

# 分组判定规则：按 (名称/标签/描述 关键词, 分组) 顺序匹配
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

# 名称优先精确归类：名称/目录名明确时直接定组，避免描述/标签里的关键词抢归类
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


# 中文查询 -> 英文关键词映射：技能描述多为英文，跨语言语义检索不稳，
# 用术语映射做关键词加权，保证中文查询能命中对应技能。
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
_prefs: dict | None = None
_prefs_lock = threading.Lock()


# ============================================================
# 扫描
# ============================================================


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


def _parse_frontmatter(text: str) -> tuple[str, str, list[str]]:
    """从 SKILL.md 里解析 name/description/tags（YAML frontmatter 或首行）。"""
    name = ""
    description = ""
    tags: list[str] = []
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
            tag_match = re.search(r"tags:\s*\[([^\]]*)\]", block)
            if tag_match:
                tags = [
                    t.strip().strip('"\'')
                    for t in tag_match.group(1).split(",")
                    if t.strip()
                ][:8]
            if name or description:
                return name, description, tags
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if lines:
        name = lines[0].lstrip("# ").strip()
        description = " ".join(lines[1:3])[:200]
    return name, description, tags


def _norm_name(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", name.lower())


def _visible(skill: dict) -> bool:
    """技能是否进入目录（屏蔽元技能/无关技能）。"""
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
                    text = skill_md.read_text(encoding="utf-8", errors="ignore")[:4000]
                    name, description, tags = _parse_frontmatter(text)
                    name = name or skill_dir.name
                    item = {
                        "name": name,
                        "dir_name": skill_dir.name,
                        "description": description or "暂无描述",
                        "source": source,
                        "category": category or source,
                        "group": "",
                        "path": str(skill_md),
                        "excerpt": text[:1500],
                        "tags": tags,
                        "sources": [source],
                        "has_commands": bool(
                            re.search(r"```(?:bash|sh|powershell|cmd)", text)
                        ),
                    }
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
                                s for s in ["codex", "hermes", "claude"]
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


# ============================================================
# 偏好（启停 / 隐藏）——只影响本助手，绝不修改技能文件
# ============================================================


def _get_cached_prefs() -> dict:
    with _prefs_lock:
        if _prefs is None:
            return {
                "enabled": set(DEFAULT_ENABLED),
                "hidden": set(),
                "custom": False,
            }
        return {
            "enabled": set(_prefs.get("enabled") or []),
            "hidden": set(_prefs.get("hidden") or []),
            "custom": bool(_prefs.get("custom")),
        }


def load_prefs(db, force: bool = False) -> dict:
    """从数据库读取技能偏好；无自定义记录时使用默认精选手集。"""
    global _prefs
    if db is None:
        return _get_cached_prefs()
    raw = None
    try:
        from .db import repository as repo

        raw = repo.get_meta(db, "skill_prefs")
    except Exception as exc:
        logger.warning("读取技能偏好失败：%s", exc)
    data: dict = {}
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                data = parsed
        except Exception as exc:
            logger.warning("skill_prefs 解析失败：%s", exc)
    with _prefs_lock:
        if data.get("custom"):
            _prefs = {
                "enabled": set(data.get("enabled") or []),
                "hidden": set(data.get("hidden") or []),
                "custom": True,
            }
        else:
            _prefs = {
                "enabled": set(DEFAULT_ENABLED),
                "hidden": set(),
                "custom": False,
            }
    return _get_cached_prefs()


def save_prefs(db, enabled: list[str] | None = None, hidden: list[str] | None = None) -> dict:
    """保存技能偏好并立即生效。enabled/hidden 传空列表即清空。"""
    global _prefs
    prev = _get_cached_prefs()
    new_enabled = set(str(x).strip() for x in (enabled if enabled is not None else prev["enabled"]) if x)
    new_hidden = set(str(x).strip() for x in (hidden if hidden is not None else prev["hidden"]) if x)
    # 隐藏的技能不应同时处于启用状态
    new_enabled -= new_hidden
    data = {
        "enabled": sorted(new_enabled),
        "hidden": sorted(new_hidden),
        "custom": True,
    }
    with _prefs_lock:
        _prefs = {
            "enabled": new_enabled,
            "hidden": new_hidden,
            "custom": True,
        }
    if db is not None:
        try:
            from .db import repository as repo

            repo.set_meta(db, "skill_prefs", json.dumps(data, ensure_ascii=False))
        except Exception as exc:
            logger.warning("保存技能偏好失败：%s", exc)
    return _get_cached_prefs()


def reset_prefs(db) -> dict:
    """恢复默认精选手集。"""
    return save_prefs(db, enabled=sorted(DEFAULT_ENABLED), hidden=[])


# ============================================================
# 检索
# ============================================================


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


# ============================================================
# 结构化技能目录（P2-13）：让 skill_lookup 返回分节摘要而非整段截断
# ============================================================

# 需要展开的优先段落（按重要性排序）
_PRIORITY_SECTIONS = (
    "when to use",
    "prerequisites",
    "steps",
    "how to",
    "usage",
    "examples",
    "workflow",
    "recommended",
    "gotchas",
    "troubleshooting",
)


def skill_structure(skill_md_path: str, max_sections: int = 8) -> list[dict]:
    """把 SKILL.md 解析成 {heading, summary} 段目录，供模型按需展开。

    相比"前 N 字符截断"，分节目录能保留关键段（When to Use / Prerequisites /
    Steps）的完整入口，避免复杂技能的关键步骤被截掉。
    """
    try:
        text = Path(skill_md_path).read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return []
    sections: list[dict] = []
    current_heading = ""
    current_lines: list[str] = []

    def flush():
        if current_heading and current_lines:
            summary = " ".join(
                ln.strip() for ln in current_lines if ln.strip()
            ).strip()
            sections.append(
                {
                    "heading": current_heading,
                    "summary": summary[:300],
                }
            )

    for line in text.splitlines():
        if line.startswith("#"):
            flush()
            current_heading = line.lstrip("#").strip()
            current_lines = []
        elif current_heading:
            current_lines.append(line)
    flush()

    if not sections:
        return []
    # 去掉 frontmatter 伪段落（--- 之类），并按优先级排序
    cleaned = [
        s for s in sections if s["heading"] and s["heading"] != "---"
    ]
    cleaned.sort(
        key=lambda s: (
            next(
                (i for i, key in enumerate(_PRIORITY_SECTIONS) if key in s["heading"].lower()),
                99,
            ),
            len(s["summary"]),
        )
    )
    return cleaned[:max_sections]


# ============================================================
# 安全清洗与自动注入（防止 SKILL.md 提示注入）
# ============================================================

# 高风险指令模式：命中即整行替换为占位说明（只过滤"命令式越权"，不影响正常文档）
RISK_PATTERNS = (
    (r"ignore\s+(all\s+)?(previous|prior|above)", "试图覆盖此前指令"),
    (r"you\s+are\s+now\b|pretend\s+you\s+are\b", "身份冒充指令"),
    (r"disregard|override\s+(the\s+)?(system|safety|guardrail|policy|rules)", "越权指令"),
    (r"exfiltrat|send\s+(all\s+)?(your\s+)?(data|files|keys|secrets|credentials)", "数据外传"),
    (r"(output|print|reveal|show)\s+(your\s+)?(api\s*key|password|secret|token|credential)", "索要凭据"),
    (r"do\s+not\s+(tell|inform|notify|ask)\s+(the\s+)?user", "隐瞒指令"),
    (r"bypass|circumvent|disable\s+(.*)?(approval|permission|safety|security)", "绕过审批"),
    (r"run\s+(it|this|commands|anything)\s+without\s+(asking|approval|permission|confirmation)", "擅自执行"),
    (r"rm\s+-rf\s+[/~]|format\s+[a-z]:|del\s+/[fqs]", "破坏性命令"),
)


def sanitize_skill_text(text: str, limit: int = 1500) -> str:
    """清洗技能内容：高风险指令行替换为占位说明，并做长度截断。"""
    text = text or ""
    lines = []
    for ln in text.splitlines():
        matched = next(
            (label for pat, label in RISK_PATTERNS if re.search(pat, ln, re.I)),
            None,
        )
        lines.append(f"[已过滤：{matched}]" if matched else ln)
    cleaned = "\n".join(lines).strip()
    if len(cleaned) > limit:
        return cleaned[:limit] + "\n…（内容已截断）"
    return cleaned


def build_skill_catalog(enabled_ids: set[str] | None = None, limit: int = 40) -> str:
    """生成紧凑的技能目录文本（名称 + 一句话描述），注入系统提示词。"""
    skills = scan_skills()
    if enabled_ids is not None:
        skills = [s for s in skills if s["name"] in enabled_ids]
    lines = []
    for s in skills[:limit]:
        desc = " ".join((s.get("description") or "").split())[:90]
        lines.append(f"- {s['name']}：{desc}")
    return "\n".join(lines)


def matching_skills_for_injection(
    query: str,
    enabled_ids: set[str] | None = None,
    top_k: int = 2,
    max_sections: int = 6,
) -> str:
    """按关键词匹配当前问题最相关的技能，返回清洗后的结构化说明。

    用于 prepare 阶段自动注入（不等模型主动调用 skill_lookup），
    内容经 sanitize_skill_text 过滤高风险指令。
    """
    hits = search_skills(
        query,
        embeddings=None,
        top_k=top_k,
        enabled_ids=enabled_ids,
    )
    blocks = []
    for h in hits:
        sections = skill_structure(h["path"], max_sections=max_sections)
        sec_text = "\n".join(
            f"- {s['heading']}：{sanitize_skill_text(s['summary'], 200)}"
            for s in sections
        )
        blocks.append(
            f"### {h['name']}（来源：{h['source']}）\n"
            f"{sanitize_skill_text(h.get('description') or '', 200)}\n"
            f"{sec_text}"
        )
    return "\n\n".join(blocks)
