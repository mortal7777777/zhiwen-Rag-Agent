<template>
  <div class="settings-pane">
    <div class="pane-head">
      <div class="pane-title">工具</div>
      <div class="pane-sub">受控执行 · 子代理与派发 · 计划模式 · 写后验证 · MCP</div>
    </div>

    <div class="settings-section">
      <div class="section-head">
        <span class="settings-label">受控执行工具</span>
        <el-switch v-model="toolsForm.advanced_tools_enabled" size="small" />
      </div>
      <div class="provider-hint">
        读取类操作（列目录 / 读文件 / 搜索）自动执行；写入、编辑、删除、
        执行命令等敏感操作在「审批」页配置确认策略。工作目录限制在下方路径内。
      </div>
      <div class="generic-form">
        <div class="generic-row">
          <span class="generic-label">工作目录</span>
          <DirPicker
            v-model="toolsForm.tool_workspace"
            placeholder="留空 = 项目根"
          />
        </div>
        <div class="generic-row">
          <span class="generic-label">子代理并行</span>
          <el-switch v-model="toolsForm.agent_subagents_enabled" size="small" />
        </div>
        <div class="generic-row">
          <span class="generic-label">计划模式（模型自主出计划）</span>
          <el-switch v-model="toolsForm.plan_mode_allowed" size="small" />
        </div>
        <div class="generic-row">
          <span class="generic-label">子代理最大轮数</span>
          <el-input-number
            v-model="toolsForm.agent_subagent_max_rounds"
            :min="1"
            :max="5"
            size="small"
          />
        </div>
        <div class="generic-row">
          <span class="generic-label">子代理调用总上限</span>
          <el-input-number
            v-model="toolsForm.agent_subagent_max_llm_calls"
            :min="1"
            :max="200"
            size="small"
          />
          <span class="generic-hint">单次运行内所有并行子代理共享的 LLM 调用次数</span>
        </div>
        <div class="generic-row">
          <span class="generic-label">写后验证命令</span>
          <el-input
            v-model="toolsForm.verify_command"
            placeholder="如 pytest（留空=按文件类型自动检测）"
            size="small"
            class="generic-select"
          />
        </div>
        <div class="generic-row">
          <span class="generic-label">类型自动验证</span>
          <el-switch v-model="toolsForm.verify_auto_detect" size="small" />
          <span class="generic-hint">.py→py_compile / .js→node --check / JSON/YAML 语法</span>
        </div>
        <div class="generic-row">
          <span class="generic-label">验证重试上限</span>
          <el-input-number
            v-model="toolsForm.verify_max_retries"
            :min="0"
            :max="5"
            size="small"
          />
        </div>
      </div>
      <div class="provider-hint">
        “子代理并行”把计划里的工具型步骤派给独立上下文的子代理同时执行；
        子代理的敏感操作会交回主代理执行（由主代理发起确认）。
        写后自动验证在每次写文件后按类型跑语法/测试校验，失败自动重试。
      </div>
      <div class="tools-test-row">
        <el-button size="small" @click="testTool('file')">测试：列出工作目录</el-button>
        <el-button size="small" @click="testTool('command')">测试：执行命令</el-button>
      </div>
    </div>

    <div class="settings-section">
      <div class="section-head">
        <span class="settings-label">预算与稳定性</span>
      </div>
      <div class="provider-hint">
        工具调用预算用尽会强制收尾（回复“继续”可接着做）；失败不占预算，
        只有连续失败超上限才收尾。看门狗用于兜底流式卡死。
      </div>
      <div class="generic-form">
        <div class="generic-row">
          <span class="generic-label">普通轮次工具预算</span>
          <el-input-number v-model="toolsForm.agent_max_iterations" :min="1" :max="200" size="small" />
        </div>
        <div class="generic-row">
          <span class="generic-label">项目任务工具预算</span>
          <el-input-number v-model="toolsForm.agent_task_max_iterations" :min="1" :max="500" size="small" />
        </div>
        <div class="generic-row">
          <span class="generic-label">连续失败上限（普通）</span>
          <el-input-number v-model="toolsForm.agent_max_failures" :min="1" :max="50" size="small" />
        </div>
        <div class="generic-row">
          <span class="generic-label">连续失败上限（任务）</span>
          <el-input-number v-model="toolsForm.agent_task_max_failures" :min="1" :max="100" size="small" />
        </div>
        <div class="generic-row">
          <span class="generic-label">单轮最多派发轮数</span>
          <el-input-number v-model="toolsForm.agent_max_dispatch_rounds" :min="1" :max="10" size="small" />
        </div>
        <div class="generic-row">
          <span class="generic-label">流式卡死看门狗（秒）</span>
          <el-input-number v-model="toolsForm.agent_llm_stall_timeout_s" :min="10" :max="1800" :step="10" size="small" />
        </div>
        <div class="generic-row">
          <span class="generic-label">任务态自动识别</span>
          <el-switch v-model="toolsForm.task_mode_detect" size="small" />
        </div>
        <div class="generic-row">
          <span class="generic-label">展示思考过程</span>
          <el-switch v-model="toolsForm.reasoning_summary_enabled" size="small" />
        </div>
      </div>
    </div>

    <div class="settings-section">
      <div class="section-head">
        <span class="settings-label">MCP 服务器</span>
        <el-switch v-model="toolsForm.mcp_enabled" size="small" />
        <el-button size="small" type="primary" plain @click="openMcpDialog()">
          <el-icon :size="13"><Plus /></el-icon>
          添加服务器
        </el-button>
        <el-button size="small" text @click="loadMcp()">
          <el-icon :size="14"><Refresh /></el-icon>
          刷新
        </el-button>
        <el-button size="small" text @click="connectMcp(null)">
          <el-icon :size="14"><VideoPlay /></el-icon>
          全部启动
        </el-button>
      </div>
      <div class="provider-hint">
        MCP 工具会自动注册为 Agent 可用工具（stdio 本地进程 / HTTP 远程）。
      </div>
      <div v-if="!mcpServers.length" class="provider-empty">
        还没有 MCP 服务器，点击"添加服务器"接入外部能力
      </div>
      <div v-for="s in mcpServers" :key="s.id" class="provider-card">
        <div class="provider-head">
          <span class="provider-name">{{ s.name }}</span>
          <el-tag size="small" effect="plain" :type="s.type === 'http' ? 'success' : 'primary'">
            {{ s.type === 'http' ? 'HTTP' : 'stdio' }}
          </el-tag>
          <el-tooltip :content="mcpBadge(s).title || ' '" placement="top">
            <span :class="['mcp-badge', mcpBadge(s).tone]">{{ mcpBadge(s).text }}</span>
          </el-tooltip>
          <span class="provider-flex"></span>
          <el-button
            v-if="mcpTools(s).length"
            link
            size="small"
            @click="toggleMcpTools(s.id)"
          >
            工具 {{ mcpTools(s).length }}
          </el-button>
          <el-button
            v-if="s.enabled && mcpBadge(s).tone !== 'ok'"
            link
            size="small"
            @click="connectMcp(s)"
          >
            启动
          </el-button>
          <el-button link size="small" @click="openMcpDialog(s)">编辑</el-button>
          <el-button link size="small" @click="testMcp(s)">测试</el-button>
          <el-switch v-model="s.enabled" size="small" @change="saveMcpList" />
          <el-button link type="danger" size="small" @click="removeMcp(s)">删除</el-button>
        </div>
        <div class="mcp-cmd">
          {{ s.type === 'http' ? s.url : [s.command, ...(s.args || [])].join(' ') }}
        </div>
        <div v-if="mcpEnvSummary(s)" class="mcp-env">{{ mcpEnvSummary(s) }}</div>
        <div v-if="mcpTools(s).length && mcpExpanded.has(s.id)" class="mcp-tools">
          <span v-for="t in mcpTools(s)" :key="t" class="mcp-tool-chip">{{ t }}</span>
        </div>
      </div>
    </div>

    <div class="settings-section">
      <div class="section-head">
        <span class="settings-label">技能沙箱</span>
        <el-switch v-model="toolsForm.skill_sandbox_enabled" size="small" />
      </div>
      <div class="settings-tip">
        开启后可在技能页预览并执行 SKILL.md 中的命令，仍受命令白名单与超时限制。
      </div>
    </div>

    <div class="settings-save-row">
      <el-button type="primary" :loading="toolsSaving" @click="saveToolsSettings">
        保存工具设置
      </el-button>
    </div>

    <!-- MCP 增改对话框：append-to-body 已传送至 body，不受面板 v-show 影响 -->
    <el-dialog
      v-model="mcpDialogVisible"
      :title="mcpEditingId ? '编辑 MCP 服务器' : '添加 MCP 服务器'"
      width="520px"
      append-to-body
    >
      <el-form :model="mcpForm" label-position="top">
        <el-form-item label="名称">
          <el-input v-model="mcpForm.name" placeholder="例如：context7 / github" />
        </el-form-item>
        <el-form-item label="传输类型">
          <el-radio-group v-model="mcpForm.type" :disabled="!!mcpEditingId">
            <el-radio-button value="stdio">stdio（本地进程）</el-radio-button>
            <el-radio-button value="http">HTTP（远程）</el-radio-button>
          </el-radio-group>
        </el-form-item>
        <el-form-item v-if="mcpForm.type === 'stdio'" label="启动命令（含参数，空格分隔）">
          <el-input v-model="mcpForm.command" placeholder="npx -y @modelcontextprotocol/server-filesystem" />
        </el-form-item>
        <el-form-item v-else label="URL">
          <el-input v-model="mcpForm.url" placeholder="https://host/mcp" />
        </el-form-item>
        <el-form-item v-if="mcpForm.type === 'stdio'" label="环境变量（每行 KEY=VALUE，可选）">
          <el-input
            v-model="mcpForm.envText"
            type="textarea"
            :rows="3"
            placeholder="FIRECRAWL_API_KEY=fc-xxxxxxxx"
          />
        </el-form-item>
        <el-form-item v-else label="请求头（每行 KEY=VALUE，可选）">
          <el-input
            v-model="mcpForm.headersText"
            type="textarea"
            :rows="3"
            placeholder="Authorization=Bearer gho_xxxxxxxx"
          />
        </el-form-item>
        <div class="settings-tip">
          修改命令/凭据会触发该服务器重连；令牌等敏感值仅存本机数据库，接口回显为
          __MASKED__（保持原样即保留已存值，删除该行则清除）。
        </div>
      </el-form>
      <template #footer>
        <el-button @click="mcpDialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="mcpTesting" @click="saveMcpDialog">
          {{ mcpEditingId ? '保存' : '添加' }}
        </el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
/**
 * 设置页 · 工具与集成面板（从 App.vue 抽出，2026-10-09 设置页拆分）。
 * 表单域与保存来自 useSettingsForm；MCP 全部状态与操作来自 useMcpServers
 * （App.vue provide，两域经 inject 共用）；MCP 增改对话框随本面板走。
 */
import { inject } from 'vue'
import { Plus, Refresh, VideoPlay } from '@element-plus/icons-vue'
import DirPicker from '../DirPicker.vue'

const { toolsForm, toolsSaving, saveToolsSettings, testTool } = inject('settingsForm')
const {
  mcpServers,
  mcpDialogVisible,
  mcpTesting,
  mcpExpanded,
  mcpEditingId,
  mcpForm,
  loadMcp,
  openMcpDialog,
  mcpBadge,
  mcpTools,
  toggleMcpTools,
  mcpEnvSummary,
  saveMcpDialog,
  saveMcpList,
  testMcp,
  connectMcp,
  removeMcp,
} = inject('settingsMcp')
</script>
