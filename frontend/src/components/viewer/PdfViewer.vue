<template>
  <div v-loading="loading" class="pdf-viewer">
    <div v-if="error" class="pv-error"><el-empty :description="error" /></div>
    <template v-else>
      <div class="pv-toolbar">
        <el-button text size="small" :disabled="currentPage <= 1" @click="goToPage(currentPage - 1)">‹</el-button>
        <span class="pv-page-indicator">
          <input
            class="pv-page-input"
            :value="pageInput"
            @change="onPageInput"
            @keyup.enter="onPageInput"
          />
          / {{ pageCount || '-' }}
        </span>
        <el-button text size="small" :disabled="currentPage >= pageCount" @click="goToPage(currentPage + 1)">›</el-button>
        <span class="pv-flex" />
        <el-select v-model="zoom" size="small" class="pv-zoom" @change="onZoomChange">
          <el-option v-for="z in zoomOptions" :key="z.value" :label="z.label" :value="z.value" />
        </el-select>
      </div>
      <div ref="scrollEl" class="pv-scroll">
        <div
          v-for="n in pageCount"
          :key="n"
          :ref="(el) => setPageEl(n, el)"
          class="pv-page"
          :data-page="n"
        >
          <canvas class="pv-canvas" />
          <div class="pv-page-no">{{ n }}</div>
        </div>
        <div v-if="!pageCount && !loading" class="pv-error"><el-empty description="PDF 无内容" /></div>
      </div>
    </template>
  </div>
</template>

<script setup>
// PDF 查看器：pdf.js 原排版渲染。
// - 懒渲染：IntersectionObserver 只渲染可视页 ±1 页，远离的页取消任务并清空画布；
// - 目录：pdf outline 书签，dest 解析为页码；
// - 引用定位：优先来源页码，并用引用文本在附近页校验（修 off-by-one / 旧页码）；
// - 位置记忆：当前页码。
import { nextTick, onBeforeUnmount, onMounted, ref } from 'vue'
import * as pdfjsLib from 'pdfjs-dist'
import workerUrl from 'pdfjs-dist/build/pdf.worker.min.mjs?url'
import { flattenText } from './textSearch'

pdfjsLib.GlobalWorkerOptions.workerSrc = workerUrl

const props = defineProps({
  relativePath: { type: String, required: true },
  srcUrl: { type: String, required: true },
})

const emit = defineEmits(['ready', 'error', 'position', 'active-change'])

const loading = ref(false)
const error = ref('')
const scrollEl = ref(null)
const pageCount = ref(0)
const currentPage = ref(1)
const pageInput = ref(1)
const zoom = ref(1)
const zoomOptions = [
  { label: '适合宽度', value: 1 },
  { label: '125%', value: 1.25 },
  { label: '150%', value: 1.5 },
  { label: '75%', value: 0.75 },
]

// 页状态：元素 / 基础尺寸 / 渲染任务 / 已渲染的缩放标识
const pageEls = new Map()
const pageInfo = new Map()
const pageCache = new Map()
let pdfDoc = null
let observer = null
let resizeObserver = null
let renderToken = 0
const visiblePages = new Set()

function setPageEl(n, el) {
  if (el) pageEls.set(n, el)
  else pageEls.delete(n)
}

function info(n) {
  if (!pageInfo.has(n)) {
    pageInfo.set(n, { baseW: 0, baseH: 0, task: null, renderedKey: '' })
  }
  return pageInfo.get(n)
}

async function getPage(n) {
  if (!pageCache.has(n)) pageCache.set(n, await pdfDoc.getPage(n))
  return pageCache.get(n)
}

// ---------------- 加载 ----------------

onMounted(load)

async function load() {
  loading.value = true
  error.value = ''
  try {
    const task = pdfjsLib.getDocument({ url: props.srcUrl })
    pdfDoc = await task.promise
    pageCount.value = pdfDoc.numPages
    await observePages()
    await prefetchViewports()
    renderVisible()
    emit('ready')
  } catch (e) {
    error.value = e?.message || 'PDF 加载失败'
    emit('error', error.value)
  } finally {
    loading.value = false
  }
}

async function observePages() {
  await nextTick()
  observer = new IntersectionObserver(
    (entries) => {
      for (const entry of entries) {
        const n = Number(entry.target.dataset.page)
        if (entry.isIntersecting) visiblePages.add(n)
        else visiblePages.delete(n)
      }
      updateCurrentPage()
      renderVisible()
    },
    { root: scrollEl.value, rootMargin: '700px 0px' },
  )
  for (const el of pageEls.values()) observer.observe(el)
  // 容器宽度变化（窗口缩放）→ 重算缩放重渲染
  let resizeTimer = null
  resizeObserver = new ResizeObserver(() => {
    if (resizeTimer) clearTimeout(resizeTimer)
    resizeTimer = setTimeout(() => {
      for (const p of pageInfo.values()) p.renderedKey = ''
      for (let n = 1; n <= pageCount.value; n++) setWrapperHeight(n)
      renderVisible()
    }, 300)
  })
  if (scrollEl.value) resizeObserver.observe(scrollEl.value)
}

// 后台逐页取基础尺寸，设置占位高度（滚动条长度接近真实）
async function prefetchViewports() {
  for (let n = 1; n <= pageCount.value; n++) {
    if (!pdfDoc) return
    try {
      const page = await getPage(n)
      const [x1, y1, x2, y2] = page.view
      const p = info(n)
      p.baseW = x2 - x1
      p.baseH = y2 - y1
      setWrapperHeight(n)
    } catch {
      return
    }
    if (n % 20 === 0) await new Promise((r) => setTimeout(r, 0))
  }
}

function contentWidth() {
  const sc = scrollEl.value
  return sc ? sc.clientWidth - 48 : 600
}

function setWrapperHeight(n) {
  const p = info(n)
  const el = pageEls.get(n)
  if (!el || !p.baseW) return
  el.style.height = `${Math.round(contentWidth() * (p.baseH / p.baseW) * zoom.value)}px`
}

// ---------------- 渲染（可视 ±1） ----------------

function desiredRange() {
  if (!visiblePages.size) return null
  const sorted = [...visiblePages].sort((a, b) => a - b)
  const min = Math.max(1, sorted[0] - 1)
  const max = Math.min(pageCount.value, sorted[sorted.length - 1] + 1)
  return [min, max]
}

function renderVisible() {
  const range = desiredRange()
  const token = ++renderToken
  if (range) {
    for (let n = range[0]; n <= range[1]; n++) renderPage(n, token)
  }
  // 屏外页：取消渲染并释放画布
  for (const [n, p] of pageInfo) {
    if (range && n >= range[0] && n <= range[1]) continue
    if (p.task) {
      p.task.cancel()
      p.task = null
    }
    const el = pageEls.get(n)
    const canvas = el?.querySelector('canvas')
    if (canvas && p.renderedKey) {
      canvas.width = 0
      canvas.height = 0
    }
    p.renderedKey = ''
  }
}

async function renderPage(n, token) {
  const p = info(n)
  const el = pageEls.get(n)
  if (!pdfDoc || !el) return
  const scaleKey = `${zoom.value}-${contentWidth()}`
  if (p.renderedKey === scaleKey) return
  p.task?.cancel()
  let task = null
  try {
    const page = await getPage(n)
    if (token !== renderToken || !pdfDoc) return
    const dpr = Math.min(window.devicePixelRatio || 1, 2)
    const base = p.baseW || page.getViewport({ scale: 1 }).width
    const cssScale = (contentWidth() / base) * zoom.value
    const viewport = page.getViewport({ scale: cssScale * dpr })
    const canvas = el.querySelector('canvas')
    canvas.width = Math.floor(viewport.width)
    canvas.height = Math.floor(viewport.height)
    canvas.style.width = `${Math.floor(viewport.width / dpr)}px`
    canvas.style.height = `${Math.floor(viewport.height / dpr)}px`
    el.style.height = `${Math.floor(viewport.height / dpr)}px`
    task = page.render({ canvasContext: canvas.getContext('2d'), viewport })
    p.task = task
    await task.promise
    if (token === renderToken) p.renderedKey = scaleKey
  } catch {
    // RenderingCancelledException 是懒渲染的正常路径，其余单页失败也不致命
  } finally {
    if (p.task === task) p.task = null
  }
}

function updateCurrentPage() {
  if (!visiblePages.size) return
  const max = Math.max(...visiblePages)
  if (max !== currentPage.value) {
    currentPage.value = max
    pageInput.value = max
    emit('active-change', { page: max })
    emit('position', { page: max })
  }
}

// ---------------- 跳页 / 目录 ----------------

function goToPage(n, { flash = false } = {}) {
  const target = Math.min(Math.max(1, n | 0), pageCount.value || 1)
  const el = pageEls.get(target)
  const sc = scrollEl.value
  if (el && sc) {
    sc.scrollTop = el.offsetTop - 8
    if (flash) {
      el.classList.add('pv-flash')
      setTimeout(() => el.classList.remove('pv-flash'), 1800)
    }
  }
  currentPage.value = target
  pageInput.value = target
  renderVisible()
}

function onPageInput(e) {
  const n = Number(String(e.target.value).replace(/\D/g, ''))
  if (n) goToPage(n)
}

function onZoomChange() {
  for (const p of pageInfo.values()) p.renderedKey = ''
  for (let n = 1; n <= pageCount.value; n++) setWrapperHeight(n)
  renderVisible()
}

const destPageCache = new Map()

async function resolveDestPage(item) {
  try {
    let dest = item.dest
    if (typeof dest === 'string') {
      if (destPageCache.has(dest)) return destPageCache.get(dest)
      dest = await pdfDoc.getDestination(dest)
    }
    if (Array.isArray(dest) && dest[0]) {
      const idx = await pdfDoc.getPageIndex(dest[0])
      const page = idx + 1
      if (typeof item.dest === 'string') destPageCache.set(item.dest, page)
      return page
    }
  } catch {
    // 无效书签条目忽略
  }
  return null
}

async function getToc() {
  if (!pdfDoc) return []
  try {
    const outline = await pdfDoc.getOutline()
    if (!outline?.length) return []
    const flat = []
    const walk = async (items, depth) => {
      for (const it of items) {
        if (flat.length >= 400) return
        flat.push({
          label: (it.title || '').trim() || '（无标题）',
          page: await resolveDestPage(it),
          indent: depth,
        })
        if (it.items?.length && depth < 3) await walk(it.items, depth + 1)
      }
    }
    await walk(outline, 0)
    return flat
  } catch {
    return []
  }
}

// ---------------- 引用定位 / 位置恢复 ----------------

async function findPageByText(anchor, from, window = 2) {
  const target = flattenText(anchor).slice(0, 60)
  if (target.length < 6) return null
  const start = Math.max(1, from - window)
  const end =
    window === Infinity
      ? Math.min(pageCount.value, 400)
      : Math.min(pageCount.value, from + window)
  for (let n = start; n <= end; n++) {
    const page = await getPage(n)
    const tc = await page.getTextContent()
    const flat = flattenText(tc.items.map((it) => it.str).join(''))
    if (flat.includes(target)) return n
  }
  return null
}

async function applyLocator(locator) {
  if (!pdfDoc) return false
  let page = locator?.page || null
  if (locator?.anchorText) {
    const fixed = await findPageByText(
      locator.anchorText,
      page || 1,
      page ? 2 : Infinity,
    )
    if (fixed) page = fixed
    else if (!page) return false
  }
  if (!page || page < 1 || page > pageCount.value) return false
  goToPage(page, { flash: true })
  return true
}

function applyPosition(pos) {
  if (!pos?.page) return
  goToPage(pos.page)
}

function jumpTo(entry) {
  if (entry?.page) goToPage(entry.page, { flash: true })
}

// ---------------- 清理 ----------------

onBeforeUnmount(() => {
  observer?.disconnect()
  resizeObserver?.disconnect()
  for (const p of pageInfo.values()) p.task?.cancel()
  pageCache.clear()
  pdfDoc?.destroy()
  pdfDoc = null
})

defineExpose({ getToc, jumpTo, applyLocator, applyPosition })
</script>

<style scoped>
.pdf-viewer {
  height: 100%;
  display: flex;
  flex-direction: column;
}

.pv-error {
  flex: 1;
  display: flex;
  align-items: center;
  justify-content: center;
}

.pv-toolbar {
  display: flex;
  align-items: center;
  gap: 4px;
  padding: 6px 10px;
  border-bottom: 1px solid var(--border, #e8ebf1);
  background: var(--bg-panel, #fbfcfe);
  flex: none;
}

.pv-flex {
  flex: 1;
}

.pv-zoom {
  width: 104px;
}

.pv-page-indicator {
  font-size: 13px;
  color: var(--text-2, #606266);
  display: inline-flex;
  align-items: center;
  gap: 4px;
}

.pv-page-input {
  width: 44px;
  text-align: center;
  border: 1px solid var(--border, #dcdfe6);
  border-radius: 6px;
  padding: 2px 4px;
  font-size: 13px;
  background: transparent;
  color: var(--text-1, #303133);
}

.pv-scroll {
  flex: 1;
  position: relative; /* offsetTop 相对本容器计算 */
  overflow-y: auto;
  padding: 20px;
  background: #e9ecf1;
}

/* 深色主题下阅读舞台压暗（页面画布仍为白纸） */
html.dark .pv-scroll {
  background: #23262d;
}

.pv-page {
  position: relative;
  margin: 0 auto 18px;
  width: fit-content;
  min-height: 200px;
  background: #fff;
  box-shadow: 0 2px 10px rgba(0, 0, 0, 0.22);
  border-radius: 4px;
  overflow: hidden;
}

.pv-canvas {
  display: block;
}

.pv-page-no {
  position: absolute;
  bottom: 4px;
  right: 8px;
  font-size: 11px;
  color: var(--text-3, #909399);
  background: rgba(127, 127, 127, 0.12);
  border-radius: 4px;
  padding: 0 5px;
}

.pv-flash {
  outline: 3px solid rgba(255, 213, 79, 0.9);
  outline-offset: 2px;
  transition: outline-color 1.2s ease;
}
</style>
