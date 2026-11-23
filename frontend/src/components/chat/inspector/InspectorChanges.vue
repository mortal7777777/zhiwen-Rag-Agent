<template>
  <div class="inspector-stack">
    <div v-if="!changes.length" class="inspector-empty">
      本会话还没有文件变更（写入 / 编辑 / 删除文件后出现在这里）
    </div>

    <article v-for="change in changes" :key="change.key" class="ichg-card">
      <header
        class="ichg-head"
        role="button"
        tabindex="0"
        @click="toggle(change.key)"
        @keydown.enter.prevent="toggle(change.key)"
      >
        <el-tag size="small" effect="plain" :type="tagType(change.action)">
          {{ actionLabel(change.action) }}
        </el-tag>
        <span class="ichg-path" :title="change.path">{{ change.path }}</span>
        <span class="ichg-turn">第 {{ change.turn }} 轮</span>
        <span v-if="change.chars" class="ichg-chars">+{{ change.chars }}</span>
        <el-icon class="itool-chev" :class="{ open: expandedKey === change.key }" :size="12">
          <ArrowDown />
        </el-icon>
      </header>

      <div v-if="expandedKey === change.key" class="ichg-detail">
        <template v-if="change.action === 'edit' && activeDiff">
          <div v-if="activeDiff.skippedPrefix" class="ichg-skip">
            前 {{ activeDiff.skippedPrefix }} 行未变化
          </div>
          <div class="ichg-diff">
            <div
              v-for="(line, i) in activeDiff.lines"
              :key="i"
              :class="['ichg-line', line.type]"
            >
              <span class="ichg-sign">{{ line.sign }}</span>
              <span class="ichg-text">{{ line.text }}</span>
            </div>
          </div>
          <div v-if="activeDiff.skippedSuffix" class="ichg-skip">
            后 {{ activeDiff.skippedSuffix }} 行未变化
          </div>
          <div v-if="activeDiff.truncated" class="ichg-skip">diff 过长，已截断</div>
        </template>

        <template v-else-if="change.action === 'write'">
          <div v-if="writeLines.length" class="ichg-diff">
            <div v-for="(line, i) in writeLines" :key="i" class="ichg-line add">
              <span class="ichg-sign">+</span>
              <span class="ichg-text">{{ line }}</span>
            </div>
          </div>
          <div v-else class="inspector-empty">轨迹未保留写入内容（可能已被截断）</div>
          <div v-if="writeTruncated" class="ichg-skip">内容过长，已截断</div>
        </template>

        <div v-else class="inspector-empty">该文件已删除，无内容可对比</div>
      </div>
    </article>
  </div>
</template>

<script setup>
/** 变更页签：write/edit/delete 的逐次操作与行级 diff。 */
import { computed, ref } from 'vue'
import { ArrowDown } from '@element-plus/icons-vue'
import { buildChangeDiff, deriveChanges } from '../../../composables/useInspectorModel'

const props = defineProps({
  messages: { type: Array, default: () => [] },
})

const changes = computed(() => deriveChanges(props.messages).slice().reverse())
const expandedKey = ref(null)

const active = computed(
  () => changes.value.find((change) => change.key === expandedKey.value) || null,
)
const activeDiff = computed(() =>
  active.value?.action === 'edit'
    ? buildChangeDiff(active.value.oldText, active.value.newText)
    : null,
)
const writeLines = computed(() => {
  if (active.value?.action !== 'write') return []
  const lines = String(active.value.content || '').split('\n')
  return lines.slice(0, 80)
})
const writeTruncated = computed(() => {
  if (active.value?.action !== 'write') return false
  return String(active.value.content || '').split('\n').length > 80
})

function toggle(key) {
  expandedKey.value = expandedKey.value === key ? null : key
}

function actionLabel(action) {
  return { write: '写入', edit: '编辑', delete: '删除' }[action] || action
}

function tagType(action) {
  if (action === 'delete') return 'danger'
  if (action === 'edit') return 'warning'
  return 'success'
}
</script>
