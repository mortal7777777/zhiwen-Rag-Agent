"""技能查看（skill_view）与写技能（write_skill）工具。"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from langchain_core.messages import ToolMessage
from langchain_core.tools import BaseTool, StructuredTool, tool
from pydantic import BaseModel, Field

from ...rag.retriever import KnowledgeBaseRetriever
from ...runtime_config import effective_or

logger = logging.getLogger(__name__)


SKILL_VIEW_TOOL_NAME = "skill_view"


_SKILL_FILE_MAX_BYTES = 256 * 1024


_MAX_SECTION_DIGEST = 60


_SECTION_SUMMARY_CHARS = 160


def _match_skill(skills: list[dict], name: str) -> dict | None:
    """按技能名（或目录名）匹配，大小写不敏感。"""
    target = (name or "").strip().lower()
    if not target:
        return None
    for skill in skills:
        if str(skill.get("name") or "").lower() == target:
            return skill
    for skill in skills:
        if str(skill.get("dir_name") or "").lower() == target:
            return skill
    return None


def _match_section(sections: list[dict], section: str) -> dict | None:
    target = (section or "").strip().lower()
    if not target:
        return None
    for item in sections:
        if item["heading"].strip().lower() == target:
            return item
    for item in sections:
        if target in item["heading"].strip().lower():
            return item
    return None


def _safe_skill_file(skill_dir: Path, file_path: str) -> Path:
    """把技能目录内的相对路径解析为绝对路径；越界/绝对路径/穿越一律拒绝。"""
    rel = (file_path or "").strip().replace("\\", "/")
    if not rel:
        raise ValueError("file_path 不能为空")
    if rel.startswith("/") or re.match(r"^[a-zA-Z]:", rel):
        raise ValueError("只允许技能目录内的相对路径（不接受绝对路径或盘符）")
    if any(part in ("", "..") for part in rel.split("/")):
        raise ValueError("路径不合法（不允许 .. 或空路径段）")
    root = skill_dir.resolve()
    candidate = (root / rel).resolve()
    if candidate != root and root not in candidate.parents:
        raise ValueError("路径超出技能目录")
    if not candidate.is_file():
        raise ValueError("文件不存在")
    return candidate


def _section_digest(sections: list[dict]) -> tuple[list[dict], bool]:
    """章节目录瘦身：最多 60 条、每条摘要 160 字，避免目录本身撑爆上下文。"""
    digest = [
        {
            "heading": s.get("heading", ""),
            "summary": (s.get("summary") or "")[:_SECTION_SUMMARY_CHARS],
        }
        for s in sections[:_MAX_SECTION_DIGEST]
    ]
    return digest, len(sections) > _MAX_SECTION_DIGEST


def _skill_payload(entry: dict, content: str, sanitize, full_max: int, extra=None) -> dict:
    body = sanitize(content, full_max) if content else ""
    payload = {
        "ok": True,
        "name": entry.get("name"),
        "source": entry.get("source"),
        "category": entry.get("category"),
        "group": entry.get("group"),
        "path": entry.get("path"),
        "skill_dir": entry.get("dir_path"),
        "linked_files": entry.get("linked_files") or {},
        "content": body,
        "content_chars": len(content),
        "truncated": False,
        "summary": f"技能 {entry.get('name')}（{len(body)} 字符）",
        "usage_hint": "附件可用 skill_view(name, file_path='references/...') 读取。",
    }
    if extra:
        payload.update(extra)
    return payload


def make_skill_view_tool(
    rag_service,
    enabled_ids: set[str] | None = None,
    *,
    conv_id: int | None = None,
    full_max_chars: int = 24000,
    dedup: bool = True,
    usage: bool = True,
) -> BaseTool:
    """技能加载工具：按名称取回 SKILL.md 全文、章节或附件。

    替代旧的向量 top-k 检索入口——L0 索引已把全部启用技能名交给模型，
    这里按准确名称做渐进披露（L1 全文/分节、L2 附件）。同一轮重复查看
    未变化的内容返回短 stub，避免重复注入撑爆上下文。
    """
    seen: dict[tuple[str, str, str], str] = {}

    @tool
    def skill_view(name: str, file_path: str = "", section: str = "") -> dict:
        """查看一个已启用技能（Skills）的完整内容或附件。
        name 必须是系统提示词技能目录中的技能名；任务与某技能部分相关就应调用。
        file_path 可选：技能目录内的相对路径（如 references/api.md），用于读附件。
        section 可选：按章节标题读取大技能的一部分（如 "When to Use"）。"""
        try:
            from ...skills import sanitize_skill_text, scan_skills, skill_sections

            skills = [
                s
                for s in scan_skills()
                if enabled_ids is None or s["name"] in enabled_ids
            ]
            entry = _match_skill(skills, name)
            if entry is None:
                return {
                    "ok": False,
                    "error": f"未找到已启用技能「{name}」",
                    "available_names": [s["name"] for s in skills][:50],
                }
            skill_md = Path(str(entry.get("path") or ""))
            skill_dir = Path(str(entry.get("dir_path") or skill_md.parent))

            if file_path:
                target = _safe_skill_file(skill_dir, file_path)
                size = target.stat().st_size
                if size > _SKILL_FILE_MAX_BYTES:
                    return {
                        "ok": False,
                        "error": (
                            f"附件过大（{size} 字节，上限 {_SKILL_FILE_MAX_BYTES}），"
                            "请改读 references/ 下更小的文件"
                        ),
                    }
                text = target.read_text(encoding="utf-8", errors="ignore")
                return _skill_payload(
                    entry,
                    text,
                    sanitize_skill_text,
                    full_max_chars,
                    extra={"file_path": file_path},
                )

            text = skill_md.read_text(encoding="utf-8", errors="ignore")
            sections = skill_sections(str(skill_md))

            if section:
                hit = _match_section(sections, section)
                if hit is None:
                    return {
                        "ok": False,
                        "error": f"未找到章节「{section}」",
                        "sections": _section_digest(sections)[0],
                    }
                return _skill_payload(
                    entry,
                    text[hit["start"] : hit["end"]],
                    sanitize_skill_text,
                    full_max_chars,
                    extra={"section": hit["heading"]},
                )

            if len(text) <= full_max_chars:
                payload = _skill_payload(
                    entry, text, sanitize_skill_text, full_max_chars
                )
            else:
                digest, digest_truncated = _section_digest(sections)
                payload = _skill_payload(
                    entry,
                    "",
                    sanitize_skill_text,
                    full_max_chars,
                    extra={
                        "truncated": True,
                        "content_chars": len(text),
                        "sections": digest,
                        "sections_truncated": digest_truncated,
                        "summary": (
                            f"技能 {entry.get('name')} 较大（{len(text)} 字符），"
                            f"已返回章节目录（{len(digest)} 节），用 section 读取指定章节"
                        ),
                        "usage_hint": (
                            "技能较大，已返回章节目录；用 "
                            "skill_view(name, section='<标题>') 读取指定章节。"
                        ),
                    },
                )
                if not sections:
                    payload["content"] = sanitize_skill_text(text, full_max_chars)

            key = (str(entry.get("name")), file_path, section)
            if dedup and seen.get(key) == payload.get("content"):
                return {
                    "ok": True,
                    "name": entry.get("name"),
                    "path": str(skill_md),
                    "skill_dir": str(skill_dir),
                    "deduplicated": True,
                    "summary": f"技能 {entry.get('name')} 内容本轮已返回且未变化，未重复注入。",
                    "usage_hint": "如需重读请换 section/file_path，或直接使用已加载内容。",
                }
            seen[key] = payload.get("content") or ""

            if usage:
                try:
                    from ...skills import record_view

                    record_view(str(entry.get("name")))
                except Exception:
                    pass
            return payload
        except Exception as exc:
            return {"ok": False, "error": f"技能加载失败：{exc}"}

    return skill_view


WRITE_SKILL_TOOL_NAME = "write_skill"


class _WriteSkillArgs(BaseModel):
    name: str = Field(
        description="技能名称（小写连字符，如 book-topic-compare）；同名技能会被覆盖"
    )
    description: str = Field(
        description="一句话描述技能用途（将写入 SKILL.md frontmatter 的 description）"
    )
    content: str = Field(
        description=(
            "SKILL.md 正文（不含 frontmatter）。"
            "用 markdown 写：何时用、前置条件、步骤、注意事项。"
            "正文开头会自动加上 frontmatter，你只需写 body。"
        )
    )
    category: str = Field(
        default="other",
        description="分类目录名（如 research/writing/coding/other），默认 other",
    )


def make_write_skill_tool(settings=None) -> BaseTool:
    """技能创作工具：把重复执行的流程沉淀成 SKILL.md，下次 skill_view 即可取回。

    写入自创技能库 data/agent_skills/<category>/<name>/SKILL.md（与 Hermes
    个人技能层隔离；设置 → 技能 → 自创 里可启用 / 共享给 Hermes 使用）。
    写完 force 刷新扫描缓存使新技能立即可见。属敏感操作（写文件），走 HITL 人工确认。
    """

    @tool
    def write_skill(
        name: str,
        description: str,
        content: str,
        category: str = "other",
    ) -> dict:
        """把一个可复用的任务流程写成技能文件（SKILL.md），下次同类任务用 skill_view 取回。
        适合：刚走通、以后会重复的多步流程；不适合：一次性任务与 1-2 步操作。
        name 小写连字符，description 一句话，content 为不含 frontmatter 的正文，category 默认 other。
        正文建议包含 When to Use / Prerequisites / Procedure / Pitfalls / Verification 章节。"""
        try:
            import os
            import re
            from datetime import date

            # 名称规范化：小写、连字符、只保留字母数字
            clean_name = re.sub(r"[^a-z0-9-]", "-", name.lower().strip())
            clean_name = re.sub(r"-+", "-", clean_name).strip("-")
            if not clean_name or len(clean_name) < 2:
                return {
                    "summary": "技能名称无效（至少 2 个字母数字字符）",
                    "error": "invalid name",
                }
            clean_cat = re.sub(
                r"[^a-z0-9-]", "-", (category or "other").lower().strip()
            ).strip("-") or "other"

            # 自创技能落点：data/agent_skills（与 Hermes 个人层隔离，按需共享）
            from ...skills import AGENT_SKILLS_DIR

            skills_root = str(AGENT_SKILLS_DIR)
            skill_dir = os.path.join(skills_root, clean_cat, clean_name)
            skill_md_path = os.path.join(skill_dir, "SKILL.md")

            # 检查是否已存在（覆盖前告知）
            existed = os.path.exists(skill_md_path)

            # 组装 frontmatter + body
            desc_clean = description.strip().replace('"', "'")[:200]
            frontmatter = (
                "---\n"
                f"name: {clean_name}\n"
                f'description: "{desc_clean}"\n'
                "origin: agent\n"
                f"created_at: {date.today().isoformat()}\n"
                "---\n\n"
            )
            full_content = frontmatter + content.strip() + "\n"

            os.makedirs(skill_dir, exist_ok=True)
            with open(skill_md_path, "w", encoding="utf-8") as f:
                f.write(full_content)

            # force 刷新 scan_skills 缓存，使新技能在下一次 skill_view 时可见
            try:
                from ...skills import scan_skills

                scan_skills(force=True)
            except Exception:
                pass  # 缓存刷新失败不影响写入成功

            if settings is None or bool(
                effective_or(settings, "skills_usage_tracking", True)
            ):
                try:
                    from ...skills import record_patch

                    record_patch(clean_name)
                except Exception:
                    pass

            return {
                "summary": (
                    f"技能 {clean_name} 已{'覆盖' if existed else '创建'}"
                    f"（自创技能库 {clean_cat}/{clean_name}/SKILL.md，"
                    f"{len(full_content)} 字符，当前为待启用状态）。"
                    "在 设置 → 技能 → 自创 里可启用（启用后 skill_view 才能查到）；"
                    "如需给 Hermes 使用，可在同一处点「共享」。"
                ),
                "name": clean_name,
                "path": skill_md_path,
                "category": clean_cat,
                "overwritten": existed,
            }
        except Exception as exc:
            return {"summary": f"技能创建失败：{exc}", "error": str(exc)}

    return write_skill
