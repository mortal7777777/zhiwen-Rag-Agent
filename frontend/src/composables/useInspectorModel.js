/**
 * 右侧会话面板的数据派生（纯函数，无 Vue / 无 IO，供组件与 Node 单测共用）。
 *
 * 数据源统一是消息里的 tool_trace：
 * - 实时消息由 useStreamHandlers 写入 status/duration_ms/artifacts/detail；
 * - 历史消息由 mapHistoryMessage 原样保留落库字段。
 */

import { diffLines } from '../utils/promptDiff.js'

const CHANGE_TOOLS = new Set(['write_file', 'edit_file', 'delete_file'])
const OUTPUT_TOOLS = new Set([
  ...CHANGE_TOOLS,
  'add_document',
])

function contentLen(value) {
  return typeof value === 'string' ? value.length : 0
}

/** 安全解析 detail（后端是 JSON 字符串；也兼容已是对象的测试数据）。 */
export function parseDetail(detail) {
  if (!detail) return null
  if (typeof detail === 'object') return detail
  try {
    const parsed = JSON.parse(String(detail))
    return parsed && typeof parsed === 'object' ? parsed : null
  } catch {
    return null
  }
}

/**
 * 单条 tool_trace → 运行视图。
 * 保留原始 entry 的响应式引用（`entry` 字段）：详情面板要在流式期间
 * 跟着 tool_result 回填实时更新，克隆会把数据冻结在打开那一刻。
 */
export function normalizeRunEntry(
  entry,
  { turn = 0, messageKey = null, messageIndex = null, index = 0 } = {},
) {
  if (!entry || !entry.name) return null
  const fallbackId = `${messageKey ?? 'trace'}-${index}`
  return {
    id: entry.id || fallbackId,
    key: `${messageKey ?? 'trace'}:${entry.id || index}`,
    name: entry.name,
    arguments: entry.arguments || {},
    summary: entry.summary || '',
    detail: entry.detail || '',
    durationMs: entry.duration_ms ?? null,
    // live 条目由 onToolStart/onToolResult 写 status；历史条目默认已完成
    status: entry.status || 'done',
    artifacts: Array.isArray(entry.artifacts) ? entry.artifacts : null,
    turn,
    messageIndex,
    messageKey,
    entry,
  }
}

/**
 * 把消息里的 tool_trace 摊平成运行列表。
 * 过滤 thinking/process 段；turn 从 1 开始按 assistant 消息计数。
 */
export function flattenToolRuns(messages = []) {
  const runs = []
  let turn = 0
  messages.forEach((msg, index) => {
    if (!msg || msg.role !== 'assistant') return
    turn += 1
    const trace = Array.isArray(msg.tool_trace) ? msg.tool_trace : []
    trace.forEach((entry, i) => {
      if (!entry || !entry.name) return
      if (entry.kind === 'thinking' || entry.kind === 'process') return
      const messageKey = msg.id || msg._localKey || index
      runs.push(
        normalizeRunEntry(entry, {
          turn,
          messageKey,
          messageIndex: index,
          index: i,
        }),
      )
    })
  })
  return runs
}

/** 子代理运行卡片：task/mode/summary/产物/命令/验证。 */
export function deriveSubagents(messages = []) {
  return flattenToolRuns(messages)
    .filter((run) => run.name === 'subagent')
    .map((run) => {
      const detail = parseDetail(run.detail)
      const args = run.arguments || {}
      const artifacts =
        run.artifacts && run.artifacts.length
          ? run.artifacts
          : Array.isArray(detail?.artifacts)
            ? detail.artifacts
            : []
      const cancelled = Boolean(run.entry?.cancelled || detail?.cancelled)
      return {
        key: run.key,
        id: run.id,
        // 取消子代理时用的稳定 id（与后端分支注册表、SSE 事件一致）
        subtaskId: String(
          args.subtask_id ||
            run.entry?.subtask_id ||
            run.entry?.arguments?.subtask_id ||
            run.id ||
            '',
        ),
        task: String(args.task || '').trim() || '（任务详情已截断）',
        mode: String(args.mode || run.entry?.mode || ''),
        profile: String(args.profile || run.entry?.profile || detail?.profile || ''),
        model: String(detail?.model || run.entry?.model || ''),
        summary: run.summary,
        status: cancelled ? 'cancelled' : run.status,
        cancelled,
        error: String(run.entry?.error || ''),
        durationMs: run.durationMs,
        rounds: Number(detail?.rounds ?? run.entry?.rounds) || 0,
        maxRounds: Number(run.entry?.maxRounds) || 0,
        currentTool: String(run.entry?.currentTool || ''),
        currentHint: String(run.entry?.currentHint || ''),
        tokens: run.entry?.tokens || detail?.tokens || null,
        findingsCount: Number(
          run.entry?.findingsCount ?? (detail?.findings || []).length,
        ) || 0,
        artifacts,
        commands: Array.isArray(detail?.commands) ? detail.commands : [],
        filesRead: Array.isArray(detail?.files_read) ? detail.files_read : [],
        verify: detail?.verify ?? null,
        structured: Boolean(detail?.structured),
        findings: Array.isArray(detail?.findings) ? detail.findings : [],
        openQuestions: Array.isArray(detail?.open_questions) ? detail.open_questions : [],
        turn: run.turn,
      }
    })
}

function artifactFromRun(run) {
  const args = run.arguments || {}
  if (run.name === 'write_file') {
    return {
      path: String(args.path || ''),
      action: 'created',
      chars: contentLen(args.content),
      content: typeof args.content === 'string' ? args.content : '',
    }
  }
  if (run.name === 'edit_file') {
    return {
      path: String(args.path || ''),
      action: 'modified',
      chars: contentLen(args.new_string),
    }
  }
  if (run.name === 'delete_file') {
    return { path: String(args.path || ''), action: 'deleted', chars: 0 }
  }
  if (run.name === 'add_document') {
    return {
      path: String(args.filename || ''),
      action: 'added_document',
      chars: contentLen(args.content),
      content: typeof args.content === 'string' ? args.content : '',
    }
  }
  return null
}

/**
 * 输出产物：按路径聚合。来源包括文件工具与子代理 artifacts。
 * 返回 [{ path, action, count, items: [...] }]，按最近一次出现的 turn 倒序。
 */
export function deriveArtifacts(messages = []) {
  const items = []
  for (const run of flattenToolRuns(messages)) {
    if (OUTPUT_TOOLS.has(run.name)) {
      const item = artifactFromRun(run)
      if (item && item.path) {
        items.push({ ...item, key: run.key, turn: run.turn, source: 'tool' })
      }
      continue
    }
    if (run.name === 'subagent') {
      const detail = parseDetail(run.detail)
      const artifacts =
        run.artifacts && run.artifacts.length
          ? run.artifacts
          : Array.isArray(detail?.artifacts)
            ? detail.artifacts
            : []
      for (const [index, artifact] of artifacts.entries()) {
        if (!artifact || !artifact.path) continue
        items.push({
          path: String(artifact.path),
          action:
            artifact.action === 'modified' ? 'modified' : 'created',
          chars: Number(artifact.bytes || 0),
          key: `${run.key}:artifact:${index}`,
          turn: run.turn,
          source: 'subagent',
        })
      }
    }
  }

  const groups = new Map()
  for (const item of items) {
    if (!groups.has(item.path)) {
      groups.set(item.path, { path: item.path, action: item.action, count: 0, items: [] })
    }
    const group = groups.get(item.path)
    group.items.push(item)
    group.action = item.action
    group.count += 1
  }
  return [...groups.values()].sort(
    (a, b) => (b.items[b.items.length - 1]?.turn || 0) - (a.items[a.items.length - 1]?.turn || 0),
  )
}

/** 文件变更：write/edit/delete 的逐次操作，供 Changes 页签展示 diff。 */
export function deriveChanges(messages = []) {
  const out = []
  for (const run of flattenToolRuns(messages)) {
    const args = run.arguments || {}
    if (run.name === 'write_file') {
      out.push({
        key: run.key,
        path: String(args.path || ''),
        action: 'write',
        content: typeof args.content === 'string' ? args.content : '',
        chars: contentLen(args.content),
        turn: run.turn,
        status: run.status,
      })
    } else if (run.name === 'edit_file') {
      out.push({
        key: run.key,
        path: String(args.path || ''),
        action: 'edit',
        oldText: typeof args.old_string === 'string' ? args.old_string : '',
        newText: typeof args.new_string === 'string' ? args.new_string : '',
        replaceAll: !!args.replace_all,
        chars: contentLen(args.new_string),
        turn: run.turn,
        status: run.status,
      })
    } else if (run.name === 'delete_file') {
      out.push({
        key: run.key,
        path: String(args.path || ''),
        action: 'delete',
        turn: run.turn,
        status: run.status,
      })
    }
  }
  return out
}

/**
 * 行级 diff（复用 promptDiff 的 LCS，但先裁掉公共前后缀并限制输入规模，
 * 避免整文件替换时 O(n²) 卡死）。返回 { lines, truncated, skippedPrefix, skippedSuffix }。
 */
export function buildChangeDiff(
  oldText,
  newText,
  { maxLines = 80, maxInputLines = 400 } = {},
) {
  const a = String(oldText ?? '').split('\n')
  const b = String(newText ?? '').split('\n')
  let start = 0
  while (start < a.length && start < b.length && a[start] === b[start]) start += 1
  let endA = a.length - 1
  let endB = b.length - 1
  while (endA >= start && endB >= start && a[endA] === b[endB]) {
    endA -= 1
    endB -= 1
  }
  const midA = a.slice(start, endA + 1)
  const midB = b.slice(start, endB + 1)
  const skippedSuffix = a.length - 1 - endA

  if (!midA.length && !midB.length) {
    return { lines: [], truncated: false, skippedPrefix: start, skippedSuffix }
  }

  let truncated = false
  let lines
  if (midA.length > maxInputLines || midB.length > maxInputLines) {
    lines = [
      ...midA
        .slice(0, maxInputLines)
        .map((text) => ({ type: 'del', sign: '−', text })),
      ...midB
        .slice(0, maxInputLines)
        .map((text) => ({ type: 'add', sign: '+', text })),
    ]
    truncated = true
  } else {
    lines = diffLines(midA.join('\n'), midB.join('\n'))
  }
  if (lines.length > maxLines) {
    lines = lines.slice(0, maxLines)
    truncated = true
  }
  return { lines, truncated, skippedPrefix: start, skippedSuffix }
}
