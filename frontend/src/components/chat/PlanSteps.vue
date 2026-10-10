<template>
  <ol class="plan-steps">
    <li
      v-for="(step, i) in steps"
      :key="i"
      class="plan-step"
      :class="{ done: i < doneCount, running: i === doneCount && showRunning }"
    >
      <span class="plan-step-icon">
        <template v-if="variant === 'progress'">
          <el-icon v-if="i < doneCount" :size="12"><CircleCheck /></el-icon>
          <span v-else-if="i === doneCount && showRunning" class="plan-step-dot"></span>
          <span v-else class="plan-step-num">{{ i + 1 }}</span>
        </template>
        <template v-else>{{ i + 1 }}</template>
      </span>
      <span class="plan-step-text">{{ step }}</span>
    </li>
  </ol>
</template>

<script setup>
/**
 * 计划步骤列表（2026-10-09 从 ChatView 抽出，两处共用）。
 *
 * 为什么做成组件而不是"父组件 :deep() 穿透"：`.plan-step*` 这类样式原本和
 * 消息级计划列表、计划确认卡的"已完成"分支共用——卡片搬进子组件后，父组件的
 * scoped 规则不再命中子组件内部元素，只能靠 :deep() 兜。把「列表 + 它的样式」
 * 一起抽成组件，两边都直接用，既没有穿透魔法也不会样式漂移。
 *
 * variant:
 *   - progress（默认）：带完成/进行中状态（消息级计划列表用，`doneCount` + `showRunning`）
 *   - plain：只带序号（计划确认卡的"已完成"分支用）
 */
import { CircleCheck } from '@element-plus/icons-vue'

defineProps({
  steps: { type: Array, default: () => [] },
  doneCount: { type: Number, default: 0 },
  showRunning: { type: Boolean, default: false },
  variant: { type: String, default: 'progress' },
})
</script>

<style scoped>
.plan-steps {
  margin: 0;
  padding-left: 20px;
}

.plan-step {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: var(--font-sm);
  color: var(--text-2);
  line-height: 1.8;
  list-style: none;
}

.plan-step-icon {
  width: 18px;
  height: 18px;
  flex-shrink: 0;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border-radius: 50%;
  font-size: var(--font-2xs);
  color: var(--text-3);
  background: var(--bg-hover);
}

.plan-step.done .plan-step-icon {
  color: var(--text-on-brand);
  background: var(--success);
}

.plan-step.done .plan-step-text {
  color: var(--text-3);
  text-decoration: line-through;
}

.plan-step.running .plan-step-icon {
  background: var(--bg-active);
  color: var(--primary);
}

.plan-step.running .plan-step-text {
  color: var(--primary);
  font-weight: 600;
}

.plan-step-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--primary);
  animation: pulse-dot 1s infinite;
}

@keyframes pulse-dot {
  0%,
  100% {
    opacity: 0.35;
    transform: scale(0.8);
  }
  50% {
    opacity: 1;
    transform: scale(1.15);
  }
}
</style>
