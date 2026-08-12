"""轨迹压缩（P1）：把长任务的计划与工具轨迹压缩成"过程摘要"。

与滚动摘要互补：滚动摘要压缩"对话历史"，轨迹压缩压缩"单次任务的中间过程"。
工具调用 >= 3 次时在 finalize 后生成，存 app_meta.trajectory_summary_{conv_id}，
下次提问时注入上下文，避免长任务信息丢失。
"""

from __future__ import annotations

import json
import logging

from langchain_core.messages import HumanMessage

from .db import repository as repo

logger = logging.getLogger(__name__)


def _meta_key(conversation_id: int) -> str:
    return f"trajectory_summary_{conversation_id}"


def compress_trajectory(
    chat,
    db,
    conversation_id: int,
    question: str,
    plan: list,
    tool_trace: list,
    max_chars: int = 300,
) -> str | None:
    """用 LLM 把一轮任务的计划+工具轨迹压缩成过程摘要并持久化。"""
    if not tool_trace or len(tool_trace) < 3:
        return None
    steps = "\n".join(
        f"{i + 1}. {t.get('name')}({json.dumps(t.get('arguments') or {}, ensure_ascii=False)[:120]})"
        f" -> {str(t.get('summary') or '')[:120]}"
        for i, t in enumerate(tool_trace[:10])
    )
    prompt = (
        "把下面一次任务的执行轨迹压缩成一份不超过"
        f"{max_chars} 字的'任务过程摘要'（第三人称，说明做了什么、得到什么结论、"
        "还有哪些未完成/存疑的点；之后同类问题可直接复用该摘要，不用重新完整执行）。\n\n"
        f"任务：{question[:300]}\n"
        f"计划：{'；'.join(plan) if plan else '（无）'}\n"
        f"工具轨迹：\n{steps}\n\n只输出摘要本身。"
    )
    try:
        resp = chat([HumanMessage(content=prompt)])
        text = (resp.content or "").strip()
        if not text:
            return None
        repo.set_meta(db, _meta_key(conversation_id), text[: max_chars * 2])
        return text
    except Exception as exc:
        logger.warning("轨迹压缩失败：%s", exc)
        return None


def load_trajectory_summary(db, conversation_id: int) -> str | None:
    """读取该会话最近一次长任务的轨迹摘要。"""
    try:
        return repo.get_meta(db, _meta_key(conversation_id))
    except Exception:
        return None
