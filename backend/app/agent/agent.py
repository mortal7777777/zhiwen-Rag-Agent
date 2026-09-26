"""Agent 服务基类：模型 / 上下文 / 视觉的懒加载，以及对外的 run / run_json 入口。

**真正的编排不在这里。** `LangGraphAgentService`（langgraph_agent.py）继承本类
并覆盖 `_run`，把流程交给 LangGraph 图（graph.py + nodes/*）。本类只剩下：

1. 模型句柄：chat / title_chat（按供应商 API 格式构建，支持工具调用）；
2. 上下文工程服务：context（摘要 / 记忆 / 规划）；
3. 视觉客户端：vision（SenseNova，惰性加载，未配 key 时不可用）；
4. 事件流入口：run（逐事件产出）与 run_json（非流式聚合）——两者都通过
   `self._run` 委派，由子类决定具体实现；
5. 共享辅助：模板解析、工具开关解析、会话标题生成。

历史上这里有一套手工 ReAct 循环（为规避 create_agent 的
GRAPH_RECURSION_LIMIT 而写），已被图编排取代并删除。
"""

from __future__ import annotations

import logging
import threading

from langchain_core.messages import HumanMessage
from langchain_core.language_models import BaseChatModel
from sqlalchemy.orm import Session

from ..config import Settings
from ..db import repository as repo
from ..llm_text import message_text
from ..rag.service import RAGService
from ..runtime_config import (
    build_chat_model,
    chat_provider_config,
    effective,
    thinking_extra_body,
    thinking_param,
)
from ..tracing import get_aux_usage_collector
from .context import ContextService

logger = logging.getLogger(__name__)


class AgentService:
    """把 RAG 服务包装成可自主决定调用工具的多轮 Agent。"""

    def __init__(self, settings: Settings, rag_service: RAGService):
        self.settings = settings
        self.rag = rag_service
        self._chat: BaseChatModel | None = None
        self._chat_plain: BaseChatModel | None = None
        self._title_chat: BaseChatModel | None = None
        self._context: ContextService | None = None
        self._vision = None

    # ---------------- 模型 ----------------

    @property
    def chat(self) -> BaseChatModel:
        """主对话模型（按供应商 API 格式构建，支持工具调用）。"""
        if self._chat is None:
            cfg = chat_provider_config(self.settings) or {}
            if not cfg.get("api_key"):
                raise RuntimeError("未配置对话模型 API Key，请在设置中配置供应商")
            self._chat = build_chat_model(
                cfg,
                temperature=effective(self.settings, "chat_temperature"),
                timeout=180,
                # 思考模式/强度：openai 格式走 extra_body，anthropic 格式走 thinking
                thinking=thinking_param(cfg),
                extra_body=thinking_extra_body(cfg),
            )
        return self._chat

    @property
    def chat_plain(self) -> BaseChatModel:
        """主模型的无思考版本，仅用于空正文重试轮。

        DeepSeek 的 Anthropic 兼容端点上思考 token 计入 max_tokens：一旦某轮
        32000 输出预算全被 reasoning 吃光，正文一个字都不会剩（实测
        answer_len=0、HTTP 200、无异常）。重试若仍开思考大概率重演，因此
        重试轮换成这一实例——其余参数（温度/超时/供应商）与 chat 完全一致，
        只有 thinking 关掉。
        """
        if self._chat_plain is None:
            cfg = chat_provider_config(self.settings) or {}
            if not cfg.get("api_key"):
                raise RuntimeError("未配置对话模型 API Key，请在设置中配置供应商")
            self._chat_plain = build_chat_model(
                cfg,
                temperature=effective(self.settings, "chat_temperature"),
                timeout=180,
                thinking={"type": "disabled"},
                extra_body={"thinking": {"type": "disabled"}},
            )
        return self._chat_plain

    @property
    def title_chat(self) -> BaseChatModel:
        """会话标题生成模型（低温，结果稳定）。"""
        if self._title_chat is None:
            cfg = chat_provider_config(self.settings) or {}
            self._title_chat = build_chat_model(
                cfg,
                model=effective(self.settings, "agent_title_model")
                or cfg.get("model")
                or "deepseek-v4-flash",
                temperature=0.0,
                timeout=60,
                # 标题/摘要等辅助调用关闭思考：更快更省，且不影响主循环
                thinking={"type": "disabled"},
                extra_body={"thinking": {"type": "disabled"}},
            )
        return self._title_chat

    def refresh(self) -> None:
        """设置变更后重置懒加载缓存，使新模型/供应商立即生效。"""
        self._chat = None
        self._chat_plain = None
        self._title_chat = None
        self._context = None
        self._vision = None

    @property
    def context(self) -> ContextService:
        """上下文工程服务：摘要 / 记忆 / 规划（使用低温模型保证稳定）。"""
        if self._context is None:
            self._context = ContextService(self.settings, self.title_chat)
        return self._context

    @property
    def vision(self):
        """SenseNova 视觉模型客户端（懒加载，未配 key 时不可用）。"""
        if self._vision is None:
            from ..rag.vision import SenseNovaVision

            self._vision = SenseNovaVision(self.settings)
        return self._vision

    # ---------------- 入口 ----------------

    def run(
        self,
        question: str,
        use_web_search: bool = False,
        use_knowledge_base: bool = False,
        conversation_id: int | None = None,
        template_id: int | None = None,
        system_prompt: str | None = None,
        db: Session | None = None,
        tool_mode: str = "auto",
        stop_event: threading.Event | None = None,
        images: list[str] | None = None,
        plan_only: bool = False,
        resume_plan: list[str] | None = None,
        project_dir: str | None = None,
        command_sandbox: str | None = None,
    ):
        """执行完整 Agent 流程，逐事件产出（session/vision/plan/tool_start/tool_result/token/done/error/error）。"""
        # 没有外部会话时，自己开一个（SSE 工作线程内使用）
        if db is None:
            try:
                from ..db.database import SessionLocal, db_ready

                if db_ready and SessionLocal is not None:
                    session = SessionLocal()
                    try:
                        yield from self._run(
                            question,
                            use_web_search,
                            use_knowledge_base,
                            conversation_id,
                            template_id,
                            system_prompt,
                            session,
                            tool_mode,
                            stop_event,
                            images,
                            plan_only=plan_only,
                            resume_plan=resume_plan,
                            project_dir=project_dir,
                            command_sandbox=command_sandbox,
                        )
                    finally:
                        session.close()
                    return
            except Exception as exc:
                logger.warning("数据库会话不可用，降级为无记忆模式：%s", exc)
        yield from self._run(
            question,
            use_web_search,
            use_knowledge_base,
            conversation_id,
            template_id,
            system_prompt,
            db,
            tool_mode,
            stop_event,
            images,
            plan_only=plan_only,
            resume_plan=resume_plan,
            project_dir=project_dir,
            command_sandbox=command_sandbox,
        )

    def run_json(
        self,
        question: str,
        use_web_search: bool = False,
        use_knowledge_base: bool = False,
        conversation_id: int | None = None,
        template_id: int | None = None,
        system_prompt: str | None = None,
        db: Session | None = None,
        tool_mode: str = "auto",
        stop_event: threading.Event | None = None,
        images: list[str] | None = None,
        plan_only: bool = False,
        resume_plan: list[str] | None = None,
        project_dir: str | None = None,
        command_sandbox: str | None = None,
    ):
        """非流式入口：收集事件并组装成 {answer, sources, tool_trace}。"""
        answer_parts: list[str] = []
        sources: list[dict] = []
        tool_trace: list[dict] = []
        conv_id: int | None = conversation_id
        title: str | None = None

        for event in self.run(
            question,
            use_web_search,
            use_knowledge_base,
            conversation_id,
            template_id,
            system_prompt,
            db,
            tool_mode,
            stop_event,
            images,
            plan_only=plan_only,
            resume_plan=resume_plan,
            project_dir=project_dir,
            command_sandbox=command_sandbox,
        ):
            name, data = event["event"], event["data"]
            if name == "session":
                conv_id = data.get("conversation_id") or conv_id
                title = data.get("title")
            elif name == "token":
                answer_parts.append(data)
            elif name == "tool_result":
                sources.extend(data.get("sources", []))
            elif name == "done":
                sources = data.get("sources", sources)
                tool_trace = data.get("tool_trace", [])

        return {
            "conversation_id": conv_id,
            "title": title,
            "answer": "".join(answer_parts),
            "sources": sources,
            "tool_trace": tool_trace,
        }

    # ---------------- 辅助 ----------------

    @staticmethod
    def _resolve_tool_flags(
        tool_mode: str,
        use_web_search: bool,
        use_knowledge_base: bool,
    ) -> tuple[bool, bool]:
        """返回 (知识库开关, 联网开关)。tool_mode 优先，兼容旧前端布尔开关。"""
        mode = (tool_mode or "").strip().lower()
        if mode == "knowledge":
            return True, False
        if mode == "web":
            return False, True
        if mode == "none":
            return False, False
        if mode == "auto":
            return True, True
        # 旧前端未传 tool_mode：直接采用布尔开关
        return use_knowledge_base, use_web_search

    def _get_template_content(
        self,
        db: Session | None,
        template_id: int | None,
        system_prompt: str | None,
    ) -> str | None:
        """优先级：会话自定义提示词 > 模板 > None(默认)。"""
        if system_prompt and system_prompt.strip():
            return system_prompt
        if template_id and db is not None:
            try:
                tpl = repo.get_template(db, template_id)
                if tpl is not None:
                    return tpl.content
            except Exception:
                pass
        return None

    def _generate_title(self, question: str) -> str:
        """根据第一句话生成会话标题（失败时截取问题前 20 字）。"""
        try:
            response = self.title_chat.invoke(
                [
                    HumanMessage(
                        content=(
                            "请为以下对话生成一个不超过 20 个字的简短标题，"
                            "直接输出标题本身，不要引号、不要解释、不要标点格式。\n\n"
                            f"用户：{question}"
                        )
                    )
                ],
                config={"callbacks": [get_aux_usage_collector()]},
            )
            title = message_text(response.content).strip().strip('"“”')
            if title:
                return title[:30]
        except Exception as exc:
            logger.warning("生成会话标题失败：%s", exc)
        return question.strip()[:20]
