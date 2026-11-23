<template>
  <div class="tool-trace">
    <template v-for="(t, i) in items" :key="i">
      <div v-if="t.kind === 'thinking'" class="think-block">
        <div class="think-head" @click="t.done && (t._open = !t._open)">
          <el-icon :size="13"><MagicStick /></el-icon>
          <span>{{ t.done ? `已深度思考 · ${t.duration}s` : '深度思考中…' }}</span>
          <el-icon v-if="t.done" :size="12" class="arrow" :class="{ open: t._open }">
            <ArrowDown />
          </el-icon>
        </div>
        <div v-if="!t.done" class="think-body live">{{ t.text }}</div>
        <div v-else-if="t._open" class="think-body">{{ t.text }}</div>
      </div>
      <div v-else-if="t.kind === 'process'" class="think-block">
        <div class="think-head" @click="t._open = !t._open">
          <el-icon :size="13"><ChatDotRound /></el-icon>
          <span>过程说明</span>
          <el-icon :size="12" class="arrow" :class="{ open: t._open }">
            <ArrowDown />
          </el-icon>
        </div>
        <div v-if="t._open" class="think-body process-body">{{ t.text }}</div>
      </div>
      <div v-else class="tool-item">
        <div
          class="tool-chip clickable"
          role="button"
          tabindex="0"
          :title="`查看 ${toolName(t.name)} 完整内容`"
          @click="emit('open-detail', t)"
          @keydown.enter.prevent="emit('open-detail', t)"
          @keydown.space.prevent="emit('open-detail', t)"
        >
          <el-icon :size="13"><component :is="toolIcon(t.name)" /></el-icon>
          <span class="tool-name">{{ toolName(t.name) }}</span>
          <span class="tool-summary">
            {{ t.summary || (t.arguments && (t.arguments.query || t.arguments.command)) || '执行中…' }}
          </span>
          <el-icon :size="12" class="tool-open-icon"><ArrowRight /></el-icon>
        </div>
      </div>
    </template>
  </div>
</template>

<script setup>
/**
 * 工具调用轨迹 + 思考链时间线（从 ChatView 抽出，2026-10-09 物理拆分）。
 *
 * 纯展示：`items` 即 msg.tool_trace，逐项按 `kind === 'thinking'` 分支渲染
 * 思考块 / 工具 chip。思考块的展开态 `_open` **原地写回**条目对象（拆分前行为；
 * 父组件加载历史时会重置为 `_open: false`，见 ChatView 的消息归一化）。
 * 工具 chip 不再内联展开：点击 emit `open-detail`，由右侧会话面板渲染完整内容。
 *
 * 契约（勿改）：
 * - `toolIcon` 映射随组件走（原先也只有这里用）；`toolName` 由父组件注入
 *   （与 useStreamHandlers / PermissionCard 同一函数）；
 * - 类名 `.think-body.live` 被 useStreamHandlers 的思考链自动滚动
 *   （querySelectorAll）按 DOM 查询，跨组件边界复用，重命名会静默失效。
 */
import {
  ArrowDown,
  ArrowRight,
  ChatDotRound,
  Collection,
  Connection,
  MagicStick,
  Picture,
  Position,
  Reading,
} from '@element-plus/icons-vue'

defineProps({
  items: { type: Array, required: true },
  toolName: { type: Function, required: true },
})
const emit = defineEmits(['open-detail'])

function toolIcon(name) {
  if (name === 'web_search') return Position
  if (name === 'image_to_text') return Picture
  if (name === 'skill_view') return Reading
  if (name && name.startsWith('mcp_')) return Connection
  return Collection
}
</script>

<style scoped>
/* 思考链时间线（原始思考，仅展示；设置页开关控制，默认关闭） */
.think-block {
  margin: 4px 0;
}

.think-head {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 2px 0;
  font-size: var(--font-sm);
  color: var(--text-3);
  cursor: pointer;
  user-select: none;
}

.think-head .arrow {
  margin-left: auto;
  transition: transform 0.2s;
}

.think-head .arrow.open {
  transform: rotate(180deg);
}

.think-body {
  margin: 4px 0 8px 19px;
  padding: 8px 12px;
  border-left: 2px solid var(--plan-border);
  border-radius: 0 var(--radius-md) var(--radius-md) 0;
  background: var(--plan-bg);
  color: var(--text-2);
  font-size: 12.5px;
  line-height: 1.7;
  white-space: pre-wrap;
  word-break: break-word;
}

.think-body.live {
  max-height: 132px;
  overflow-y: auto;
}

/* 工具轮过渡说明：从正文撤回后归档到这里，默认展开但限高 */
.process-body {
  max-height: 160px;
  overflow-y: auto;
  border-left-color: var(--border);
  background: var(--bg-hover);
}

/* 工具轨迹 */
.tool-trace {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-top: 8px;
}

.tool-item {
  display: flex;
  flex-direction: column;
  min-width: 0;
}

.tool-chip {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  padding: 4px 10px;
  border-radius: var(--radius-pill);
  background: var(--bg-card-2);
  border: 1px solid var(--border);
  color: var(--text-2);
  font-size: var(--font-xs);
}

.tool-chip.clickable {
  cursor: pointer;
}

.tool-chip.clickable:hover,
.tool-chip.clickable:focus-visible {
  border-color: var(--primary);
  color: var(--primary-text);
  outline: none;
}

.tool-open-icon {
  color: var(--text-3);
}

.tool-chip .el-icon {
  color: var(--primary);
}

.tool-name {
  font-weight: 600;
}

.tool-summary {
  color: var(--text-3);
  max-width: 320px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
</style>
