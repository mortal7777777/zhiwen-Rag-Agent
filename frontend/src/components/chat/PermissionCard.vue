<template>
  <div class="permission-box">
    <div
      v-for="p in permissions"
      :key="p.id"
      :data-perm-id="p.id"
    >
      <div v-if="p.status === 'pending'" class="permission-card pending">
        <div class="permission-head">
          <el-icon :size="14"><Lock /></el-icon>
          <span class="permission-title">等待人工确认</span>
          <span class="permission-tool">{{ toolName(p.tool) }}</span>
        </div>
        <div class="permission-body">
          <div class="permission-summary">{{ p.summary }}</div>
          <div
            v-if="p.tool === 'edit_file' && p.args?.diff_lines?.length"
            class="perm-diff"
          >
            <div class="perm-diff-head">变更预览（红删绿增）</div>
            <div
              v-for="(d, di) in p.args.diff_lines"
              :key="di"
              :class="[
                'perm-diff-line',
                d.op === '-' ? 'del' : d.op === '+' ? 'add' : '',
              ]"
            >
              <span class="perm-diff-sign">{{ d.op === '-' ? '−' : d.op === '+' ? '+' : '·' }}</span>
              <span class="perm-diff-text">{{ d.text }}</span>
            </div>
          </div>
          <div
            v-else-if="p.tool === 'edit_file' && p.args?.old_string != null"
            class="perm-diff"
          >
            <div class="perm-diff-head">变更预览（红删绿增）</div>
            <div class="perm-diff-line del">
              <span class="perm-diff-sign">−</span>
              <span class="perm-diff-text">{{ p.args.old_string }}</span>
            </div>
            <div class="perm-diff-line add">
              <span class="perm-diff-sign">+</span>
              <span class="perm-diff-text">{{ p.args.new_string }}</span>
            </div>
          </div>
          <pre v-else-if="p.args_text" class="permission-args">{{ p.args_text }}</pre>
        </div>
        <div class="permission-actions">
          <el-input
            v-model="p.reason"
            placeholder="备注（可选）"
            size="small"
            class="permission-reason"
            @keyup.enter="emit('decide', p, p._focusIndex ?? 0)"
          />
          <div class="permission-opts" tabindex="0">
            <button
              type="button"
              class="perm-opt primary"
              :class="{ active: p._focusIndex === 0 }"
              @click="emit('decide', p, 0)"
              @mouseenter="p._focusIndex = 0"
            >
              <span class="perm-num">1</span>批准
            </button>
            <button
              type="button"
              class="perm-opt danger"
              :class="{ active: p._focusIndex === 1 }"
              @click="emit('decide', p, 1)"
              @mouseenter="p._focusIndex = 1"
            >
              <span class="perm-num">2</span>拒绝
            </button>
            <button
              v-if="p.rememberable"
              type="button"
              class="perm-opt"
              :class="{ active: p._focusIndex === 2 }"
              @click="emit('decide', p, 2)"
              @mouseenter="p._focusIndex = 2"
            >
              <span class="perm-num">3</span>批准并记住
            </button>
          </div>
          <span class="perm-key-hint">↑↓ 切换 · Enter 确认 · Esc 返回输入</span>
        </div>
      </div>
      <!-- 处理后折叠为一行状态条，不占空间 -->
      <div v-else :class="['permission-resolved-chip', p.status]">
        <el-icon :size="13">
          <CircleCheck v-if="p.status === 'approved'" />
          <Close v-else />
        </el-icon>
        <span class="prc-label">{{ p.status === 'approved' ? '已批准' : '已拒绝' }}</span>
        <span class="prc-tool">{{ toolName(p.tool) }}</span>
        <span class="prc-summary">{{ p.summary }}</span>
        <span v-if="p.reason" class="prc-reason">（{{ p.reason }}）</span>
      </div>
    </div>
    <div
      v-if="permissions.some((p) => p._suspended && !p._decision)"
      class="suspension-bar"
    >
      <span class="suspension-text">等待你的决定：全部处理后将自动继续执行</span>
      <el-button size="small" :disabled="loading" @click="emit('stop')">
        全部拒绝并停止
      </el-button>
    </div>
  </div>
</template>

<script setup>
/**
 * 人工确认卡（从 ChatView 抽出，2026-10-09 物理拆分）。
 *
 * 纯展示 + 两个事件：`decide(p, optionIndex)`（0=批准 / 1=拒绝 / 2=批准并记住）
 * 与 `stop`（挂起期间"全部拒绝并停止"）。决定后的提交与续跑逻辑留在父组件
 * （涉及 /agent/resume 与跨消息的批次判定），键盘处理也留在父组件（全局监听、
 * 依赖跨消息扫描与输入框焦点状态）。
 *
 * 约定（与拆分前行为一致，勿改）：
 * - `p.reason` / `p._focusIndex` **原地写回**：父组件的键盘处理与提交逻辑读同一份对象；
 * - `data-perm-id` 保留在每张卡的外层包装 div 上、`.permission-opts` 保持可聚焦：
 *   父组件的全局键盘（数字键 / ↑↓ / Enter / Esc）靠这两个选择器定位卡片；
 * - `toolName` 由父组件注入（与 useAgentStream 的 actions 同一函数）。
 *
 * 样式 `.permission-*` / `.perm-*` / `.prc-*` 及挂起条随组件走（scoped）；
 * 父组件的「中断未跑完」条保留一份 `.suspension-bar` 基样式复用。
 */
import { CircleCheck, Close, Lock } from '@element-plus/icons-vue'

defineProps({
  permissions: { type: Array, required: true },
  loading: { type: Boolean, default: false },
  toolName: { type: Function, required: true },
})
const emit = defineEmits(['decide', 'stop'])
</script>

<style scoped>
.permission-box {
  display: flex;
  flex-direction: column;
  gap: 8px;
  margin-top: 8px;
}

/* 原生挂起：等全部卡片决定后自动继续；也可一键全拒收尾 */
.suspension-bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  padding: 6px 10px;
  border-radius: var(--radius-sm);
  border: 1px dashed var(--border);
  background: var(--bg-soft, transparent);
  font-size: var(--font-xs);
  color: var(--text-2);
}

.permission-card {
  border-radius: var(--radius-md);
  border: 1px solid var(--border);
  background: var(--bg-card-2);
  overflow: hidden;
}

.permission-card.pending {
  border-color: var(--warning);
  box-shadow: 0 0 0 1px var(--warning-bg) inset;
}

.permission-card.approved {
  border-color: var(--el-color-success-light-5);
  opacity: 0.85;
}

.permission-card.denied {
  border-color: var(--danger-border);
  opacity: 0.85;
}

.permission-head {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 8px 12px;
  font-size: var(--font-sm);
  font-weight: 600;
  color: var(--text-2);
  background: var(--bg-card-3, rgba(128, 128, 128, 0.08));
  border-bottom: 1px solid var(--border);
}

.permission-head .el-icon {
  color: var(--warning-text);
}

.permission-card.approved .permission-head .el-icon {
  color: var(--success-text);
}

.permission-card.denied .permission-head .el-icon {
  color: var(--danger-text);
}

.permission-title {
  color: var(--text-1);
}

.permission-tool {
  margin-left: auto;
  padding: 1px 8px;
  border-radius: var(--radius-pill);
  background: var(--bg-card-2);
  border: 1px solid var(--border);
  color: var(--text-3);
  font-size: var(--font-2xs);
  font-weight: 400;
}

.permission-body {
  padding: 10px 12px 4px;
}

.permission-summary {
  font-size: var(--font-sm);
  font-weight: 600;
  color: var(--text-1);
  line-height: 1.5;
}

.permission-args {
  margin: 8px 0 0;
  padding: 8px 10px;
  border-radius: var(--radius-sm);
  background: var(--bg-card-3, rgba(128, 128, 128, 0.07));
  border: 1px solid var(--border);
  font-family: 'Cascadia Code', Consolas, monospace;
  font-size: var(--font-xs);
  line-height: 1.5;
  color: var(--text-2);
  white-space: pre-wrap;
  word-break: break-all;
  max-height: 180px;
  overflow: auto;
}

/* 编辑类审批的变更预览（红删绿增） */
.perm-diff {
  margin-top: 8px;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  overflow: hidden;
  max-height: 300px;
  overflow-y: auto;
}

.perm-diff-head {
  padding: 4px 10px;
  font-size: var(--font-2xs);
  color: var(--text-3);
  background: var(--bg-card-3, rgba(128, 128, 128, 0.07));
  border-bottom: 1px solid var(--border);
}

.perm-diff-line {
  display: flex;
  gap: 8px;
  padding: 6px 10px;
  font-family: 'Cascadia Code', Consolas, monospace;
  font-size: var(--font-xs);
  line-height: 1.5;
  white-space: pre-wrap;
  word-break: break-all;
}

.perm-diff-line.del {
  background: color-mix(in srgb, var(--danger) 10%, transparent);
  color: var(--danger-text);
}

.perm-diff-line.add {
  background: color-mix(in srgb, var(--success) 10%, transparent);
  color: var(--success-text);
}

.perm-diff-sign {
  flex-shrink: 0;
  font-weight: 700;
}

.permission-actions {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 12px 10px;
}

.permission-reason {
  flex: 1;
}

/* 审批选项卡：数字键选择 / 方向键移动 / Enter 确认 / Esc 返回输入 */
.permission-opts {
  display: flex;
  align-items: center;
  gap: 6px;
  outline: none;
}

.perm-opt {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  padding: 4px 10px;
  border-radius: var(--radius-sm);
  border: 1px solid var(--border);
  background: var(--bg-card-2);
  color: var(--text-2);
  font-size: var(--font-sm);
  cursor: pointer;
  transition: all 0.15s;
}

.perm-opt .perm-num {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 16px;
  height: 16px;
  border-radius: var(--radius-xs);
  background: var(--bg-hover);
  font-size: var(--font-2xs);
  font-weight: 700;
  color: var(--text-3);
}

.perm-opt.primary {
  border-color: var(--el-color-primary-light-5);
  color: var(--primary);
}

.perm-opt.danger {
  border-color: var(--danger-border);
  color: var(--danger-text);
}

.perm-opt:hover,
.perm-opt.active {
  border-color: var(--primary);
  background: var(--bg-active);
  box-shadow: 0 0 0 2px var(--bg-active);
}

.perm-opt.active .perm-num {
  background: var(--primary);
  color: var(--text-on-brand);
}

.perm-key-hint {
  font-size: var(--font-2xs);
  color: var(--text-3);
  white-space: nowrap;
}

/* 审批处理后的紧凑状态条 */
.permission-resolved-chip {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-top: 8px;
  padding: 4px 10px;
  border-radius: var(--radius-sm);
  border: 1px solid var(--border);
  background: var(--bg-card-2);
  font-size: var(--font-xs);
  color: var(--text-2);
}

.permission-resolved-chip.approved {
  border-color: var(--el-color-success-light-7);
}

.permission-resolved-chip.approved > .el-icon {
  color: var(--success-text);
}

.permission-resolved-chip.denied {
  border-color: var(--danger-border);
}

.permission-resolved-chip.denied > .el-icon {
  color: var(--danger-text);
}

.prc-label {
  font-weight: 600;
  flex-shrink: 0;
}

.prc-tool {
  padding: 0 7px;
  border-radius: var(--radius-pill);
  background: var(--bg-hover);
  color: var(--text-3);
  font-size: var(--font-2xs);
  flex-shrink: 0;
}

.prc-summary {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--text-3);
}

.prc-reason {
  color: var(--text-3);
  flex-shrink: 0;
}
</style>
