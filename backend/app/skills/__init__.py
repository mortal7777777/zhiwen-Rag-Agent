"""技能包装配层（原 app/skills.py 包化，2026-10-10）。

子模块：prefs（偏好）/ scan（扫描检索）/ agent_skills（自创技能管理）/
structure（章节摘要）/ sanitize（内容安全）/ catalog（目录组装）。
原文件里的全部模块级名字在此重新导出，历史导入路径与命名空间
（tests 的 `app.skills.X` 访问）保持不变。
"""
from .prefs import (  # noqa: F401
    DEFAULT_ENABLED,
    _prefs,
    _prefs_lock,
    _get_cached_prefs,
    load_prefs,
    save_prefs,
    reset_prefs,
)
from .scan import (  # noqa: F401
    _PROJECT_ROOT,
    AGENT_SKILLS_DIR,
    HERMES_SKILLS_DIR,
    AGENT_SOURCE,
    _expand_plugin_skill_root,
    DEFAULT_ROOTS,
    _SOURCE_PRIORITY,
    SKIP_NAMES,
    SKIP_CATEGORIES,
    SKILL_GROUPS,
    _GROUP_RULES,
    _NAME_OVERRIDES,
    TRANSLATE,
    _SCAN_TTL,
    _cache,
    _scanned_at,
    _iter_skill_dirs,
    _parse_frontmatter,
    _norm_name,
    _visible,
    _group_for,
    scan_skills,
    search_skills,
)
from .agent_skills import (  # noqa: F401
    _SAFE_NAME_RE,
    _resolve_agent_skill,
    _read_skill_origin,
    share_agent_skill,
    unshare_agent_skill,
    delete_agent_skill,
    agent_skill_content,
)
from .structure import (  # noqa: F401
    _PRIORITY_SECTIONS,
    skill_structure,
)
from .sanitize import (  # noqa: F401
    RISK_PATTERNS,
    sanitize_skill_text,
)
from .catalog import (  # noqa: F401
    build_skill_catalog,
    matching_skills_for_injection,
)
