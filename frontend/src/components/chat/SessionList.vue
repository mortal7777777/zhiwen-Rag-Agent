<template>
  <aside :class="['session-panel', { collapsed }]">
    <div class="session-head">
      <el-button
        v-if="!collapsed"
        type="primary"
        class="new-chat-btn"
        :disabled="loading"
        :title="loading ? '生成中，完成后可新建对话' : '新建对话'"
        @click="emit('new-chat')"
      >
        <el-icon><Plus /></el-icon>
        新建对话
      </el-button>
      <el-tooltip
        v-else
        :show-after="1000"
        :content="loading ? '生成中，完成后可新建对话' : '新建会话'"
        placement="right"
        :hide-after="800"
      >
        <el-button
          circle
          type="primary"
          plain
          class="new-chat-btn-mini"
          :disabled="loading"
          @click="emit('new-chat')"
        >
          <el-icon><Plus /></el-icon>
        </el-button>
      </el-tooltip>
      <!-- 会话搜索（P3·⑥）：标题子串过滤，置顶组同样参与 -->
      <el-input
        v-if="!collapsed"
        v-model="searchQuery"
        size="small"
        clearable
        placeholder="搜索会话或消息"
        :prefix-icon="Search"
        class="session-search"
        @keydown.esc="searchQuery = ''"
      />
    </div>

    <template v-if="!collapsed">
      <!-- 后端搜索：标题 / 工作区 / 消息正文，带命中片段 -->
      <template v-if="searchActive">
        <div class="search-summary">
          <span class="search-summary-query">“{{ searchQuery.trim() }}”</span>
          <span v-if="!searchLoading">{{ searchResults.length }} 个会话</span>
        </div>
        <div v-if="searchLoading" class="search-hint">正在搜索会话与消息…</div>
        <div v-else-if="!searchResults.length" class="session-empty search-empty">
          <el-icon :size="22"><Search /></el-icon>
          <p>没有匹配的会话</p>
          <span>试试标题、工作区名或消息里的关键词</span>
        </div>
        <div v-else class="session-list">
          <div
            v-for="item in searchResults"
            :key="`search:${item.id}`"
            class="session-item search-hit"
            role="button"
            tabindex="0"
            @click="emit('switch', item)"
            @keydown.enter.prevent="emit('switch', item)"
            @keydown.space.prevent="emit('switch', item)"
          >
            <div class="search-hit-body">
              <span class="session-title">
                <span
                  v-for="(seg, i) in highlightSegments(item.title, searchQuery)"
                  :key="i"
                  :class="{ hit: seg.hit }"
                >{{ seg.text }}</span>
              </span>
              <span v-if="item.snippet" class="search-snippet">
                <span
                  v-for="(seg, i) in highlightSegments(item.snippet, searchQuery)"
                  :key="i"
                  :class="{ hit: seg.hit }"
                >{{ seg.text }}</span>
              </span>
              <span class="search-meta">
                {{ workspaceName(item.project_dir) }}
                <template v-if="item.match_count"> · 正文命中 {{ item.match_count }} 处</template>
                <template v-else> · 标题或工作区命中</template>
                <template v-if="item.forked_from_conversation_id">
                  · 分支自「{{ forkSourceTitle(item) || '来源会话已删除' }}」
                </template>
              </span>
            </div>
          </div>
        </div>
      </template>

      <template v-else>
        <div v-if="!sessions.length" class="session-empty">
          <el-icon :size="26"><ChatDotRound /></el-icon>
          <p>暂无历史会话</p>
          <span>点击上方「新建对话」开始提问</span>
        </div>
        <div class="session-list">
          <template v-for="row in rows" :key="row.key">
            <div
              v-if="row.type === 'label'"
              :class="['session-group-label', { 'in-workspace': row.indent }]"
            >
              {{ row.text }}
            </div>
            <!-- div + role/tabindex：内部有新建/折叠子按钮，button 不能嵌套 button -->
            <div
              v-else-if="row.type === 'ws'"
              class="workspace-group-head"
              role="button"
              tabindex="0"
              :title="row.path ? `工作区：${row.path}` : '未设置工作目录的会话'"
              :aria-expanded="!row.collapsed"
              @click="toggleWorkspace(row.wsKey)"
              @keydown.enter.prevent="toggleWorkspace(row.wsKey)"
              @keydown.space.prevent="toggleWorkspace(row.wsKey)"
            >
              <el-icon class="ws-chevron" :class="{ collapsed: row.collapsed }" :size="12">
                <ArrowDown />
              </el-icon>
              <el-icon class="ws-folder" :size="13"><FolderOpened /></el-icon>
              <span class="ws-name">{{ row.name }}</span>
              <span class="ws-count">{{ row.count }}</span>
              <button
                type="button"
                class="ws-new-chat"
                :disabled="loading"
                :title="loading ? '生成中，完成后可新建' : `在「${row.name}」工作区新建对话`"
                :aria-label="`在「${row.name}」工作区新建对话`"
                @click.stop="emit('new-chat-in-workspace', row.path)"
                @keydown.enter.stop.prevent="emit('new-chat-in-workspace', row.path)"
                @keydown.space.stop.prevent="emit('new-chat-in-workspace', row.path)"
              >
                <el-icon :size="12"><Plus /></el-icon>
              </button>
            </div>
            <!-- div + role/tabindex：内部还有重命名/删除/置顶子按钮，button 不能嵌套 button -->
            <div
              v-else
              :class="['session-item', { active: row.session.id === activeId, 'in-workspace': row.indent }]"
              role="button"
              tabindex="0"
              :aria-current="row.session.id === activeId ? 'true' : null"
              @click="emit('switch', row.session)"
              @keydown.enter.prevent="emit('switch', row.session)"
              @keydown.space.prevent="emit('switch', row.session)"
            >
              <div class="session-info">
                <el-icon v-if="row.session.pinned" class="pin-mark" :size="12">
                  <StarFilled />
                </el-icon>
                <el-tooltip
                  v-if="row.session.forked_from_conversation_id"
                  :show-after="800"
                  :content="forkTooltip(row.session)"
                  placement="top"
                  :hide-after="600"
                >
                  <el-icon class="fork-mark" :size="12"><Share /></el-icon>
                </el-tooltip>
                <span class="session-title">{{ row.session.title }}</span>
                <span v-if="row.session.message_count" class="session-count">
                  {{ row.session.message_count }}
                </span>
              </div>
              <div class="session-actions" @click.stop>
                <button
                  type="button"
                  class="session-action"
                  :title="row.session.pinned ? '取消置顶' : '置顶'"
                  :aria-label="row.session.pinned ? '取消置顶' : '置顶会话'"
                  @click="emit('pin', row.session)"
                >
                  <el-icon>
                    <StarFilled v-if="row.session.pinned" />
                    <Star v-else />
                  </el-icon>
                </button>
                <button
                  type="button"
                  class="session-action"
                  title="重命名"
                  aria-label="重命名会话"
                  @click="emit('rename', row.session)"
                >
                  <el-icon><EditPen /></el-icon>
                </button>
                <button
                  type="button"
                  class="session-action"
                  title="删除"
                  aria-label="删除会话"
                  @click="emit('delete', row.session)"
                >
                  <el-icon><Delete /></el-icon>
                </button>
              </div>
            </div>
          </template>
        </div>
      </template>
    </template>

    <div class="session-foot">
      <el-tooltip :show-after="1000"
        :content="collapsed ? '展开会话列表' : '收起会话列表'"
        placement="right"
       :hide-after="800">
        <button
          type="button"
          class="panel-toggle"
          :aria-label="collapsed ? '展开会话列表' : '收起会话列表'"
          @click="emit('update:collapsed', !collapsed)"
        >
          <el-icon :size="17">
            <Expand v-if="collapsed" />
            <Fold v-else />
          </el-icon>
        </button>
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
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import {
  ArrowDown,
  ChatDotRound,
  Delete,
  EditPen,
  Expand,
  Fold,
  FolderOpened,
  Plus,
  Search,
  Share,
  Star,
  StarFilled,
} from '@element-plus/icons-vue'

const props = defineProps({
  sessions: { type: Array, required: true },
  activeId: { type: [Number, String], default: null },
  collapsed: { type: Boolean, default: false },
  loading: { type: Boolean, default: false },
  groupByWorkspace: { type: Boolean, default: false },
  searchResults: { type: Array, default: () => [] },
  searchLoading: { type: Boolean, default: false },
})
const emit = defineEmits([
  'new-chat',
  'new-chat-in-workspace',
  'switch',
  'rename',
  'delete',
  'pin',
  'update:collapsed',
  'search',
])

// 会话搜索：输入防抖后交给父组件查后端（标题/工作区/消息正文）
const searchQuery = ref('')
const searchActive = computed(() => !!searchQuery.value.trim())
let searchTimer = null

watch(searchQuery, (value) => {
  if (searchTimer) clearTimeout(searchTimer)
  const query = value.trim()
  if (!query) {
    emit('search', '')
    return
  }
  searchTimer = setTimeout(() => emit('search', query), 250)
})

// 选中会话/新建对话后回到正常列表
watch(
  () => props.activeId,
  () => {
    if (searchQuery.value) searchQuery.value = ''
  },
)

onBeforeUnmount(() => {
  if (searchTimer) clearTimeout(searchTimer)
})

/** 关键词命中分段（大小写不敏感），模板逐段渲染，避免 v-html。 */
function highlightSegments(text, query) {
  const source = String(text || '')
  const needle = String(query || '').trim().toLowerCase()
  if (!needle) return [{ text: source, hit: false }]
  const lower = source.toLowerCase()
  const out = []
  let from = 0
  let index = lower.indexOf(needle)
  while (index >= 0) {
    if (index > from) out.push({ text: source.slice(from, index), hit: false })
    out.push({ text: source.slice(index, index + needle.length), hit: true })
    from = index + needle.length
    index = lower.indexOf(needle, from)
  }
  if (from < source.length) out.push({ text: source.slice(from), hit: false })
  return out
}

const WS_COLLAPSED_KEY = 'rag_ws_collapsed'
const DEFAULT_WS_KEY = '__default__'
const BUCKET_LABELS = ['今天', '昨天', '7 天内', '更早']

function loadCollapsedWorkspaces() {
  try {
    const raw = JSON.parse(localStorage.getItem(WS_COLLAPSED_KEY) || '[]')
    return new Set(Array.isArray(raw) ? raw.map(String) : [])
  } catch {
    return new Set()
  }
}

const collapsedWorkspaces = ref(loadCollapsedWorkspaces())

function toggleWorkspace(key) {
  const next = new Set(collapsedWorkspaces.value)
  if (next.has(key)) next.delete(key)
  else next.add(key)
  collapsedWorkspaces.value = next
  try {
    localStorage.setItem(WS_COLLAPSED_KEY, JSON.stringify([...next]))
  } catch {
    // ignore
  }
}

function workspaceKey(path) {
  return path ? String(path) : DEFAULT_WS_KEY
}

/** 工作区显示名：取目录 basename（兼容 \ 与 /），默认工作区用固定文案 */
function workspaceName(path) {
  if (!path) return '默认工作区'
  const trimmed = String(path).replace(/[\\/]+$/, '')
  const index = Math.max(trimmed.lastIndexOf('\\'), trimmed.lastIndexOf('/'))
  const base = index >= 0 ? trimmed.slice(index + 1) : trimmed
  return base || trimmed || String(path)
}

/** fork 来源会话标题（源已被删除时返回空串） */
function forkSourceTitle(session) {
  const origin = session?.forked_from_conversation_id
  if (!origin) return ''
  return props.sessions.find((s) => s.id === origin)?.title || ''
}

function forkTooltip(session) {
  const title = forkSourceTitle(session)
  return title ? `分支自「${title}」` : '分支会话（来源会话已删除或不可见）'
}

function sessionTime(session) {
  const t = session.updated_at ? new Date(session.updated_at) : null
  return t && !Number.isNaN(t.getTime()) ? t : null
}

function timeStarts() {
  const now = new Date()
  const startToday = new Date(now.getFullYear(), now.getMonth(), now.getDate())
  const startYesterday = new Date(startToday)
  startYesterday.setDate(startToday.getDate() - 1)
  const start7 = new Date(startToday)
  start7.setDate(startToday.getDate() - 7)
  return [startToday, startYesterday, start7]
}

function bucketIndex(session, starts) {
  const t = sessionTime(session)
  if (!t) return 3
  if (t >= starts[0]) return 0
  if (t >= starts[1]) return 1
  if (t >= starts[2]) return 2
  return 3
}

function latestTime(items) {
  return items.reduce((max, s) => {
    const t = sessionTime(s)
    return t && t.getTime() > max ? t.getTime() : max
  }, 0)
}

function sessionRow(key, session, indent = false) {
  return { type: 'session', key, session, indent }
}

/**
 * 扁平行渲染：label（分组/时间桶标题）/ ws（工作区头）/ session。
 * 会话项标记只写一份（子组件会破坏 scoped 样式，见组件说明）。
 * 置顶 → 工作区 → 时间桶；关闭工作区分组时保持原来的置顶 + 时间桶。
 */
const rows = computed(() => {
  const list = props.sessions
  const starts = timeStarts()
  const out = []

  const pinned = list.filter((s) => s.pinned)
  if (pinned.length) {
    out.push({ type: 'label', key: 'pin', text: '置顶' })
    pinned.forEach((s) => out.push(sessionRow(`pin:${s.id}`, s)))
  }
  const rest = list.filter((s) => !s.pinned)

  function pushBuckets(items, prefix, indent = false) {
    const buckets = [[], [], [], []]
    items.forEach((s) => buckets[bucketIndex(s, starts)].push(s))
    buckets.forEach((bucket, i) => {
      if (!bucket.length) return
      out.push({ type: 'label', key: `${prefix}:b${i}`, text: BUCKET_LABELS[i], indent })
      bucket.forEach((s) => out.push(sessionRow(`${prefix}:b${i}:${s.id}`, s, indent)))
    })
  }

  if (!props.groupByWorkspace) {
    pushBuckets(rest, 'time')
    return out
  }

  const byWorkspace = new Map()
  rest.forEach((s) => {
    const key = workspaceKey(s.project_dir)
    if (!byWorkspace.has(key)) {
      byWorkspace.set(key, { key, path: s.project_dir || '', items: [] })
    }
    byWorkspace.get(key).items.push(s)
  })
  const groups = [...byWorkspace.values()].sort(
    (a, b) => latestTime(b.items) - latestTime(a.items) || a.key.localeCompare(b.key),
  )
  groups.forEach((group) => {
    const expanded = !collapsedWorkspaces.value.has(group.key)
    out.push({
      type: 'ws',
      key: `ws:${group.key}`,
      wsKey: group.key,
      name: workspaceName(group.path),
      path: group.path,
      count: group.items.length,
      collapsed: !expanded,
    })
    if (expanded) pushBuckets(group.items, `ws:${group.key}`, true)
  })
  return out
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
  /* 用 --primary-grad（两端都按"白字 ≥4.5"收过），别用 --primary→--primary-2：
     后者在浅色下最亮端 #4a94ff 白字只有 3.19 */
  background: var(--primary-grad);
  border: none;
  box-shadow: 0 4px 12px color-mix(in srgb, var(--primary) 25%, transparent);
}

/* 生成中保持禁用（全局 loading 未按会话隔离），但把 EP 的浅灰禁用态
   换成可读的卡片色，避免整行文字发灰看不清。
   theme.css 的禁用桥接选择器带 html 前缀，这里用 !important 才能盖住。 */
.new-chat-btn.is-disabled,
.new-chat-btn-mini.is-disabled {
  --el-button-disabled-bg-color: var(--bg-card-2) !important;
  --el-button-disabled-text-color: var(--text-2) !important;
  --el-button-disabled-border-color: var(--border) !important;
  background: var(--bg-card-2) !important;
  border: 1px solid var(--border) !important;
  color: var(--text-2) !important;
  opacity: 1;
  box-shadow: none;
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

.session-group-label.in-workspace {
  padding-left: 24px;
}

/* 工作区分组头（可折叠） */
.workspace-group-head {
  display: flex;
  align-items: center;
  gap: 6px;
  width: 100%;
  padding: 8px 10px 4px;
  border: none;
  background: transparent;
  font: inherit;
  text-align: left;
  color: var(--text-2);
  cursor: pointer;
  border-radius: var(--radius-sm);
}

.workspace-group-head:hover {
  background: var(--bg-hover);
}

.workspace-group-head:focus-visible {
  outline: 2px solid var(--primary);
  outline-offset: -2px;
}

.ws-chevron {
  flex-shrink: 0;
  color: var(--text-3);
  transition: transform 0.15s ease;
}

.ws-chevron.collapsed {
  transform: rotate(-90deg);
}

.ws-folder {
  flex-shrink: 0;
  color: var(--text-3);
}

.ws-name {
  flex: 1;
  min-width: 0;
  font-size: var(--font-2xs);
  font-weight: 600;
  letter-spacing: 0.03em;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.ws-count {
  flex-shrink: 0;
  font-size: var(--font-2xs);
  color: var(--text-3);
  background: var(--bg-card-2);
  border-radius: var(--radius-pill);
  padding: 1px 6px;
}

/* 工作区头上的「在此工作区新建对话」 */
.ws-new-chat {
  flex-shrink: 0;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 18px;
  height: 18px;
  padding: 0;
  border: none;
  border-radius: var(--radius-xs);
  background: transparent;
  color: var(--text-3);
  cursor: pointer;
  opacity: 0.65;
  transition: color 0.15s, background 0.15s, opacity 0.15s;
}

.ws-new-chat:hover:not(:disabled),
.ws-new-chat:focus-visible:not(:disabled) {
  color: var(--primary-text);
  background: var(--bg-card-2);
  opacity: 1;
  outline: none;
}

.ws-new-chat:disabled {
  opacity: 0.3;
  cursor: not-allowed;
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

.session-actions .session-action {
  display: flex;
  align-items: center;
  justify-content: center;
  border: none;
  background: transparent;
  padding: 2px;
  font: inherit;
  border-radius: var(--radius-xs);
  color: var(--text-3);
  cursor: pointer;
}

.session-actions .session-action:hover {
  color: var(--primary);
  background: var(--bg-hover);
}

/* 键盘可达：会话项（div[role=button]）聚焦时给出可见提示 */
.session-item:focus-visible {
  outline: 2px solid var(--primary);
  outline-offset: -2px;
}

.session-foot {
  padding: 10px 14px;
  flex-shrink: 0;
  /* 收起态中间列表缺席（v-if），没有这条会把「展开」按钮顶在栏顶；
     展开态有 flex:1 的列表在前，margin-top:auto 不影响其仍沉底部 */
  margin-top: auto;
}

/* 会话搜索框：紧贴「新建对话」 */
.session-search {
  margin-top: 8px;
}

/* 搜索结果：标题 + 命中片段 + 工作区/命中数 */
.search-summary {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 8px;
  padding: 8px 10px 2px;
  font-size: var(--font-2xs);
  color: var(--text-3);
}

.search-summary-query {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--text-2);
  font-weight: 600;
}

.search-hint {
  padding: 10px;
  font-size: var(--font-2xs);
  color: var(--text-3);
}

.search-empty {
  padding: 28px 16px;
}

.search-hit {
  align-items: flex-start;
}

.search-hit-body {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 3px;
}

.search-hit .session-title {
  flex: none;
  width: 100%;
  white-space: normal;
  line-height: 1.45;
  word-break: break-word;
}

.search-snippet {
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
  font-size: var(--font-2xs);
  line-height: 1.5;
  color: var(--text-3);
  word-break: break-word;
}

.search-meta {
  font-size: var(--font-2xs);
  color: var(--text-3);
}

.search-hit .hit {
  color: var(--primary-text);
  font-weight: 600;
  background: var(--primary-soft);
  border-radius: 2px;
}

/* 置顶标记：不 hover 也可见（操作区的星标按钮 hover 才出） */
.pin-mark {
  flex-shrink: 0;
  color: var(--el-color-warning);
}

/* fork 来源标记：提示"这条会话是从别的会话分支出来的" */
.fork-mark {
  flex-shrink: 0;
  color: var(--text-3);
}

.panel-toggle {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 100%;
  height: 30px;
  border: none;
  background: transparent;
  padding: 0;
  font: inherit;
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
