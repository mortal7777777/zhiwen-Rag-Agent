<template>
  <div v-loading="loading" class="text-viewer">
    <div v-if="error" class="tv-error">
      <el-empty :description="error" />
    </div>
    <template v-else-if="data">
      <div v-if="isLegacyDoc" class="tv-banner">
        <el-icon><InfoFilled /></el-icon>
        旧版 .doc（Word 97-2003）以提取文本方式预览，不含图片与排版；
        用 Word「另存为 .docx」后重新上传，可获得保留排版和图片的专业视图
      </div>
      <div v-else-if="data.truncated" class="tv-banner">
        <el-icon><WarningFilled /></el-icon>
        文档共 {{ data.char_count?.toLocaleString() }} 字符，预览仅前
        {{ (data.content || '').length.toLocaleString() }} 字符，定位可能超出范围
      </div>
      <div ref="scrollEl" class="tv-scroll" @scroll="onScroll">
        <div class="tv-column">
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
      </div>
    </template>
  </div>
</template>

<script setup>
// 文本类查看器（md / txt / 提取文本的 csv·xlsx·doc）：
// - 按标题分节渲染（无标题长文按行数分段，避免巨型单节）；
// - 目录 = 各节标题锚点；
// - 位置记忆 = 当前节 id + 节内滚动比例；
// - 引用定位 = 压平文本在渲染 DOM 里检索（见 textSearch.js）。
import { computed, nextTick, onBeforeUnmount, onMounted, ref } from 'vue'
import { InfoFilled, WarningFilled } from '@element-plus/icons-vue'
import MarkdownContent from '../MarkdownContent.vue'
import { previewDocument } from '../../api'
import {
  buildTextIndex,
  charIndexAtViewportTop,
  findTextInElement,
  flashElement,
  legacyApplyPosition,
  scrollTargetOfIndex,
  scrollToAndFlash,
} from './textSearch'

const props = defineProps({
  relativePath: { type: String, required: true },
})

const emit = defineEmits(['ready', 'error', 'position', 'active-change'])

const loading = ref(false)
const error = ref('')
const data = ref(null)
const scrollEl = ref(null)

const isMarkdown = computed(() => data.value?.kind === 'markdown')
const isLegacyDoc = computed(() =>
  props.relativePath.toLowerCase().endsWith('.doc'),
)

// ---------------- 分节（跳过代码围栏内的伪标题） ----------------

const MD_HEADING = /^(#{1,6})\s+(.+?)\s*#*\s*$/
// 纯文书的章节标题：第X章/节/回、序言/附录等独立成行（允许冒号后缀如"第一章：xxx"）
const TXT_HEADING =
  /^\s*(第[一二三四五六七八九十百千万零〇0-9]+[章节篇卷部回][:：\s][^。！？；，]{0,24}|第[一二三四五六七八九十百千万零〇0-9]+[章节篇卷部回]|序章|序言|前言|引言|绪论|后记|结语|目录|附录[^。！？；，]{0,20}|Chapter\s+\d+.*|CHAPTER\s+\d+.*)\s*$/

// 无标题的纯文本节最多容纳行数：超限强制开新节——
// 防止整本书挤进一个巨型 pre（命中后无法精确定位 + 渲染卡顿）
const MAX_PLAIN_LINES = 120

// 章节标记："第X章/节/篇/卷/回/部"
const CHAP_MARK_RE = /第[一二三四五六七八九十百千万零〇0-9]+[章节篇卷部回]/g

// 章尾/章首导航词（网页转书籍的常见标记）
const NAV_TITLES = new Set([
  '上一章', '下一章', '本章', '目录', '返回目录', '章节列表', '回到目录', '更多',
])

// 导航式行：含导航词（防"目录 下一章"、"**第X章完***"、"（第二卷结束）"式
// 章尾标记）：这类行在书里是页眉/页脚元素而非章节标题，一律不建节
const NAV_LINE_RE = /(目录|上一章|下一章|本章完|返回目录|章节列表)/
const MARKED_END_RE =
  /^[*#_\s]*第[^。！？]*章?完[*#_\s]*$|^[*#_]{2,}|[（(]?第[一二三四五六七八九十百千万〇0-9]+[卷篇章部][^）)\n]{0,8}[（(]?(完|结束)[）)]?$/
const ENDMARK_LIMIT = 18

// 目录清单行：短行、含章节标记或导航词（章尾按钮/目录条目）。
// 目录条目可能含逗号/问号（"第二章：A，以及B 第十九章：C"），不以句读结尾过滤。
function isTocListingLine(trimmed) {
  if (!trimmed || trimmed.length > 52) return false
  if ((trimmed.match(CHAP_MARK_RE) || []).length >= 1) return true
  return NAV_TITLES.has(trimmed.replace(/[：:·.—–]+/g, '').trim())
}

// 预扫描：
// 1) 连续 ≥3 行的目录/导航清单行 → 整段不建节（目录条目被当成章节标题是
//    书本 TOC 乱入的主因——节标题会完整复刻书的目录，"第一章：A 第十八章：B"
//    这类一行两列、以及"上一章/目录/下一章"按钮行都该归属正文而非章节）；
// 2) 统计短行文本出现次数：封面书名/栏目名重复出现多为版式而非章节标题。
function prescanRawText(lines, markdown) {
  const bad = new Set()
  const textCount = new Map()
  if (markdown) return { bad, textCount }
  let run = []
  const flush = () => {
    if (run.length >= 3) {
      const hasChap = run.some((i) => (lines[i].match(CHAP_MARK_RE) || []).length >= 1)
      const hasNav = run.some((i) =>
        NAV_TITLES.has(lines[i].trim().replace(/[：:·.—–]+/g, '').trim()),
      )
      if (hasChap || hasNav) run.forEach((i) => bad.add(i))
    }
    run = []
  }
  lines.forEach((line, i) => {
    const t = line.trim()
    if (isTocListingLine(t)) run.push(i)
    else flush()
    if (t && t.length <= 24) textCount.set(t, (textCount.get(t) || 0) + 1)
  })
  flush()
  return { bad, textCount }
}

// 章节标记型短行（无冒号式章节标题，如"第一章赤裸的真里""第 3 讲"）：
// 可带标点尾（"…对吗？""「嗡」"）、可含西文词（Brahmacharya 等），只限总长；
// 一行内 ≥2 个章节标记（目录页一列两行式）与不含章节标记的普通短行
// （目录页单行条目、引文、书名行"瑜伽始末　第二卷"）都不建节。
function isChapterMarkedShortLine(trimmed) {
  const chap = (trimmed.match(CHAP_MARK_RE) || []).length
  if (chap < 1 || chap >= 2) return false
  return trimmed.length <= 40
}

function splitSections(content, markdown) {
  const lines = (content || '').split('\n')
  const { bad, textCount } = prescanRawText(lines, markdown)
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
      } else {
        const trimmed = line.trim()
        const chapMarks = (trimmed.match(CHAP_MARK_RE) || []).length
        const isNavLine =
          NAV_TITLES.has(trimmed) ||
          (trimmed.length <= 14 && NAV_LINE_RE.test(trimmed)) ||
          (trimmed.length <= ENDMARK_LIMIT && MARKED_END_RE.test(trimmed))
        // 章节式标题（第X章：…/绪论/附录…）；一行内 ≥2 个章节标记
        // （书的目录常一列排两个标题）、导航式行（"目录""上一章""***第X章完***"）
        // 与目录清单行都不是章节标题
        if (
          !bad.has(i) &&
          !isNavLine &&
          chapMarks < 2 &&
          TXT_HEADING.test(line) &&
          trimmed.length <= 60
        ) {
          heading = { level: 1, title: trimmed, line }
        } else if (
          !bad.has(i) &&
          !isNavLine &&
          // 无冒号式章节标题只在含章节标记时建节；普通短行/引文/书名行
          // 一律归入正文（段落式书长文按行数强制分段，见 MAX_PLAIN_LINES）
          isChapterMarkedShortLine(trimmed)
        ) {
          heading = { level: 1, title: trimmed, line }
        }
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
    // 纯文本节行数超限：开新节（无标题时用"第 N 段"标记，避免多个匿名"正文"节）
    if (!markdown && !current.heading && current.body.length >= MAX_PLAIN_LINES) {
      const segNo = sections.length
      current = { heading: { level: 1, title: `第 ${segNo} 段`, line: '' }, body: [] }
      sections.push(current)
    }
    current.body.push(line)
  }
  return sections.map((s, idx) => ({
    id: `sec-${idx}`,
    title: s.heading?.title || '正文',
    level: s.heading?.level || 1,
    source: (s.heading?.line ? s.heading.line + '\n' : '') + s.body.join('\n'),
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
  const seen = new Set()
  const toc = []
  for (const s of sections.value) {
    // 无标题节的占位标题（"正文""第 N 段"）不是章节，不出现在目录里
    if (s.title === '正文' || /^第 ?\d+ ?段$/.test(s.title)) continue
    if (seen.has(s.title)) continue
    seen.add(s.title)
    toc.push({
      id: s.id,
      label: s.title,
      indent: Math.max(0, s.level - 1),
    })
  }
  return toc
}

function jumpTo(entry) {
  const el = document.getElementById(entry.id)
  const sc = scrollEl.value
  if (!el || !sc) return
  sc.scrollTo({
    top: sc.scrollTop + el.getBoundingClientRect().top -
      sc.getBoundingClientRect().top - 12,
    behavior: 'smooth',
  })
  // 主动高亮（等滚动节报告会把上一节列为 active，目录高亮差一节）
  activeId = entry.id
  emit('active-change', entry.id)
  scrollToAndFlash(el.querySelector('h1,h2,h3,h4,h5,h6') || el)
}

// ---------------- 滚动：当前节高亮 + 位置记忆 ----------------

let scrollTimer = null
let activeId = ''
// 文本字符索引缓存（内容不变则索引不变；key 含 content 长度防错配）
let textIndexCache = { key: '', index: null }

function getTextIndex() {
  const sc = scrollEl.value
  if (!sc) return null
  const key = `${props.relativePath}|${data.value?.content?.length || ''}`
  if (!textIndexCache.index || textIndexCache.key !== key) {
    textIndexCache = { key, index: buildTextIndex(sc) }
  }
  return textIndexCache.index
}

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
  // 位置记忆 = 视口顶部文本的平坦字符偏移（布局无关；几何方案在
  // 字体/分节变化后回放会错位两个屏以上）
  const ci = charIndexAtViewportTop(sc, getTextIndex())
  if (ci != null) emit('position', { char: ci })
}

// ---------------- 定位：引用锚点 → 上次位置 ----------------

// 返回 true 表示定位成功（父组件据此决定是否回退到记忆位置）。
// 布局真实（无 content-visibility）：节 offsetTop + 命中行号 × 行高 = 精确目标，
// 平滑滚动到命中字符所在行并高亮。
async function applyLocator(locator) {
  const sc = scrollEl.value
  if (!sc || !locator?.anchorText) return false
  const hit = findTextInElement(sc, locator.anchorText)
  if (!hit?.node) return false
  // Range 字符级定位：取命中字符的真实渲染坐标（pre-wrap 软折行由浏览器计算，
  // 行号估算在软折行场景必错——以 Range rect 为准，全量布局后同步准确）
  try {
    const range = document.createRange()
    const node = hit.node
    const start = Math.max(0, hit.offset)
    const nodeLen = (node.nodeValue || '').length
    range.setStart(node, start)
    range.setEnd(node, Math.min(start + 1, nodeLen))
    const r = range.getBoundingClientRect()
    const cRect = sc.getBoundingClientRect()
    if (r.height > 0) {
      sc.scrollTo({
        top: Math.max(0, sc.scrollTop + r.top - cRect.top - cRect.height * 0.3),
        behavior: 'smooth',
      })
    }
  } catch {
    return false
  }
  flashElement(hit.element)
  return true
}

function applyPosition(pos) {
  const sc = scrollEl.value
  if (!sc || !pos) return
  // 新版：字符索引精确回放（瞬时定位，Range 基于当前真实布局）
  if (pos.char != null) {
    const top = scrollTargetOfIndex(sc, getTextIndex(), pos.char)
    if (top != null) {
      sc.scrollTo({ top, behavior: 'auto' })
      reportScroll()
      return
    }
  }
  // 旧版记忆（anchorId + offsetRatio）兜底
  if (legacyApplyPosition(sc, pos)) reportScroll()
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

.tv-banner {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 7px 14px;
  margin: 10px 16px 0;
  border-radius: 8px;
  font-size: 12.5px;
  line-height: 1.5;
  color: var(--text-2, #4c515b);
  background: var(--primary-soft, rgba(22, 119, 255, 0.08));
  border: 1px solid var(--border, #e8ebf1);
}

.tv-scroll {
  flex: 1;
  position: relative; /* offsetTop 相对本容器计算 */
  overflow-y: auto;
  padding: 18px 24px 60px;
}

/* 阅读栏：限宽居中，收起目录后不会满屏拉宽 */
.tv-column {
  max-width: 880px;
  margin: 0 auto;
}

.doc-section {
  font-size: 15px;
  line-height: 1.85;
  color: var(--text-1, #23262d);
}

.doc-section + .doc-section {
  margin-top: 28px;
  border-top: 1px solid var(--border, #e8ebf1);
  padding-top: 22px;
}

.doc-section :deep(h1),
.doc-section :deep(h2),
.doc-section :deep(h3) {
  margin-top: 1.2em;
}

.tv-plain {
  margin: 0;
  white-space: pre-wrap;
  word-break: break-word;
  font-size: 14.5px;
  line-height: 1.85;
  text-align: justify;
  font-family: Consolas, 'JetBrains Mono', monospace;
  color: var(--text-1, #23262d);
}

/* 引用定位高亮 */
.doc-section :deep(.viewer-flash) {
  background: rgba(255, 213, 79, 0.55);
  border-radius: 4px;
  transition: background 1.2s ease;
}
</style>
