"""检索改写提示词的消息布局（2026-10-23）。

回归：问题曾被同时写进 SystemMessage 模板和最后一条 HumanMessage，
既重复占 token，也把用户输入抬到了系统指令的优先级。
"""

from __future__ import annotations

import pytest

from app.rag.llm import (
    HYDE_TEMPLATE,
    MULTI_TURN_TEMPLATE,
    PER_BOOK_TEMPLATE,
    REWRITE_TEMPLATE,
    DeepSeekChat,
)


@pytest.mark.parametrize(
    "template",
    [REWRITE_TEMPLATE, MULTI_TURN_TEMPLATE, HYDE_TEMPLATE, PER_BOOK_TEMPLATE],
)
def test_templates_do_not_embed_question(template):
    assert "{question}" not in template


def _build(system: str, question: str, prefix: str = "") -> list:
    svc = object.__new__(DeepSeekChat)  # 不触发模型客户端构造
    return svc._build_messages(system, question, None, user_prefix=prefix)


def test_rewrite_question_appears_only_in_human_message():
    question = "那篇文章讲了什么？"
    messages = _build(REWRITE_TEMPLATE.format(n=3), question)
    assert question not in messages[0].content
    assert messages[-1].content == question


def test_per_book_titles_go_to_human_message():
    question = "《A》和《B》的核心观点是什么？"
    prefix = "用户问题点名的书：《A》、《B》\n\n"
    messages = _build(PER_BOOK_TEMPLATE, question, prefix)
    assert "《A》" not in messages[0].content
    assert messages[-1].content == prefix + question
