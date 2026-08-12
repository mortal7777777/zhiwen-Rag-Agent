"""欢迎页每日建议：知识库相关 + 热点新闻 + 通用，按天缓存。"""

from __future__ import annotations

import json
import logging
from datetime import date

from fastapi import APIRouter, Depends
from langchain_core.messages import HumanMessage
from sqlalchemy.orm import Session

from ..agent.agent import AgentService
from ..db import get_db
from ..db import repository as repo
from ..rag.service import RAGService
from .deps import get_agent_service, get_service

logger = logging.getLogger(__name__)
router = APIRouter(tags=["suggestions"])

GENERAL_POOL = [
    "帮我写一封简洁的工作周报",
    "用通俗的语言解释大模型是怎么工作的",
    "给我一个学习 RAG 的 30 天路线图",
    "帮我把这段话润色得更专业",
]


def _kb_question(service: RAGService, day_index: int) -> str:
    try:
        docs = [
            d
            for d in service.list_documents()
            if d.get("size", 0) > 2000 and "欢迎" not in d.get("name", "")
        ]
        if docs:
            doc = docs[day_index % len(docs)]
            base = doc["name"]
            for suffix in (".pdf", ".doc", ".docx", ".epub", ".txt", ".md"):
                if base.lower().endswith(suffix):
                    base = base[: -len(suffix)]
                    break
            # 去掉 "1_" 这类序号前缀
            base = base.lstrip("0123456789_.- ")
            return f"知识库里的《{base}》主要讲了什么？"
    except Exception as exc:
        logger.warning("生成知识库建议失败：%s", exc)
    return "帮我总结一下知识库里《示例书》的核心观点"


def _news_question(agent: AgentService) -> str:
    try:
        from ddgs import DDGS

        raw = list(DDGS().news("今日热点", region="cn-zh", max_results=5))
        titles = [i.get("title") for i in raw if i.get("title")][:5]
        if titles:
            prompt = (
                "根据以下今日新闻标题，生成一个普通用户会向智能助手提出的问题，"
                "要求能引出这些新闻内容。只输出问题本身，不要解释。\n"
                + "\n".join(f"- {t}" for t in titles)
            )
            resp = agent.title_chat.invoke([HumanMessage(content=prompt)])
            question = (resp.content or "").strip().strip('"“”')
            if question and len(question) < 60:
                return question
    except Exception as exc:
        logger.warning("生成热点建议失败：%s", exc)
    return "今天有什么值得关注的新闻？"


def _build_items(agent: AgentService, service: RAGService) -> list[dict]:
    today = date.today()
    day_index = today.toordinal()
    return [
        {"type": "kb", "text": _kb_question(service, day_index)},
        {"type": "news", "text": _news_question(agent)},
        {"type": "general", "text": GENERAL_POOL[day_index % len(GENERAL_POOL)]},
    ]


@router.get("/suggestions")
def suggestions(
    db: Session = Depends(get_db),
    agent: AgentService = Depends(get_agent_service),
    service: RAGService = Depends(get_service),
) -> list[dict]:
    """按天返回 3 条建议（知识库/热点/通用），当天内缓存。"""
    today = date.today().isoformat()
    cache_key = f"suggestions_{today}"
    cached = repo.get_meta(db, cache_key)
    if cached:
        try:
            return json.loads(cached)
        except Exception:
            pass
    items = _build_items(agent, service)
    try:
        repo.set_meta(db, cache_key, json.dumps(items, ensure_ascii=False))
    except Exception as exc:
        logger.warning("建议缓存写入失败：%s", exc)
    return items
