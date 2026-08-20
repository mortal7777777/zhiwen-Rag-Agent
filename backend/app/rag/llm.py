"""生成模型封装：回答生成 + Query 改写（当前使用 DeepSeek，OpenAI 兼容接口）。

扩展点：如果以后要换其他模型，只需实现相同的 generate() 接口。
"""

from __future__ import annotations

import json
import re

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI

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
3. 如果问题暗指某句名言或著名论断（例如"枪杆子里面出政权""实事求是""星星之火可以燎原"），
   必须把这句话原样写进至少一个查询；
4. 只输出 JSON 数组，数组元素是字符串，不要任何多余解释。

用户问题：{question}
"""

MULTI_TURN_TEMPLATE = """根据对话历史，把用户的最新问题改写成一条不依赖上下文的独立问题。
要求：
1. 把"它""这篇""那篇文章""刚才说的"等指代补全成具体内容；
2. 只输出改写后的问题本身，不要解释、不要引号、不要前缀。

用户最新问题：{question}
"""

HYDE_TEMPLATE = """请针对用户的问题，写一段 100~200 字的假设性知识库文档片段。
要求：
1. 内容应与真实知识库（如《示例选集》等著作）的风格一致，直接陈述可能回答该问题的内容；
2. 包含可能出现的专有名词、篇名、术语和关键表述；
3. 只输出文档片段本身，不要解释、不要引号。

用户问题：{question}
"""


class DeepSeekChat:
    """基于检索上下文的生成模型。"""

    def __init__(
        self,
        api_key: str,
        base_url: str,
        model: str,
        temperature: float = 0.3,
        rewrite_temperature: float = 0.0,
        thinking_enabled: bool = True,
        thinking_effort: str = "high",
    ):
        from ..runtime_config import thinking_extra_body

        cfg = {
            "thinking_enabled": thinking_enabled,
            "thinking_effort": thinking_effort,
        }
        self._llm = ChatOpenAI(
            api_key=api_key,
            base_url=base_url,
            model=model,
            temperature=temperature,
            extra_body=thinking_extra_body(cfg),
        )
        # 改写用温度 0：同样的输入必须产出同样的改写查询，保证检索结果可复现。
        # 检索改写是确定性输出，关闭思考（思考模式下 temperature 无效）
        self._rewrite_llm = ChatOpenAI(
            api_key=api_key,
            base_url=base_url,
            model=model,
            temperature=rewrite_temperature,
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
        return response.content

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
            if chunk.content:
                yield chunk.content

    def rewrite_queries(
        self,
        question: str,
        history: list[dict] | None = None,
        n: int = 3,
    ) -> list[str]:
        """把模糊问题改写成多个检索查询；失败时退化为原问题。

        例如"那篇关于实践的文章讲了啥？" -> ["《示例书》的主要内容是什么？", ...]
        """
        messages = self._build_messages(
            REWRITE_TEMPLATE.format(n=n, question=question),
            question,
            history,
        )

        try:
            response = self._rewrite_llm.invoke(messages, config=self._config())
            queries = self._parse_queries(response.content)
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

        例如历史提到《示例书》，当前问"它的主要观点是什么？"，
        会补全为"《示例书》的主要观点是什么？"。
        """
        if not history:
            return question
        messages = self._build_messages(
            MULTI_TURN_TEMPLATE.format(question=question),
            question,
            history,
        )
        try:
            content = self._rewrite_llm.invoke(
                messages, config=self._config()
            ).content.strip()
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
            HYDE_TEMPLATE.format(question=question),
            question,
            history,
        )
        try:
            content = self._rewrite_llm.invoke(
                messages, config=self._config()
            ).content.strip()
            return content if content else question
        except Exception:
            return question

    def _build_messages(
        self,
        system_content: str,
        question: str,
        history: list[dict] | None,
    ) -> list:
        """组装 system + history + 当前问题的消息列表。"""
        messages = [SystemMessage(content=system_content)]
        for item in history or []:
            content = item.get("content", "")
            if item.get("role") == "user":
                messages.append(HumanMessage(content=content))
            elif item.get("role") == "assistant":
                messages.append(AIMessage(content=content))
        messages.append(HumanMessage(content=question))
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
