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

// ---------------- 文本字符索引（位置记忆核心） ----------------
// 「压平全文 → 文本节点」映射与布局无关：依赖字体/分片/宽度变化的
// 几何类记忆（节 id + 比例）在布局抖动后会回放到错误位置，字符索引不受影响。

export function buildTextIndex(root) {
  const items = []
  let total = ''
  const walker = root.ownerDocument.createTreeWalker(
    root,
    window.NodeFilter.SHOW_TEXT,
  )
  if (!walker) return { items, total }
  let n = walker.nextNode()
  while (n) {
    const raw = n.nodeValue || ''
    if (raw.trim()) {
      const map = []
      let flat = ''
      for (let i = 0; i < raw.length; i++) {
        if (!STRIP_ONE.test(raw[i])) {
          flat += raw[i]
          map.push(i)
        }
      }
      if (flat) {
        items.push({ node: n, start: total.length, flat, map })
        total += flat
      }
    }
    n = walker.nextNode()
  }
  return { items, total }
}

// 平坦字符索引 → 原始文本节点与字符偏移
export function positionFromIndex(index, idx) {
  if (!index?.items?.length || typeof idx !== 'number') return null
  let lo = 0
  let hi = index.items.length - 1
  let best = -1
  while (lo <= hi) {
    const mid = (lo + hi) >> 1
    if (index.items[mid].start <= idx) {
      best = mid
      lo = mid + 1
    } else {
      hi = mid - 1
    }
  }
  if (best < 0) return null
  const it = index.items[best]
  const inner = Math.max(0, Math.min(idx - it.start, it.flat.length - 1))
  return { node: it.node, offset: it.map[inner] }
}

// 当前视口顶部的平坦字符索引。
// 定位到"第一个底边越过容器顶线的文本节点"，再在节点内对字符 Range
// 二分查找第一个"行底边 ≥ 容器顶线"的字符——即视口顶行本身。
// 字符索引与布局无关（部分文本节点长几十行，比例估算会偏几行，
// 二分到行级后几何扰动不再影响位置记忆）。
export function charIndexAtViewportTop(sc, index) {
  if (!sc || !index?.items?.length) return null
  const cRect = sc.getBoundingClientRect()
  let best = null
  for (const it of index.items) {
    const host = it.node.parentElement || it.node
    const r = host.getBoundingClientRect()
    if (r.height > 0 && r.bottom >= cRect.top) {
      best = it
      break
    }
  }
  if (!best) {
    const last = index.items[index.items.length - 1]
    return last.start + last.flat.length
  }
  // 二分第一个"行底边 >= 容器顶线"的保留字符（纵向单调）
  let lo = 0
  let hi = best.flat.length - 1
  let found = -1
  const node = best.node
  const nodeLen = (node.nodeValue || '').length
  while (lo <= hi) {
    const mid = (lo + hi) >> 1
    const charIdx = best.map[mid]
    let bottom = -1
    try {
      const range = document.createRange()
      range.setStart(node, charIdx)
      range.setEnd(node, Math.min(charIdx + 1, nodeLen))
      const r = range.getBoundingClientRect()
      if (r.height > 0) bottom = r.bottom
    } catch {
      bottom = -1
    }
    if (bottom >= cRect.top) {
      found = mid
      hi = mid - 1
    } else {
      lo = mid + 1
    }
  }
  if (found < 0) return best.start
  return best.start + found
}

// Range 精确定位一个平坦字符索引 → 目标滚动偏移；找不到返回 null。
// align='top'（位置恢复：顶行对齐视口顶）或 'center'（引用定位：字符位于上方 30%）。
export function scrollTargetOfIndex(sc, index, idx, align = 'top') {
  const hit = positionFromIndex(index, idx)
  if (!hit?.node) return null
  const node = hit.node
  const start = Math.max(0, hit.offset)
  const nodeLen = (node.nodeValue || '').length
  try {
    const range = document.createRange()
    range.setStart(node, start)
    range.setEnd(node, Math.min(start + 1, nodeLen))
    const r = range.getBoundingClientRect()
    if (r.height <= 0) return null
    const cRect = sc.getBoundingClientRect()
    const offset =
      align === 'center'
        ? sc.scrollTop + r.top - cRect.top - cRect.height * 0.3
        : sc.scrollTop + r.top - cRect.top - 12
    return Math.max(0, offset)
  } catch {
    return null
  }
}

// 兼容旧格式：{anchorId, offsetRatio} —— 新记忆统一 {char}，此函数仅在
// 旧数据回放时使用（几何方案，错误容忍度低但至少不下落文档底部）。
export function legacyApplyPosition(sc, pos) {
  if (!sc || !pos?.anchorId) return false
  const el = document.getElementById(pos.anchorId)
  if (!el) return false
  const span = Math.max(1, el.offsetHeight - sc.clientHeight)
  sc.scrollTo({
    top: Math.max(0, el.offsetTop + (pos.offsetRatio || 0) * span),
    behavior: 'smooth',
  })
  return true
}
