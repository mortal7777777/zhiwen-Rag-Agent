<template>
  <el-dialog
    :model-value="modelValue"
    title="系统提示词片段"
    width="820px"
    append-to-body
    @update:model-value="$emit('update:modelValue', $event)"
    @open="load"
  >
    <div class="pf-hint">
      片段并入系统提示词，修改只影响<strong>新会话</strong>；已有会话保持锁定版本，
      可在会话输入框旁手动“应用最新”。保存即生成新版本。
    </div>
    <div v-loading="loading" class="pf-list">
      <div v-for="f in fragments" :key="f.key" class="pf-item">
        <div class="pf-info">
          <div class="pf-head">
            <span class="pf-title">{{ f.title }}</span>
            <el-tag size="small" :type="f.scope === 'static' ? 'info' : 'warning'">
              {{ positionLabel(f.position) }}
            </el-tag>
            <el-tag v-if="f.is_customized" size="small" type="success">已自定义</el-tag>
            <el-tag v-else size="small">默认</el-tag>
            <el-tag v-if="f.default_changed" size="small" type="danger" effect="plain">
              默认已更新
            </el-tag>
            <el-tag v-if="f.version_no" size="small" type="info">v{{ f.version_no }}</el-tag>
            <el-tag v-if="isAdvanced(f.key)" size="small" type="danger" effect="plain">
              高级
            </el-tag>
          </div>
          <div class="pf-desc">{{ f.description }}</div>
          <div class="pf-meta">
            <code>{{ f.key }}</code>
            <span v-if="f.placeholder">占位符 {{ f.placeholder }}</span>
          </div>
        </div>
        <div class="pf-actions">
          <el-button size="small" @click="openEdit(f)">编辑</el-button>
          <el-button size="small" text @click="openVersions(f)">历史</el-button>
          <el-button
            v-if="f.is_customized"
            size="small"
            type="warning"
            plain
            @click="reset(f)"
          >
            恢复默认
          </el-button>
        </div>
      </div>
    </div>

    <el-dialog
      v-model="editVisible"
      :title="`编辑片段 · ${editForm.title}`"
      width="720px"
      append-to-body
    >
      <div v-if="isAdvanced(editForm.key)" class="pf-warning">
        该片段与计划/任务状态机强耦合，删除或改写关键字可能导致流程无法推进。
      </div>
      <div v-if="editForm.defaultChanged" class="pf-warning">
        代码内置默认值已更新；当前自定义内容基于旧默认，保存后会以当前默认作为新基线。
      </div>
      <div v-if="editForm.placeholder" class="pf-placeholder">
        必须且只能保留一次占位符 <code>{{ editForm.placeholder }}</code>
      </div>
      <el-input
        v-model="editForm.content"
        type="textarea"
        :rows="16"
        placeholder="片段内容"
      />
      <el-input
        v-model="editForm.note"
        class="pf-note-input"
        placeholder="变更说明（可选，写入版本备注）"
      />
      <template #footer>
        <el-button text @click="normalizeBreaks">合并换行</el-button>
        <el-button text @click="openPreview">预览</el-button>
        <el-button @click="editVisible = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="save">保存并发布</el-button>
      </template>
    </el-dialog>

    <PromptVersionsDialog
      v-model="versionsVisible"
      kind="fragment"
      :target-key="versionTarget.key"
      :title="`版本历史 · ${versionTarget.title}`"
      @changed="onVersionChanged"
    />
    <PromptPreviewDialog
      v-model="previewVisible"
      :request="previewRequest"
      :title="`片段预览 · ${editForm.title}`"
    />
  </el-dialog>
</template>

<script setup>
import { ref } from 'vue'
import { ElMessage } from 'element-plus/es/components/message/index'
import { ElMessageBox } from 'element-plus/es/components/message-box/index'
import {
  lintPromptFragment,
  listPromptFragments,
  normalizePrompt,
  resetPromptFragment,
  updatePromptFragment,
} from '../../api'
import PromptVersionsDialog from './PromptVersionsDialog.vue'
import PromptPreviewDialog from './PromptPreviewDialog.vue'

const props = defineProps({
  modelValue: { type: Boolean, default: false },
})
const emit = defineEmits(['update:modelValue', 'changed'])

const loading = ref(false)
const fragments = ref([])
const editVisible = ref(false)
const saving = ref(false)
const editForm = ref({ key: '', title: '', content: '', note: '', placeholder: '' })
const versionsVisible = ref(false)
const versionTarget = ref({ key: '', title: '' })
const previewVisible = ref(false)
const previewRequest = ref({})

function isAdvanced(key) {
  return key === 'plan.mode' || key === 'todos.rules'
}

function positionLabel(position) {
  return {
    core: '静态核心',
    pre_history: '历史前动态块',
    post_history: '历史后动态块',
  }[position] || position || '动态块'
}

async function load() {
  loading.value = true
  try {
    const data = await listPromptFragments()
    fragments.value = data.fragments || []
  } catch (error) {
    ElMessage.error(error.response?.data?.detail || '加载提示词片段失败')
    fragments.value = []
  } finally {
    loading.value = false
  }
}

function openEdit(fragment) {
  editForm.value = {
    key: fragment.key,
    title: fragment.title,
    content: fragment.content,
    note: '',
    placeholder: fragment.placeholder || '',
    defaultChanged: !!fragment.default_changed,
  }
  editVisible.value = true
}

async function normalizeBreaks() {
  try {
    const result = await normalizePrompt({ content: editForm.value.content })
    if (!result.changed) {
      ElMessage.info('没有检测到句中硬换行')
      return
    }
    editForm.value.content = result.content
    ElMessage.success('已合并句中硬换行，请预览后发布')
  } catch (error) {
    ElMessage.error(error.response?.data?.detail || '合并换行失败')
  }
}

function openPreview() {
  previewRequest.value = {
    fragment_key: editForm.value.key,
    fragment_content: editForm.value.content,
  }
  previewVisible.value = true
}

async function save() {
  if (!editForm.value.content.trim()) {
    ElMessage.warning('内容不能为空')
    return
  }
  saving.value = true
  try {
    const check = await lintPromptFragment(editForm.value.key, {
      content: editForm.value.content,
    })
    if (!check.ok) {
      ElMessage.error((check.errors || []).join('；') || '发布前检查未通过')
      return
    }
    if (check.warnings?.length) {
      try {
        await ElMessageBox.confirm(
          check.warnings.join('；'),
          '发布前提示（仍要发布？）',
          {
            type: 'warning',
            confirmButtonText: '仍然发布',
            cancelButtonText: '返回修改',
          },
        )
      } catch {
        return
      }
    }
    await updatePromptFragment(editForm.value.key, {
      content: editForm.value.content,
      note: editForm.value.note,
    })
    editVisible.value = false
    ElMessage.success('已发布新版本；已有会话保持锁定版本')
    await load()
    emit('changed')
  } catch (error) {
    ElMessage.error(error.response?.data?.detail || '保存失败')
  } finally {
    saving.value = false
  }
}

async function reset(fragment) {
  try {
    await ElMessageBox.confirm(
      `恢复「${fragment.title}」为代码默认值？会生成一个新版本，已有会话不受影响。`,
      '恢复默认',
      { type: 'warning', confirmButtonText: '恢复', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  try {
    await resetPromptFragment(fragment.key)
    ElMessage.success('已恢复默认值')
    await load()
    emit('changed')
  } catch (error) {
    ElMessage.error(error.response?.data?.detail || '恢复失败')
  }
}

function openVersions(fragment) {
  versionTarget.value = { key: fragment.key, title: fragment.title }
  versionsVisible.value = true
}

async function onVersionChanged() {
  await load()
  emit('changed')
}

defineExpose({ load })
</script>

<style scoped>
.pf-hint {
  margin-bottom: 12px;
  font-size: 13px;
  line-height: 1.7;
  color: var(--el-text-color-secondary);
}
.pf-list {
  max-height: 520px;
  overflow: auto;
}
.pf-item {
  display: flex;
  gap: 12px;
  align-items: flex-start;
  padding: 12px 4px;
  border-bottom: 1px solid var(--el-border-color-lighter);
}
.pf-info {
  flex: 1;
  min-width: 0;
}
.pf-head {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
}
.pf-title {
  font-weight: 600;
}
.pf-desc {
  margin-top: 4px;
  font-size: 13px;
  color: var(--el-text-color-regular);
}
.pf-meta {
  display: flex;
  gap: 14px;
  margin-top: 5px;
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
.pf-meta code {
  font-family: var(--font-mono);
}
.pf-actions {
  display: flex;
  gap: 4px;
  flex-shrink: 0;
}
.pf-warning {
  margin-bottom: 10px;
  padding: 8px 10px;
  border-radius: 6px;
  font-size: 12px;
  color: var(--el-color-danger);
  background: var(--el-color-danger-light-9);
}
.pf-placeholder {
  margin-bottom: 10px;
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
.pf-note-input {
  margin-top: 10px;
}
</style>
