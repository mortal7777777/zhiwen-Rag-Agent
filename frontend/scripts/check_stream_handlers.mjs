/**
 * useAgentStream 的事件序列断言（无浏览器、无后端，Node 直接跑）。
 *
 * 用法：npm run check:stream   （CI 的 frontend job 与 pre-commit 都会跑）
 *
 * 覆盖：session / stream_delta / stream_end / token / tool_start+tool_result /
 *      reasoning_delta / plan+plan_progress / todos /
 *      run_suspended(ops|plan)/done 共 10 类事件的"状态迁移"是否正确——
 *      这正是拆分前埋在 4000 行视图里、无法单测的那部分逻辑。
 */
import { ref } from 'vue'
import { createStreamHandlers } from '../src/composables/useAgentStream.js'

// onReasoningDelta 内部走 nextTick + querySelectorAll 做思考链自动滚动；Node 下给个空实现
globalThis.document = { querySelectorAll: () => [] }

const failures = []
function check(name, cond, extra = '') {
  if (cond) {
    console.log(`  ok   ${name}`)
  } else {
    failures.push(name)
    console.log(`  FAIL ${name}${extra ? ` — ${extra}` : ''}`)
  }
}

function makeHarness() {
  const calls = []
  const live = {
    skipNextToken: false,
    streamedLen: 0,
    suspended: null,
  }
  const state = {
    activeId: ref(null),
    currentTitle: ref(''),
    currentTool: ref(''),
    pendingText: ref(''),
    streamMsg: {
      tool_trace: [],
      sources: [],
      plan: [],
      planTotal: 0,
      planDone: 0,
      planCurrent: null,
      content: '',
      _streaming: true,
    },
  }
  const actions = {}
  for (const name of [
    'closeThinkingSegment',
    'dropStreamedRound',
    'flushTypewriter',
    'focusPermissionCard',
    'focusPlanCard',
    'pushSuspendedCards',
    'scrollToBottom',
    'startTypewriter',
    'upsertSession',
  ]) {
    actions[name] = (...args) => calls.push([name, ...args])
  }
  actions.toolName = (raw) => `工具:${raw}`

  const handlers = createStreamHandlers({
    state,
    actions,
    live: {
      getSkipNextToken: () => live.skipNextToken,
      setSkipNextToken: (v) => {
        live.skipNextToken = v
      },
      getStreamedLen: () => live.streamedLen,
      setStreamedLen: (v) => {
        live.streamedLen = v
      },
      setSuspended: (v) => {
        live.suspended = v
      },
    },
  })
  const called = (name) => calls.some((c) => c[0] === name)
  return { handlers, state, live, calls, called }
}

console.log('useAgentStream 事件序列检查')

// 1) session
{
  const h = makeHarness()
  h.handlers.onSession({ conversation_id: 42, title: '新标题' })
  check('onSession 设置 activeId', h.state.activeId.value === 42)
  check('onSession 设置标题', h.state.currentTitle.value === '新标题')
  check('onSession 通知会话列表', h.called('upsertSession'))
}

// 2) 正文增量：pendingText/streamedLen 累加 + 启动打字机
{
  const h = makeHarness()
  h.handlers.onStreamDelta('你好')
  h.handlers.onStreamDelta('世界')
  check('stream_delta 累加正文', h.state.pendingText.value === '你好世界')
  check('stream_delta 累加计数', h.live.streamedLen === 4)
  check('stream_delta 启动打字机', h.called('startTypewriter'))
}

// 3) stream_end{keep:true} 后的全文 token 必须被跳过（只跳一次）
{
  const h = makeHarness()
  h.handlers.onStreamDelta('答案')
  h.handlers.onStreamEnd({ keep: true })
  h.handlers.onToken('答案')
  check('keep=true 后跳过全文 token', h.state.pendingText.value === '答案')
  check('跳过标记已复位', h.live.skipNextToken === false)
  h.handlers.onToken('补充')
  check('后续 token 正常追加', h.state.pendingText.value === '答案补充')
}

// 4) stream_end{keep:false} 撤回工具轮过渡文本
{
  const h = makeHarness()
  h.handlers.onStreamDelta('我将调用工具')
  h.handlers.onStreamEnd({ keep: false })
  check('keep=false 触发挥手', h.called('dropStreamedRound'))
}

// 5) 工具轨迹：tool_start 建条目、tool_result 回填摘要与来源
{
  const h = makeHarness()
  h.handlers.onToolStart({ name: 'knowledge_base_search', arguments: { query: 'x' } })
  check('tool_start 写入轨迹', h.state.streamMsg.tool_trace.length === 1)
  check('tool_start 更新状态条', h.state.currentTool.value === '工具:knowledge_base_search…')
  h.handlers.onToolResult({
    name: 'knowledge_base_search',
    summary: '检索到 2 条',
    detail: '详情',
    sources: [{ index: 1 }],
  })
  const entry = h.state.streamMsg.tool_trace[0]
  check('tool_result 回填摘要', entry.summary === '检索到 2 条')
  check('tool_result 回填详情', entry.detail === '详情')
  check('tool_result 追加来源', h.state.streamMsg.sources.length === 1)
  check('tool_result 后状态条连续', h.state.currentTool.value === '继续思考…')
}

// 6) 思考链增量按段聚合
{
  const h = makeHarness()
  h.handlers.onReasoningDelta('第一段')
  h.handlers.onReasoningDelta('续写')
  const seg = h.state.streamMsg.tool_trace[0]
  check('reasoning 聚合成 thinking 段', seg.kind === 'thinking' && seg.text === '第一段续写')
}

// 7) 计划 / 任务清单 / 进度
{
  const h = makeHarness()
  h.handlers.onPlan({ steps: ['A', 'B'] })
  h.handlers.onPlanProgress({ done: 1, total: 2, current: 'B' })
  h.handlers.onTodos({ todos: [{ text: 'A', done: true }] })
  check('plan 写入步骤', h.state.streamMsg.plan.length === 2)
  check('plan_progress 写入进度', h.state.streamMsg.planDone === 1 && h.state.streamMsg.planCurrent === 'B')
  check('todos 写入清单', h.state.streamMsg.todos.length === 1)
}

// 8) 挂起（只有敏感操作）：渲染审批卡并记录挂起态
{
  const h = makeHarness()
  h.handlers.onSession({ conversation_id: 7 })
  h.handlers.onSuspended({ conversation_id: 7, ops: [{ call_id: 'c1' }] })
  check('挂起写入组件状态', h.live.suspended && h.live.suspended.conversationId === 7)
  check('挂起提交前标记未提交', h.live.suspended.submitted === false)
  check('挂起渲染审批卡', h.called('pushSuspendedCards'))
  check('无计划时聚焦审批卡', h.called('focusPermissionCard'))
}

// 9) 挂起（带计划）：生成计划确认卡
{
  const h = makeHarness()
  h.handlers.onSuspended({ conversation_id: 7, ops: [], plan: { steps: ['写 X', '跑 Y'], summary: '计划' } })
  const approval = h.state.streamMsg.planApproval
  check('计划卡内容正确', approval && approval.text === '写 X\n跑 Y' && approval.state === 'pending')
  check('标记等待计划决定', h.live.suspended.planPending === true)
  check('聚焦计划卡', h.called('focusPlanCard'))
}

// 10) done：收尾（切完整渲染 + 空回答兜底）
{
  const h = makeHarness()
  h.handlers.onDone()
  check('done 结束流式态', h.state.streamMsg._streaming === false)
  check('done 冲刷打字机', h.called('flushTypewriter'))
  check('done 空回答兜底', h.state.streamMsg.content === '（未生成回答内容）')
}

if (failures.length) {
  console.error(`\n失败 ${failures.length} 项：${failures.join('、')}`)
  process.exit(1)
}
console.log('\n全部通过：SSE 事件→状态 映射符合预期。')
