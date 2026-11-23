<template>
  <div class="inspector-stack">
    <div v-if="!groups.length" class="inspector-empty">
      本会话还没有文件产出（写文件 / 编辑 / 向知识库添加文档后出现在这里）
    </div>

    <article v-for="group in groups" :key="group.path" class="iout-card">
      <header
        class="iout-head"
        role="button"
        tabindex="0"
        @click="toggle(group.path)"
        @keydown.enter.prevent="toggle(group.path)"
      >
        <el-icon class="iout-icon" :size="14"><Document /></el-icon>
        <span class="iout-name" :title="group.path">{{ baseName(group.path) }}</span>
        <el-tag size="small" effect="plain" :type="actionTag(group.action)">
          {{ actionLabel(group.action) }}
        </el-tag>
        <span v-if="group.count > 1" class="iout-count">×{{ group.count }}</span>
        <el-icon class="itool-chev" :class="{ open: expanded.has(group.path) }" :size="12">
          <ArrowDown />
        </el-icon>
      </header>
      <div class="iout-dir" :title="group.path">{{ group.path }}</div>

      <div v-if="expanded.has(group.path)" class="iout-detail">
        <div v-for="item in group.items" :key="item.key" class="iout-item">
          <div class="iout-item-head">
            <span class="iout-item-action">{{ actionLabel(item.action) }}</span>
            <span class="iout-item-turn">第 {{ item.turn }} 轮</span>
            <span v-if="item.source === 'subagent'" class="iout-item-src">子代理</span>
            <span v-if="item.chars" class="iout-item-chars">{{ item.chars }}{{ item.action === 'created' ? ' 字符' : '' }}</span>
          </div>
          <pre v-if="item.content" class="iout-preview">{{ previewOf(item.content) }}</pre>
        </div>
        <div v-if="group.items.every((i) => !i.content)" class="inspector-empty">
          轨迹未保留文件内容（可能已被截断），仅显示路径与动作
        </div>
      </div>
    </article>
  </div>
</template>

<script setup>
/** 产出页签：来自文件工具与子代理 artifacts，按路径聚合。 */
import { computed, ref } from 'vue'
import { ArrowDown, Document } from '@element-plus/icons-vue'
import { deriveArtifacts } from '../../../composables/useInspectorModel'

const props = defineProps({
  messages: { type: Array, default: () => [] },
})

const groups = computed(() => deriveArtifacts(props.messages))
const expanded = ref(new Set())

function toggle(path) {
  const next = new Set(expanded.value)
  if (next.has(path)) next.delete(path)
  else next.add(path)
  expanded.value = next
}

function baseName(path) {
  const parts = String(path || '').split(/[\\/]/)
  return parts[parts.length - 1] || path
}

function actionLabel(action) {
  return {
    created: '新建',
    modified: '修改',
    deleted: '删除',
    added_document: '入库',
    added: '新增',
  }[action] || action
}

function actionTag(action) {
  if (action === 'deleted') return 'danger'
  if (action === 'modified') return 'warning'
  return 'success'
}

function previewOf(content) {
  const text = String(content || '')
  return text.length > 2000 ? `${text.slice(0, 2000)}\n…（已截断）` : text
}
</script>
