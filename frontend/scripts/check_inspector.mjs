/**
 * 右侧会话面板数据派生检查（Node，无浏览器/后端）。
 * 锁定 useInspectorModel 的过滤、分组、状态与 diff 边界。
 */
import {
  buildChangeDiff,
  deriveArtifacts,
  deriveChanges,
  deriveSubagents,
  flattenToolRuns,
  normalizeRunEntry,
  parseDetail,
} from '../src/composables/useInspectorModel.js'
import { formatJson, tokenizeJson } from '../src/utils/jsonHighlight.js'

const failures = []
function check(name, cond, extra = '') {
  if (cond) console.log(`  ok   ${name}`)
  else {
    failures.push(name)
    console.log(`  FAIL ${name}${extra ? ` — ${extra}` : ''}`)
  }
}

console.log('inspector 数据派生检查')

const messages = [
  {
    id: 1,
    role: 'user',
    content: '帮我写文件',
  },
  {
    id: 2,
    role: 'assistant',
    content: '好',
    tool_trace: [
      { kind: 'thinking', text: '想想', done: true },
      { kind: 'process', text: '过渡', done: true },
      {
        id: 't1',
        name: 'write_file',
        arguments: { path: 'out/a.txt', content: 'hello\nworld' },
        summary: '已写入',
        duration_ms: 12,
      },
      {
        id: 't2',
        name: 'edit_file',
        arguments: {
          path: 'out/a.txt',
          old_string: 'hello',
          new_string: 'hi',
          replace_all: false,
        },
        summary: '已编辑',
        duration_ms: 8,
      },
      {
        id: 'subtask_c1_0',
        name: 'subagent',
        arguments: {
          task: '调研 A',
          mode: 'research',
          subtask_id: 'subtask_c1_0',
        },
        summary: 'A 的结论',
        duration_ms: 30,
        currentTool: 'grep_search',
        maxRounds: 3,
        tokens: { total_tokens: 123 },
        detail: JSON.stringify({
          artifacts: [{ path: 'out/b.md', action: 'created', bytes: 42 }],
          commands: [{ command: 'ls', exit_code: 0 }],
          files_read: ['ref.md'],
          verify: [{ path: 'out/b.md', exit_code: 0 }],
          rounds: 2,
        }),
      },
    ],
  },
]

{
  const runs = flattenToolRuns(messages)
  check('flatten：跳过 thinking/process', runs.length === 3)
  check('flatten：turn 按 assistant 计数', runs.every((r) => r.turn === 1))
  check('flatten：历史条目默认 done', runs.every((r) => r.status === 'done'))
  check('flatten：保留原始 entry 引用（详情实时更新）', runs[0].entry === messages[1].tool_trace[2])
}

{
  const liveEntry = { id: 'l2', name: 'bash', arguments: { command: 'ls' }, status: 'running' }
  const run = normalizeRunEntry(liveEntry, { turn: 3, messageKey: 'm1', index: 2 })
  check(
    'normalize：字段映射与 key',
    run.name === 'bash' && run.turn === 3 && run.key === 'm1:l2' && run.status === 'running',
  )
  check('normalize：保留 entry 引用', run.entry === liveEntry)
  check('normalize：空条目返回 null', normalizeRunEntry(null) === null)
}

{
  check('formatJson：合法 JSON 美化', formatJson('{"a":1}') === '{\n  "a": 1\n}')
  check('formatJson：普通文本原样', formatJson('hello') === 'hello')
  check('formatJson：空值返回空串', formatJson(null) === '')
  const tokens = tokenizeJson('{"a":1,"ok":true,"s":"x"}')
  check(
    'tokenizeJson：key/数字/字面量/字符串分类',
    tokens.some((t) => t.cls === 'key' && t.text === '"a"') &&
      tokens.some((t) => t.cls === 'number' && t.text === '1') &&
      tokens.some((t) => t.cls === 'literal' && t.text === 'true') &&
      tokens.some((t) => t.cls === 'string' && t.text === '"x"'),
  )
  check('tokenizeJson：片段拼回原文', tokens.map((t) => t.text).join('') === '{"a":1,"ok":true,"s":"x"}')
}

{
  const live = [
    {
      id: 9,
      role: 'assistant',
      tool_trace: [
        { id: 'l1', name: 'bash', arguments: { command: 'x' }, status: 'running' },
      ],
    },
  ]
  const runs = flattenToolRuns(live)
  check('flatten：live running 状态保留', runs[0].status === 'running')
}

{
  const subs = deriveSubagents(messages)
  check('subagents：筛出子代理卡片', subs.length === 1 && subs[0].task === '调研 A')
  check('subagents：mode 与摘要', subs[0].mode === 'research' && subs[0].summary === 'A 的结论')
  check('subagents：耗时与执行轮数', subs[0].durationMs === 30 && subs[0].rounds === 2)
  check('subagents：detail 产物解析', subs[0].artifacts[0].path === 'out/b.md')
  check('subagents：命令/读取/验证计数', subs[0].commands.length === 1 && subs[0].filesRead[0] === 'ref.md' && !!subs[0].verify)
  check(
    'subagents：取消用 subtask_id/当前工具/轮数上限/tokens',
    subs[0].subtaskId === 'subtask_c1_0' &&
      subs[0].currentTool === 'grep_search' &&
      subs[0].maxRounds === 3 &&
      subs[0].tokens.total_tokens === 123,
  )
}

{
  const cancelled = [
    {
      id: 3,
      role: 'assistant',
      tool_trace: [
        {
          id: 'subtask_c2_0',
          name: 'subagent',
          arguments: { task: '调研 B', subtask_id: 'subtask_c2_0' },
          status: 'running',
          cancelled: true,
          summary: '已取消',
        },
        {
          id: 'subtask_c2_1',
          name: 'subagent',
          arguments: { task: '调研 C', subtask_id: 'subtask_c2_1' },
          summary: '已取消',
          detail: JSON.stringify({ cancelled: true, rounds: 1 }),
        },
      ],
    },
  ]
  const subs = deriveSubagents(cancelled)
  check(
    'subagents：取消终态优先于 running',
    subs[0].status === 'cancelled' && subs[0].cancelled === true,
  )
  check(
    'subagents：历史记录从 detail.cancelled 识别取消',
    subs[1].status === 'cancelled' && subs[1].cancelled === true,
  )
}

{
  const groups = deriveArtifacts(messages)
  const paths = groups.map((g) => g.path)
  check('artifacts：按路径聚合（含子代理产物）', paths.includes('out/a.txt') && paths.includes('out/b.md'))
  const file = groups.find((g) => g.path === 'out/a.txt')
  check('artifacts：同路径多次操作计数', file && file.count === 2 && file.action === 'modified')
  const sub = groups.find((g) => g.path === 'out/b.md')
  check('artifacts：子代理产物标记来源', sub && sub.items[0].source === 'subagent')
}

{
  const changes = deriveChanges(messages)
  check('changes：write + edit', changes.length === 2)
  check('changes：edit 保留新旧文本', changes[1].action === 'edit' && changes[1].oldText === 'hello' && changes[1].newText === 'hi')
}

{
  const same = buildChangeDiff('a\nb\nc', 'a\nb\nc')
  check('diff：无变化返回空行', same.lines.length === 0 && same.skippedPrefix === 3)
  const edit = buildChangeDiff('a\nb\nc', 'a\nB\nc')
  check('diff：中间一行替换为删+增', edit.lines.some((l) => l.type === 'del' && l.text === 'b') && edit.lines.some((l) => l.type === 'add' && l.text === 'B'))
  check('diff：跳过公共前后缀', edit.skippedPrefix === 1 && edit.skippedSuffix === 1)
  const big = Array.from({ length: 600 }, (_, i) => `old-line-${i}`).join('\n')
  const bigOther = Array.from({ length: 600 }, (_, i) => `new-line-${i}`).join('\n')
  const capped = buildChangeDiff(big, bigOther, { maxLines: 10, maxInputLines: 400 })
  check('diff：大输入截断不卡死', capped.truncated === true && capped.lines.length <= 10)
}

check('parseDetail：坏 JSON 返回 null', parseDetail('{oops') === null)

if (failures.length) {
  console.log(`\n未通过 ${failures.length} 项：${failures.join('、')}`)
  process.exit(1)
}
console.log('\n全部通过：inspector 数据派生与 diff 边界符合预期。')
