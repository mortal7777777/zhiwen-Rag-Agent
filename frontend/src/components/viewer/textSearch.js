// 全文锚点定位：把元素树里的文本"压平"（去空白与标点）后做 indexOf，
// 再把命中位置映射回具体文本节点，用于引用片段 → 原文高亮。
// 压平后匹配对 Markdown 语法符号、换行、全半角标点都不敏感。

// Unicode 属性转义需要 u 标志；保留中英文与数字
const STRIP_RE = /[\s\p{P}\p{S}]+/gu
// 单字符判断用（不能带 g 标志，否则 test 会推进 lastIndex）
const STRIP_ONE = /[\s\p{P}\p{S}]/u

export function flattenText(text) {
  return (text || '').toLowerCase().replace(STRIP_RE, '')
}

// 逐步缩短锚点重试（引用片段可能被截断/改写）
function anchorCandidates(anchor) {
  const flat = flattenText(anchor)
  const cands = [flat]
  for (const len of [80, 40, 24, 14]) {
    if (flat.length > len) cands.push(flat.slice(0, len))
  }
  return [...new Set(cands)].filter((c) => c.length >= 6)
}

/**
 * 在 root 元素（或 Document.body）的文本节点里查找 anchor。
 * 返回 { node, offset, element }，找不到返回 null。
 * element = 命中文本节点最近的块级展示祖先，用于滚动与高亮。
 */
export function findTextInElement(root, anchor) {
  if (!root || !anchor) return null
  const candidates = anchorCandidates(anchor)
  if (!candidates.length) return null

  // 第一次遍历：为每个文本节点记录压平区间的起点，构建全局索引
  const nodes = []
  let total = ''
  const walker = root.ownerDocument.createTreeWalker(
    root,
    window.NodeFilter.SHOW_TEXT,
  )
  if (!walker) return null
  let current = walker.nextNode()
  while (current) {
    const raw = current.nodeValue || ''
    if (raw.trim()) {
      // 每个保留字符在原始串里的下标，用于把压平偏移映射回去
      const map = []
      let flat = ''
      for (let i = 0; i < raw.length; i++) {
        if (!STRIP_ONE.test(raw[i])) {
          flat += raw[i]
          map.push(i)
        }
      }
      if (flat) {
        nodes.push({ node: current, start: total.length, flat, map })
        total += flat
      }
    }
    current = walker.nextNode()
  }

  for (const cand of candidates) {
    const at = total.indexOf(cand)
    if (at < 0) continue
    // 找到覆盖该压平下标的文本节点
    let hit = null
    for (const item of nodes) {
      const end = item.start + item.flat.length
      if (at >= item.start && at < end) {
        hit = { node: item.node, offset: item.map[at - item.start] }
        break
      }
    }
    if (!hit) continue
    const element = blockAncestor(hit.node)
    return { ...hit, element }
  }
  return null
}

const BLOCK_TAGS = new Set([
  'P', 'LI', 'H1', 'H2', 'H3', 'H4', 'H5', 'H6', 'BLOCKQUOTE',
  'PRE', 'TD', 'TH', 'DIV', 'SECTION', 'ARTICLE', 'DD', 'DT',
])

function blockAncestor(textNode) {
  let el = textNode.parentElement
  while (el && el.parentElement && !BLOCK_TAGS.has(el.tagName.toUpperCase())) {
    el = el.parentElement
  }
  return el || textNode.parentElement
}

const FLASH_CLASS = 'viewer-flash'

// 短暂高亮目标元素（样式定义在各查看器 / 主题里）
export function flashElement(el, duration = 1800) {
  if (!el) return
  el.classList.add(FLASH_CLASS)
  setTimeout(() => el.classList.remove(FLASH_CLASS), duration)
}

// 滚动到元素并高亮（尽量居中）
export function scrollToAndFlash(el, container) {
  if (!el) return
  if (container) {
    const cRect = container.getBoundingClientRect()
    const r = el.getBoundingClientRect()
    container.scrollTop += r.top - cRect.top - cRect.height * 0.25
  } else {
    el.scrollIntoView({ behavior: 'smooth', block: 'center' })
  }
  flashElement(el)
}
