<template>
  <div class="plan-box">
    <div class="plan-head">
      <el-icon :size="14"><List /></el-icon>
      {{ plan && plan.length ? '执行计划' : '任务清单' }}
      <span v-if="planDone != null" class="plan-progress">
        {{ planDone }}/{{ planTotal != null ? planTotal : plan.length }}
      </span>
    </div>
    <div v-if="todos && todos.length" class="plan-todos">
      <div
        v-for="(t, i) in todos"
        :key="t.id"
        :class="['plan-todo', { done: todoDone(t) }]"
      >
        <el-icon v-if="todoDone(t)" :size="13" class="plan-todo-check">
          <CircleCheck />
        </el-icon>
        <span v-else class="plan-todo-box"></span>
        <span class="plan-todo-text">{{ t.text }}</span>
      </div>
    </div>
    <PlanSteps
      v-else
      :steps="plan"
      :done-count="planDone"
      :show-running="showRunning"
    />
  </div>
</template>

<script setup>
/**
 * 执行计划 / 任务清单盒（从 ChatView 抽出，2026-10-09 物理拆分）。
 *
 * 纯展示：有 todos 时渲染清单（todo_update 工具写的执行清单），否则渲染
 * plan 的步骤列表（PlanSteps 组件）；头部按有无 plan 显示「执行计划 / 任务清单」。
 *
 * 注意：`.plan-head` 样式是**从上一窗口的拆分里补回来的**——它原先同时服务
 * 本盒头部与计划审批卡头部，审批卡拆分时样式随组件走了、本盒这份被误删，
 * 导致头部失去 flex/主色/间距（2026-10-09 接手窗口用 HEAD 对比发现并恢复）。
 */
import { CircleCheck, List } from '@element-plus/icons-vue'
import PlanSteps from './PlanSteps.vue'

defineProps({
  plan: { type: Array, default: () => [] },
  todos: { type: Array, default: () => [] },
  planDone: { type: Number, default: null },
  planTotal: { type: Number, default: null },
  showRunning: { type: Boolean, default: false },
})

/** 任务项是否已完成：仅以模型/系统按进度标记的 done 为准，用户不可手改 */
function todoDone(t) {
  return !!t.done
}
</script>

<style scoped>
/* 执行计划 */
.plan-box {
  margin-bottom: 8px;
  padding: 10px 14px;
  border-radius: var(--radius-md);
  background: var(--plan-bg);
  border: 1px solid var(--plan-border);
}

.plan-head {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: var(--font-sm);
  font-weight: 600;
  color: var(--primary);
  margin-bottom: 6px;
}

.plan-progress {
  margin-left: auto;
  font-size: var(--font-2xs);
  font-weight: 500;
  color: var(--text-3);
  background: var(--bg-hover);
  border-radius: var(--radius-pill);
  padding: 1px 8px;
}

/* TodoWrite 任务清单 */
.plan-todos {
  display: flex;
  flex-direction: column;
  gap: 2px;
  padding: 6px 10px 10px;
}

.plan-todo {
  display: flex;
  align-items: flex-start;
  gap: 7px;
  padding: 5px 8px;
  border-radius: var(--radius-sm);
  font-size: var(--font-sm);
  color: var(--text-1);
}

.plan-todo.done {
  color: var(--text-3);
  text-decoration: line-through;
}

.plan-todo-check {
  color: var(--success-text);
  margin-top: 2px;
}

.plan-todo-box {
  width: 13px;
  height: 13px;
  margin-top: 2px;
  border-radius: var(--radius-xs);
  border: 1.5px solid var(--border-strong);
  flex-shrink: 0;
}

.plan-todo-text {
  line-height: 1.5;
  word-break: break-word;
}
</style>
