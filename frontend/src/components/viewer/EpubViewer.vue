<template>
  <div v-loading="loading" class="epub-viewer">
    <div v-if="error" class="ep-error"><el-empty :description="error" /></div>
    <div v-else ref="containerEl" class="ep-container" />
    <div v-if="searching" class="ep-searching">
      <el-icon class="is-loading"><Loading /></el-icon>
      正在全书定位引用…
    </div>
  </div>
</template>

<script setup>
// EPUB 查看器：epub.js 章节化阅读。
// - 目录：spine navigation（book.loaded.navigation）；
// - 位置记忆：relocated 事件的 CFI（epub 原生书签）；
// - 引用定位：顺序扫描 spine 各章文本，命中后打开该章并高亮。
import { onBeforeUnmount, onMounted, ref } from 'vue'
import { Loading } from '@element-plus/icons-vue'
import ePub from 'epubjs'
import { flattenText, findTextInElement, flashElement } from './textSearch'

const props = defineProps({
  relativePath: { type: String, required: true },
  srcUrl: { type: String, required: true },
})

const emit = defineEmits(['ready', 'error', 'position', 'active-change'])

const loading = ref(false)
const error = ref('')
const searching = ref(false)
const containerEl = ref(null)

let book = null
let rendition = null
let tocFlat = []

onMounted(load)

async function load() {
  loading.value = true
  error.value = ''
  try {
    book = ePub(props.srcUrl)
    rendition = book.renderTo(containerEl.value, {
      width: '100%',
      height: '100%',
      // 滚动模式：分页模式（paginated）没有滚动条且滚轮不翻页，
      // 桌面阅读场景用每章滚动（章内滚轮/滚动条原生可用）
      flow: 'scrolled-doc',
      allowScriptedContent: false,
    })
    // 章内高亮样式（注入到 iframe）
    rendition.themes.default({
      body: { padding: '0 6px' },
      '.viewer-flash': {
        background: 'rgba(255, 213, 79, 0.55)',
        'border-radius': '4px',
        transition: 'background 1.2s ease',
      },
    })
    rendition.on('relocated', (loc) => {
      if (loc?.start?.cfi) emit('position', { cfi: loc.start.cfi })
      const href = loc?.start?.href
      if (href) emit('active-change', { href })
    })
    await rendition.display()
    tocFlat = await flattenNavigation()
    emit('ready')
  } catch (e) {
    error.value = e?.message || 'EPUB 加载失败'
    emit('error', error.value)
  } finally {
    loading.value = false
  }
}

async function flattenNavigation() {
  try {
    const nav = await book.loaded.navigation
    const flat = []
    const walk = (items, depth) => {
      for (const it of items || []) {
        const label = (it.label || '').trim()
        if (label) flat.push({ label, href: it.href, indent: depth })
        if (it.subitems?.length && depth < 3) walk(it.subitems, depth + 1)
      }
    }
    walk(nav.toc, 0)
    return flat
  } catch {
    return []
  }
}

function getToc() {
  return tocFlat.slice(0, 400).map((t) => ({ ...t }))
}

function jumpTo(entry) {
  if (entry?.href) rendition?.display(entry.href)
}

// ---------------- 引用定位：spine 全书扫描 ----------------

async function applyLocator(locator) {
  const anchor = locator?.anchorText
  if (!anchor || !book?.spine) return false
  const target = flattenText(anchor).slice(0, 60)
  if (target.length < 6) return false
  searching.value = true
  try {
    for (const item of book.spine.items) {
      if (!item?.href) continue
      let doc = null
      try {
        doc = await item.load(book.load.bind(book))
        const flat = flattenText(doc?.body?.textContent || '')
        if (!flat.includes(target)) continue
        await rendition.display(item.href)
        // 渲染后在活动 iframe 内精确定位滚动 + 高亮
        const contents = rendition.getContents()[0]
        if (contents?.document?.body) {
          const hit = findTextInElement(contents.document.body, anchor)
          if (hit?.element) {
            hit.element.scrollIntoView({ behavior: 'smooth', block: 'center' })
            flashElement(hit.element)
          }
        }
        return true
      } catch {
        // 单章加载失败继续下一章
      } finally {
        try {
          item.unload()
        } catch {
          // 已卸载则忽略
        }
      }
    }
    return false
  } finally {
    searching.value = false
  }
}

function applyPosition(pos) {
  if (pos?.cfi) rendition?.display(pos.cfi)
}

onBeforeUnmount(() => {
  try {
    rendition?.destroy()
  } catch {
    // 销毁过程中的异常忽略
  }
  rendition = null
  book = null
})

defineExpose({ getToc, jumpTo, applyLocator, applyPosition })
</script>

<style scoped>
.epub-viewer {
  height: 100%;
  position: relative;
  display: flex;
  flex-direction: column;
}

.ep-error {
  flex: 1;
  display: flex;
  align-items: center;
  justify-content: center;
}

.ep-container {
  flex: 1;
  min-height: 200px;
}

.ep-searching {
  position: absolute;
  right: 16px;
  bottom: 16px;
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 6px 12px;
  border-radius: 999px;
  font-size: 12px;
  color: var(--text-2, #606266);
  background: var(--bg-card, #fff);
  box-shadow: 0 2px 8px rgba(0, 0, 0, 0.15);
}
</style>
