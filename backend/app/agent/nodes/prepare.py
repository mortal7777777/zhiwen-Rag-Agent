"""prepare 节点：会话/历史/滚动摘要/规划/记忆/视觉/时间注入，组装分层消息与工具（自 langgraph_agent.py 拆分）。"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from ...config import Settings
from ...db import repository as repo
from ...rag.service import RAGService
from ...runtime_config import effective_or
from ..context import trim_history_for_budget
from ..prompts import compose_system_prompt

logger = logging.getLogger(__name__)
from ..state import RESULTS_RESET, AgentState, EventBus
from .common import _plan_hint, _is_project_task
from ..utils import (
    _rows_to_history,
    _tools_prefix_hash,
    _tools_prefix_size,
    _chain_fingerprints,
    _chain_preview,
    _dblock_needs_injection,
    _dblock_version,
)

from langchain_core.messages import HumanMessage, SystemMessage
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from ..langgraph_agent import LangGraphAgentService  # 仅类型注解

# ==== 函数体（原文）====
def _prepare_node(state: AgentState, runtime) -> dict:
    """会话/历史/规划/记忆/视觉/工具组装，输出分层消息。

    第二参数必须是 LangGraph 注入的 Runtime（按参数名注入），请求级依赖从
    `runtime.context`（Deps）取；状态里的遥测字典用 `rt` 指代。
    """
    deps = runtime.context
    service: LangGraphAgentService = deps.service
    bus: EventBus = deps.bus
    db: Session | None = deps.db
    settings: Settings = service.settings
    rag: RAGService = service.rag
    rt: dict = state["runtime"]
    stop_event = deps.stop_event

    question = state["question"]
    images = state["images"] or []
    use_web_search = state["use_web_search"]
    use_knowledge_base = state["use_knowledge_base"]
    conversation_id = state["conversation_id"]
    template_id = state["template_id"]
    system_prompt = state["system_prompt"]
    tool_mode = state["tool_mode"]

    def stopped() -> bool:
        return stop_event is not None and stop_event.is_set()

    # ---- 会话与历史（MySQL 记忆；失败则降级为无记忆模式）----
    conv_id = conversation_id
    conv_title = None
    is_new_conversation = False
    history_messages: list = []
    summary_text: str | None = None
    # 本轮发送窗口（DB 行）：D 块"变化才追加"的判据——窗口内已有同内容副本就跳过
    window_rows: list[dict] = []
    if db is not None:
        try:
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
            else:
                if template_id is not None and (conv.template_id or None) != (
                    template_id or None
                ):
                    conv = repo.update_conversation(
                        db, conv.id, template_id=template_id
                    )
                # 入口阶段提前建号的新会话：视作新会话以触发生成标题
                if state.get("early_created"):
                    is_new_conversation = True
            conv_id = conv.id
            conv_title = conv.title
            summary_text, recent_rows = service.context.compact_conversation(
                db, conv_id
            )
            # 压缩后裁剪必须与压缩用同一模板预算，否则调大的预算会被兜底值砍回
            recent_rows = trim_history_for_budget(
                recent_rows,
                service.context.history_budget_for(db, conv_id),
            )
            window_rows = recent_rows
            history_messages = _rows_to_history(recent_rows, history_messages)
        except Exception as exc:
            logger.warning("读写会话失败，降级为无记忆模式：%s", exc)
            conv_id = conversation_id
            history_messages = []
    rt["conv_id"] = conv_id

    # ---- 新会话标题：后台线程生成，不阻塞首 token ----
    title_holder: list[str] = []
    title_thread: threading.Thread | None = None
    if is_new_conversation and db is not None:
        def _generate_title_async():
            try:
                from ...db.database import SessionLocal, db_ready

                if not (db_ready and SessionLocal is not None):
                    return
                s = SessionLocal()
                try:
                    title = service._generate_title(question)
                    if title:
                        repo.rename_conversation(s, conv_id, title)
                        title_holder.append(title)
                finally:
                    s.close()
            except Exception as exc:
                logger.warning("后台生成会话标题失败：%s", exc)

        title_thread = threading.Thread(target=_generate_title_async, daemon=True)
        title_thread.start()
    # 标题伴侣（结果盒 + 线程）是请求级活对象：挂到 Deps，finalize 从 context 读
    deps.title_holder = title_holder
    deps.title_thread = title_thread

    # ---- 计划来源：用户确认过的计划（tools 节点在原 run 内直接种入 state）----
    # 2026-09-25 删除 planner：它只看单条问题、不知道对话与磁盘现状，却决定了
    # 派发与预算（背景见 subagent.py 顶部注释与 CLAUDE.md 编排段）。现在主代理在
    # 运行中用 dispatch_subtasks 自行声明子任务，计划清单由声明追加。
    plan_steps: list[str] = []
    plan_map: list[dict] = []
    state["plan_steps"] = plan_steps
    state["plan_map"] = plan_map

    # ---- TodoWrite 任务清单：按计划播种，跨轮跟踪进度 ----
    todos: list[dict] = []
    if db is not None and conv_id is not None and not stopped():
        try:
            from ...todos import (
                plan_progress,
                refresh_todos_for_plan,
                todos_to_text,
            )

            todos = refresh_todos_for_plan(db, conv_id, plan_steps)
        except Exception as exc:
            logger.warning("任务清单加载失败：%s", exc)
            todos = []
    plan_done_count = 0
    plan_push_count = 0
    if todos:
        try:
            from ...todos import plan_progress

            plan_done_count, _total, _remaining = plan_progress(todos)
        except Exception as exc:
            logger.warning("任务清单进度计算失败：%s", exc)
            plan_done_count = 0
    state["todos"] = todos
    state["plan_done_count"] = plan_done_count
    # ---- 复杂度判定：必须放在 todos 之后——存在未完成任务清单即视为任务态，
    #      否则"继续"类追问会掉回非任务预算（12 次）做到一半被迫收尾 ----
    task_mode = _is_project_task(settings, question, plan_steps, todos, plan_map)
    state["task_mode"] = task_mode
    state["force_continue"] = False
    state["plan_push_count"] = plan_push_count
    if todos:
        bus.emit("todos", {"todos": todos})

    # ---- 知识库文档清单：让模型知道"库里有什么" ----
    kb_documents: list[str] = []
    if use_knowledge_base:
        try:
            kb_documents = [item["name"] for item in rag.list_documents()][:30]
        except Exception as exc:
            logger.warning("获取知识库文档清单失败：%s", exc)
            kb_documents = []
    state["kb_documents"] = kb_documents

    # ---- 技能偏好：目录索引常驻（内容经清洗，防提示注入）----
    # 按 Anthropic Agent Skills 设计：系统提示词只放技能索引（名字+一句话），
    # 模型认为需要某技能时主动调用 skill_lookup 工具加载全文。
    # 不再每轮自动注入匹配技能的全文——避免每轮内容变化破坏缓存前缀。
    skill_enabled_ids = None
    skills_on = effective_or(settings, "skills_enabled", True)
    if db is not None:
        try:
            from ...skills import load_prefs

            skill_enabled_ids = set(
                (load_prefs(db) if db is not None else {}).get("enabled") or []
            )
        except Exception as exc:
            logger.warning("技能偏好加载失败：%s", exc)
            skill_enabled_ids = None
    skills_catalog_text = ""
    skill_auto_text = ""
    if skills_on and skill_enabled_ids:
        try:
            from ...skills import build_skill_catalog

            skills_catalog_text = build_skill_catalog(skill_enabled_ids)
        except Exception as exc:
            logger.warning("技能目录生成失败：%s", exc)
            skills_catalog_text = ""

    # ---- 系统提示词：静态核心 + 动态部分分离（为 prompt caching 服务）----
    # 静态核心（模板+工具规则）放消息最前、跨轮字节级稳定，命中 DeepSeek 等
    # 提供商的自动前缀缓存；动态部分（任务清单/文档清单/技能目录）放历史之后。
    template_content = service._get_template_content(db, template_id, system_prompt)
    advanced_tools_on = effective_or(settings, "advanced_tools_enabled", True)
    system_prompt_core = compose_system_prompt(
        template_content,
        use_knowledge_base=use_knowledge_base,
        use_web_search=use_web_search,
        advanced_tools=advanced_tools_on,
        plan_only=bool(state.get("plan_only")),
        static_only=True,
    )
    # 静态动态块（技能目录+文档清单）：会话内字节级稳定，可放 history 前
    # 作为前缀缓存命中区；todos 每轮变化，单独放 history 后（见下方组装）
    static_dynamic_text = compose_system_prompt(
        template_content,
        use_knowledge_base=use_knowledge_base,
        use_web_search=use_web_search,
        kb_documents=kb_documents,
        advanced_tools=advanced_tools_on,
        todos_text="",
        skills_catalog=skills_catalog_text,
        dynamic_only=True,
    )
    todos_prompt_text = compose_system_prompt(
        template_content,
        use_knowledge_base=use_knowledge_base,
        use_web_search=use_web_search,
        kb_documents=None,
        advanced_tools=advanced_tools_on,
        todos_text=todos_to_text(todos) if todos else "",
        skills_catalog="",
        dynamic_only=True,
    )

    bus.emit(
        "session",
        {"conversation_id": conv_id, "title": conv_title},
    )
    bus.emit("status", {"phase": "prepare", "text": "正在准备上下文…"})
    if plan_steps:
        bus.emit("plan", {"steps": plan_steps})

    # ---- 长期事实记忆召回 ----
    memory_hits: list[str] = []
    memory_summary: str | None = None
    if db is not None and conv_id is not None and not stopped():
        try:
            rag.acquire_gpu()
            try:
                memory_hits = service.context.retrieve_memories(
                    db, rag.embeddings, question
                )
            finally:
                rag.release_gpu()
        except Exception as exc:
            logger.warning("记忆召回失败：%s", exc)
            memory_hits = []
        try:
            memory_summary = service.context.get_memory_summary(db)
        except Exception:
            memory_summary = None
    state["memory_hits"] = memory_hits
    state["memory_summary"] = memory_summary
    state["summary_text"] = summary_text

    # ---- 用户上传图片：主模型无视觉时先识图并注入上下文 ----
    vision_descriptions: list[str] = []
    if (
        images
        and settings.vision_auto_describe
        and not settings.main_model_vision
        and not stopped()
    ):
        try:
            if service.vision.configured:
                desc = service.vision.describe_images(images)
                if desc:
                    vision_descriptions = [desc]
                    bus.emit(
                        "vision",
                        {
                            "provider": "sensenova",
                            "model": service.vision.provider.get("model")
                            or "sensenova-6.8-flash-lite",
                            "descriptions": vision_descriptions,
                        },
                    )
        except Exception as exc:
            logger.warning("图片识别失败，跳过：%s", exc)
            vision_descriptions = []
    state["vision_descriptions"] = vision_descriptions

    # ---- 组装分层上下文：时间 -> 摘要 -> 记忆 -> 图片 -> 历史 -> 问题 ----
    messages: list = []
    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    weekday = "一二三四五六日"[now.weekday()]
    time_context = f"当前时间：{now:%Y-%m-%d %H:%M}（星期{weekday}，Asia/Shanghai）。"
    if db is not None and use_web_search:
        try:
            digest_raw = repo.get_meta(db, f"suggestions_{now:%Y-%m-%d}")
            if digest_raw:
                digest = json.loads(digest_raw)
                news = next((i for i in digest if i.get("type") == "news"), None)
                if news:
                    time_context += f"\n今日热点话题（仅供参考，可据此展开）：{news.get('text', '')}"
        except Exception:
            pass

    # 文件型项目记忆（AGENTS.md）与最近任务轨迹摘要：始终注入（若有）
    project_memory_text = None
    trajectory_summary = None
    try:
        from ...project_memory import load_project_memory
        from ...trajectory import load_trajectory_summary

        project_memory_text = load_project_memory(
            settings, state.get("project_dir")
        )
        if db is not None and conv_id is not None:
            trajectory_summary = load_trajectory_summary(db, conv_id)
    except Exception as exc:
        logger.warning("加载项目记忆/轨迹摘要失败：%s", exc)

    # 纯追加链（DeepSeek 自动前缀缓存无显式断点，只能靠前缀字节稳定）：
    #   1. 静态核心（模板+工具规则）：跨轮字节级稳定，是缓存命中区
    #   2. 技能目录+文档清单：会话内字节级稳定，放 history 前作为缓存命中区
    #   3. 历史消息：只追加不修改；finalize 会把"问题之后"的全部消息
    #      （含 D 块 system 行与轮内提示）落库，下一轮请求 = 上一轮请求
    #      + [问题, D 块]，跨轮前缀命中最大化
    #   4. 问题 + D 块（项目记忆/todos/摘要/记忆/图片/时间）：每轮变化
    #      只影响链尾自身，不波及已发前缀
    messages.append(SystemMessage(content=system_prompt_core))
    # 技能目录+文档清单：会话内字节级稳定，放 history 前作为缓存命中区
    if static_dynamic_text:
        messages.append(SystemMessage(content=static_dynamic_text))
    messages.extend(history_messages)
    persist_start = len(messages)
    # ---- D 块：变化才追加 ----
    # D 块每轮重新渲染；若"该键最后一个版本就是当前版本、且它仍在窗口内"
    # 则不重复注入——否则每轮制造一份副本（进历史占上下文）且内容必然
    # miss（前缀缓存失效）。判据是**版本账本 + 窗口存在性**两条（见
    # utils._dblock_needs_injection）：只看窗口内有没有同内容，会在
    # A→B→A 状态回退时保留过期的 B（旧实现的实际缺陷）。
    # 账本放 state（随检查点持久化），返回值必须带出去——节点内直接改
    # state 的字段不生效（历史大坑）。
    ledger: dict[str, str] = dict(state.get("dblock_versions") or {})

    def _append_if_changed(key: str, content: str) -> None:
        if not _dblock_needs_injection(key, content, ledger, window_rows):
            return
        messages.append(SystemMessage(content=content))
        version = _dblock_version(content)
        if ledger.get(key) and ledger[key] != version:
            # 状态回退/内容变化导致重新注入：留痕便于排查"为什么这里没命中缓存"
            logger.info(
                "D 块重新注入：%s（%s → %s）", key, ledger[key], version
            )
        ledger[key] = version

    if project_memory_text:
        _append_if_changed(
            "project_memory",
            "项目记忆（AGENTS.md）：\n"
            "（以下为历史记录，可能过时；与当前代码/文件/文档冲突时以现状为准）\n"
            + project_memory_text,
        )
    _append_if_changed("todos", todos_prompt_text or "")
    if trajectory_summary:
        _append_if_changed(
            "trajectory", "最近任务过程摘要：\n" + trajectory_summary
        )
    if summary_text:
        _append_if_changed(
            "summary",
            "以下是对本会话早期内容的摘要（供参考）：\n" + summary_text,
        )
    if memory_summary:
        _append_if_changed(
            "memory_profile",
            "关于用户（长期记忆摘要，通常应作为默认背景）：\n" + memory_summary,
        )
    if memory_hits:
        _append_if_changed(
            "memory_hits",
            "相关的长期记忆（每条格式为 [分类|录入日期]，属历史信息："
            "与当前对话内容冲突时以当前对话为准，不要用旧记忆否定用户刚说的话）：\n"
            + "\n".join(f"- {item}" for item in memory_hits),
        )
    if vision_descriptions:
        _append_if_changed(
            "vision",
            "用户上传了图片。以下是视觉模型对图片的理解，请据此回答：\n"
            + "\n".join(
                f"- 图片{i + 1}：{text}"
                for i, text in enumerate(vision_descriptions)
            ),
        )
    # 时间戳是纯背景信息（无指令约束力），放链尾避免破坏前缀缓存；
    # 精度到分钟，同一分钟内的多轮请求字节相同（被上面的去重跳过）
    _append_if_changed("time", time_context)
    messages.append(HumanMessage(content=question))

    # ---- 按开关组装工具 ----
    # 走服务层共用构建器：挂起恢复不重跑 prepare，需用同一套工具面重建 Deps.tools
    tools = service.build_tool_list(
        db,
        use_web_search=use_web_search,
        use_knowledge_base=use_knowledge_base,
        conv_id=conv_id,
        project_dir=state.get("project_dir"),
        command_sandbox=rt.get("command_sandbox"),
    )

    # ---- 工具定义哈希（缓存前缀监测）----
    # bind_tools 的工具定义是缓存前缀的一部分，会话内变化会断缓存。
    # 记录到 run 的 token_usage.tools_hash，并与上一 run 对比告警。
    tools_hash = _tools_prefix_hash(tools)
    rt["tools_hash"] = tools_hash
    # 工具面体积：缓存命中时它仍在输入里计费，记入运行记录才能验收"工具瘦身"
    rt["tools_chars"] = _tools_prefix_size(tools)
    if db is not None and conv_id is not None:
        try:
            prev_runs = repo.list_agent_runs(db, limit=1, conversation_id=conv_id)
            if prev_runs:
                prev_usage = prev_runs[0].get("token_usage")
                prev_hash = (
                    prev_usage.get("tools_hash")
                    if isinstance(prev_usage, dict)
                    else None
                )
                if prev_hash and prev_hash != tools_hash:
                    logger.warning(
                        "工具定义前缀变化 %s -> %s（conv=%s）：缓存前缀失效，"
                        "命中率可能下降；常见原因=用户自写工具/技能注入/"
                        "MCP 服务器变更",
                        prev_hash,
                        tools_hash,
                        conv_id,
                    )
        except Exception as exc:
            logger.warning("工具哈希对比失败：%s", exc)

    # ---- 消息链指纹（缓存前缀分歧定位）----
    # 纯追加链的判据：本轮链应以上一轮实际发送的链为前缀。逐条指纹
    # 比对，首个分歧位置就是 provider 前缀命中的断点，写日志与 trace，
    # 用于定位"哪里断了"（工具轮消息重建、静态段渲染、D 块位置等）。
    chain_hashes = _chain_fingerprints(messages)
    rt["chain_len"] = len(messages)
    rt["chain_hashes"] = chain_hashes
    if db is not None and conv_id is not None:
        divergence = None
        try:
            prev_runs = repo.list_agent_runs(db, limit=1, conversation_id=conv_id)
            prev_usage = (prev_runs[0].get("token_usage") if prev_runs else None) or {}
            prev_chain = (
                prev_usage.get("chain_hashes")
                if isinstance(prev_usage, dict)
                else None
            )
            if prev_chain:
                for i in range(min(len(prev_chain), len(chain_hashes))):
                    if prev_chain[i] != chain_hashes[i]:
                        divergence = i
                        break
                if divergence is not None:
                    logger.warning(
                        "消息链前缀分歧：第 %d/%d 条起（此前 %d 条一致）"
                        "｜本轮该条：%s",
                        divergence + 1,
                        len(chain_hashes),
                        divergence,
                        _chain_preview(messages[divergence]),
                    )
                elif len(prev_chain) > len(chain_hashes):
                    logger.warning(
                        "消息链缩短：上轮 %d 条 -> 本轮 %d 条（非纯追加）",
                        len(prev_chain),
                        len(chain_hashes),
                    )
        except Exception as exc:
            logger.debug("消息链指纹比对失败：%s", exc)
        rt["chain_divergence"] = divergence

    state["messages"] = messages
    deps.tools = tools  # 活对象走 Deps，不进检查点
    state["persist_start"] = persist_start
    return {
        "messages": messages,
        "persist_start": persist_start,
        # D 块版本账本：随检查点跨轮保留（丢失只会多注入一次，不会漏）
        "dblock_versions": ledger,
        "plan_steps": plan_steps,
        "plan_map": plan_map,
        "kb_documents": kb_documents,
        "memory_hits": memory_hits,
        "memory_summary": memory_summary,
        "vision_descriptions": vision_descriptions,
        "summary_text": summary_text,
        "todos": todos,
        "plan_done_count": plan_done_count,
        # 每轮重置进度行去重标记：保证本轮至少注入一条计划进度
        "plan_progress_sig": "",
        # 每轮重置空正文重试计数：checkpointer 会把上一轮的计数带进来，
        # 不重置则一次用掉后整个会话都失去兜底（2026-09-24 实测）
        "empty_retry_count": 0,
        "empty_retry_pending": False,
        "force_continue": False,
        "plan_push_count": plan_push_count,
        # 每轮重置派发状态：上一轮的声明/轮数绝不能跨轮残留在 checkpointer 里
        "pending_subtasks": [],
        "dispatch_rounds": 0,
        # 清空子代理结果通道：它是 reducer 通道，传入空列表清不掉（会被 add），
        # 必须显式走一次 reset 归约（旧实现靠跨轮游标兜底，见 state.py）
        "subagent_results": [RESULTS_RESET],
        "sources": state["sources"],
        "tool_trace": state["tool_trace"],
        "tool_calls_used": state["tool_calls_used"],
        "forced_final": state["forced_final"],
        "last_call_warned": state["last_call_warned"],
        "verify_fail_count": int(state.get("verify_fail_count") or 0),
        "task_mode": bool(state.get("task_mode")),
    }


# ============================================================
# 子代理并行（Send fan-out / fan-in，类 Claude Code 的 Task）
# ============================================================
