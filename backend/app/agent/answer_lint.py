"""答复核对：把 mutation 产生的关键事实与最终答复比对，缺口用系统块补齐。

只处理结构化事实（ID / 金额 / 状态），不做自由文本语义判断；只在缺口存在时
追加一段"执行结果核对"，不触发额外 LLM 调用。追加内容会随最终消息一起落库，
因此必须在 finalize 持久化之前调用。
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any

_SUMMARY_LIMIT = 700
_CURRENCY_RE = re.compile(r"(?<=\d),(?=\d)")


def normalize_claim_text(text: Any) -> str:
    s = unicodedata.normalize("NFKC", str(text or "")).casefold()
    s = _CURRENCY_RE.sub("", s)
    for token in ("$", "usd", "#", " "):
        s = s.replace(token, "")
    return re.sub(r"\s+", " ", s).strip()


def _amount_present(normalized_text: str, value: str) -> bool:
    text = normalized_text.replace(" ", "")
    value = str(value).strip()
    if not value:
        return False
    if value in text:
        return True
    # 1288.00 / 1288.0 允许只写整数部分
    try:
        number = float(value)
    except ValueError:
        return False
    if number == int(number):
        return str(int(number)) in text
    return False


def missing_claims(text: str, claims: list[dict]) -> list[dict]:
    normalized = normalize_claim_text(text)
    missing: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for claim in claims or []:
        kind = str(claim.get("kind") or "")
        value = str(claim.get("value") or "").strip()
        if not value:
            continue
        key = (kind, value.casefold())
        if key in seen:
            continue
        seen.add(key)
        if kind == "amount":
            present = _amount_present(normalized, value)
        elif kind == "payment" and " $" in value:
            # payment claim = "完整支付 ID $金额"：两者都出现才算满足
            pid, amount = value.rsplit(" $", 1)
            present = (
                normalize_claim_text(pid) in normalized
                and _amount_present(normalized, amount)
            )
        else:
            present = normalize_claim_text(value) in normalized
        if not present:
            missing.append(claim)
    return missing


def render_action_summary(missing: list[dict]) -> str:
    """把缺失事实按 (tool,target) 分组，生成 ≤700 字符的系统核对块。"""
    groups: dict[tuple[str, str], list[str]] = {}
    for claim in missing or []:
        tool = str(claim.get("tool") or "").strip()
        target = str(claim.get("target") or "").strip()
        label = str(claim.get("label") or "").strip()
        value = str(claim.get("value") or "").strip()
        if not value:
            continue
        key = (tool, target)
        part = f"{label} {value}" if label else value
        bucket = groups.setdefault(key, [])
        if part not in bucket:
            bucket.append(part)
    if not groups:
        return ""
    lines: list[str] = []
    for (tool, target), parts in groups.items():
        head = tool or "action"
        if target:
            head = f"{head} → {target}"
        lines.append(f"- {head}: " + "；".join(parts))
    body = "\n".join(lines)
    prefix = "\n\n**执行结果核对（系统）**：\n"
    text = prefix + body
    if len(text) > _SUMMARY_LIMIT:
        text = text[: _SUMMARY_LIMIT - 1] + "…"
    return text


def apply_lint(
    text: str, ledger: list[dict]
) -> tuple[str, str, list[dict]]:
    """返回 (新文本, 追加块, 缺失事实)；无缺口时追加块为空。"""
    if not text or not ledger:
        return text, "", []
    claims: list[dict] = []
    for entry in ledger:
        if not entry.get("ok", True):
            continue
        for claim in entry.get("claims") or []:
            claims.append(claim)
    if not claims:
        return text, "", []
    missing = missing_claims(text, claims)
    if not missing:
        return text, "", []
    appended = render_action_summary(missing)
    if not appended:
        return text, "", missing
    return text + appended, appended, missing
