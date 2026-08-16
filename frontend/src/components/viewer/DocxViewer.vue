<template>
  <div v-loading="loading" class="docx-viewer">
    <div v-if="error" class="dx-error"><el-empty :description="error" /></div>
    <div v-else ref="containerEl" class="dx-container" @scroll="onScroll" />
  </div>
</template>

<script setup>
// DOCX 查看器：docx-preview 保留 Word 排版渲染为 HTML。
// - 页级 content-visibility: auto（docx-preview 按 Word 分节输出 <section class="docx">）；
// - 目录 = 渲染后收集 h1~h6 标题锚点；
// - 位置记忆 / 引用定位与文本查看器同套逻辑（最近标题 + 文本检索）。
import { nextTick, onBeforeUnmount, onMounted, ref } from 'vue'
import axios from 'axios'
import { renderAsync } from 'docx-preview'
import { getDocumentFileUrl } from '../../api'
import { findTextInElement, scrollToAndFlash } from './textSearch'

const props = defineProps({
  relativePath: { type: String, required: true },
  srcUrl: { type: String, required: true },
})

const emit = defineEmits(['ready', 'error', 'position', 'active-change'])

const loading = ref(false)
const error = ref('')
const containerEl = ref(null)
let headings = []
let activeId = ''
let scrollTimer = null

onMounted(load)

async function load() {
  loading.value = true
  error.value = ''
  try {
    const res = await axios.get(props.srcUrl || getDocumentFileUrl(props.relativePath), {
      responseType: 'arraybuffer',
    })
    await renderAsync(res.data, containerEl.value, null, {
      className: 'docx',
      inWrapper: true,
      ignoreLastRenderedPageBreak: false,
      experimental: false,
    })
    await nextTick()
    collectHeadings()
    emit('ready')
  } catch (e) {
    error.value = e?.response?.data?.detail || e?.message || 'DOCX 渲染失败'
    emit('error', error.value)
  } finally {
    loading.value = false
  }
}

function collectHeadings() {
  headings = []
  const els = containerEl.value?.querySelectorAll('h1,h2,h3,h4,h5,h6') || []
  els.forEach((el, i) => {
    const id = `dx-h-${i}`
    el.id = id
    headings.push({
      id,
      label: (el.textContent || '').trim().slice(0, 60) || `标题 ${i + 1}`,
      indent: Math.max(0, Number(el.tagName[1]) - 1),
      el,
    })
  })
}

function getToc() {
  return headings.slice(0, 300).map((h) => ({
    id: h.id,
    label: h.label,
    indent: h.indent,
  }))
}

function jumpTo(entry) {
  const el = containerEl.value?.querySelector(`#${CSS.escape(entry.id)}`)
  if (el && containerEl.value) scrollToAndFlash(el, containerEl.value)
}

function onScroll() {
  if (scrollTimer) return
  scrollTimer = setTimeout(() => {
    scrollTimer = null
    reportScroll()
  }, 150)
}

function reportScroll() {
  const sc = containerEl.value
  if (!sc) return
  const top = sc.scrollTop + 80
  let hit = null
  for (const h of headings) {
    if (h.el.offsetTop <= top) hit = h
    else break
  }
  if (hit && hit.id !== activeId) {
    activeId = hit.id
    emit('active-change', activeId)
  }
  if (hit) {
    const span = Math.max(1, hit.el.offsetHeight - sc.clientHeight)
    const ratio = Math.min(1, Math.max(0, (sc.scrollTop - hit.el.offsetTop) / span))
    emit('position', { anchorId: activeId, offsetRatio: ratio })
  }
}

// 两段式：先滚到命中元素所在 Word 分节（section.docx 总有布局）触发渲染，
// 再精确定位——content-visibility 跳过渲染的节内 rect 不可靠
async function applyLocator(locator) {
  const sc = containerEl.value
  if (!sc || !locator?.anchorText) return false
  const hit = findTextInElement(sc, locator.anchorText)
  if (!hit?.element) return false
  const section = hit.element.closest('section.docx')
  if (section) {
    sc.scrollTop = section.offsetTop - 8
    await new Promise((r) => setTimeout(r, 150))
  }
  scrollToAndFlash(hit.element, sc)
  return true
}

function applyPosition(pos) {
  const sc = containerEl.value
  if (!sc || !pos?.anchorId) return
  const el = sc.querySelector(`#${CSS.escape(pos.anchorId)}`)
  if (!el) return
  const span = Math.max(1, el.offsetHeight - sc.clientHeight)
  sc.scrollTop = el.offsetTop + (pos.offsetRatio || 0) * span
  reportScroll()
}

onBeforeUnmount(() => {
  if (scrollTimer) clearTimeout(scrollTimer)
})

defineExpose({ getToc, jumpTo, applyLocator, applyPosition })
</script>

<style scoped>
.docx-viewer {
  height: 100%;
  display: flex;
  flex-direction: column;
}

.dx-error {
  flex: 1;
  display: flex;
  align-items: center;
  justify-content: center;
}

.dx-container {
  flex: 1;
  position: relative; /* offsetTop 相对本容器计算 */
  overflow-y: auto;
  background: var(--bg-app, #f3f4f8);
}

/* docx-preview 输出 .docx-wrapper > section.docx（Word 分节 = 页），
   屏外页跳过渲染，DOM 完整可检索 */
.dx-container :deep(.docx-wrapper) {
  display: flex;
  flex-direction: column;
  align-items: center;
  padding: 16px 0 40px;
  background: none;
}

.dx-container :deep(.docx-wrapper > section.docx) {
  content-visibility: auto;
  contain-intrinsic-size: auto 1056px;
  box-shadow: 0 1px 6px rgba(0, 0, 0, 0.16);
  margin-bottom: 16px;
  background: #fff;
}

.dx-container :deep(.viewer-flash) {
  background: rgba(255, 213, 79, 0.55);
  border-radius: 4px;
  transition: background 1.2s ease;
}
</style>
