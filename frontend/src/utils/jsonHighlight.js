/**
 * JSON 美化与分段着色（纯函数，无 Vue / 无 DOM，供 Node 检查与详情面板共用）。
 * 着色只产出 `{ text, cls }` 分段，由模板逐段渲染——不拼 HTML、不用 v-html。
 */

/** 能解析就 pretty 成 2 空格缩进；否则原样返回。 */
export function formatJson(value) {
  if (value == null) return ''
  const text = typeof value === 'string' ? value : safeStringify(value)
  const trimmed = String(text).trim()
  if (!trimmed) return String(text)
  try {
    return JSON.stringify(JSON.parse(trimmed), null, 2)
  } catch {
    return String(text)
  }
}

function safeStringify(value) {
  try {
    return JSON.stringify(value, null, 2)
  } catch {
    return String(value)
  }
}

const TOKEN_RE = /("(?:\\.|[^"\\])*")(\s*:)?|\b(true|false|null)\b|(-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)/g

/** 把 JSON 文本拆成 key / string / number / literal / plain 片段。 */
export function tokenizeJson(text) {
  const source = String(text ?? '')
  const out = []
  let last = 0
  for (const match of source.matchAll(TOKEN_RE)) {
    if (match.index > last) {
      out.push({ text: source.slice(last, match.index), cls: 'plain' })
    }
    if (match[1] != null) {
      if (match[2] != null) {
        out.push({ text: match[1], cls: 'key' })
        const rest = match[0].slice(match[1].length)
        if (rest) out.push({ text: rest, cls: 'plain' })
      } else {
        out.push({ text: match[1], cls: 'string' })
      }
    } else if (match[3] != null) {
      out.push({ text: match[3], cls: 'literal' })
    } else {
      out.push({ text: match[4], cls: 'number' })
    }
    last = match.index + match[0].length
  }
  if (last < source.length) out.push({ text: source.slice(last), cls: 'plain' })
  return out
}
