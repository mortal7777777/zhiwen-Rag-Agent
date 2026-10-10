/**
 * 设置页 · 工具 / 审批 / 检索 / 记忆四页共用的表单域（从 App.vue 抽出，2026-10-09）。
 *
 * 为什么单独成模块：四个面板共用同一个 toolsForm，且只有**单一提交函数**
 * （saveToolsSettings）——这是设置页最大的耦合点，集中在这里、由 App.vue
 * 创建唯一实例 provide('settingsForm')，面板经 inject 读写（变量名与拆分前一致，
 * 模板可逐字搬迁）。`reload` 为注入的完整刷新回调：保存成功后与服务端归一化
 * 对齐（拆分前是 saveToolsSettings 里直接调用 loadSettings）。
 */
import { ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { executeTool, saveSettings } from '../api'

export function useSettingsForm(reload) {
  const toolsForm = ref({
    advanced_tools_enabled: false,
    agent_subagents_enabled: true,
    plan_mode_allowed: true,
    agent_subagent_max_rounds: 2,
    verify_command: '',
    verify_auto_detect: true,
    verify_max_retries: 1,
    tool_workspace: '',
    command_allowlist: '',
    command_timeout: 60,
    command_sandbox: 'subprocess',
    sandbox_image: 'python:3.11-slim',
    sandbox_workspace_readonly: false,
    tool_permission_mode: 'ask',
    permission_timeout: 300,
    skill_sandbox_enabled: false,
    reasoning_summary_enabled: false,
    trajectory_compress_enabled: true,
    mcp_enabled: true,
    // 预算与稳定性
    agent_max_iterations: 20,
    agent_task_max_iterations: 50,
    agent_max_failures: 3,
    agent_task_max_failures: 6,
    agent_max_dispatch_rounds: 3,
    agent_llm_stall_timeout_s: 150,
    task_mode_detect: true,
    // 检索
    kb_chat_min_score: 0.2,
    crag_fallback_enabled: true,
    crag_min_score: 0.45,
    vision_max_images: 6,
    // 上下文与记忆 / 检查点
    history_max_messages: 400,
    history_max_tokens: 64000,
    memory_auto_extract_min_chars: 400,
    project_memory_max_chars: 2600,
    memory_consolidate_threshold: 10,
    memory_consolidate_interval_hours: 24,
    checkpoint_keep_per_thread: 20,
  })
  const toolsSaving = ref(false)

  /** 把后端 editable 快照映射进表单（原先内联在 loadSettings 里）。 */
  function applyEditable(editable) {
    toolsForm.value = {
      advanced_tools_enabled: editable.advanced_tools_enabled !== false,
      agent_subagents_enabled: editable.agent_subagents_enabled !== false,
      plan_mode_allowed: editable.plan_mode_allowed !== false,
      agent_subagent_max_rounds: editable.agent_subagent_max_rounds ?? 2,
      verify_command: editable.verify_command || '',
      verify_auto_detect: editable.verify_auto_detect !== false,
      verify_max_retries: editable.verify_max_retries ?? 1,
      tool_workspace: editable.tool_workspace || '',
      command_allowlist: editable.command_allowlist || '',
      command_timeout: editable.command_timeout ?? 60,
      command_sandbox: editable.command_sandbox || 'subprocess',
      sandbox_image: editable.sandbox_image || 'python:3.11-slim',
      sandbox_workspace_readonly: editable.sandbox_workspace_readonly === true,
      tool_permission_mode: editable.tool_permission_mode || 'ask',
      permission_timeout: editable.permission_timeout ?? 300,
      skill_sandbox_enabled: editable.skill_sandbox_enabled === true,
      reasoning_summary_enabled: editable.reasoning_summary_enabled !== false,
      trajectory_compress_enabled: editable.trajectory_compress_enabled !== false,
      mcp_enabled: editable.mcp_enabled !== false,
      agent_max_iterations: editable.agent_max_iterations ?? 20,
      agent_task_max_iterations: editable.agent_task_max_iterations ?? 50,
      agent_max_failures: editable.agent_max_failures ?? 3,
      agent_task_max_failures: editable.agent_task_max_failures ?? 6,
      agent_max_dispatch_rounds: editable.agent_max_dispatch_rounds ?? 3,
      agent_llm_stall_timeout_s: editable.agent_llm_stall_timeout_s ?? 150,
      task_mode_detect: editable.task_mode_detect !== false,
      kb_chat_min_score: editable.kb_chat_min_score ?? 0.2,
      crag_fallback_enabled: editable.crag_fallback_enabled !== false,
      crag_min_score: editable.crag_min_score ?? 0.45,
      vision_max_images: editable.vision_max_images ?? 6,
      history_max_messages: editable.history_max_messages ?? 400,
      history_max_tokens: editable.history_max_tokens ?? 64000,
      memory_auto_extract_min_chars: editable.memory_auto_extract_min_chars ?? 400,
      project_memory_max_chars: editable.project_memory_max_chars ?? 2600,
      memory_consolidate_threshold: editable.memory_consolidate_threshold ?? 10,
      memory_consolidate_interval_hours: editable.memory_consolidate_interval_hours ?? 24,
      checkpoint_keep_per_thread: editable.checkpoint_keep_per_thread ?? 20,
    }
  }

  async function saveToolsSettings() {
    toolsSaving.value = true
    try {
      const f = toolsForm.value
      await saveSettings({
        advanced_tools_enabled: f.advanced_tools_enabled,
        agent_subagents_enabled: f.agent_subagents_enabled,
        plan_mode_allowed: f.plan_mode_allowed,
        agent_subagent_max_rounds: f.agent_subagent_max_rounds,
        verify_command: f.verify_command,
        verify_auto_detect: f.verify_auto_detect,
        verify_max_retries: f.verify_max_retries,
        tool_workspace: f.tool_workspace,
        command_allowlist: f.command_allowlist,
        command_timeout: f.command_timeout,
        command_sandbox: f.command_sandbox,
        sandbox_image: f.sandbox_image,
        sandbox_workspace_readonly: f.sandbox_workspace_readonly,
        tool_permission_mode: f.tool_permission_mode,
        permission_timeout: f.permission_timeout,
        skill_sandbox_enabled: f.skill_sandbox_enabled,
        reasoning_summary_enabled: f.reasoning_summary_enabled,
        trajectory_compress_enabled: f.trajectory_compress_enabled,
        mcp_enabled: f.mcp_enabled,
        agent_max_iterations: f.agent_max_iterations,
        agent_task_max_iterations: f.agent_task_max_iterations,
        agent_max_failures: f.agent_max_failures,
        agent_task_max_failures: f.agent_task_max_failures,
        agent_max_dispatch_rounds: f.agent_max_dispatch_rounds,
        agent_llm_stall_timeout_s: f.agent_llm_stall_timeout_s,
        task_mode_detect: f.task_mode_detect,
        kb_chat_min_score: f.kb_chat_min_score,
        crag_fallback_enabled: f.crag_fallback_enabled,
        crag_min_score: f.crag_min_score,
        vision_max_images: f.vision_max_images,
        history_max_messages: f.history_max_messages,
        history_max_tokens: f.history_max_tokens,
        memory_auto_extract_min_chars: f.memory_auto_extract_min_chars,
        project_memory_max_chars: f.project_memory_max_chars,
        memory_consolidate_threshold: f.memory_consolidate_threshold,
        memory_consolidate_interval_hours: f.memory_consolidate_interval_hours,
        checkpoint_keep_per_thread: f.checkpoint_keep_per_thread,
      })
      await reload()
      ElMessage.success('设置已保存并生效')
    } catch (error) {
      ElMessage.error(error.response?.data?.detail || '保存失败')
    } finally {
      toolsSaving.value = false
    }
  }

  /** 设置页「测试」按钮：命令（必须命中白名单）或列出工作目录。 */
  async function testTool(type) {
    try {
      if (type === 'command') {
        const { value } = await ElMessageBox.prompt(
          '输入要测试的命令（必须命中白名单）',
          '测试执行',
          {
            inputValue: 'dir',
            confirmButtonText: '执行',
            cancelButtonText: '取消',
            inputPlaceholder: '例如：dir / python -V',
          },
        )
        const result = await executeTool({ type: 'command', command: value })
        ElMessageBox.alert(
          result.error || result.output || '（无输出）',
          `执行结果（exit=${result.exit_code ?? '-'}）`,
          { confirmButtonText: '知道了' },
        )
      } else {
        const result = await executeTool({ type: 'file', operation: 'list', path: '.' })
        const names = (result.entries || []).map((e) => e.name).join('\n') || '（空目录）'
        ElMessageBox.alert(
          result.error || `工作目录：${result.workspace || '-'}\n\n${names}`,
          '文件工具测试',
          { confirmButtonText: '知道了' },
        )
      }
    } catch (error) {
      if (error === 'cancel' || error === 'close') return
      ElMessage.error(error.response?.data?.detail || '测试失败')
    }
  }

  return { toolsForm, toolsSaving, saveToolsSettings, testTool, applyEditable }
}
