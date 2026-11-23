<template>
  <div class="model-subnav" role="tablist" :aria-label="label">
    <button
      v-for="tab in tabs"
      :key="tab.key"
      type="button"
      role="tab"
      :aria-selected="active === tab.key"
      :class="['model-subnav-item', { active: active === tab.key }]"
      @click="select(tab.key)"
    >
      <el-icon v-if="tab.icon" :size="14"><component :is="tab.icon" /></el-icon>
      <span>{{ tab.label }}</span>
      <span v-if="tab.badge" class="model-subnav-badge">{{ tab.badge }}</span>
    </button>
  </div>
</template>

<script setup>
/**
 * 设置页通用分组导航（2026-11-22）：模型/工具/记忆面板共用。
 *
 * 受控用法：v-model 绑定当前分组 key；传 storage-key 后自动记住上次选择。
 * 样式沿用 settings.css 的 .model-subnav（名字保留，避免再改视觉变量）。
 */
import { onMounted, ref, watch } from 'vue'

const props = defineProps({
  tabs: { type: Array, required: true },
  modelValue: { type: String, default: '' },
  storageKey: { type: String, default: '' },
  label: { type: String, default: '设置分组' },
})
const emit = defineEmits(['update:modelValue'])

function valid(key) {
  return Boolean(key) && props.tabs.some((tab) => tab.key === key)
}

function readStored() {
  if (!props.storageKey) return ''
  try {
    return localStorage.getItem(props.storageKey) || ''
  } catch {
    return ''
  }
}

const stored = readStored()
const active = ref(
  valid(stored)
    ? stored
    : valid(props.modelValue)
      ? props.modelValue
      : props.tabs[0]?.key || '',
)

function select(key) {
  if (key) active.value = key
}

watch(
  () => props.modelValue,
  (value) => {
    if (valid(value) && value !== active.value) active.value = value
  },
)

watch(active, (value) => {
  emit('update:modelValue', value)
  if (!props.storageKey) return
  try {
    localStorage.setItem(props.storageKey, value)
  } catch {
    // ignore
  }
})

// 把 localStorage 恢复出的分组同步给父组件（父组件据此 v-show）
onMounted(() => emit('update:modelValue', active.value))
</script>
