<template>
  <el-dialog
    :model-value="modelValue"
    :title="title || '版本历史'"
    width="720px"
    append-to-body
    @update:model-value="$emit('update:modelValue', $event)"
    @open="load"
  >
    <div v-loading="loading" class="pv-body">
      <el-empty v-if="!loading && !versions.length" description="暂无版本" />
      <div v-else class="pv-layout">
        <div class="pv-list">
          <div
            v-for="v in versions"
            :key="v.id"
            :class="['pv-item', { active: selected?.id === v.id }]"
            @click="selected = v"
          >
            <div class="pv-item-head">
              <span class="pv-no">v{{ v.version_no }}</span>
              <el-tag v-if="v.version_no === versions[0]?.version_no" size="small" type="success">
                当前
              </el-tag>
            </div>
            <div class="pv-meta">{{ formatTime(v.created_at) }} · {{ v.chars }} 字</div>
            <div v-if="v.note" class="pv-note">{{ v.note }}</div>
          </div>
        </div>
        <div class="pv-preview">
          <template v-if="selected">
            <div class="pv-preview-head">
              <span>
                v{{ selected.version_no }} 内容
                <template v-if="diffMode && compare"> · {{ compare.label }}</template>
              </span>
              <div class="pv-head-actions">
                <el-button
                  v-if="compare"
                  size="small"
                  text
                  @click="diffMode = !diffMode"
                >
                  {{ diffMode ? '看原文' : '对比当前' }}
                </el-button>
                <el-button
                  v-if="canRestore"
                  size="small"
                  type="primary"
                  plain
                  @click="restore"
                >
                  恢复此版本
                </el-button>
              </div>
            </div>
            <div v-if="diffMode && compare" class="pv-diff">
              <div class="pv-diff-head">
                − 删除 / + 新增；只比较行内容，不比较语义
              </div>
              <div
                v-for="(line, index) in diffEntries"
                :key="index"
                :class="['pv-diff-line', line.type]"
              >
                <span class="pv-diff-sign">{{ line.sign }}</span>
                <span class="pv-diff-text">{{ line.text || ' ' }}</span>
              </div>
            </div>
            <pre v-else class="pv-content">{{ selected.content }}</pre>
          </template>
          <el-empty v-else description="选择一个版本查看内容" />
        </div>
      </div>
    </div>
  </el-dialog>
</template>

<script setup>
import { computed, ref } from 'vue'
import { ElMessage } from 'element-plus/es/components/message/index'
import { ElMessageBox } from 'element-plus/es/components/message-box/index'
import {
  listPromptFragmentVersions,
  listTemplateVersions,
  restorePromptFragment,
  restoreTemplateVersion,
} from '../../api'
import { diffLines } from '../../utils/promptDiff'

const props = defineProps({
  modelValue: { type: Boolean, default: false },
  kind: { type: String, default: 'template' }, // template | fragment
  targetId: { type: Number, default: 0 },
  targetKey: { type: String, default: '' },
  title: { type: String, default: '' },
  readonly: { type: Boolean, default: false },
})
const emit = defineEmits(['update:modelValue', 'changed'])

const loading = ref(false)
const versions = ref([])
const selected = ref(null)
const diffMode = ref(false)

const canRestore = computed(() => !props.readonly && !!selected.value)

const compare = computed(() => {
  if (!selected.value || versions.value.length < 2) return null
  const current = versions.value[0]
  if (selected.value.id === current.id) {
    return {
      old: versions.value[1],
      new: current,
      label: `v${versions.value[1].version_no} → v${current.version_no}`,
    }
  }
  return {
    old: selected.value,
    new: current,
    label: `v${selected.value.version_no} → v${current.version_no}`,
  }
})

const diffEntries = computed(() => {
  if (!compare.value) return []
  return diffLines(compare.value.old.content, compare.value.new.content)
})

function formatTime(value) {
  if (!value) return ''
  const d = new Date(value)
  return Number.isNaN(d.getTime()) ? String(value) : d.toLocaleString()
}

async function load() {
  loading.value = true
  selected.value = null
  diffMode.value = false
  try {
    const data =
      props.kind === 'fragment'
        ? await listPromptFragmentVersions(props.targetKey)
        : await listTemplateVersions(props.targetId)
    versions.value = data.versions || []
    selected.value = versions.value[0] || null
  } catch (error) {
    ElMessage.error(error.response?.data?.detail || '加载版本失败')
    versions.value = []
  } finally {
    loading.value = false
  }
}

async function restore() {
  if (!selected.value) return
  try {
    await ElMessageBox.confirm(
      `恢复 v${selected.value.version_no} 会以该内容新建一个版本（不会删除历史），继续？`,
      '恢复版本',
      { type: 'warning', confirmButtonText: '恢复', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  try {
    if (props.kind === 'fragment') {
      await restorePromptFragment(props.targetKey, { version_id: selected.value.id })
    } else {
      await restoreTemplateVersion(props.targetId, { version_id: selected.value.id })
    }
    ElMessage.success('已恢复（生成新版本）')
    emit('changed')
    await load()
  } catch (error) {
    ElMessage.error(error.response?.data?.detail || '恢复失败')
  }
}

defineExpose({ load })
</script>

<style scoped>
.pv-body {
  min-height: 260px;
}
.pv-layout {
  display: grid;
  grid-template-columns: 240px 1fr;
  gap: 14px;
}
.pv-list {
  max-height: 460px;
  overflow: auto;
  border-right: 1px solid var(--el-border-color-lighter);
  padding-right: 10px;
}
.pv-item {
  padding: 8px 10px;
  border-radius: 8px;
  cursor: pointer;
  border: 1px solid transparent;
}
.pv-item:hover {
  background: var(--el-fill-color-light);
}
.pv-item.active {
  background: var(--el-fill-color-light);
  border-color: var(--el-color-primary-light-7);
}
.pv-item-head {
  display: flex;
  align-items: center;
  gap: 6px;
}
.pv-no {
  font-weight: 600;
}
.pv-meta {
  margin-top: 3px;
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
.pv-note {
  margin-top: 4px;
  font-size: 12px;
  color: var(--el-text-color-regular);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.pv-preview {
  min-width: 0;
}
.pv-preview-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 8px;
  font-size: 13px;
  font-weight: 600;
}
.pv-head-actions {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-shrink: 0;
}
.pv-diff {
  max-height: 420px;
  overflow: auto;
  padding: 10px 12px;
  border-radius: 8px;
  background: var(--el-fill-color-lighter);
  font-family: var(--font-mono);
  font-size: 12px;
  line-height: 1.65;
}
.pv-diff-head {
  margin-bottom: 6px;
  color: var(--el-text-color-secondary);
}
.pv-diff-line {
  display: flex;
  gap: 6px;
  white-space: pre-wrap;
  word-break: break-word;
}
.pv-diff-line.add {
  background: var(--el-color-success-light-9);
  color: var(--el-color-success-dark-2);
}
.pv-diff-line.del {
  background: var(--el-color-danger-light-9);
  color: var(--el-color-danger-dark-2);
}
.pv-diff-sign {
  width: 12px;
  flex-shrink: 0;
  user-select: none;
}
.pv-diff-text {
  white-space: pre-wrap;
  word-break: break-word;
}
.pv-content {
  margin: 0;
  max-height: 420px;
  overflow: auto;
  padding: 12px;
  border-radius: 8px;
  background: var(--el-fill-color-lighter);
  white-space: pre-wrap;
  word-break: break-word;
  font-family: var(--font-mono);
  font-size: 12px;
  line-height: 1.65;
}
</style>
