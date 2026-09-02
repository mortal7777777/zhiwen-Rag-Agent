"""查询扩展编排：把模糊问题扩展成多条检索查询。

组合三种互补策略：
1. Multi-Query   —— 让 LLM 从不同角度改写问题（同义、篇名、术语）；
2. HyDE          —— 让 LLM 写一段"假设性答案"，其语义比问题更接近文档；
3. 多轮补全      —— 结合对话历史，把"它""这篇"等指代补全成独立问题。

所有策略都做失败降级：LLM 不可用时只保留原问题，不影响主流程。
"""

from __future__ import annotations

import logging
import time

from .llm import DeepSeekChat

logger = logging.getLogger(__name__)

# 单次查询总数上限，避免检索耗时和候选噪声失控
MAX_QUERIES = 4

# 常见复杂任务词：命中即视为需要扩展
COMPLEX_KEYWORDS = (
    "总结", "分析", "对比", "区别", "原理", "步骤", "方案", "如何", "为什么",
    "优缺点", "推荐", "建议", "比较", "原因", "影响", "关系", "含义", "评价",
    "整理", "综述", "概括", "详细", "介绍",
    # 2026-08 补:书名/主题类问法也走扩展(实测 13~15 字书名题因跳过扩展,
    # 原查询与库内表述错位,context_recall 归零)
    "观点", "主题", "内容", "主要", "核心", "讲什么", "是什么", "属于",
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
        # 简单内存缓存：相同问题（含近一轮历史）的扩展结果直接复用
        self._cache: dict[str, tuple[float, list[str]]] = {}
        self._cache_ttl = 600.0

    def expand(
        self,
        question: str,
        history: list[dict] | None = None,
    ) -> list[str]:
        """返回扩展后的查询列表，原问题永远排第一。"""
        cache_key = self._cache_key(question, history)
        hit = self._cache.get(cache_key)
        if hit and time.time() - hit[0] < self._cache_ttl:
            return list(hit[1])

        queries: list[str] = [question]
        simple = self._is_simple(question, history)

        # Multi-Query / HyDE / 多轮补全彼此独立，并行调用可把扩展耗时减半
        from concurrent.futures import ThreadPoolExecutor

        futures: dict[str, object] = {}
        with ThreadPoolExecutor(max_workers=3) as pool:
            if self._use_multi_turn and history:
                futures["disambiguate"] = pool.submit(
                    self._chat.disambiguate, question, history
                )
            if not simple:
                if self._use_multi_query:
                    futures["rewrite"] = pool.submit(
                        self._chat.rewrite_queries,
                        question,
                        history,
                        n=self._variants,
                    )
                if self._use_hyde:
                    futures["hyde"] = pool.submit(
                        self._chat.hypothetical_document, question, history
                    )

            if "disambiguate" in futures:
                try:
                    standalone = futures["disambiguate"].result()
                    if standalone and standalone != question:
                        queries.append(standalone)
                except Exception as exc:
                    logger.warning("多轮补全失败：%s", exc)
            if not simple:
                if "rewrite" in futures:
                    try:
                        rewritten = futures["rewrite"].result()
                        queries.extend(rewritten or [])
                    except Exception as exc:
                        logger.warning("Multi-Query 失败：%s", exc)
                if "hyde" in futures:
                    try:
                        hypothetical = futures["hyde"].result()
                        if hypothetical and hypothetical != question:
                            queries.append(hypothetical)
                    except Exception as exc:
                        logger.warning("HyDE 失败：%s", exc)

        # 去重并截断
        seen: set[str] = set()
        result: list[str] = []
        for query in queries:
            key = query.strip()
            if key and key not in seen:
                seen.add(key)
                result.append(query)
        result = result[:MAX_QUERIES]
        self._cache[cache_key] = (time.time(), result)
        if len(self._cache) > 128:
            self._cache.clear()
        return result

    @staticmethod
    def _cache_key(question: str, history: list[dict] | None) -> str:
        """缓存键：问题 + 最近一条历史的后 60 字符（足够区分指代语境）。"""
        tail = ""
        if history:
            last = history[-1]
            tail = "|" + str(last.get("content") or "")[-60:]
        return question.strip()[:200] + tail

    @staticmethod
    def _is_simple(question: str, history: list[dict] | None) -> bool:
        q = question.strip()
        if len(q) > 16:
            return False
        # 书名/篇名查询强制扩展:原查询常与库内表述错位(如书名题"《某书》的主要
        # 观点是什么?"),Multi-Query 改写能补上"篇名+术语"角度,提高召回
        if "《" in q or "》" in q:
            return False
        if any(k in q for k in COMPLEX_KEYWORDS):
            return False
        if any(a in q for a in ANAPHORA):
            return False
        return True
