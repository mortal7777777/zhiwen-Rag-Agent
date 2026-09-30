<template>
  <div class="dirpicker">
    <div class="dirpicker-row">
      <el-input
        :model-value="modelValue"
        :placeholder="placeholder"
        size="small"
        @update:model-value="onInput"
        @blur="checkPath"
      />
      <el-button size="small" @click="open">
        <el-icon :size="13"><FolderOpened /></el-icon>
        浏览
      </el-button>
    </div>
    <div v-if="status" :class="['dirpicker-status', statusClass]">{{ status }}</div>

    <el-dialog v-model="visible" title="选择目录" width="560px" append-to-body>
      <div class="dirpicker-bar">
        <el-button size="small" :disabled="!listing.parent" @click="browse(listing.parent || '')">
          ↑ 上级
        </el-button>
        <el-button size="small" @click="browse('')">盘符 / 根</el-button>
        <span class="dirpicker-path">{{ listing.path || '（选择一个盘符 / 根目录）' }}</span>
      </div>
      <div v-if="loading" class="dirpicker-hint">加载中…</div>
      <div v-else-if="error" class="dirpicker-hint err">{{ error }}</div>
      <div v-else class="dirpicker-list">
        <div v-if="!listing.dirs.length" class="dirpicker-hint">（没有子目录）</div>
        <div
          v-for="d in listing.dirs"
          :key="d"
          class="dirpicker-item"
          @click="browse(d)"
        >
          <el-icon :size="14"><FolderOpened /></el-icon>
          <span class="dirpicker-name">{{ baseName(d) }}</span>
        </div>
        <div v-if="listing.truncated" class="dirpicker-hint">（条目过多，仅显示前 500 个）</div>
      </div>
      <template #footer>
        <el-button @click="visible = false">取消</el-button>
        <el-button type="primary" :disabled="!listing.path" @click="choose">
          选用此目录
        </el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { ref } from 'vue'
import { FolderOpened } from '@element-plus/icons-vue'
import { browseDirs } from '../api'

// 目录输入：文本框 + 「浏览」对话框（后端只列目录、仅本机可访问）+ 失焦存在性校验
const props = defineProps({
  modelValue: { type: String, default: '' },
  placeholder: { type: String, default: '留空 = 默认' },
})
const emit = defineEmits(['update:modelValue'])

const visible = ref(false)
const loading = ref(false)
const error = ref('')
const listing = ref({ path: '', parent: null, dirs: [], truncated: false })
const status = ref('')
const statusClass = ref('')

function onInput(value) {
  status.value = ''
  emit('update:modelValue', value)
}

function baseName(p) {
  return String(p).replace(/[\\/]+$/, '').split(/[\\/]/).pop() || p
}

async function browse(path) {
  loading.value = true
  error.value = ''
  try {
    listing.value = await browseDirs(path)
  } catch (e) {
    error.value = e.response?.data?.detail || '读取目录失败'
  } finally {
    loading.value = false
  }
}

function open() {
  visible.value = true
  browse(props.modelValue || '')
}

function choose() {
  emit('update:modelValue', listing.value.path)
  status.value = ''
  visible.value = false
}

async function checkPath() {
  const value = String(props.modelValue || '').trim()
  if (!value) {
    status.value = ''
    return
  }
  try {
    const res = await browseDirs(value)
    status.value = `✓ 目录存在（含 ${res.dirs.length} 个子目录）`
    statusClass.value = 'ok'
  } catch (e) {
    status.value = `✗ ${e.response?.data?.detail || '路径不可用'}`
    statusClass.value = 'err'
  }
}
</script>

<style scoped>
.dirpicker {
  display: flex;
  flex-direction: column;
  gap: 4px;
  width: 100%;
}

.dirpicker-row {
  display: flex;
  gap: 6px;
  align-items: center;
}

.dirpicker-row .el-input {
  flex: 1;
}

.dirpicker-status {
  font-size: var(--font-xs);
  color: var(--text-2);
}

.dirpicker-status.ok {
  color: var(--success-text);
}

.dirpicker-status.err {
  color: var(--danger-text);
}

.dirpicker-bar {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 8px;
}

.dirpicker-path {
  flex: 1;
  font-size: var(--font-xs);
  color: var(--text-2);
  word-break: break-all;
}

.dirpicker-list {
  display: flex;
  flex-direction: column;
  max-height: 320px;
  overflow: auto;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  padding: 4px;
}

.dirpicker-item {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 8px;
  border-radius: var(--radius-sm);
  cursor: pointer;
  font-size: var(--font-sm);
}

.dirpicker-item:hover {
  background: var(--bg-hover);
}

.dirpicker-hint {
  padding: 8px;
  font-size: var(--font-xs);
  color: var(--text-2);
}

.dirpicker-hint.err {
  color: var(--danger-text);
}
</style>
