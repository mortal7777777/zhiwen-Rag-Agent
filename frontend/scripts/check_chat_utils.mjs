/**
 * ChatView 纯函数检查（无浏览器/后端，Node 直接跑）。
 *
 * 这些函数从 4300 行视图里抽出（useChatUtils），此前完全没有单测；
 * 抽出的第一原则是行为逐字节不变，这里锁住映射/截断/兜底口径。
 */
import { marked } from 'marked'
import { ACTIVITY_COPY, pickActivityLabel } from '../src/utils/activityCopy.js'
import { injectCitations, prepareMarkdownSource } from '../src/utils/citations.js'
import {
  categoryLabel,
  categoryTagType,
  formatArgs,
  formatCheckpointTime,
  formatCount,
  formatFileSize,
  formatMemoryDate,
  isFailed,
  mapHistoryMessage,
  sortSources,
  toolName,
} from '../src/composables/useChatUtils.js'

const failures = []
function check(name, cond, extra = '') {
  if (cond) console.log(`  ok   ${name}`)
  else {
    failures.push(name)
    console.log(`  FAIL ${name}${extra ? ` — ${extra}` : ''}`)
  }
}

console.log('useChatUtils 纯函数检查')

check('toolName：核心工具映射', toolName('knowledge_base_search') === '知识库检索')
check('toolName：新增提问工具', toolName('ask_user_question') === '向你提问')
check('toolName：MCP 前缀展开', toolName('mcp_browser_click') === 'MCP · browser click')
check('toolName：未知工具原样返回', toolName('custom_tool') === 'custom_tool')

check('formatArgs：空对象为空串', formatArgs({}) === '' && formatArgs(null) === '')
{
  const text = formatArgs({ path: 'a.txt', nested: { a: 1 }, skip: null })
  check('formatArgs：跳过 null 并序列化对象', text.includes('path: a.txt') && text.includes('nested: {"a":1}') && !text.includes('skip'))
  const long = formatArgs({ content: 'x'.repeat(200) })
  check('formatArgs：长文本截断并标注总长', long.includes('…（共 200 字符）'))
}

check('formatFileSize：B/KB/MB', formatFileSize(900) === '900B' && formatFileSize(2048) === '2KB' && formatFileSize(2.5 * 1024 * 1024) === '2.5MB')
check('formatCount：万级缩写', formatCount(9999) === '9999' && formatCount(12345) === '1.2万')

{
  const msg = mapHistoryMessage({
    id: 7,
    role: 'assistant',
    content: '答案',
    tool_trace: [{ name: 'bash', summary: 'ok' }],
    sources: [{ index: 1 }],
  })
  check('mapHistoryMessage：保留 id/role/content', msg.id === 7 && msg.role === 'assistant' && msg.content === '答案')
  check('mapHistoryMessage：工具轨迹默认折叠', msg.tool_trace[0]._open === false)
  check('mapHistoryMessage：补齐 UI 字段', msg.permissions.length === 0 && msg._streaming === false && msg._sourcesOpen === false)
}

check('isFailed：仅请求失败前缀', isFailed('请求失败：x') === true && isFailed('正常回答') === false)
check(
  'formatCheckpointTime：空值与非法值兜底',
  formatCheckpointTime(null) === '—' && formatCheckpointTime('not-a-date') === 'Invalid Date',
)
check('formatMemoryDate：空值兜底', formatMemoryDate(null) === '未知')
check('记忆分类映射', categoryLabel('profile') === '用户画像' && categoryLabel('unknown') === 'unknown')
check('记忆分类标签色', categoryTagType('decision') === 'danger' && categoryTagType('unknown') === 'info')

console.log('运行状态文案检查')
{
  check(
    'thinking 相位按持续时间轮换',
    pickActivityLabel('thinking', '', 0) === ACTIVITY_COPY.thinking[0] &&
      pickActivityLabel('thinking', '', 8) !== ACTIVITY_COPY.thinking[0],
  )
  check('tool 相位显示工具明细', pickActivityLabel('tool', '知识库检索…', 0) === '知识库检索…')
  check(
    'finalizing 才会出现「快好了」',
    ACTIVITY_COPY.finalizing.includes('快好了…') &&
      pickActivityLabel('finalizing', '', 8).includes('快好了') &&
      !ACTIVITY_COPY.thinking.includes('快好了…'),
  )
  check(
    'reasoning 不再拼接明细（重复文案根因）',
    pickActivityLabel('reasoning', '思考中…', 0) === ACTIVITY_COPY.reasoning[0],
  )
  check(
    '每个可轮换相位至少两条文案',
    ['prepare', 'thinking', 'reasoning', 'vision', 'answering', 'finalizing', 'waiting', 'subagent', 'resume', 'retry'].every(
      (phase) => Array.isArray(ACTIVITY_COPY[phase]) && ACTIVITY_COPY[phase].length >= 2,
    ),
  )
  check(
    '未知相位回退到明细文本',
    pickActivityLabel('notice', 'Hook 拦截：x', 0) === 'Hook 拦截：x',
  )
  check(
    'retry 相位有重试文案',
    pickActivityLabel('retry', '', 0).includes('重试'),
  )
  check(
    'plan 相位显示当前步骤',
    pickActivityLabel('plan', '第 2/5 步 · 核对引用', 0) === '第 2/5 步 · 核对引用',
  )
}

console.log('引用角标注入检查')
{
  const prose = injectCitations('结论见 [1] 与 [2]。')
  check('正文 [n] 转成角标', prose.includes('data-cite="1"') && prose.includes('data-cite="2"'))

  const link = injectCitations('参见 [1](https://example.com)')
  check('Markdown 链接不误伤', link === '参见 [1](https://example.com)')

  const fenced = injectCitations('```js\nconst x = arr[1]\n```')
  check('围栏代码里的 [n] 不变', fenced === '```js\nconst x = arr[1]\n```')

  const inline = injectCitations('用 `arr[0]` 取值，来源见 [3]')
  check(
    '行内代码不变、正文角标生效',
    inline.includes('`arr[0]`') && inline.includes('data-cite="3"'),
  )

  check('转义 \\[1\\] 不替换', injectCitations('\\[1\\]') === '\\[1\\]')
}

console.log('Markdown 波浪号渲染检查')
{
  const rangeHtml = marked.parse(
    prepareMarkdownSource('一轮大约 40~60 分钟，一晚能跑 8~12 轮'),
  )
  check(
    '单波浪号区间不生成删除线',
    !rangeHtml.includes('<del>') && rangeHtml.includes('40~60'),
  )
  const strikeHtml = marked.parse(prepareMarkdownSource('~~这段应删除~~'))
  check('双波浪号删除线保留', strikeHtml.includes('<del>这段应删除</del>'))
  const codeHtml = marked.parse(prepareMarkdownSource('`40~60` 与 40~60'))
  check(
    '代码内波浪号不受影响、正文区间正常',
    codeHtml.includes('<code>40~60</code>') && !codeHtml.includes('<del>'),
  )
}

console.log('引用来源排序检查')
{
  const out = sortSources([
    { index: 6, type: 'web' },
    { index: 7, type: 'web' },
    { index: 1, content: 'kb' },
    { content: '无编号' },
  ])
  check(
    'sortSources：按 index 升序、缺编号置后',
    out.map((s) => s.index ?? '-').join(',') === '1,6,7,-',
  )
  const mapped = mapHistoryMessage({
    id: 1,
    role: 'assistant',
    content: 'x',
    sources: [{ index: 3 }, { index: 1 }],
  })
  check(
    'mapHistoryMessage：历史来源同样排序',
    mapped.sources.map((s) => s.index).join(',') === '1,3',
  )
}

if (failures.length) {
  console.error(`\n失败 ${failures.length} 项：${failures.join('、')}`)
  process.exit(1)
}
console.log('\n全部通过：ChatView 纯函数映射符合预期。')
