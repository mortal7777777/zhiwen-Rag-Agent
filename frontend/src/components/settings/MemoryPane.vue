<template>
  <div class="settings-pane">
    <div class="pane-head">
      <div class="pane-title">上下文与记忆</div>
      <div class="pane-sub">历史窗口 · 长期记忆 · 项目记忆 · 检查点</div>
    </div>

    <div class="settings-section">
      <div class="settings-label">上下文窗口</div>
      <div class="generic-form">
        <div class="generic-row">
          <span class="generic-label">历史窗口条数上限</span>
          <el-input-number
            v-model="toolsForm.history_max_messages"
            :min="8"
            :max="500"
            size="small"
          />
        </div>
        <div class="generic-row">
          <span class="generic-label">历史 token 预算兜底</span>
          <el-input-number
            v-model="toolsForm.history_max_tokens"
            :min="1000"
            :max="200000"
            :step="500"
            size="small"
          />
        </div>
        <div class="generic-row">
          <span class="generic-label">轨迹压缩摘要</span>
          <el-switch v-model="toolsForm.trajectory_compress_enabled" size="small" />
          <span class="generic-hint">工具调用 ≥3 次的长任务生成过程摘要，下一轮注入（默认关闭）</span>
        </div>
      </div>
      <div class="settings-tip">
        超出软窗口（条数或 token 预算）时下一轮自动压缩：保留近期原文，
        更早的对话并入滚动摘要（会话头部「上下文」可查看占用与手动压缩）。
      </div>
    </div>

    <div class="settings-section">
      <div class="settings-label">长期记忆</div>
      <div class="generic-form">
        <div class="generic-row">
          <span class="generic-label">自动提取阈值（字符）</span>
          <el-input-number
            v-model="toolsForm.memory_auto_extract_min_chars"
            :min="50"
            :max="5000"
            :step="50"
            size="small"
          />
          <span class="generic-hint">回答短于该长度不触发事实提取</span>
        </div>
        <div class="generic-row">
          <span class="generic-label">整合阈值（条）</span>
          <el-input-number
            v-model="toolsForm.memory_consolidate_threshold"
            :min="2"
            :max="500"
            size="small"
          />
        </div>
        <div class="generic-row">
          <span class="generic-label">整合间隔（小时）</span>
          <el-input-number
            v-model="toolsForm.memory_consolidate_interval_hours"
            :min="1"
            :max="720"
            size="small"
          />
        </div>
      </div>
    </div>

    <div class="settings-section">
      <div class="section-head">
        <span class="settings-label">项目记忆（AGENTS.md）</span>
        <el-button size="small" @click="exportProjectMemory">导出</el-button>
        <el-button size="small" @click="viewProjectMemory">查看</el-button>
      </div>
      <div class="generic-form">
        <div class="generic-row">
          <span class="generic-label">注入上限（字符）</span>
          <el-input-number
            v-model="toolsForm.project_memory_max_chars"
            :min="500"
            :max="20000"
            :step="100"
            size="small"
          />
        </div>
      </div>
      <div class="settings-tip">
        把用户画像与长期记忆导出为可编辑文件（类 CLAUDE.md / AGENTS.md），
        每次会话自动加载进上下文；超过上限按行截断注入。
      </div>
    </div>

    <div class="settings-section">
      <div class="settings-label">检查点（原生快照）</div>
      <div class="generic-form">
        <div class="generic-row">
          <span class="generic-label">每会话保留快照数</span>
          <el-input-number
            v-model="toolsForm.checkpoint_keep_per_thread"
            :min="5"
            :max="200"
            size="small"
          />
        </div>
      </div>
      <div class="settings-tip">
        每轮收尾后每个会话只保留最近 N 条快照（chat 头部「快照」抽屉可查看/回滚，
        崩溃后也可据此续跑）；调小可进一步压缩 checkpoints.sqlite 体积。
      </div>
    </div>

    <div class="settings-save-row">
      <el-button type="primary" :loading="toolsSaving" @click="saveToolsSettings">
        保存记忆设置
      </el-button>
    </div>

    <!-- 项目记忆（AGENTS.md）预览：append-to-body 已传送至 body，不受面板 v-show 影响 -->
    <el-dialog v-model="memoryDialogVisible" title="项目记忆（AGENTS.md）" width="640px" append-to-body>
      <div class="memory-path">{{ memoryPath }}</div>
      <el-input
        v-model="memoryContent"
        type="textarea"
        :rows="16"
        readonly
        class="memory-textarea"
      />
    </el-dialog>
  </div>
</template>

<script setup>
/**
 * 设置页 · 上下文与记忆面板（从 App.vue 抽出，2026-10-09 设置页拆分）。
 * 表单域与保存来自 useSettingsForm；「项目记忆」导出/查看及其预览对话框
 * 属本面板专用，随组件走（对话框 append-to-body，单根节点内不影响父级 v-show）。
 */
import { inject, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { exportProjectMemory as apiExportProjectMemory, getProjectMemory } from '../../api'

const { toolsForm, toolsSaving, saveToolsSettings } = inject('settingsForm')

const memoryDialogVisible = ref(false)
const memoryPath = ref('')
const memoryContent = ref('')

async function exportProjectMemory() {
  try {
    const result = await apiExportProjectMemory()
    if (result.ok) {
      ElMessage.success(`已导出：${result.path}`)
    } else {
      ElMessage.error(result.error || '导出失败')
    }
  } catch (error) {
    ElMessage.error(error.response?.data?.detail || '导出失败')
  }
}

async function viewProjectMemory() {
  try {
    const data = await getProjectMemory()
    memoryPath.value = data.path || ''
    memoryContent.value = data.content || '（暂无内容，先点击"导出"生成）'
    memoryDialogVisible.value = true
  } catch (error) {
    ElMessage.error(error.response?.data?.detail || '读取失败')
  }
}
</script>
