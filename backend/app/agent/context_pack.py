"""子代理上下文包增强（Phase B 6.4）。

目标：减少子代理的探索轮数——review 直接看到改动，explore 直接看到相关文件候选。
所有探测都是只读、限时、可失败：任何一段构建失败只记 warning，不影响子任务。

总量上限 3000 字符；优先级：工作目录/工具/模式提示 > 顶层清单 > git 状态 >
清单探测 > 关键词命中文件 > 近期对话摘要。
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import time
from pathlib import Path

from .agent_types import MODE_PACK_HINTS, mode_tools, normalize_mode

logger = logging.getLogger(__name__)

MAX_CONTEXT_PACK_CHARS = 3000
_GIT_TIMEOUT = 5
_LISTING_LIMIT = 15
_KEYWORD_LIMIT = 5
_KEYWORD_FILES = 8
_KEYWORD_SEARCH_SECONDS = 3.0

_SKIP_DIRS = {
    ".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build",
    ".pytest_cache", ".mypy_cache", ".idea", ".vscode",
}
_TEXT_SUFFIXES = {
    ".py", ".js", ".mjs", ".ts", ".tsx", ".jsx", ".vue", ".md", ".json",
    ".toml", ".yaml", ".yml", ".txt", ".cfg", ".ini", ".html", ".css", ".sql",
}
_STOPWORDS = {
    "the", "and", "for", "with", "from", "that", "this", "you", "your",
    "are", "not", "use", "using", "into", "all", "any", "can", "will",
    "目标", "任务", "子任务", "输出", "产出", "验收", "标准", "禁止",
    "要求", "需要", "必须", "不要", "请用", "当前", "相关", "文件", "代码",
    "一个", "这个", "进行", "查看", "列出", "给出", "以及", "或者", "如果",
}

_MANIFEST_RULES = (
    ("package.json", "Node/前端"),
    ("pyproject.toml", "Python"),
    ("requirements.txt", "Python"),
    ("Cargo.toml", "Rust"),
    ("go.mod", "Go"),
    ("pom.xml", "Java/Maven"),
    ("build.gradle", "Java/Gradle"),
    ("docker-compose.yml", "Docker"),
    ("Dockerfile", "Docker"),
)


def _run(cmd: list[str], cwd: Path | None = None, timeout: float = _GIT_TIMEOUT) -> str:
    kwargs: dict = {}
    if os.name == "nt":
        # 后端是隐藏常驻进程；git/grep 子进程不要弹控制台窗口
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            **kwargs,
        )
        return (proc.stdout or "").strip()
    except Exception as exc:  # noqa: BLE001 - 探测失败只影响提示质量
        logger.debug("上下文包命令失败（%s）：%s", " ".join(cmd[:3]), exc)
        return ""


def _is_git_repo(workspace: Path) -> bool:
    return _run(["git", "-C", str(workspace), "rev-parse", "--is-inside-work-tree"]) == "true"


def _trim(text: str, limit: int) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "…（已截断）"


def _listing(workspace: Path) -> str:
    entries: list[str] = []
    try:
        names = sorted(os.listdir(workspace))
    except OSError as exc:
        logger.debug("上下文包列目录失败：%s", exc)
        return ""
    for name in names:
        if len(entries) >= _LISTING_LIMIT:
            break
        if name.startswith(".") or name in ("node_modules", "__pycache__"):
            continue
        full = workspace / name
        try:
            stat = os.stat(full)
            kind = "/" if os.path.isdir(full) else ""
            stamp = time.strftime("%Y-%m-%d %H:%M", time.localtime(stat.st_mtime))
            entries.append(f"  - {name}{kind}  ({stat.st_size} B, {stamp})")
        except OSError:
            entries.append(f"  - {name}")
    if not entries:
        return ""
    return (
        "工作目录顶层内容（含修改时间，用于识别历史遗留产物）：\n"
        + "\n".join(entries)
    )


def _git_section(workspace: Path) -> str:
    if not _is_git_repo(workspace):
        return ""
    status = _trim(
        _run(["git", "-C", str(workspace), "status", "--short"]), 600
    )
    diff = _trim(
        _run(["git", "-C", str(workspace), "diff", "--stat"]), 400
    )
    parts = []
    if status:
        parts.append("git status --short：\n" + status)
    if diff:
        parts.append("git diff --stat：\n" + diff)
    if not parts:
        return "Git 状态：工作区干净（无未提交改动）。"
    return "Git 状态（只读，改动/新增即本任务相关线索）：\n" + "\n\n".join(parts)


def _manifest_section(workspace: Path) -> str:
    lines: list[str] = []
    for name, kind in _MANIFEST_RULES:
        path = workspace / name
        if not path.is_file():
            continue
        head = ""
        try:
            if name == "package.json":
                data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
                deps = sorted(
                    set(data.get("dependencies") or {})
                    | set(data.get("devDependencies") or {})
                )[:10]
                scripts = list((data.get("scripts") or {}).keys())[:6]
                pieces = [f"name={data.get('name') or ''}"]
                if scripts:
                    pieces.append("scripts=" + ",".join(scripts))
                if deps:
                    pieces.append("deps=" + ",".join(deps))
                head = "；".join(pieces)
            elif name == "requirements.txt":
                items = [
                    line.strip()
                    for line in path.read_text(encoding="utf-8", errors="replace").splitlines()
                    if line.strip() and not line.strip().startswith("#")
                ][:12]
                head = ", ".join(items)
            else:
                text = path.read_text(encoding="utf-8", errors="replace")[:1500]
                head = " ".join(text.split())[:300]
        except Exception as exc:  # noqa: BLE001
            logger.debug("清单解析失败 %s：%s", name, exc)
            head = ""
        lines.append(f"- {name}（{kind}）" + (f"：{_trim(head, 320)}" if head else ""))
    if not lines:
        return ""
    return "项目清单：" + "\n".join(lines)


def _keywords(task: str, limit: int = _KEYWORD_LIMIT) -> list[str]:
    tokens = re.findall(r"[A-Za-z_][A-Za-z0-9_.\-/]{2,}|[一-鿿]{2,8}", task or "")
    picked: list[str] = []
    for token in tokens:
        key = token.strip(".,;:!?()[]{}<>\"'`")
        lower = key.lower()
        if len(key) < 3 or lower in _STOPWORDS or lower in {p.lower() for p in picked}:
            continue
        picked.append(key)
        if len(picked) >= limit:
            break
    return picked


def _keyword_files_git(workspace: Path, keywords: list[str]) -> list[str]:
    pattern = "|".join(re.escape(k) for k in keywords)
    out = _run(
        [
            "git", "-C", str(workspace), "grep", "--untracked", "-il", "-E",
            pattern, "--", ".",
        ],
        timeout=_GIT_TIMEOUT,
    )
    return [line.lstrip("./") for line in out.splitlines() if line.strip()][:_KEYWORD_FILES]


def _keyword_files_walk(workspace: Path, keywords: list[str]) -> list[str]:
    lowered = [k.lower() for k in keywords]
    found: list[str] = []
    deadline = time.monotonic() + _KEYWORD_SEARCH_SECONDS
    scanned = 0
    for root, dirs, files in os.walk(workspace):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS and not d.startswith(".")]
        for name in files:
            if len(found) >= _KEYWORD_FILES or time.monotonic() > deadline:
                return found
            path = Path(root) / name
            if path.suffix.lower() not in _TEXT_SUFFIXES:
                continue
            scanned += 1
            if scanned > 400:
                return found
            try:
                text = path.read_text(encoding="utf-8", errors="ignore")[:65536].lower()
            except OSError:
                continue
            if any(k in text for k in lowered):
                found.append(path.relative_to(workspace).as_posix())
    return found


def _keyword_section(workspace: Path, task: str) -> str:
    keywords = _keywords(task)
    if not keywords:
        return ""
    files = (
        _keyword_files_git(workspace, keywords)
        if _is_git_repo(workspace)
        else _keyword_files_walk(workspace, keywords)
    )
    if not files:
        return ""
    return (
        "任务关键词命中文件（只读探测，优先从这些文件入手）："
        f"关键词={', '.join(keywords)}\n"
        + "\n".join(f"  - {p}" for p in files)
    )


def _summary_section(summary_text: str | None) -> str:
    text = _trim(summary_text or "", 600)
    if not text:
        return ""
    return "近期对话摘要（只读，供判断任务背景）：\n" + text


def build_context_pack(
    workspace: Path,
    *,
    task: str = "",
    mode: str = "research",
    tool_names: list[str] | tuple[str, ...] | None = None,
    summary_text: str | None = None,
) -> str:
    """组装上下文包（≤ MAX_CONTEXT_PACK_CHARS）。"""
    canonical = normalize_mode(mode)
    sections: list[str] = [f"工作目录：{workspace}"]
    sections.append(
        "本子任务可用工具："
        + "、".join(list(tool_names or mode_tools(canonical)))
    )
    hint = MODE_PACK_HINTS.get(canonical)
    if hint:
        sections.append(hint)
    sections.append(_listing(workspace))
    sections.append(_git_section(workspace))
    sections.append(_manifest_section(workspace))
    sections.append(_keyword_section(workspace, task))
    sections.append(_summary_section(summary_text))
    pack = "\n\n".join(s for s in sections if s)
    if len(pack) <= MAX_CONTEXT_PACK_CHARS:
        return pack
    return pack[:MAX_CONTEXT_PACK_CHARS].rstrip() + "\n…（上下文包已截断）"
