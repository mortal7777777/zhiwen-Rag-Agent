<template>
  <aside :class="['session-panel', { collapsed }]">
    <div class="session-head">
      <el-button
        v-if="!collapsed"
        type="primary"
        class="new-chat-btn"
        :disabled="loading"
        @click="emit('new-chat')"
      >
        <el-icon><Plus /></el-icon>
        新建对话
      </el-button>
      <el-tooltip v-else content="展开/收起会话列表" placement="right">
        <el-button circle type="primary" plain :disabled="loading" @click="emit('new-chat')">
          <el-icon><Plus /></el-icon>
        </el-button>
      </el-tooltip>
    </div>

    <template v-if="!collapsed">
      <div v-if="!sessions.length" class="session-empty">
        <el-icon :size="26"><ChatDotRound /></el-icon>
        <p>暂无历史会话</p>
        <span>点击上方「新建对话」开始提问</span>
      </div>
      <div class="session-list">
        <template v-for="group in groups" :key="group.label">
          <div class="session-group-label">{{ group.label }}</div>
          <div
            v-for="session in group.items"
            :key="session.id"
            :class="['session-item', { active: session.id === activeId }]"
            @click="emit('switch', session)"
          >
            <div class="session-info">
              <span class="session-title">{{ session.title }}</span>
              <span v-if="session.message_count" class="session-count">
                {{ session.message_count }}
              </span>
            </div>
            <div class="session-actions" @click.stop>
              <el-icon @click="emit('rename', session)"><EditPen /></el-icon>
              <el-icon @click="emit('delete', session)"><Delete /></el-icon>
            </div>
          </div>
        </template>
      </div>
    </template>

    <div class="session-foot">
      <el-tooltip
        :content="collapsed ? '展开会话列表' : '收起会话列表'"
        placement="right"
      >
        <div class="panel-toggle" @click="emit('update:collapsed', !collapsed)">
          <el-icon :size="17">
            <Expand v-if="collapsed" />
            <Fold v-else />
          </el-icon>
        </div>
      </el-tooltip>
    </div>
  </aside>
</template>

<script setup>
/**
 * 左侧会话列表（从 ChatView 抽出，2026-10-09 物理拆分）。
 *
 * 纯展示 + 五个事件：`new-chat` / `switch(session)` / `rename(session)` /
 * `delete(session)` / `update:collapsed`（折叠态归父组件持有，`v-model:collapsed`
 * 绑定；页头也有一处折叠按钮，共用同一状态）。
 *
 * 布局契约：本组件根元素即原先的 `<aside class="session-panel">`；父组件
 * `.chat-page`（flex 容器）作用到组件根上不受影响（Vue 作用域样式对子组件
 * 根节点有效），`.session-panel` / `.session-panel.collapsed` 等样式随组件走。
 */
import { computed } from 'vue'
import { ChatDotRound, Delete, EditPen, Expand, Fold, Plus } from '@element-plus/icons-vue'

const props = defineProps({
  sessions: { type: Array, required: true },
  activeId: { type: [Number, String], default: null },
  collapsed: { type: Boolean, default: false },
  loading: { type: Boolean, default: false },
})
const emit = defineEmits(['new-chat', 'switch', 'rename', 'delete', 'update:collapsed'])

// 按最近更新时间分组：今天 / 昨天 / 7 天内 / 更早（组内保持后端「最近更新在前」序）
const groups = computed(() => {
  const now = new Date()
  const startToday = new Date(now.getFullYear(), now.getMonth(), now.getDate())
  const startYesterday = new Date(startToday)
  startYesterday.setDate(startToday.getDate() - 1)
  const start7 = new Date(startToday)
  start7.setDate(startToday.getDate() - 7)

  const buckets = [
    { label: '今天', items: [] },
    { label: '昨天', items: [] },
    { label: '7 天内', items: [] },
    { label: '更早', items: [] },
  ]
  for (const s of props.sessions) {
    const t = s.updated_at ? new Date(s.updated_at) : null
    if (!t || Number.isNaN(t.getTime())) buckets[3].items.push(s)
    else if (t >= startToday) buckets[0].items.push(s)
    else if (t >= startYesterday) buckets[1].items.push(s)
    else if (t >= start7) buckets[2].items.push(s)
    else buckets[3].items.push(s)
  }
  return buckets.filter((b) => b.items.length)
})
</script>

<style scoped>
.session-panel {
  width: 248px;
  flex-shrink: 0;
  display: flex;
  flex-direction: column;
  background: var(--bg-panel);
  border-radius: var(--radius-md);
  border: 1px solid var(--border);
  overflow: hidden;
  transition: width 0.25s ease;
}

.session-panel.collapsed {
  width: 56px;
}

.session-head {
  padding: 14px;
  flex-shrink: 0;
}

.new-chat-btn {
  width: 100%;
  border-radius: var(--radius-md);
  background: linear-gradient(135deg, var(--primary) 0%, var(--primary-2) 100%);
  border: none;
  box-shadow: 0 4px 12px color-mix(in srgb, var(--primary) 25%, transparent);
}

.session-panel.collapsed .session-head {
  display: flex;
  justify-content: center;
  padding: 14px 0;
}

.session-empty {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 6px;
  color: var(--text-3);
  font-size: var(--font-sm);
  text-align: center;
  padding: 40px 16px;
}

.session-empty .el-icon {
  color: var(--text-3);
  opacity: 0.55;
  margin-bottom: 2px;
}

.session-empty p {
  margin: 0;
}

.session-empty span {
  font-size: var(--font-xs);
  color: var(--text-3);
  opacity: 0.8;
}

.session-list {
  flex: 1;
  overflow-y: auto;
  padding: 0 10px 10px;
}

/* 时间分组标题 */
.session-group-label {
  padding: 10px 10px 4px;
  font-size: var(--font-2xs);
  font-weight: 600;
  letter-spacing: 0.06em;
  color: var(--text-3);
}

.session-item {
  position: relative;
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 8px 10px;
  border-radius: var(--radius-sm);
  cursor: pointer;
  transition: background 0.15s;
  margin-bottom: 2px;
}

.session-item:hover {
  background: var(--bg-hover);
}

.session-item.active {
  background: var(--bg-active);
}

/* 选中项：左侧品牌渐变指示条（与全局导航选中态同一视觉语言） */
.session-item.active::before {
  content: '';
  position: absolute;
  left: 1px;
  top: 50%;
  transform: translateY(-50%);
  width: 3px;
  height: 16px;
  border-radius: var(--radius-pill);
  background: var(--primary-grad);
}

.session-info {
  flex: 1;
  min-width: 0;
  display: flex;
  align-items: center;
  gap: 6px;
}

.session-title {
  flex: 1;
  min-width: 0;
  font-size: var(--font-sm);
  color: var(--text-2);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.session-item.active .session-title {
  color: var(--primary);
  font-weight: 600;
}

.session-count {
  flex-shrink: 0;
  font-size: var(--font-2xs);
  color: var(--text-3);
  background: var(--bg-card-2);
  border-radius: var(--radius-pill);
  padding: 1px 6px;
}

.session-actions {
  display: none;
  gap: 4px;
  flex-shrink: 0;
}

.session-item:hover .session-actions {
  display: flex;
}

.session-actions .el-icon {
  color: var(--text-3);
  padding: 2px;
  border-radius: var(--radius-xs);
}

.session-actions .el-icon:hover {
  color: var(--primary);
  background: var(--bg-hover);
}

.session-foot {
  padding: 10px 14px;
  flex-shrink: 0;
}

.panel-toggle {
  display: flex;
  align-items: center;
  justify-content: center;
  height: 30px;
  border-radius: var(--radius-sm);
  color: var(--text-3);
  cursor: pointer;
  transition: background 0.15s;
}

.panel-toggle:hover {
  background: var(--bg-hover);
}

/* 响应式（随侧栏一起搬来；1024px 断点的 .chat-header 规则留在 ChatView） */
@media (max-width: 1440px) {
  .session-panel {
    width: 208px;
  }
}

@media (max-width: 1024px) {
  /* 会话栏改抽屉：展开时浮层覆盖，收起时保持 56px 竖条 */
  .session-panel:not(.collapsed) {
    position: fixed;
    left: 12px;
    top: 12px;
    bottom: 12px;
    z-index: var(--z-overlay);
    width: 258px;
    box-shadow: var(--shadow-modal);
  }
}
</style>
