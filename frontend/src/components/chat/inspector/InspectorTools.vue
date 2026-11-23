<template>
  <div class="inspector-stack">
    <div class="inspector-scope">
      <button
        type="button"
        :class="['inspector-scope-btn', { active: scope === 'current' }]"
        @click="scope = 'current'"
      >
        本轮
      </button>
      <button
        type="button"
        :class="['inspector-scope-btn', { active: scope === 'all' }]"
        @click="scope = 'all'"
      >
        全部
      </button>
    </div>

    <div v-if="!groups.length" class="inspector-empty">
      {{ scope === 'current' ? '本轮还没有工具调用' : '本会话还没有工具调用' }}
    </div>

    <section v-for="group in groups" :key="group.turn" class="itool-group">
      <div class="inspector-group-label">
        第 {{ group.turn }} 轮
        <span v-if="running && group.turn === maxTurn" class="itool-live">运行中</span>
      </div>
      <div
        v-for="run in group.runs"
        :key="run.key"
        class="itool-row"
        role="button"
        tabindex="0"
        :title="`查看 ${toolName(run.name)} 完整内容`"
        @click="emit('open-detail', run.entry || run)"
        @keydown.enter.prevent="emit('open-detail', run.entry || run)"
      >
        <span :class="['itool-dot', run.status]"></span>
        <span class="itool-name">{{ toolName(run.name) }}</span>
        <span class="itool-summary">{{ run.summary || (run.status === 'running' ? '执行中…' : '（无摘要）') }}</span>
        <span v-if="run.durationMs != null" class="itool-dur">{{ fmtMs(run.durationMs) }}</span>
        <el-icon class="itool-chev" :size="12"><ArrowRight /></el-icon>
      </div>
    </section>
  </div>
</template>

<script setup>
/** 工具轨迹分区：本轮/全部，按轮次分组；点击行在右侧面板打开完整详情。 */
import { computed, ref } from 'vue'
import { ArrowRight } from '@element-plus/icons-vue'
import { toolName } from '../../../composables/useChatUtils'
import { flattenToolRuns } from '../../../composables/useInspectorModel'

const props = defineProps({
  messages: { type: Array, default: () => [] },
  running: { type: Boolean, default: false },
})
const emit = defineEmits(['open-detail'])

const scope = ref('current')

const runs = computed(() => flattenToolRuns(props.messages))
const maxTurn = computed(() => runs.value.reduce((max, r) => Math.max(max, r.turn), 0))
const visible = computed(() =>
  scope.value === 'current'
    ? runs.value.filter((run) => run.turn === maxTurn.value)
    : runs.value,
)
const groups = computed(() => {
  const map = new Map()
  for (const run of visible.value) {
    if (!map.has(run.turn)) map.set(run.turn, [])
    map.get(run.turn).push(run)
  }
  return [...map.entries()]
    .sort((a, b) => b[0] - a[0])
    .map(([turn, items]) => ({ turn, runs: items }))
})

function fmtMs(ms) {
  if (ms == null) return ''
  return ms < 1000 ? `${ms}ms` : `${(ms / 1000).toFixed(1)}s`
}
</script>
