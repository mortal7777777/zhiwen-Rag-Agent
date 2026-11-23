<template>
  <el-drawer
    v-model="visible"
    :title="title"
    size="70%"
    class="doc-viewer-drawer"
    :destroy-on-close="true"
    append-to-body
    @closed="onClosed"
  >
    <div class="dv-layout">
      <aside v-if="toc.length" v-show="!tocCollapsed" class="dv-toc">
        <div class="dv-toc-head">
          <span>目录（{{ toc.length }}）</span>
          <el-icon class="dv-toc-toggle" title="收起目录" @click="tocCollapsed = true">
            <DArrowLeft />
          </el-icon>
        </div>
        <div class="dv-toc-list">
          <div
            v-for="(t, i) in toc"
            :key="i"
            class="dv-toc-item"
            :class="{ active: isActive(t) }"
            :style="{ paddingLeft: `${10 + t.indent * 14}px` }"
            :title="t.label"
            @click="jumpTo(t)"
          >
            {{ t.label }}
          </div>
        </div>
      </aside>
      <div
        class="dv-main"
        :class="{ 'no-toc': !toc.length || tocCollapsed }"
        @mouseup="captureSelection"
        @keyup="captureSelection"
      >
        <el-icon
          v-if="toc.length && tocCollapsed"
          class="dv-toc-expand"
          title="展开目录"
          @click="tocCollapsed = false"
        >
          <List />
        </el-icon>
        <component
          :is="viewerComp"
          :key="relativePath"
          ref="viewerRef"
          :relative-path="relativePath"
          :src-url="srcUrl"
          @ready="onReady"
          @error="onError"
          @position="onPosition"
          @active-change="onActiveChange"
          @selection="onEpubSelection"
        />
        <!-- 划选操作条：解释 / 翻译 / 总结（直接带原文进对话） -->
        <div v-if="selection.text" class="dv-selection-bar">
          <span class="dv-selection-preview">{{ selectionPreview }}</span>
          <el-button size="small" type="primary" @click="askSelection('explain')">
            解释
          </el-button>
          <el-button size="small" @click="askSelection('translate')">翻译</el-button>
          <el-button size="small" @click="askSelection('summarize')">总结</el-button>
          <el-icon class="dv-selection-close" title="取消" @click="clearSelection">
            <Close />
          </el-icon>
        </div>
      </div>
    </div>
  </el-drawer>
</template>

<script setup>
// 文档查看器统一入口（知识库页 / 聊天引用共用）：
// - 按扩展名分发：pdf→pdf.js，epub→epub.js，docx→docx-preview，其余→文本查看器；
// - 目录侧栏统一渲染（各查看器提供 getToc/jumpTo）；
// - 定位优先级：引用定位（page/anchorText）→ 上次记住的位置；
// - 位置记忆按相对路径存 localStorage，文档 mtime 变化即失效。
import { computed, defineAsyncComponent, nextTick, ref } from 'vue'
import { Close, DArrowLeft, List } from '@element-plus/icons-vue'
import { getDocumentFileUrl, listDocuments } from '../api'
import TextViewer from './viewer/TextViewer.vue'
import { createPositionSaver, loadPosition } from './viewer/positionMemory'

// 重型渲染器（pdf.js / epub.js / docx-preview）按需异步加载
const DocxViewer = defineAsyncComponent(() => import('./viewer/DocxViewer.vue'))
const EpubViewer = defineAsyncComponent(() => import('./viewer/EpubViewer.vue'))
const PdfViewer = defineAsyncComponent(() => import('./viewer/PdfViewer.vue'))

const emit = defineEmits(['ask-selection'])

const visible = ref(false)
const relativePath = ref('')
const locator = ref(null)
const mtime = ref('')
const toc = ref([])
const tocCollapsed = ref(false)
const viewerRef = ref(null)
const activeId = ref('')
const activeInfo = ref(null)
const selection = ref({ text: '', context: '', page: null })
const selectionPreview = computed(() => {
  const text = String(selection.value.text || '').replace(/\s+/g, ' ').trim()
  return text.length > 90 ? `${text.slice(0, 90)}…` : text
})

/** DOM 查看器（文本/DOCX/PDF 文本层）的选区：取原文 + 所在段落 + 页码 */
function captureSelection(event) {
  if (event?.target?.closest?.('.dv-selection-bar')) return
  const sel = window.getSelection()
  const text = sel?.toString().trim() || ''
  if (text.length < 2) return
  const anchor = sel.anchorNode
  const el = anchor?.nodeType === 3 ? anchor.parentElement : anchor
  if (!el?.closest) return
  const pageEl = el.closest('.pv-page')
  const page = pageEl ? Number(pageEl.dataset.page) : null
  let context = text
  if (!pageEl) {
    // 文本/DOCX：取最近块级容器的完整段落，帮助模型解决指代
    const block = el.closest(
      'p, li, blockquote, pre, h1, h2, h3, h4, h5, h6, .doc-section',
    )
    context = String(block?.innerText || text).trim().slice(0, 1500)
  }
  selection.value = { text: text.slice(0, 4000), context, page }
}

/** EPUB 在 iframe 内渲染，父页面拿不到选区，由 EpubViewer 转发 */
function onEpubSelection(payload) {
  if (!payload?.text) return
  selection.value = {
    text: String(payload.text).slice(0, 4000),
    context: String(payload.context || payload.text).slice(0, 1500),
    page: null,
  }
}

function askSelection(intent) {
  if (!selection.value.text) return
  emit('ask-selection', {
    ...selection.value,
    source: relativePath.value,
    intent,
  })
  clearSelection()
}

function clearSelection() {
  selection.value = { text: '', context: '', page: null }
  try {
    window.getSelection()?.removeAllRanges()
  } catch {
    /* ignore */
  }
}

let saver = null
let mtimeWaiter = null

// 文档列表缓存（拿 mtime 用，60s TTL）
let docListCache = { at: 0, list: [] }

const title = computed(() => {
  const name = relativePath.value.split('/').pop() || '文档查看'
  return `📄 ${name}`
})

const suffix = computed(() => {
  const name = relativePath.value.split('/').pop() || ''
  const idx = name.lastIndexOf('.')
  return idx >= 0 ? name.slice(idx).toLowerCase() : ''
})

const viewerComp = computed(() => {
  if (suffix.value === '.pdf') return PdfViewer
  if (suffix.value === '.epub') return EpubViewer
  if (suffix.value === '.docx') return DocxViewer
  return TextViewer
})

const srcUrl = computed(() =>
  relativePath.value ? getDocumentFileUrl(relativePath.value) : '',
)

async function fetchMtime(path) {
  try {
    if (Date.now() - docListCache.at > 60_000) {
      docListCache.list = await listDocuments()
      docListCache.at = Date.now()
    }
    const row = docListCache.list.find((d) => d.relative_path === path)
    return row?.modified || ''
  } catch {
    return ''
  }
}

// 对外入口：打开文档；locator = { page?, anchorText? }（引用定位，优先于记忆位置）
function open(path, loc = null) {
  relativePath.value = path
  locator.value = loc || null
  clearSelection()
  toc.value = []
  activeId.value = ''
  activeInfo.value = null
  tocCollapsed.value = false
  posSuppressUntil = 0
  visible.value = true
  mtimeWaiter = fetchMtime(path).then((m) => {
    mtime.value = m
    saver = createPositionSaver(path, m)
  })
}

async function onReady() {
  if (mtimeWaiter) await mtimeWaiter
  const viewer = viewerRef.value
  if (!viewer) return
  try {
    toc.value = (await viewer.getToc?.()) || []
  } catch {
    toc.value = []
  }
  // 目录侧栏渲染会挤窄主区（如 236px），内容随之整版重排——
  // 必须在侧栏生效后再做 rect 定位，否则定位目标按满宽布局计算、整体偏移
  await nextTick()
  const remembered = loadPosition(relativePath.value, mtime.value)
  let located = false
  if (locator.value) {
    try {
      located = await viewer.applyLocator?.(locator.value)
    } catch {
      located = false
    }
  }
  if (!located && remembered) viewer.applyPosition?.(remembered)
  // 引用定位产生滚动时上报的位置会覆盖用户的阅读位置（下次打开便不是
  // 上次读到哪），短窗口内抑制位置记忆写入；窗口过后由用户滚动照常记录
  posSuppressUntil = located ? Date.now() + 1600 : 0
}

let posSuppressUntil = 0

function onError(message) {
  // 各查看器内部已展示错误，这里仅保留钩子便于日后上报
  void message
}

function onPosition(pos) {
  if (Date.now() < posSuppressUntil) return
  saver?.push(pos)
}

function onActiveChange(payload) {
  if (typeof payload === 'string') activeId.value = payload
  else activeInfo.value = payload || null
}

function isActive(entry) {
  if (entry.id) return entry.id === activeId.value
  if (entry.page && activeInfo.value?.page) return entry.page === activeInfo.value.page
  return false
}

function jumpTo(entry) {
  viewerRef.value?.jumpTo?.(entry)
}

function onClosed() {
  saver?.flush()
  clearSelection()
}

function close() {
  visible.value = false
}

defineExpose({ open, close })
</script>

<style scoped>
.dv-layout {
  display: flex;
  height: 100%;
  min-height: 0;
}

.dv-toc {
  width: 236px;
  flex: none;
  border-right: 1px solid var(--border);
  background: var(--bg-panel);
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.dv-toc-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 10px 12px;
  font-size: var(--font-sm);
  font-weight: 600;
  color: var(--text-1);
  border-bottom: 1px solid var(--border);
  flex: none;
}

.dv-toc-toggle,
.dv-toc-expand {
  cursor: pointer;
  color: var(--text-3);
}

.dv-toc-toggle:hover,
.dv-toc-expand:hover {
  color: var(--primary);
}

.dv-toc-list {
  flex: 1;
  overflow-y: auto;
  padding: 8px 0 24px;
}

.dv-toc-item {
  padding: 6px 10px;
  padding-right: 8px;
  font-size: var(--font-sm);
  line-height: 1.5;
  color: var(--text-2);
  cursor: pointer;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  border-left: 2px solid transparent;
}

.dv-toc-item:hover {
  color: var(--primary);
  background: var(--bg-hover);
}

.dv-toc-item.active {
  color: var(--primary);
  border-left-color: var(--primary);
  background: var(--primary-soft, rgba(22, 119, 255, 0.08));
}

.dv-main {
  flex: 1;
  min-width: 0;
  position: relative;
  background: var(--bg-chat);
}

html.dark .dv-main {
  background: var(--bg-chat);
}

.dv-toc-expand {
  position: absolute;
  left: 12px;
  top: 12px;
  z-index: 5;
  padding: 5px;
  border-radius: var(--radius-sm);
  color: var(--text-2);
  background: var(--bg-card);
  border: 1px solid var(--border);
  box-shadow: var(--shadow-soft, 0 2px 10px rgba(31, 35, 41, 0.05));
}

.dv-selection-bar {
  position: absolute;
  right: 16px;
  top: 12px;
  z-index: 6;
  display: flex;
  align-items: center;
  gap: 8px;
  max-width: min(680px, calc(100% - 32px));
  padding: 8px 10px;
  border-radius: var(--radius-sm);
  background: var(--bg-card);
  border: 1px solid var(--border);
  box-shadow: var(--shadow-soft, 0 2px 10px rgba(31, 35, 41, 0.05));
}

.dv-selection-preview {
  max-width: 280px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: var(--font-xs);
  color: var(--text-3);
}

.dv-selection-close {
  flex-shrink: 0;
  cursor: pointer;
  color: var(--text-3);
}

.dv-selection-close:hover {
  color: var(--danger-text);
}
</style>

<style>
/* 抽屉（teleport 到 body，scoped 够不着）：滚动交给各查看器自己管理 */
.doc-viewer-drawer .el-drawer__body {
  padding: 0;
  overflow: hidden;
}
</style>

