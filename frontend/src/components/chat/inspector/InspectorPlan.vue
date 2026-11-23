<template>
  <div class="inspector-stack">
    <div v-if="source" class="inspector-source">计划来源：{{ source }}</div>

    <div v-if="planTotal" class="iplan-progress">
      <div class="iplan-head">
        <span>执行进度</span>
        <span>{{ planDone || 0 }} / {{ planTotal }}</span>
      </div>
      <div class="iplan-bar">
        <i :style="{ width: `${pct}%` }" />
      </div>
    </div>

    <TodoPanel
      v-if="displayPlan.length || todos.length"
      :plan="displayPlan"
      :todos="todos"
      :plan-done="isLive ? planDone : null"
      :plan-total="isLive ? planTotal : null"
      :show-running="running"
    />
    <div v-else class="inspector-empty">本会话暂无计划或任务清单</div>
  </div>
</template>

<script setup>
/** 计划页签：实时计划/清单 + 最近一次运行记录里的计划。 */
import { computed } from 'vue'
import TodoPanel from '../TodoPanel.vue'

const props = defineProps({
  plan: { type: Array, default: () => [] },
  todos: { type: Array, default: () => [] },
  planDone: { type: Number, default: null },
  planTotal: { type: Number, default: null },
  running: { type: Boolean, default: false },
  historyPlan: { type: Array, default: () => [] },
})

const isLive = computed(() => props.plan.length > 0)
const displayPlan = computed(() => (isLive.value ? props.plan : props.historyPlan))
const source = computed(() => {
  if (isLive.value) return '本次运行'
  if (props.historyPlan.length) return '最近一次运行记录'
  return ''
})
const pct = computed(() => {
  if (!props.planTotal) return 0
  return Math.min(100, Math.round(((props.planDone || 0) / props.planTotal) * 100))
})
</script>
