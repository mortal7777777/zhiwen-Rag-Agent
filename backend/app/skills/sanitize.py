"""技能 · 内容安全（从 app/skills.py 拆出，2026-10-10）。

技能文本风险模式过滤（注入前 sanitize）。"""


from __future__ import annotations

import json
import logging
import os
import re
import shutil
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)

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
