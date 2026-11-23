"""Agent 文件/命令工具（按域拆分的包；保持原 `app.tools_extra` 导入路径）。"""

from __future__ import annotations

from langchain_core.tools import BaseTool

from .bash import (
    _docker_run_cmd,
    _run_command,
    _run_docker_command,
    extract_commands_from_skill,
    make_bash_tool,
    make_command_tool,
    run_sandbox_command,
)
from .bg_jobs import (
    _BG_DEFAULT_MAX_S,
    _BG_JOBS,
    _BG_LOCK,
    _BG_MAX_ALLOWED_S,
    _BG_MAX_LOG_BYTES,
    _BG_MAX_PER_CONV,
    _BG_MAX_POLL_BYTES,
    _BG_MAX_TOTAL,
    _BG_MAX_WAIT_MS,
    _BG_RETAIN_S,
    _BgJob,
    _bg_cleanup_locked,
    _bg_clamp_seconds,
    _bg_expire,
    _bg_kill,
    _bg_kill_all,
    _bg_read,
    _bg_register_existing,
    _bg_start,
    make_background_tools,
)
from .common import (
    _atomic_write,
    _bash_argv,
    _kill_process_tree,
    _resolve_workspace,
    _safe_path,
    _truncate,
    bash_backend_info,
    command_allowed,
    detect_bash_backend,
    shell_path,
    shell_python,
)
from .fs import (
    _diff_context,
    _diff_window,
    make_delete_file_tool,
    make_edit_file_tool,
    make_file_tool,
    make_grep_search_tool,
    make_list_dir_tool,
    make_read_file_tool,
    make_write_file_tool,
)


def make_agent_tools(
    settings,
    project_dir: str | None = None,
    sandbox_override: str | None = None,
    conv_id: int | None = None,
) -> list[BaseTool]:
    """Agent 可用的文件/命令工具：读类自动，写/命令类敏感（HITL）。

    project_dir：会话工作目录（CLI 启动目录），未配置 tool_workspace 时生效。
    sandbox_override：请求级沙箱覆盖（subprocess|docker），空则跟随设置。
    conv_id：已有会话时额外注册后台任务工具（bash_background/bash_output/bash_kill）。
    """
    tools = [
        make_list_dir_tool(settings, project_dir),
        make_read_file_tool(settings, project_dir),
        make_grep_search_tool(settings, project_dir),
        make_write_file_tool(settings, project_dir),
        make_edit_file_tool(settings, project_dir),
        make_delete_file_tool(settings, project_dir),
        make_bash_tool(settings, project_dir, sandbox_override, conv_id),
    ]
    tools.extend(make_background_tools(settings, project_dir, sandbox_override, conv_id))
    return tools
