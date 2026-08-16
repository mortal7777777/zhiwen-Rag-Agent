"""Agent 服务：基于 LangChain 框架的手工 ReAct 循环智能助手。

为什么用"手工循环"而不是 langchain.agents.create_agent：
1. create_agent 底层是 LangGraph，达到 recursion_limit 会直接抛
   GRAPH_RECURSION_LIMIT（用户曾遇到"Recursion limit of 18 reached"）；
2. 手工循环可以精确控制工具调用次数上限，超过上限后强制模型基于已有信息
   直接回答，而不是报错；
3. 事件流（token / tool_start / tool_result）与之前完全一致，仍全部使用
   LangChain 的消息抽象、ChatOpenAI 与 BaseTool，属于 LangChain 框架。

工作方式（与 DeepSeek 网页端类似的"按需调用"）：
1. 根据前端开关用 @tool 组装可用工具（知识库 / 联网搜索 / 识图）；
2. 从 MySQL 加载历史消息 + 滚动摘要 + 长期记忆，注入系统提示；
3. ReAct 循环：模型决定调用哪个工具 -> 执行 -> 再思考，直到给出最终回答；
4. 知识库文档清单注入系统提示，让模型知道"知识库里有哪本书"；
5. 用户上传图片且主模型无视觉能力时，先用 SenseNova 视觉模型自动识图。
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from langchain_core.messages import (
    AIMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_openai import ChatOpenAI
from sqlalchemy.orm import Session

from ..config import Settings
from ..db import repository as repo
from ..rag.service import RAGService
from ..runtime_config import chat_provider_config, effective, vision_provider_config
from ..tracing import get_aux_usage_collector, get_usage_collector, reset_usage, usage_summary, write_trace
from .context import ContextService, estimate_tokens, trim_history_for_budget
from .prompts import compose_system_prompt
from .tools import build_tools, extract_sources

logger = logging.getLogger(__name__)


class AgentService:
    """把 RAG 服务包装成可自主决定调用工具的多轮 Agent。"""

    def __init__(self, settings: Settings, rag_service: RAGService):
        self.settings = settings
        self.rag = rag_service
        self._chat: ChatOpenAI | None = None
        self._title_chat: ChatOpenAI | None = None
        self._context: ContextService | None = None
        self._vision = None

    # ---------------- 模型 ----------------

    @property
    def chat(self) -> ChatOpenAI:
        """主对话模型（LangChain ChatOpenAI，支持工具调用）。"""
        if self._chat is None:
            cfg = chat_provider_config(self.settings) or {}
            if not cfg.get("api_key"):
                raise RuntimeError("未配置对话模型 API Key，请在设置中配置供应商")
            self._chat = ChatOpenAI(
                api_key=cfg.get("api_key"),
                base_url=cfg.get("base_url") or "https://api.deepseek.com",
                model=cfg.get("model") or "deepseek-v4-flash",
                temperature=effective(self.settings, "chat_temperature"),
                request_timeout=180,
                max_retries=2,
            )
        return self._chat

    @property
    def title_chat(self) -> ChatOpenAI:
        """会话标题生成模型（低温，结果稳定）。"""
        if self._title_chat is None:
            cfg = chat_provider_config(self.settings) or {}
            self._title_chat = ChatOpenAI(
                api_key=cfg.get("api_key") or "",
                base_url=cfg.get("base_url") or "https://api.deepseek.com",
                model=effective(self.settings, "agent_title_model")
                or cfg.get("model")
                or "deepseek-v4-flash",
                temperature=0.0,
                request_timeout=60,
            )
        return self._title_chat

    def refresh(self) -> None:
        """设置变更后重置懒加载缓存，使新模型/供应商立即生效。"""
        self._chat = None
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

    # ---------------- 核心流程 ----------------

    def _run(
        self,
        question: str,
        use_web_search: bool,
        use_knowledge_base: bool,
        conversation_id: int | None,
        template_id: int | None,
        system_prompt: str | None,
        db: Session | None,
        tool_mode: str,
        stop_event: threading.Event | None,
        images: list[str] | None,
        plan_only: bool = False,
        resume_plan: list[str] | None = None,
        command_sandbox: str | None = None,  # 兼容基类签名（旧版实现不使用）
    ):
        settings = self.settings
        images = [img for img in (images or []) if img and img.strip()][
            : settings.vision_max_images
        ]

        # 工具开关解析：tool_mode 优先，兼容旧前端布尔开关
        use_knowledge_base, use_web_search = self._resolve_tool_flags(
            tool_mode, use_web_search, use_knowledge_base
        )

        def stopped() -> bool:
            return stop_event is not None and stop_event.is_set()

        run_started = time.perf_counter()
        run_error: str | None = None
        token_usage: dict | None = None
        # 本轮所有 LLM 调用的 token 用量聚合（线程级 callback）
        reset_usage()

        # 1. 会话与历史（MySQL 记忆；失败则降级为无记忆模式）
        conv_id = conversation_id
        conv_title = None
        is_new_conversation = False
        history_messages: list = []
        summary_text: str | None = None
        memory_hits: list[str] = []
        if db is not None:
            try:
                # 模板绑定跟随会话：0/None 表示默认模板
                bound_template_id = template_id or None
                conv = (
                    repo.get_conversation(db, conversation_id)
                    if conversation_id
                    else None
                )
                if conv is None:
                    conv = repo.create_conversation(
                        db, title="新对话", template_id=bound_template_id
                    )
                    is_new_conversation = True
                elif template_id is not None and (conv.template_id or None) != (
                    template_id or None
                ):
                    # 用户在对话中切换了模板（0=默认）：持久化到会话
                    conv = repo.update_conversation(
                        db, conv.id, template_id=template_id
                    )
                conv_id = conv.id
                conv_title = conv.title
                # 会话滚动摘要：早期对话压缩，最近消息按 token 预算裁剪
                summary_text, recent_rows = self.context.compact_conversation(
                    db, conv_id
                )
                recent_rows = trim_history_for_budget(
                    recent_rows, settings.history_max_tokens
                )
                for row in recent_rows:
                    if row["role"] == "user":
                        history_messages.append(HumanMessage(content=row["content"]))
                    elif row["role"] == "assistant":
                        history_messages.append(AIMessage(content=row["content"]))
            except Exception as exc:
                logger.warning("读写会话失败，降级为无记忆模式：%s", exc)
                conv_id = conversation_id
                history_messages = []

        # 2. 新会话自动起标题：后台线程生成，不阻塞首 token（DeepSeek 网页端式后置标题）
        title_holder: list[str] = []
        title_thread: threading.Thread | None = None
        if is_new_conversation and db is not None:
            def _generate_title_async():
                try:
                    from ..db.database import SessionLocal, db_ready

                    if not (db_ready and SessionLocal is not None):
                        return
                    s = SessionLocal()
                    try:
                        title = self._generate_title(question)
                        if title:
                            repo.rename_conversation(s, conv_id, title)
                            title_holder.append(title)
                    finally:
                        s.close()
                except Exception as exc:
                    logger.warning("后台生成会话标题失败：%s", exc)

            title_thread = threading.Thread(target=_generate_title_async, daemon=True)
            title_thread.start()

        # 3. 复杂问题先规划（Plan-and-Execute 轻量版）
        tools_enabled = use_knowledge_base or use_web_search
        plan_steps: list[str] = []
        if tools_enabled:
            try:
                plan_steps = self.context.plan(question)
            except Exception as exc:
                logger.warning("任务规划失败：%s", exc)
                plan_steps = []

        # 4. 知识库文档清单：让模型知道"知识库里有哪本书"，避免直接去联网
        kb_documents: list[str] = []
        if use_knowledge_base:
            try:
                kb_documents = [
                    item["name"] for item in self.rag.list_documents()
                ][:30]
            except Exception as exc:
                logger.warning("获取知识库文档清单失败：%s", exc)
                kb_documents = []

        # 5. 系统提示词 = 模板（或默认） + 工具规则 + 执行计划 + 文档清单
        template_content = self._get_template_content(db, template_id, system_prompt)
        system_prompt_final = compose_system_prompt(
            template_content,
            use_knowledge_base=use_knowledge_base,
            use_web_search=use_web_search,
            kb_documents=kb_documents,
        )
        if plan_steps:
            system_prompt_final += (
                "\n\n# 执行计划\n请按以下步骤执行（无需向用户复述计划）：\n"
                + "\n".join(f"{i + 1}. {step}" for i, step in enumerate(plan_steps))
            )

        yield {
            "event": "session",
            "data": {"conversation_id": conv_id, "title": conv_title},
        }
        if plan_steps:
            yield {"event": "plan", "data": {"steps": plan_steps}}

        # 6. 长期事实记忆召回（按语义，注入相关片段）
        if db is not None and conv_id is not None:
            try:
                self.rag.acquire_gpu()
                try:
                    memory_hits = self.context.retrieve_memories(
                        db, self.rag.embeddings, question
                    )
                finally:
                    self.rag.release_gpu()
            except Exception as exc:
                logger.warning("记忆召回失败：%s", exc)
                memory_hits = []

        # 7. 用户上传图片：主模型无视觉时，先用 SenseNova 识图并注入上下文
        vision_descriptions: list[str] = []
        if (
            images
            and settings.vision_auto_describe
            and not settings.main_model_vision
        ):
            try:
                if self.vision.configured:
                    desc = self.vision.describe_images(images)
                    if desc:
                        vision_descriptions = [desc]
                        yield {
                            "event": "vision",
                            "data": {
                                "provider": "sensenova",
                                "model": (
                                    vision_provider_config(self.settings) or {}
                                ).get("model")
                                or "sensenova-6.8-flash-lite",
                                "descriptions": vision_descriptions,
                            },
                        }
            except Exception as exc:
                logger.warning("图片识别失败，跳过：%s", exc)
                vision_descriptions = []

        # 8. 组装分层上下文：摘要 -> 记忆 -> 图片理解 -> 历史 -> 当前问题
        messages = []
        # 当前时间注入：让模型清楚"现在是什么时候"，判断时效性
        now = datetime.now(ZoneInfo("Asia/Shanghai"))
        weekday = "一二三四五六日"[now.weekday()]
        time_context = f"当前时间：{now:%Y-%m-%d %H:%M}（星期{weekday}，Asia/Shanghai）。"
        if db is not None and use_web_search:
            try:
                digest_raw = repo.get_meta(db, f"suggestions_{now:%Y-%m-%d}")
                if digest_raw:
                    digest = json.loads(digest_raw)
                    news = next(
                        (i for i in digest if i.get("type") == "news"), None
                    )
                    if news:
                        time_context += f"\n今日热点话题（仅供参考，可据此展开）：{news.get('text', '')}"
            except Exception:
                pass
        messages.append(SystemMessage(content=time_context))
        if summary_text:
            messages.append(
                SystemMessage(
                    content="以下是对本会话早期内容的摘要（供参考）：\n" + summary_text
                )
            )
        memory_summary = (
            self.context.get_memory_summary(db)
            if db is not None and conv_id is not None
            else None
        )
        if memory_summary:
            messages.append(
                SystemMessage(
                    content="关于用户（长期记忆摘要，通常应作为默认背景）：\n"
                    + memory_summary
                )
            )
        if memory_hits:
            messages.append(
                SystemMessage(
                    content="相关的长期记忆（如与本轮相关可参考）：\n"
                    + "\n".join(f"- {item}" for item in memory_hits)
                )
            )
        if vision_descriptions:
            messages.append(
                SystemMessage(
                    content=(
                        "用户上传了图片。以下是视觉模型（SenseNova）对图片的理解，"
                        "请据此回答：\n"
                        + "\n".join(
                            f"- 图片{i + 1}：{text}"
                            for i, text in enumerate(vision_descriptions)
                        )
                    )
                )
            )
        messages.extend(history_messages)
        messages.append(HumanMessage(content=question))

        # 9. 按开关组装 LangChain 工具
        skill_enabled_ids = None
        try:
            from ..skills import load_prefs

            skill_enabled_ids = set(
                (load_prefs(db) if db is not None else {}).get("enabled") or []
            )
        except Exception:
            skill_enabled_ids = None
        tools = build_tools(
            self.rag,
            use_web_search,
            use_knowledge_base,
            effective(self.settings, "web_search_provider"),
            settings.tavily_api_key,
            effective(self.settings, "web_search_max_results"),
            crag_enabled=settings.crag_fallback_enabled,
            crag_min_score=settings.crag_min_score,
            vision=self.vision,
            skills_enabled=effective(self.settings, "skills_enabled", True),
            skill_enabled_ids=skill_enabled_ids,
        )

        # 10. 手工 ReAct 循环（带工具调用上限，避免 GRAPH_RECURSION_LIMIT）
        tool_trace: list[dict] = []
        trace_by_id: dict = {}
        sources: list[dict] = []
        final_text = ""
        stopped_flag = False
        max_tool_calls = max(1, settings.agent_max_iterations)
        tool_calls_used = 0
        forced_final = False
        last_call_warned = False

        def model_for_call() -> ChatOpenAI:
            # 强制收尾时不绑定工具 -> 模型无法再调用工具，保证有最终回答
            if forced_final or not tools:
                return self.chat
            return self.chat.bind_tools(tools)

        try:
            while True:
                if stopped():
                    stopped_flag = True
                    break

                # 还剩最后一次工具调用时提前提示：优先补充检索，随后必须作答
                remaining_calls = max_tool_calls - tool_calls_used
                if (
                    tools
                    and plan_steps
                    and remaining_calls == 1
                    and not forced_final
                    and not last_call_warned
                ):
                    last_call_warned = True
                    messages.append(
                        SystemMessage(
                            content=(
                                "你只剩最后一次工具调用机会。若信息仍不完整，"
                                "请利用这次机会做一次补充检索（优先 knowledge_base_search"
                                " 或 web_search）；之后必须给出最终回答。"
                                "若仍不足，明确告诉用户缺少什么，不要编造。"
                            )
                        )
                    )

                chunks: list = []
                # LLM 调用并发上限（生成阶段不占 GPU 锁）
                self.rag.acquire_llm()
                try:
                    stream = model_for_call().stream(
                        messages,
                        config={"callbacks": [get_usage_collector()]},
                    )
                    for chunk in stream:
                        if stopped():
                            stopped_flag = True
                            break
                        chunks.append(chunk)
                        content = getattr(chunk, "content", None)
                        if content:
                            final_text += content
                            yield {"event": "token", "data": content}
                finally:
                    self.rag.release_llm()
                if stopped_flag:
                    break
                if not chunks:
                    break

                merged = chunks[0]
                for chunk in chunks[1:]:
                    merged = merged + chunk
                usage = getattr(merged, "usage_metadata", None)
                if usage:
                    token_usage = dict(usage)
                    # 流式调用不在 on_llm_end 的 llm_output 里，需手动补记到聚合统计
                    get_usage_collector().add_usage(
                        int(usage.get("input_tokens") or 0),
                        int(usage.get("output_tokens") or 0),
                    )
                tool_calls = getattr(merged, "tool_calls", None) or []
                content = merged.content or ""

                messages.append(
                    AIMessage(content=content, tool_calls=list(tool_calls))
                )
                if not tool_calls:
                    break  # 模型给出最终回答

                # 记录并广播工具调用
                tools_by_name = {t.name: t for t in tools}
                for tc in tool_calls:
                    entry = {
                        "name": tc.get("name", ""),
                        "arguments": tc.get("args", {}) or {},
                        "summary": "",
                        "step": len(tool_trace) + 1,
                    }
                    tool_trace.append(entry)
                    if tc.get("id"):
                        trace_by_id[tc["id"]] = entry
                    yield {
                        "event": "tool_start",
                        "data": {
                            "id": tc.get("id"),
                            "name": tc.get("name", ""),
                            "arguments": tc.get("args", {}) or {},
                        },
                    }

                # 逐个执行工具并回填 ToolMessage
                for tc in tool_calls:
                    name = tc.get("name", "")
                    tool = tools_by_name.get(name)
                    if tool is None:
                        result: dict = {"error": f"未知工具：{name}"}
                    elif plan_only and name in (
                        "write_file",
                        "edit_file",
                        "delete_file",
                        "bash",
                        "command_tool",
                    ):
                        result = {
                            "error": "当前为计划模式（只读）：不允许写文件/编辑/删除/执行命令。"
                            "请整理执行计划等待用户确认。",
                            "summary": "计划模式：已拦截该敏感操作",
                        }
                    else:
                        try:
                            tool_t0 = time.perf_counter()
                            result = tool.invoke(tc.get("args") or {})
                            entry["duration_ms"] = round(
                                (time.perf_counter() - tool_t0) * 1000
                            )
                            if not isinstance(result, dict):
                                result = {"result": result}
                        except Exception as exc:
                            logger.warning("工具 %s 执行失败：%s", name, exc)
                            entry["duration_ms"] = -1
                            result = {"error": str(exc)}

                    messages.append(
                        ToolMessage(
                            content=json.dumps(result, ensure_ascii=False),
                            name=name,
                            tool_call_id=tc.get("id") or "",
                        )
                    )
                    parsed = (
                        result
                        if isinstance(result, dict)
                        else {"summary": str(result)}
                    )
                    trace = trace_by_id.get(tc.get("id"))
                    if trace is not None:
                        trace["summary"] = parsed.get("summary", "")
                    tool_sources = (
                        extract_sources(parsed)
                        if name in ("knowledge_base_search", "web_search")
                        else []
                    )
                    sources.extend(tool_sources)
                    yield {
                        "event": "tool_result",
                        "data": {
                            "id": tc.get("id"),
                            "name": name,
                            "summary": parsed.get("summary", ""),
                            "duration_ms": entry.get("duration_ms"),
                            "sources": tool_sources,
                        },
                    }

                tool_calls_used += len(tool_calls)
                # 计划进度回填：让模型知道当前执行到计划第几步
                if plan_steps:
                    done_steps = min(tool_calls_used, len(plan_steps))
                    remaining = plan_steps[done_steps:]
                    progress = (
                        f"计划进度：已完成 {done_steps}/{len(plan_steps)} 步。"
                        + (
                            f"剩余：{'；'.join(remaining)}"
                            if remaining
                            else "剩余：无，可以基于已获取的信息作答。"
                        )
                    )
                    messages.append(SystemMessage(content=progress))
                if tool_calls_used >= max_tool_calls and not forced_final:
                    forced_final = True
                    messages.append(
                        SystemMessage(
                            content=(
                                "你已达到本轮允许的工具调用次数上限。"
                                "请基于已经获取的信息直接给出最终回答；"
                                "如果信息仍然不足，请明确告诉用户缺少什么，不要继续调用工具。"
                            )
                        )
                    )
        except Exception as exc:
            logger.exception("Agent 执行失败")
            run_error = str(exc)
            yield {"event": "error", "data": {"message": str(exc)}}

        # 11. 引用去重（按 index 去重，保证 [n] 编号与来源卡片一一对应）
        deduped: list[dict] = []
        seen_src: set[str] = set()
        for item in sources:
            key = str(item.get("index") or item.get("content", ""))
            if key and key not in seen_src:
                seen_src.add(key)
                deduped.append(item)
        deduped = deduped[:8]

        # 12. 联网来源自动附录：模型未标注 [n] 时，在正文末尾补"参考来源"链接
        appendix = ""
        if final_text and not re.search(r"\[\d{1,3}\]", final_text):
            web_sources = [s for s in deduped if s.get("type") == "web" and s.get("url")]
            if web_sources:
                refs = "\n".join(
                    f"{s.get('index')}. [{s.get('title', '来源')}]({s.get('url')})"
                    for s in web_sources
                )
                appendix = f"\n\n**参考来源**\n{refs}"
                final_text += appendix

        # 13. 持久化 user 消息 + assistant 消息（LangChain 记忆写入 MySQL）
        if db is not None and conv_id is not None:
            try:
                repo.add_message(db, conv_id, "user", question)
                if not stopped_flag and final_text:
                    repo.add_message(
                        db,
                        conv_id,
                        "assistant",
                        final_text,
                        tool_trace=tool_trace,
                        sources=deduped,
                    )
            except Exception as exc:
                logger.warning("保存对话消息失败：%s", exc)

        # 14. 决策运行记录（可观测性：问题、计划、工具轨迹、耗时、状态）
        if db is not None:
            try:
                run_status = (
                    "stopped"
                    if stopped_flag
                    else "error"
                    if run_error
                    else "ok"
                )
                run = repo.create_agent_run(
                    db,
                    conversation_id=conv_id,
                    question=question,
                    plan=plan_steps,
                    tool_trace=tool_trace,
                    answer_len=len(final_text),
                    latency_ms=round((time.perf_counter() - run_started) * 1000),
                    status=run_status,
                    error=run_error,
                    token_usage={**usage_summary(), "last_call": token_usage},
                )
                write_trace(
                    self.settings,
                    run.id,
                    {
                        "conversation_id": conv_id,
                        "question": question[:1000],
                        "plan": plan_steps,
                        "tool_trace": tool_trace,
                        "answer_len": len(final_text),
                        "latency_ms": round(
                            (time.perf_counter() - run_started) * 1000
                        ),
                        "status": run_status,
                        "error": run_error,
                        "memory_hits": memory_hits[:5],
                        "usage": usage_summary(),
                    },
                )
            except Exception as exc:
                logger.warning("写入 Agent 运行记录失败：%s", exc)

        # 15. 标题后置：后台已生成则推送给前端更新（未完成则由会话刷新兜底）
        if title_thread is not None and title_thread.is_alive():
            title_thread.join(timeout=1.0)
        if title_holder:
            yield {"event": "title", "data": {"title": title_holder[0]}}

        # 16. 返回结果；附录作为 token 补发（前端会拼到正文末尾）
        if appendix:
            yield {"event": "token", "data": appendix}
        yield {
            "event": "done",
            "data": {
                "ok": True,
                "stopped": stopped_flag,
                "sources": deduped,
                "tool_trace": tool_trace,
            },
        }

        # 17. 后台提取长期事实记忆（done 之后执行，不阻塞流式输出）
        if (
            db is not None
            and conv_id is not None
            and not stopped_flag
            and final_text
        ):
            try:
                facts = self.context.extract_facts(question, final_text)
                if facts:
                    added = self.context.add_facts(
                        db, self.rag.embeddings, facts, conv_id
                    )
                    if added:
                        logger.info("新增 %d 条长期记忆", added)
                    # 按间隔自动整合整理（合并重复/覆盖过时/生成画像摘要）
                    try:
                        result = self.context.maybe_consolidate(db)
                        if result and (result["merged"] or result["archived"]):
                            logger.info("长期记忆自动整合：%s", result)
                    except Exception as exc:
                        logger.warning("长期记忆自动整合失败：%s", exc)
            except Exception as exc:
                logger.warning("长期记忆提取失败：%s", exc)

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
            title = (response.content or "").strip().strip('"“”')
            if title:
                return title[:30]
        except Exception as exc:
            logger.warning("生成会话标题失败：%s", exc)
        return question.strip()[:20]
