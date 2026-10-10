"""工具面装配：按开关组装本轮工具集（顺序=缓存前缀键的一部分，勿随意调整）。

从 LangGraphAgentService 拆出（2026-10-09）：只做移动，未改逻辑。"""

from __future__ import annotations

import logging
from sqlalchemy.orm import Session
from ...runtime_config import effective, effective_or


logger = logging.getLogger(__name__)


class ToolSurfaceMixin:
    def build_tool_list(
        self,
        db: Session | None,
        *,
        use_web_search: bool,
        use_knowledge_base: bool,
        conv_id: int | None,
        project_dir: str | None,
        command_sandbox: str | None,
    ) -> list:
        """按开关组装本轮工具集。

        挂起恢复不重跑 prepare，必须用同一套构建逻辑重建 Deps.tools，
        否则 tools 节点里所有工具都会查不到（"未知工具"）。
        顺序即 bind_tools 的 tools 数组顺序，参与前缀缓存键，勿随意调整。
        """
        settings = self.settings
        from ..tools import (
            build_tools,
            make_enter_plan_mode_tool,
            make_exit_plan_mode_tool,
        )

        skills_on = effective_or(settings, "skills_enabled", True)
        skill_enabled_ids = None
        if db is not None:
            try:
                from ...skills import load_prefs

                skill_enabled_ids = set((load_prefs(db) or {}).get("enabled") or [])
            except Exception as exc:
                logger.warning("技能偏好加载失败：%s", exc)
                skill_enabled_ids = None
        tools = build_tools(
            self.rag,
            use_web_search,
            use_knowledge_base,
            effective(settings, "web_search_provider"),
            settings.tavily_api_key,
            effective(settings, "web_search_max_results"),
            crag_enabled=bool(effective(settings, "crag_fallback_enabled")),
            crag_min_score=float(effective(settings, "crag_min_score") or 0.45),
            vision=self.vision,
            skills_enabled=skills_on,
            skill_enabled_ids=skill_enabled_ids,
            searxng_base_url=effective(settings, "searxng_base_url") or "",
            searxng_engines=effective(settings, "searxng_engines") or "",
        )
        # 扩展工具：MCP + 受控执行（文件/命令，敏感操作走人工确认）
        tools.extend(self.mcp_tools(db))
        tools.extend(
            self.extra_tools(project_dir=project_dir, sandbox=command_sandbox)
        )
        # 用户自写工具（动态目录）：每次重建自动加载（新工具下一个请求即生效）
        try:
            from ...user_tool_loader import load_user_tools

            tools.extend(load_user_tools())
        except Exception as exc:
            logger.warning("用户自写工具加载失败：%s", exc)
        if db is not None and conv_id is not None:
            try:
                from ...todos import make_todo_tool

                tools.append(make_todo_tool(db, conv_id))
            except Exception as exc:
                logger.warning("任务清单工具加载失败：%s", exc)
        # 声明式派发工具：让主代理（而非 planner + 规则层）决定派什么子任务。
        # 必须**无条件**注册——bind_tools 的 tools 数组是 DeepSeek 前缀缓存键的
        # 一部分，会话内变化会整链断缓存（CLAUDE.md 不变量）。
        if effective(settings, "agent_subagents_enabled") is not False:
            try:
                from ..nodes.subagent import make_dispatch_tool

                tools.append(make_dispatch_tool(settings))
            except Exception as exc:
                logger.warning("派发工具加载失败：%s", exc)
        # 计划模式控制工具：设置页开关（runtime_config）关闭时不注册——模型物理上
        # 无法进入，是硬否决而不是靠提示词自觉。
        if effective_or(settings, "plan_mode_allowed", True) is not False:
            try:
                tools.append(make_enter_plan_mode_tool())
                tools.append(make_exit_plan_mode_tool())
            except Exception as exc:
                logger.warning("计划模式工具加载失败：%s", exc)
        return tools

    def extra_tools(
        self, project_dir: str | None = None, sandbox: str | None = None
    ) -> list:
        """受控执行工具：类 Claude Code 文件/命令工具集（需在设置中开启总开关）。

        sandbox：请求级沙箱覆盖（subprocess|docker），空则跟随设置页。
        """
        if not effective_or(self.settings, "advanced_tools_enabled", True):
            return []
        try:
            from ...tools_extra import make_agent_tools

            return make_agent_tools(self.settings, project_dir, sandbox)
        except Exception as exc:
            logger.warning("受控执行工具加载失败：%s", exc)
            return []
