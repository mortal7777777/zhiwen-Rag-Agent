<template>
  <div
    v-if="plain"
    class="markdown-body plain-stream"
    v-text="content"
  ></div>
  <div v-else class="markdown-body" v-html="rendered" @click="onBodyClick"></div>
</template>

<script setup>
import { onBeforeUnmount, shallowRef, watch } from 'vue'
import { marked } from 'marked'
import DOMPurify from 'dompurify'
import { ElMessage, ElMessageBox } from 'element-plus'
import { executeTool } from '../api'

const props = defineProps({
  content: { type: String, default: '' },
  plain: { type: Boolean, default: false },
})

marked.setOptions({ gfm: true, breaks: true })

function escapeHtml(text) {
  return String(text)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
}

// 自定义代码块渲染：直接生成"语言标签 + 复制 + 在终端执行（仅 shell 类语言）"的悬浮头部，
// 由 marked renderer 产出，避免 v-html 整体替换导致 DOM 注入丢失。
// 非 shell 代码块（python 示例/JSON/SQL 等）不显示执行按钮——它们不是
// 可直接执行的命令，点了只会让 bash 报错。
const SHELL_LANGS = new Set([
  'bash', 'sh', 'shell', 'zsh', 'cmd', 'bat', 'powershell', 'ps1',
  'console', 'terminal',
])
const codeRenderer = new marked.Renderer()
codeRenderer.code = (token) => {
  const lang = token.lang || 'text'
  const text = token.text || ''
  const runBtn = SHELL_LANGS.has(lang.toLowerCase())
    ? '<button type="button" class="code-btn exec" data-code-run>在终端执行</button>'
    : ''
  return (
    '<div class="code-block">' +
    '<div class="code-head">' +
    `<span class="code-lang">${escapeHtml(lang)}</span>` +
    '<button type="button" class="code-btn" data-code-copy>复制</button>' +
    runBtn +
    '</div>' +
    `<pre><code class="language-${escapeHtml(lang)}">${escapeHtml(text)}</code></pre>` +
    '</div>'
  )
}

// 外部链接新窗口打开
DOMPurify.addHook('afterSanitizeAttributes', (node) => {
  if (node.tagName === 'A') {
    node.setAttribute('target', '_blank')
    node.setAttribute('rel', 'noopener noreferrer')
  }
})

const rendered = shallowRef('')
let renderTimer = null
let lastRender = 0

function renderNow() {
  // 先把 [n] 引用标记转成可点击的角标（避开 Markdown 链接 [x](url) 语法）
  const withCites = (props.content || '').replace(
    /(?<!\\)\[(\d{1,3})\](?!\()/g,
    '<sup class="cite" data-cite="$1">[$1]</sup>',
  )
  const raw = marked.parse(withCites, { renderer: codeRenderer })
  rendered.value = DOMPurify.sanitize(raw, { USE_PROFILES: { html: true } })
}

// 事件委托：复制 / 在终端执行（v-html 内容无法直接绑定 Vue 事件）
async function onBodyClick(event) {
  const copyBtn = event.target.closest('[data-code-copy]')
  const runBtn = event.target.closest('[data-code-run]')
  if (copyBtn) {
    const block = copyBtn.closest('.code-block')
    const text = block ? block.querySelector('code').textContent : ''
    try {
      await navigator.clipboard.writeText(text)
      copyBtn.textContent = '已复制'
      setTimeout(() => (copyBtn.textContent = '复制'), 1200)
    } catch {
      ElMessage.error('复制失败')
    }
  } else if (runBtn) {
    const block = runBtn.closest('.code-block')
    const text = block ? block.querySelector('code').textContent : ''
    try {
      await ElMessageBox.confirm(
        `将在受控终端执行以下命令（需在设置中开启受控执行并配置白名单）：\n\n${text.slice(0, 500)}`,
        '终端执行确认',
        { type: 'warning', confirmButtonText: '执行', cancelButtonText: '取消' },
      )
    } catch {
      return
    }
    try {
      const result = await executeTool({ type: 'command', command: text })
      if (result.error) {
        ElMessage.error(result.summary || result.error)
      } else {
        ElMessageBox.alert(
          result.output || '（无输出）',
          `执行结果（exit=${result.exit_code ?? '-'}）`,
          { confirmButtonText: '知道了' },
        )
      }
    } catch (error) {
      ElMessage.error(error.response?.data?.detail || '执行失败')
    }
  }
}

// 流式输出时内容高频变化：用 60ms 节流渲染（至少每 60ms 出一次画面），
// 既避免每帧全量解析 Markdown 导致卡顿，也不会被"打字机连续更新"饿死
// （尾部防抖在连续更新时会被不断推迟，导致内容整段跳出来）。
watch(
  () => props.content,
  () => {
    if (props.plain) return
    const now = Date.now()
    const wait = 60 - (now - lastRender)
    if (wait <= 0) {
      if (renderTimer) {
        clearTimeout(renderTimer)
        renderTimer = null
      }
      renderNow()
      lastRender = Date.now()
    } else if (!renderTimer) {
      renderTimer = setTimeout(() => {
        renderTimer = null
        renderNow()
        lastRender = Date.now()
      }, wait)
    }
  },
  { immediate: true },
)

// 流式结束（plain=false）时内容可能没再变化，必须主动渲染一次，
// 否则正文会一直停留在空白（v-html 的 rendered 从未生成）
watch(
  () => props.plain,
  (plain) => {
    if (!plain) renderNow()
  },
)

onBeforeUnmount(() => {
  clearTimeout(renderTimer)
})
</script>

<style>
.markdown-body {
  font-size: 14px;
  line-height: 1.75;
  color: var(--text-1);
  word-break: break-word;
  min-width: 0;
  max-width: 100%;
}

/* 流式期间的轻量渲染：纯文本 + 保真换行，避免逐帧重解析 Markdown 导致闪烁/跳动 */
.markdown-body.plain-stream {
  white-space: pre-wrap;
  word-break: break-word;
  /* 与 markdown-body 完全一致，避免切换瞬间行高/字号变化造成跳动 */
  font-size: 14px;
  line-height: 1.75;
}

.markdown-body > :first-child {
  margin-top: 0;
}

.markdown-body > :last-child {
  margin-bottom: 0;
}

.markdown-body h1,
.markdown-body h2,
.markdown-body h3,
.markdown-body h4 {
  margin: 18px 0 10px;
  font-weight: 600;
  line-height: 1.4;
}

.markdown-body h1 { font-size: 20px; }
.markdown-body h2 { font-size: 17px; }
.markdown-body h3 { font-size: 15px; }

.markdown-body p {
  margin: 8px 0;
}

.markdown-body ul,
.markdown-body ol {
  padding-left: 1.6em;
  margin: 8px 0;
}

.markdown-body li {
  margin: 4px 0;
}

.markdown-body code {
  padding: 2px 6px;
  border-radius: 4px;
  font-size: 13px;
  font-family: Consolas, Monaco, 'Courier New', monospace;
  background: var(--bg-hover);
  color: #c7254e;
}

.code-block {
  position: relative;
  margin: 12px 0;
  border-radius: 10px;
  border: 1px solid #2a3140;
  overflow: hidden;
  background: #171c26;
  box-shadow: 0 2px 10px rgba(0, 0, 0, 0.18);
}

.code-head {
  position: absolute;
  top: 0;
  left: 0;
  right: 0;
  z-index: 2;
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 10px;
  background: #1f2532;
  border-bottom: 1px solid #2a3140;
}

.code-lang {
  flex: 1;
  font-size: 11px;
  color: #8a93a6;
  text-transform: uppercase;
  letter-spacing: 0.4px;
}

.code-btn {
  border: 1px solid #3a4356;
  background: transparent;
  color: #c4cad6;
  font-size: 11.5px;
  padding: 2px 10px;
  border-radius: 6px;
  cursor: pointer;
  transition: all 0.2s;
}

.code-btn:hover {
  background: #2a3140;
  color: #fff;
}

.code-btn.exec {
  color: #6cb2ff;
}

.code-btn.exec:hover {
  background: rgba(74, 148, 255, 0.15);
  color: #9cc8ff;
}

.code-block pre {
  margin: 0;
  padding: 38px 16px 14px;
  overflow: auto;
  background: transparent;
  color: #e8eaed;
  border-radius: 0;
}

.code-block pre code {
  padding: 0;
  background: transparent;
  color: inherit;
  font-size: 13px;
  line-height: 1.6;
}

.markdown-body blockquote {
  margin: 10px 0;
  padding: 8px 14px;
  border-left: 3px solid #1677ff;
  border-radius: 0 8px 8px 0;
  background: var(--bg-card-2);
  color: var(--text-2);
}

.markdown-body table {
  display: block;
  max-width: 100%;
  overflow-x: auto;
  margin: 12px 0;
  border-collapse: collapse;
  font-size: 13px;
}

.markdown-body th,
.markdown-body td {
  padding: 8px 12px;
  border: 1px solid var(--border-strong);
  text-align: left;
}

.markdown-body th {
  background: var(--bg-hover);
  font-weight: 600;
}

.markdown-body a {
  color: #1677ff;
  text-decoration: none;
}

.markdown-body a:hover {
  text-decoration: underline;
}

.markdown-body hr {
  border: none;
  border-top: 1px solid var(--border-strong);
  margin: 16px 0;
}

.markdown-body img {
  max-width: 100%;
  border-radius: 8px;
}

.markdown-body strong {
  font-weight: 600;
}

.markdown-body sup.cite {
  display: inline-block;
  margin: 0 2px;
  padding: 0 4px;
  border-radius: 4px;
  background: var(--bg-active);
  color: var(--primary);
  font-size: 11px;
  line-height: 1.4;
  cursor: pointer;
  user-select: none;
}

.markdown-body sup.cite:hover {
  background: var(--primary);
  color: #fff;
}
</style>
