<template>
  <div
    class="plan-approval-box"
    :data-plan-state="approval.state"
    tabindex="0"
    @keydown="emit('keydown', $event)"
  >
    <div class="plan-head">
      <el-icon :size="14"><List /></el-icon>
      计划已就绪，确认后开始执行
    </div>
    <div v-if="approval.summary" class="plan-approval-summary">
      {{ approval.summary }}
    </div>
    <template v-if="approval.state === 'pending'">
      <el-input
        :model-value="approval.text"
        type="textarea"
        :autosize="{ minRows: 3, maxRows: 10 }"
        class="plan-approval-input"
        @update:model-value="onText"
      />
      <div class="plan-approval-hint">
        直接编辑上面的步骤（一行一步）；<b>Enter 或 Ctrl+Enter 确认执行</b>，Esc 取消。
      </div>
      <div class="plan-approval-actions">
        <el-button
          type="primary"
          size="small"
          :disabled="loading"
          @click="emit('confirm')"
        >
          确认执行
        </el-button>
        <el-button size="small" :disabled="loading" @click="emit('cancel')">
          取消
        </el-button>
      </div>
    </template>
    <div v-else class="plan-approval-done">
      <PlanSteps :steps="approval.steps" variant="plain" />
    </div>
  </div>
</template>

<script setup>
/**
 * 计划确认卡（从 ChatView 抽出，2026-10-09 物理拆分）。
 *
 * 纯展示 + 三个事件：`confirm` / `cancel` / `keydown`（键盘语义由父组件决定，
 * 因为"Enter 确认、Esc 取消"要和输入框焦点状态一起判断）。
 *
 * 注意：`approval.text` 是父组件持有的对象属性，编辑时**原地写回**——保持
 * 拆分前 `v-model="msg.planApproval.text"` 的行为（父组件 confirm 时读同一份）。
 *
 * 样式：`.plan-approval-*`/`.plan-head` 随组件一起搬过来；步骤清单用共享的
 * `PlanSteps` 组件（样式随它走，不依赖父组件 :deep 穿透）。
 */
import { List } from '@element-plus/icons-vue'
import PlanSteps from './PlanSteps.vue'

const props = defineProps({
  approval: { type: Object, required: true },
  loading: { type: Boolean, default: false },
})
const emit = defineEmits(['confirm', 'cancel', 'keydown'])

function onText(value) {
  props.approval.text = value
}
</script>

<style scoped>
.plan-approval-box {
  margin-bottom: 8px;
  padding: 10px 14px;
  border-radius: var(--radius-md);
  background: var(--plan-bg);
  border: 1px solid var(--primary);
}

.plan-approval-summary {
  font-size: var(--font-sm);
  color: var(--text-2);
  margin-bottom: 6px;
}

.plan-approval-input {
  margin-bottom: 4px;
}

.plan-approval-input :deep(.el-textarea__inner) {
  font-size: var(--font-sm);
  line-height: 1.7;
  font-family: inherit;
}

.plan-approval-hint {
  font-size: var(--font-xs);
  color: var(--text-3, var(--text-2));
  margin-bottom: 8px;
}

.plan-approval-actions {
  display: flex;
  gap: 8px;
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
</style>
