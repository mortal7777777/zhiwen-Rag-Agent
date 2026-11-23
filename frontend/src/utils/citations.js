/**
 * 把正文里的 [n] 引用标记转成可点击角标。
 *
 * 必须在 Markdown 解析之前做，否则角标会被当成普通文本转义；但也不能
 * 对代码段做替换，否则 `arr[1]`、代码示例里的 [0] 会被改写成可见的
 * <sup> 文本。这里按围栏代码块 / 行内代码切段，只处理普通正文。
 */
const CITE_RE = /(?<!\\)\[(\d{1,3})\](?!\()/g
const CODE_SPLIT_RE = /(```[\s\S]*?```|`[^`\n]*`)/g
// marked 18 把单个 ~ 也当删除线：40~60 会渲染成 40<del>60</del>。
// 只转义孤立波浪号，保留 GFM 的 ~~删除线~~。
const SINGLE_TILDE_RE = /(?<!~)(?<!\\)~(?![~])/g

function mapProse(text, fn) {
  return String(text || '')
    .split(CODE_SPLIT_RE)
    .map((part, index) => {
      // split 带捕获组：奇数下标是代码段，原样保留
      if (index % 2 === 1) return part
      return fn(part)
    })
    .join('')
}

function renderCitations(part) {
  return part.replace(
    CITE_RE,
    '<sup class="cite" data-cite="$1">[$1]</sup>',
  )
}

export function injectCitations(text) {
  return mapProse(text, renderCitations)
}

/** Markdown 源码预处理：引用角标 + 孤立波浪号转义（代码段不动）。 */
export function prepareMarkdownSource(text) {
  return mapProse(text, (part) =>
    renderCitations(part).replace(SINGLE_TILDE_RE, '\\~'),
  )
}
