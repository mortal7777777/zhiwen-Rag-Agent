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
      <div class="dv-main" :class="{ 'no-toc': !toc.length || tocCollapsed }">
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
        />
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
import { computed, defineAsyncComponent, ref } from 'vue'
import { DArrowLeft, List } from '@element-plus/icons-vue'
import { getDocumentFileUrl, listDocuments } from '../api'
import TextViewer from './viewer/TextViewer.vue'
import { createPositionSaver, loadPosition } from './viewer/positionMemory'

// 重型渲染器（pdf.js / epub.js / docx-preview）按需异步加载
const DocxViewer = defineAsyncComponent(() => import('./viewer/DocxViewer.vue'))
const EpubViewer = defineAsyncComponent(() => import('./viewer/EpubViewer.vue'))
const PdfViewer = defineAsyncComponent(() => import('./viewer/PdfViewer.vue'))

const visible = ref(false)
const relativePath = ref('')
const locator = ref(null)
const mtime = ref('')
const toc = ref([])
const tocCollapsed = ref(false)
const viewerRef = ref(null)
const activeId = ref('')
const activeInfo = ref(null)

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
  toc.value = []
  activeId.value = ''
  activeInfo.value = null
  tocCollapsed.value = false
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
}

function onError(message) {
  // 各查看器内部已展示错误，这里仅保留钩子便于日后上报
  void message
}

function onPosition(pos) {
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
}

defineExpose({ open })
</script>

<style scoped>
.dv-layout {
  display: flex;
  height: 100%;
  min-height: 0;
}
</style>

<style>
/* 抽屉（teleport 到 body，scoped 够不着）：滚动交给各查看器自己管理 */
.doc-viewer-drawer .el-drawer__body {
  padding: 0;
  overflow: hidden;
}
</style>

<style scoped>
.dv-toc {
  width: 232px;
  flex: none;
  border-right: 1px solid var(--border, #dcdfe6);
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.dv-toc-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 8px 10px;
  font-size: 13px;
  font-weight: 600;
  color: var(--text-1, #303133);
  border-bottom: 1px solid var(--border, #dcdfe6);
  flex: none;
}

.dv-toc-toggle,
.dv-toc-expand {
  cursor: pointer;
  color: var(--text-3, #909399);
}

.dv-toc-toggle:hover,
.dv-toc-expand:hover {
  color: var(--el-color-primary);
}

.dv-toc-list {
  flex: 1;
  overflow-y: auto;
  padding: 6px 0 20px;
}

.dv-toc-item {
  padding: 6px 10px;
  padding-right: 8px;
  font-size: 12.5px;
  line-height: 1.4;
  color: var(--text-2, #606266);
  cursor: pointer;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  border-left: 2px solid transparent;
}

.dv-toc-item:hover {
  color: var(--el-color-primary);
  background: var(--bg-card-2, rgba(128, 128, 128, 0.06));
}

.dv-toc-item.active {
  color: var(--el-color-primary);
  border-left-color: var(--el-color-primary);
  background: var(--bg-card-2, rgba(128, 128, 128, 0.08));
}

.dv-main {
  flex: 1;
  min-width: 0;
  position: relative;
}

.dv-toc-expand {
  position: absolute;
  left: 10px;
  top: 10px;
  z-index: 5;
  padding: 4px;
  border-radius: 6px;
  background: var(--bg-card, #fff);
  box-shadow: 0 1px 4px rgba(0, 0, 0, 0.15);
}

.dv-main.no-toc {
  padding-left: 0;
}
</style>
