"""子代理结构化结果协议（Phase A 6.3）。

子代理在摘要末尾输出一个 ```json 围栏，服务端解析为 findings/open_questions。
解析失败必须无损回退：保留原始摘要、structured=False，绝不阻断 merge。
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

SEVERITIES = ("CRITICAL", "HIGH", "MEDIUM", "LOW")
MAX_FINDINGS = 20
MAX_OPEN_QUESTIONS = 10

_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.S | re.I)
_SEVERITY_ALIASES = {
    "CRITICAL": "CRITICAL",
    "BLOCKER": "CRITICAL",
    "FATAL": "CRITICAL",
    "HIGH": "HIGH",
    "MAJOR": "HIGH",
    "ERROR": "HIGH",
    "MEDIUM": "MEDIUM",
    "MODERATE": "MEDIUM",
    "WARN": "MEDIUM",
    "WARNING": "MEDIUM",
    "LOW": "LOW",
    "MINOR": "LOW",
    "INFO": "LOW",
}


def _last_json_fence(text: str) -> dict[str, Any] | None:
    """返回最后一个能解析成 dict 的 json 围栏（忽略非 JSON 围栏）。"""
    matches = list(_FENCE_RE.finditer(text or ""))
    if not matches:
        return None
    raw = matches[-1].group(1)
    try:
        data = json.loads(raw)
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _text(value: Any, limit: int) -> str:
    return str(value or "").strip()[:limit]


def _confidence(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.5
    return max(0.0, min(1.0, round(number, 3)))


def _line(value: Any) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _normalize_finding(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    claim = _text(raw.get("claim"), 1000)
    if not claim:
        return None
    severity = _SEVERITY_ALIASES.get(
        str(raw.get("severity") or "").strip().upper(), "MEDIUM"
    )
    return {
        "severity": severity,
        "confidence": _confidence(raw.get("confidence")),
        "path": _text(raw.get("path"), 300),
        "line": _line(raw.get("line")),
        "claim": claim,
        "evidence": _text(raw.get("evidence"), 2000),
        "trigger": _text(raw.get("trigger"), 1000),
    }


def _strip_fences(text: str) -> str:
    return _FENCE_RE.sub("", text or "").strip()


def parse_structured_result(text: str) -> dict[str, Any]:
    """解析子代理输出。

    返回 {summary, findings, open_questions, structured}：
    - 最后的 json 围栏合法：summary 用其中的 summary（为空则用围栏外正文），
      findings/open_questions 归一化，structured=True；
    - 无围栏 / JSON 非法 / 字段不可用：原文作为 summary，structured=False。
    """
    original = str(text or "").strip()
    data = _last_json_fence(original)
    if data is None:
        return {
            "summary": original,
            "findings": [],
            "open_questions": [],
            "structured": False,
        }
    findings = [
        item
        for item in (
            _normalize_finding(raw) for raw in (data.get("findings") or [])[:MAX_FINDINGS]
        )
        if item
    ]
    questions = [
        q
        for q in (
            _text(raw, 500)
            for raw in (data.get("open_questions") or [])[:MAX_OPEN_QUESTIONS]
        )
        if q
    ]
    summary = _text(data.get("summary"), 4000) or _strip_fences(original)
    if not summary and not findings and not questions:
        return {
            "summary": original,
            "findings": [],
            "open_questions": [],
            "structured": False,
        }
    return {
        "summary": summary,
        "findings": findings,
        "open_questions": questions,
        "structured": True,
    }
