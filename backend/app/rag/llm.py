"""生成模型封装：回答生成 + Query 改写（当前使用 DeepSeek，OpenAI 兼容接口）。

扩展点：如果以后要换其他模型，只需实现相同的 generate() 接口。
"""

from __future__ import annotations

import json
import re

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from ..llm_text import message_text
from ..tracing import get_usage_collector

SYSTEM_TEMPLATE = """# 角色
你是严谨的知识库问答助手，回答必须以用户提供的文本内容为唯一事实依据。

# 规则
1. 仅使用下方"文本内容"中的信息回答；内容中没有的信息，明确回答"抱歉，提供的文本中没有这个信息"，绝不编造。
2. 先直接给出结论，再用文本中的论据简要支撑；引用时说明出处（文件名/页码）。
3. 当文本内容与常识冲突时，以文本为准，并提示用户这是文档中的说法。
4. 默认使用中文回答，语言简洁准确。

# 文本内容
{context}
"""

REWRITE_TEMPLATE = """你是一个检索查询改写助手。用户会给出一个问题（可能口语化、模糊，或依赖上下文指代）。
请把它改写成 {n} 个适合向量检索和关键词检索的独立查询，要求：
1. 保留原意，把"它""这篇""那篇文章"等指代补全成具体篇名/概念；
2. 每个查询从不同角度表达：原意复述、具体篇名/术语、同义或补充说法；
3. 如果问题指向某句名言或著名论断，保留该表述原样写进至少一个查询；
4. 保持原语言；每条查询不超过 40 字；不得引入问题中没有的新事实；
5. 只输出 JSON 数组，数组元素是字符串，不要任何多余解释。
"""

MULTI_TURN_TEMPLATE = """根据对话历史，把用户最新问题改写成一条不依赖上下文的独立问题。
要求：
1. 把"它""这篇""那篇文章""刚才说的"等指代补全成具体内容；
2. 保持原语言；不得引入对话中没有的新事实；
3. 只输出改写后的问题本身，不要解释、不要引号、不要前缀。
"""

HYDE_TEMPLATE = """请针对用户的问题，写一段 100~200 字的假设性知识库文档片段。
要求：
1. 内容应与用户知识库中真实文档的风格一致，直接陈述可能回答该问题的内容；
2. 包含可能出现的专有名词、篇名、术语和关键表述；
3. 不要编造具体引用、页码或来源，只写可能的表述；
4. 只输出文档片段本身，不要解释、不要引号。
"""

PER_BOOK_TEMPLATE = """用户问题点名了若干本书。请为每本书生成两段面向“书内检索”的文本：
1. query：一条贴近用户问法的短查询；
2. hyde：一段 80~150 字的假设性原文片段（HyDE），用该书可能出现的原词、术语和句式，直接陈述可能回答问题的内容；
3. 只输出 JSON 数组，元素为 {"book": "书名", "query": "查询", "hyde": "假设原文"}，不要解释。
"""


class DeepSeekChat:
    """基于检索上下文的生成模型（API 格式由供应商配置决定）。"""

    def __init__(
        self,
        provider: dict,
        temperature: float = 0.3,
        rewrite_temperature: float = 0.0,
    ):
        from ..runtime_config import build_chat_model, thinking_extra_body, thinking_param

        cfg = provider or {}
        self._llm = build_chat_model(
            cfg,
            temperature=temperature,
            thinking=thinking_param(cfg),
            extra_body=thinking_extra_body(cfg),
        )
        # 改写用温度 0：同样的输入必须产出同样的改写查询，保证检索结果可复现。
        # 检索改写是确定性输出，关闭思考（思考模式下 temperature 无效）
        self._rewrite_llm = build_chat_model(
            cfg,
            temperature=rewrite_temperature,
            thinking={"type": "disabled"},
            extra_body={"thinking": {"type": "disabled"}},
        )

    @staticmethod
    def _config() -> dict:
        """调用时按当前线程注入用量收集器，避免共享实例跨线程串号。"""
        return {"callbacks": [get_usage_collector()]}

    def generate(
        self,
        question: str,
        context: str,
        history: list[dict] | None = None,
    ) -> str:
        """把检索上下文 + 历史对话 + 当前问题组装成消息，调用大模型。"""
        messages = self._build_messages(
            SYSTEM_TEMPLATE.format(context=context),
            question,
            history,
        )
        response = self._llm.invoke(messages, config=self._config())
        return message_text(response.content)

    async def astream(
        self,
        question: str,
        context: str,
        history: list[dict] | None = None,
    ):
        """流式生成回答：逐 token 产出文本片段。"""
        messages = self._build_messages(
            SYSTEM_TEMPLATE.format(context=context),
            question,
            history,
        )
        async for chunk in self._llm.astream(messages, config=self._config()):
            text = message_text(chunk.content)
            if text:
                yield text

    def rewrite_queries(
        self,
        question: str,
        history: list[dict] | None = None,
        n: int = 3,
    ) -> list[str]:
        """把模糊问题改写成多个检索查询；失败时退化为原问题。

        例如"那篇关于某主题的文章讲了啥？" -> ["《具体篇名》的主要内容是什么？", ...]
        """
        messages = self._build_messages(
            REWRITE_TEMPLATE.format(n=n),
            question,
            history,
        )

        try:
            response = self._rewrite_llm.invoke(messages, config=self._config())
            queries = self._parse_queries(message_text(response.content))
            if queries:
                return queries
        except Exception:
            pass
        return [question]

    def disambiguate(
        self,
        question: str,
        history: list[dict] | None = None,
    ) -> str:
        """多轮补全：结合历史把模糊指代消歧成独立问题。

        例如历史提到某篇文档，当前问"它的主要观点是什么？"，
        会补全为"《具体篇名》的主要观点是什么？"。
        """
        if not history:
            return question
        messages = self._build_messages(
            MULTI_TURN_TEMPLATE,
            question,
            history,
        )
        try:
            content = message_text(
                self._rewrite_llm.invoke(messages, config=self._config()).content
            ).strip()
            content = content.strip('"“”')
            return content if content else question
        except Exception:
            return question

    def hypothetical_document(
        self,
        question: str,
        history: list[dict] | None = None,
    ) -> str:
        """HyDE：生成一段假设性文档，作为额外的检索查询。

        假设性文档的语义与真实答案接近，向量检索时可以把它当作查询向量，
        从而缓解"问题表述"与"文档表述"不一致的问题。
        """
        messages = self._build_messages(
            HYDE_TEMPLATE,
            question,
            history,
        )
        try:
            content = message_text(
                self._rewrite_llm.invoke(messages, config=self._config()).content
            ).strip()
            return content if content else question
        except Exception:
            return question

    def per_book_queries(
        self,
        question: str,
        titles: list[str],
        history: list[dict] | None = None,
    ) -> dict[str, list[str]]:
        """为每个点名书生成 1-2 条书内检索查询（一次 LLM 调用）。

        P1：全局 rewrite/HyDE 不知道“哪本书”，抽象问题（如“信念如何塑造
        经验”）容易与书内原句（“你透过你的信念形成实相”）错位。这里按书
        生成查询，供 S3′ 的 book_scoped_children 使用；失败返回空 dict，
        调用方自动退回全局扩展查询。
        """
        titles = [str(t).strip() for t in titles if str(t).strip()]
        if not titles:
            return {}
        messages = self._build_messages(
            PER_BOOK_TEMPLATE,
            question,
            history,
            user_prefix=f"用户问题点名的书：{'、'.join(titles)}\n\n",
        )
        try:
            content = message_text(
                self._rewrite_llm.invoke(messages, config=self._config()).content
            )
            items = self._parse_book_queries(content)
        except Exception:
            return {}
        result: dict[str, list[str]] = {}
        for idx, item in enumerate(items):
            book = str(item.get("book") or "").strip()
            matched = next(
                (
                    t for t in titles
                    if book == t or (book and (book in t or t in book))
                ),
                None,
            )
            if matched is None and len(items) == len(titles):
                matched = titles[idx]
            if matched is None:
                continue
            bucket = result.setdefault(matched, [])
            for key in ("query", "hyde"):
                text = str(item.get(key) or "").strip()
                if text and text not in bucket and len(bucket) < 2:
                    bucket.append(text)
        return result

    @staticmethod
    def _parse_book_queries(content: str) -> list[dict]:
        match = re.search(r"\[.*\]", content or "", re.DOTALL)
        if not match:
            return []
        try:
            data = json.loads(match.group(0))
        except json.JSONDecodeError:
            return []
        if not isinstance(data, list):
            return []
        return [item for item in data if isinstance(item, dict)]

    def _build_messages(
        self,
        system_content: str,
        question: str,
        history: list[dict] | None,
        user_prefix: str = "",
    ) -> list:
        """组装 system + history + 当前问题的消息列表。

        用户问题只出现在最后一条 HumanMessage（不要在 system 模板里重复），
        避免把用户输入抬到系统指令的优先级，也少一份 token。
        """
        messages = [SystemMessage(content=system_content)]
        for item in history or []:
            content = item.get("content", "")
            if item.get("role") == "user":
                messages.append(HumanMessage(content=content))
            elif item.get("role") == "assistant":
                messages.append(AIMessage(content=content))
        messages.append(HumanMessage(content=f"{user_prefix}{question}" if user_prefix else question))
        return messages

    @staticmethod
    def _parse_queries(content: str) -> list[str]:
        """从模型输出里解析 JSON 字符串数组。"""
        match = re.search(r"\[.*\]", content, re.DOTALL)
        if not match:
            return []
        try:
            data = json.loads(match.group(0))
        except json.JSONDecodeError:
            return []
        if not isinstance(data, list):
            return []
        queries = [str(item).strip() for item in data if str(item).strip()]
        return queries
