<template>
  <div v-loading="loading" class="text-viewer">
    <div v-if="error" class="tv-error">
      <el-empty :description="error" />
    </div>
    <template v-else-if="data">
      <div v-if="data.truncated" class="tv-truncated">
        文档共 {{ data.char_count?.toLocaleString() }} 字符，预览仅前
        {{ (data.content || '').length.toLocaleString() }} 字符，定位可能超出范围
      </div>
      <div ref="scrollEl" class="tv-scroll" @scroll="onScroll">
        <div
          v-for="sec in sections"
          :key="sec.id"
          :id="sec.id"
          class="doc-section"
          :data-sec-title="sec.title"
        >
          <MarkdownContent v-if="isMarkdown" :content="sec.source" />
          <pre v-else class="tv-plain">{{ sec.source }}</pre>
        </div>
      </div>
    </template>
  </div>
</template>

<script setup>
// 文本类查看器（md / txt / 提取文本的 csv·xlsx·doc）：
// - 按标题分节渲染，section 级 content-visibility: auto 让浏览器跳过屏外渲染；
// - 目录 = 各节标题锚点；
// - 位置记忆 = 当前节 id + 节内滚动比例；
// - 引用定位 = 压平文本在渲染 DOM 里检索（见 textSearch.js）。
import { computed, nextTick, onBeforeUnmount, onMounted, ref } from 'vue'
import MarkdownContent from '../MarkdownContent.vue'
import { previewDocument } from '../../api'
import { findTextInElement, scrollToAndFlash } from './textSearch'

const props = defineProps({
  relativePath: { type: String, required: true },
})

const emit = defineEmits(['ready', 'error', 'position', 'active-change'])

const loading = ref(false)
const error = ref('')
const data = ref(null)
const scrollEl = ref(null)

const isMarkdown = computed(() => data.value?.kind === 'markdown')

// ---------------- 分节（跳过代码围栏内的伪标题） ----------------

const MD_HEADING = /^(#{1,6})\s+(.+?)\s*#*\s*$/
// 纯文书的章节标题：第X章/节/回、Chapter N、序言/附录等独立成行
const TXT_HEADING =
  /^\s*(第[一二三四五六七八九十百千万零〇0-9]+[章节篇卷部回]|序章|序言|前言|引言|后记|结语|附录[^\s]*|Chapter\s+\d+.*|CHAPTER\s+\d+.*)\s*$/

function splitSections(content, markdown) {
  const lines = (content || '').split('\n')
  const sections = []
  let inFence = false
  let current = null
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i]
    if (/^\s*(```|~~~)/.test(line)) inFence = !inFence
    let heading = null
    if (!inFence) {
      if (markdown) {
        const m = line.match(MD_HEADING)
        if (m) heading = { level: m[1].length, title: m[2].trim(), line }
      } else if (TXT_HEADING.test(line) && line.trim().length <= 60) {
        heading = { level: 1, title: line.trim(), line }
      }
    }
    if (heading) {
      current = { heading, body: [] }
      sections.push(current)
      continue
    }
    if (!current) {
      current = { heading: null, body: [] }
      sections.push(current)
    }
    current.body.push(line)
  }
  return sections.map((s, idx) => ({
    id: `sec-${idx}`,
    title: s.heading?.title || '正文',
    level: s.heading?.level || 1,
    source: (s.heading ? s.heading.line + '\n' : '') + s.body.join('\n'),
  }))
}

const sections = computed(() =>
  data.value ? splitSections(data.value.content, isMarkdown.value) : [],
)

// ---------------- 加载 ----------------

onMounted(load)

async function load() {
  loading.value = true
  error.value = ''
  try {
    data.value = await previewDocument(props.relativePath)
    await nextTick()
    emit('ready')
  } catch (e) {
    error.value = e?.response?.data?.detail || e?.message || '文档加载失败'
    emit('error', error.value)
  } finally {
    loading.value = false
  }
}

// ---------------- 目录 / 跳转 ----------------

function getToc() {
  return sections.value.map((s) => ({
    id: s.id,
    label: s.title,
    indent: Math.max(0, s.level - 1),
  }))
}

function jumpTo(entry) {
  const el = document.getElementById(entry.id)
  if (!el || !scrollEl.value) return
  scrollEl.value.scrollTop += el.getBoundingClientRect().top -
    scrollEl.value.getBoundingClientRect().top - 12
  scrollToAndFlash(el.querySelector('h1,h2,h3,h4,h5,h6') || el)
}

// ---------------- 滚动：当前节高亮 + 位置记忆 ----------------

let scrollTimer = null
let activeId = ''

function onScroll() {
  if (scrollTimer) return
  scrollTimer = setTimeout(() => {
    scrollTimer = null
    reportScroll()
  }, 150)
}

function visibleSection() {
  const sc = scrollEl.value
  if (!sc) return null
  const top = sc.scrollTop + 80
  let hit = null
  for (const sec of sections.value) {
    const el = document.getElementById(sec.id)
    if (!el) continue
    if (el.offsetTop <= top) hit = { sec, el }
    else break
  }
  return hit || (sections.value[0]
    ? { sec: sections.value[0], el: document.getElementById(sections.value[0].id) }
    : null)
}

function reportScroll() {
  const hit = visibleSection()
  if (!hit) return
  if (hit.sec.id !== activeId) {
    activeId = hit.sec.id
    emit('active-change', activeId)
  }
  const sc = scrollEl.value
  const span = Math.max(1, hit.el.offsetHeight - sc.clientHeight)
  const ratio = Math.min(1, Math.max(0, (sc.scrollTop - hit.el.offsetTop) / span))
  emit('position', { anchorId: activeId, offsetRatio: ratio })
}

// ---------------- 定位：引用锚点 → 上次位置 ----------------

// 返回 true 表示定位成功（父组件据此决定是否回退到记忆位置）
function applyLocator(locator) {
  if (!locator?.anchorText) return false
  const sc = scrollEl.value
  if (!sc) return false
  const hit = findTextInElement(sc, locator.anchorText)
  if (!hit?.element) return false
  scrollToAndFlash(hit.element, sc)
  return true
}

function applyPosition(pos) {
  const sc = scrollEl.value
  if (!sc || !pos?.anchorId) return
  const el = document.getElementById(pos.anchorId)
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
.text-viewer {
  height: 100%;
  display: flex;
  flex-direction: column;
  min-height: 200px;
}

.tv-error {
  flex: 1;
  display: flex;
  align-items: center;
  justify-content: center;
}

.tv-truncated {
  padding: 6px 10px;
  margin-bottom: 8px;
  border-radius: 8px;
  font-size: 12px;
  color: var(--text-3, #909399);
  background: var(--bg-card-2, rgba(128, 128, 128, 0.08));
}

.tv-scroll {
  flex: 1;
  position: relative; /* offsetTop 相对本容器计算 */
  overflow-y: auto;
  padding: 4px 16px 40px;
  scroll-behavior: auto;
}

/* 按需渲染：屏外节跳过布局/绘制，DOM 保持完整可检索 */
.doc-section {
  content-visibility: auto;
  contain-intrinsic-size: auto 600px;
}

.doc-section + .doc-section {
  margin-top: 20px;
  border-top: 1px dashed var(--border, #dcdfe6);
  padding-top: 16px;
}

.tv-plain {
  margin: 0;
  white-space: pre-wrap;
  word-break: break-word;
  font-size: 13px;
  line-height: 1.7;
  font-family: var(--font-mono, Consolas, monospace);
}

/* 引用定位高亮 */
.doc-section :deep(.viewer-flash) {
  background: rgba(255, 213, 79, 0.55);
  border-radius: 4px;
  transition: background 1.2s ease;
}
</style>
