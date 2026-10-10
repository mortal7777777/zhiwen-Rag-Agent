/**
 * API 层契约 + SSE 传输层行为断言（无浏览器、无后端，Node 直接跑）。
 *
 * 用法：npm run check:api
 * 覆盖：
 *   1. barrel（api/index.js）导出面快照 —— 拆分后不能悄悄丢接口；
 *   2. 各域模块之间无重名导出（export * 冲突会静默丢名字）；
 *   3. SSE 帧解析：跨 chunk 半帧、多帧同包、空行分隔；
 *   4. error 事件 → 抛错；非 2xx / 无 body → 抛错并带后端 detail；
 *   5. streamRequest 把 payload 正确发成 JSON POST。
 */
import * as api from '../src/api/index.js'
import * as agentApi from '../src/api/agent.js'
import * as documentsApi from '../src/api/documents.js'
import * as sessionsApi from '../src/api/sessions.js'
import * as settingsApi from '../src/api/settings.js'
import { consumeAgentStream, streamRequest } from '../src/api/sse.js'

const failures = []
function check(name, cond, extra = '') {
  if (cond) {
    console.log(`  ok   ${name}`)
  } else {
    failures.push(name)
    console.log(`  FAIL ${name}${extra ? ` — ${extra}` : ''}`)
  }
}

// 2026-10-09 拆分前的导出面快照（只能增，不能减）
const EXPECTED_EXPORTS = [
  'abandonRun', 'addMemory', 'archiveDocumentVersion', 'browseDirs',
  'compactConversation', 'connectMcpServers', 'consolidateMemories',
  'continueAgentRun', 'createConversation', 'createTemplate', 'deleteAgentSkill',
  'deleteConversation', 'deleteDocument', 'deleteDocumentVersion', 'deleteMemory',
  'deleteTemplate', 'executeTool', 'exportProjectMemory', 'exportProviders',
  'getAgentContext',
  'getAgentSkillContent', 'getConversationMessages', 'getConversationTimeline',
  'getDocumentFileUrl', 'getDocumentMeta', 'getIndexStatus', 'getPendingApproval',
  'getProjectMemory', 'getProviderPresets', 'getRunStats', 'getRunTrace', 'getSettings',
  'getSkillCommands',
  'getSuggestions', 'getTodos', 'importProviders', 'listChatModels', 'listConversations',
  'listDocuments',
  'listDocumentVersions', 'listMcpServers', 'listMemories', 'listProviderModels',
  'listRuns', 'listSkills',
  'listTemplates', 'previewDocument', 'rebuildIndex', 'renameConversation',
  'renameDocument', 'resetSkillPrefs', 'resumeAgentChat', 'restoreDocumentVersion',
  'rewindConversation', 'rollbackConversation', 'runSkillCommand', 'saveDocumentMeta',
  'saveMcpServers', 'saveSettings', 'saveSkillPrefs', 'saveTodos', 'searchSkills',
  'setDataDir', 'shareAgentSkill', 'speedtestProviders', 'streamAgentChat',
  'switchChatProvider',
  'testMcpServer', 'testProvider',
  'unshareAgentSkill', 'updateMemory', 'updateTemplate', 'uploadDocuments',
]

console.log('API 层契约 + SSE 行为检查')

// 1) 导出面快照
{
  const missing = EXPECTED_EXPORTS.filter((n) => typeof api[n] === 'undefined')
  check(`barrel 导出面完整（${EXPECTED_EXPORTS.length} 个）`, missing.length === 0, `缺少 ${missing.join(', ')}`)
}

// 2) 域模块之间不得重名（export * 会静默丢名字）
{
  const seen = new Map()
  const dupes = []
  for (const [label, mod] of [
    ['agent', agentApi],
    ['documents', documentsApi],
    ['sessions', sessionsApi],
    ['settings', settingsApi],
  ]) {
    for (const name of Object.keys(mod)) {
      if (seen.has(name)) dupes.push(`${name}（${seen.get(name)} 与 ${label}）`)
      else seen.set(name, label)
    }
  }
  check('域模块无重名导出', dupes.length === 0, dupes.join('; '))
}

function sseBody(frames, chunkSize = 4096) {
  const encoder = new TextEncoder()
  const text = frames.map((f) => `data: ${JSON.stringify(f)}\n\n`).join('')
  const bytes = encoder.encode(text)
  return new ReadableStream({
    start(controller) {
      for (let i = 0; i < bytes.length; i += chunkSize) {
        controller.enqueue(bytes.slice(i, i + chunkSize))
      }
      controller.close()
    },
  })
}

// 3) 帧解析：故意按 7 字节切片（半个 JSON 也会被切开）
{
  const seen = []
  const handlers = {
    onSession: (d) => seen.push(['session', d.conversation_id]),
    onStreamDelta: (t) => seen.push(['delta', t]),
    onStreamEnd: (d) => seen.push(['end', d.keep]),
    onToken: (t) => seen.push(['token', t]),
    onPlan: (d) => seen.push(['plan', d.steps.join('/')]),
    onDone: () => seen.push(['done']),
  }
  const response = { body: sseBody([
    { event: 'session', data: { conversation_id: 9 } },
    { event: 'stream_delta', data: '你' },
    { event: 'plan', data: { steps: ['A', 'B'] } },
    { event: 'stream_end', data: { keep: true } },
    { event: 'token', data: '你好' },
    { event: 'done', data: { ok: true } },
  ], 7) }
  await consumeAgentStream(response, handlers)
  check(
    '半帧/多帧解析与事件顺序',
    JSON.stringify(seen) === JSON.stringify([
      ['session', 9], ['delta', '你'], ['plan', 'A/B'], ['end', true], ['token', '你好'], ['done'],
    ]),
    JSON.stringify(seen),
  )
}

// 4) error 事件 → 抛错（带后端 message）
{
  let threw = ''
  try {
    await consumeAgentStream(
      { body: sseBody([{ event: 'error', data: { message: '模型调用失败' } }]) },
      {},
    )
  } catch (err) {
    threw = err.message
  }
  check('error 事件抛错', threw === '模型调用失败', threw)
}

// 5) streamRequest：非 2xx → 抛错并带 detail
{
  const originalFetch = globalThis.fetch
  globalThis.fetch = async () => ({
    ok: false,
    status: 409,
    body: null,
    json: async () => ({ detail: '该会话有待确认的操作' }),
  })
  let threw = ''
  try {
    await streamRequest('/api/agent/stream', {}, {})
  } catch (err) {
    threw = err.message
  }
  check('非 2xx 抛后端 detail', threw === '该会话有待确认的操作', threw)

  // 6) streamRequest：payload 正确序列化为 JSON POST
  let captured = null
  globalThis.fetch = async (url, init) => {
    captured = { url, init }
    return { ok: true, status: 200, body: sseBody([{ event: 'done', data: {} }]) }
  }
  let done = false
  await streamRequest('/api/agent/resume', { conversation_id: 3 }, { onDone: () => (done = true) })
  check('请求方法与头正确', captured.init.method === 'POST' && captured.init.headers['Content-Type'] === 'application/json')
  check('payload 序列化正确', captured.init.body === JSON.stringify({ conversation_id: 3 }))
  check('成功后分派事件', done === true)
  globalThis.fetch = originalFetch
}

if (failures.length) {
  console.error(`\n失败 ${failures.length} 项：${failures.join('、')}`)
  process.exit(1)
}
console.log('\n全部通过：API 导出面完整、SSE 解析与错误处理符合预期。')
