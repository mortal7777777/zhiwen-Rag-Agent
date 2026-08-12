"""查询扩展编排：把模糊问题扩展成多条检索查询。

组合三种互补策略：
1. Multi-Query   —— 让 LLM 从不同角度改写问题（同义、篇名、术语）；
2. HyDE          —— 让 LLM 写一段"假设性答案"，其语义比问题更接近文档；
3. 多轮补全      —— 结合对话历史，把"它""这篇"等指代补全成独立问题。

所有策略都做失败降级：LLM 不可用时只保留原问题，不影响主流程。
"""

from __future__ import annotations

import logging

from .llm import DeepSeekChat

logger = logging.getLogger(__name__)

# 单次查询总数上限，避免检索耗时和候选噪声失控
MAX_QUERIES = 6

# 常见复杂任务词：命中即视为需要扩展
COMPLEX_KEYWORDS = (
    "总结", "分析", "对比", "区别", "原理", "步骤", "方案", "如何", "为什么",
    "优缺点", "推荐", "建议", "比较", "原因", "影响", "关系", "含义", "评价",
    "整理", "综述", "概括", "详细", "介绍",
)

# 指代词：出现则说明问题依赖上下文，需要多轮补全
ANAPHORA = ("它", "这", "那", "该", "他", "她", "此", "刚才", "上述", "上文", "里面")


class QueryExpander:
    """根据配置组合多种查询扩展策略。"""

    def __init__(
        self,
        chat: DeepSeekChat,
        variants: int = 3,
        use_multi_query: bool = True,
        use_hyde: bool = True,
        use_multi_turn: bool = True,
    ):
        self._chat = chat
        self._variants = variants
        self._use_multi_query = use_multi_query
        self._use_hyde = use_hyde
        self._use_multi_turn = use_multi_turn

    def expand(
        self,
        question: str,
        history: list[dict] | None = None,
    ) -> list[str]:
        """返回扩展后的查询列表，原问题永远排第一。"""
        queries: list[str] = [question]

        if self._use_multi_turn and history:
            standalone = self._chat.disambiguate(question, history)
            if standalone and standalone != question:
                queries.append(standalone)

        # 简单问题（短 + 无复杂任务词 + 无指代）：直接返回，跳过 Multi-Query/HyDE
        if self._is_simple(question, history):
            return queries[:MAX_QUERIES]

        if self._use_multi_query:
            rewritten = self._chat.rewrite_queries(
                question,
                history,
                n=self._variants,
            )
            queries.extend(rewritten)

        if self._use_hyde:
            hypothetical = self._chat.hypothetical_document(question, history)
            if hypothetical and hypothetical != question:
                queries.append(hypothetical)

        # 去重并截断
        seen: set[str] = set()
        result: list[str] = []
        for query in queries:
            key = query.strip()
            if key and key not in seen:
                seen.add(key)
                result.append(query)
        return result[:MAX_QUERIES]

    @staticmethod
    def _is_simple(question: str, history: list[dict] | None) -> bool:
        q = question.strip()
        if len(q) > 16:
            return False
        if any(k in q for k in COMPLEX_KEYWORDS):
            return False
        if any(a in q for a in ANAPHORA):
            return False
        return True
