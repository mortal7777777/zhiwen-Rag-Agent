/**
 * Agent SSE 事件 → 组件状态的映射（从 ChatView.buildStreamHandlers 抽出，2026-10-08）。
 *
 * 为什么抽出来：这块是"协议 → 状态"的纯映射（160 行），却埋在 4000 行的视图里，
 * 既没法单测，也很难看清哪些事件改了哪些状态。抽出后：
 *   - 事件协议的变化只改这一个文件（配合 scripts/check_sse_protocol.py 守门）；
 *   - 可以用 `npm run check:stream` 在 Node 里直接跑事件序列断言（见 scripts/check_stream_handlers.mjs）。
 *
 * 不变式：本模块**不碰网络与 DOM 结构**（唯一的 DOM 访问是思考链自动滚动的
 * querySelectorAll，属于展示细节），所有组件副作用都通过 actions 回调注入。
 *
 * @param {object} deps
 * @param {object} deps.state  只读状态：{ activeId, currentTitle, streamMsg, pendingText }
 *                             （ref / 普通对象，直接改 .value 或对象属性）
 * @param {object} deps.actions 组件副作用回调：
 *   clearActivity, setActivity, closeThinkingSegment, dropStreamedRound, flushTypewriter,
 *   focusPermissionCard, focusPlanCard, pushSuspendedCards, scrollToBottom,
 *   startTypewriter, toolName, upsertSession
 * @param {object} deps.live   需要**实时读写**的组件级 let 变量（不能用值捕获，否则会读到旧值）：
 *   { getSkipNextToken, setSkipNextToken, getStreamedLen, setStreamedLen, setSuspended }
 */
import { nextTick } from 'vue'
import { sortSources } from './useChatUtils.js'
import { desktopShell } from '../utils/desktopShell.js'

export function createStreamHandlers({ state, actions, live }) {
  const { activeId, currentTitle, streamMsg, pendingText } = state
  const {
    clearActivity,
    setActivity,
    closeThinkingSegment,
    dropStreamedRound,
    flushTypewriter,
    focusPermissionCard,
    focusPlanCard,
    focusQuestionCard,
    pushSuspendedCards,
    scrollToBottom,
    startTypewriter,
    toolName,
    upsertSession,
    onAnswerComplete,
  } = actions

  function findSubagentTrace(subtaskId) {
    if (!streamMsg || !subtaskId) return null
    const trace = streamMsg.tool_trace || []
    return trace.find((t) => t.id === subtaskId) || null
  }

  /** 子代理事件按 subtask_id 落到轨迹条目；没收到 tool_start 时补建卡片。 */
  function ensureSubagentTrace(data) {
    if (!streamMsg) return null
    let trace = findSubagentTrace(data?.subtask_id)
    if (!trace) {
      trace = {
        id: data?.subtask_id || null,
        name: 'subagent',
        arguments: {
          task: data?.task || '',
          mode: data?.mode || '',
          profile: data?.profile || '',
          subtask_id: data?.subtask_id || null,
        },
        summary: '',
        status: 'running',
        rounds: 0,
        currentTool: '',
      }
      streamMsg.tool_trace.push(trace)
    }
    return trace
  }

  return {
    onSession: (data) => {
      activeId.value = data.conversation_id
      if (data.title) currentTitle.value = data.title
      // 方案 A（2026-11-19）：断连不再自动中止——「停止」按钮要靠 run_id 走
      // /agent/cancel 显式取消；三个流式入口（send/resume/continue）共用本
      // 处理器，turn 由 useSuspension.buildStreamHandlers 注入（Node 单测无 turn）
      if (data.run_id && state.turn) state.turn.runId = data.run_id
      upsertSession(data)
    },
    onTitle: (data) => {
      // 新会话标题是后台生成的，生成完成后推送到前端更新。
      // 必须按事件携带的会话号更新：流式期间切走时，旧流不能给当前会话改名。
      if (!data.title) return
      const cid = data.conversation_id ?? activeId.value
      if (cid != null) upsertSession({ conversation_id: cid, title: data.title })
      if (cid == null || cid === activeId.value) {
        currentTitle.value = data.title
      }
    },
    onReasoningDelta: (text) => {
      // 原始思考链增量（仅展示）：按轮分段落进工具轨迹时间线
      if (!text || !streamMsg) return
      setActivity('reasoning', '思考中…')
      const trace = streamMsg.tool_trace
      let seg = trace[trace.length - 1]
      if (!seg || seg.kind !== 'thinking' || seg.done) {
        seg = {
          kind: 'thinking',
          text: '',
          done: false,
          duration: 0,
          _open: false,
          _startedAt: Date.now(),
        }
        trace.push(seg)
      }
      seg.text += text
      scrollToBottom()
      nextTick(() => {
        const els = document.querySelectorAll('.think-body.live')
        const el = els[els.length - 1]
        if (el) el.scrollTop = el.scrollHeight
      })
    },
    onStatus: (data) => {
      // 动态运行状态：相位由后端给定，文案在相位内轮换
      if (data.text || data.phase) {
        setActivity(data.phase || 'thinking', data.text || '')
        scrollToBottom()
      }
    },
    onHook: (data) => {
      // PreToolUse/PostToolUse 生命周期事件：拦截时给出明显提示
      setActivity(
        'notice',
        data.decision === 'deny'
          ? `Hook 拦截：${data.name}`
          : `Hook 触发：${data.name}`,
      )
      scrollToBottom()
    },
    onPlanProgress: (data) => {
      // 计划按当前进度展示：已完成/进行中/未开始
      streamMsg.planDone = data.done || 0
      streamMsg.planTotal =
        data.total != null ? data.total : streamMsg.plan.length
      streamMsg.planCurrent = data.current || null
      if (data.current) {
        setActivity(
          'plan',
          `第 ${streamMsg.planDone}/${streamMsg.planTotal} 步 · ${data.current}`,
        )
      }
      scrollToBottom()
    },
    onSubagentStart: (data) => {
      const trace = ensureSubagentTrace(data)
      if (!trace) return
      trace.status = 'running'
      trace.rounds = 0
      trace.cancelled = false
      trace.currentTool = ''
      if (data.model) trace.model = data.model
      if (data.max_rounds) trace.maxRounds = data.max_rounds
      if (data.profile) trace.profile = data.profile
      if (data.mode) trace.mode = data.mode
      scrollToBottom()
    },
    onSubagentProgress: (data) => {
      const trace = ensureSubagentTrace(data)
      if (!trace) return
      trace.status = 'running'
      if (data.round) trace.rounds = data.round
      if (data.max_rounds) trace.maxRounds = data.max_rounds
      if (data.phase === 'tool' && data.tool) {
        trace.currentTool = data.tool
        if (data.hint) trace.currentHint = data.hint
      }
      scrollToBottom()
    },
    onSubagentResult: (data) => {
      const trace = findSubagentTrace(data?.subtask_id)
      if (!trace) return
      const status = String(data.status || '')
      trace.cancelled = status === 'cancelled' || !!data.cancelled
      trace.status = trace.cancelled
        ? 'cancelled'
        : status === 'error'
          ? 'error'
          : 'done'
      if (data.summary) trace.summary = data.summary
      if (data.duration_ms != null) trace.duration_ms = data.duration_ms
      if (data.rounds) trace.rounds = data.rounds
      if (data.tokens) trace.tokens = data.tokens
      if (data.error) trace.error = data.error
      if (data.findings != null) trace.findingsCount = data.findings
      trace.currentTool = ''
      scrollToBottom()
    },
    onToolStart: (data) => {
      closeThinkingSegment()
      setActivity('tool', `${toolName(data.name)}…`)
      streamMsg.tool_trace.push({
        // 后端 tool_start 带 LangChain tool_call id（并行同名工具靠它区分归属）
        id: data.id || null,
        name: data.name,
        arguments: data.arguments,
        summary: '',
        status: 'running',
      })
      scrollToBottom()
    },
    onPlan: (data) => {
      streamMsg.plan = data.steps || []
      streamMsg.planTotal = streamMsg.plan.length
      scrollToBottom()
    },
    onTodos: (data) => {
      // TodoWrite 任务清单：规划后随 SSE 推送，可勾选跟踪
      streamMsg.todos = data.todos || []
      scrollToBottom()
    },
    onVision: () => {
      setActivity('vision', '识别图片中…')
    },
    onToolResult: (data) => {
      // 工具完成后保持反馈连续，衔接下一轮思考（避免状态条闪空）
      setActivity('thinking', '继续思考…')
      // 归属优先按 call_id 精确回填（并行子代理各跑一次 bash 时按名字
      // +无 summary 匹配会把第二个结果挂到第一条轨迹上）；无 id 的
      // 旧事件回退按名字兜底
      const trace =
        (data.id &&
          streamMsg.tool_trace.find((t) => t.id && t.id === data.id)) ||
        streamMsg.tool_trace.find((t) => t.name === data.name && !t.summary)
      if (trace) {
        trace.summary = data.summary
        // 完整输出（截断版）：工具卡可展开查看
        if (data.detail) trace.detail = data.detail
        // 被取消/失败的分支保留终态，不被 merge 的 tool_result 覆盖成 done
        trace.status = trace.cancelled
          ? 'cancelled'
          : trace.error
            ? 'error'
            : 'done'
        trace.duration_ms = data.duration_ms ?? null
        // 子代理结果携带产物清单（右侧「子代理/产出」页签用）
        if (Array.isArray(data.artifacts)) trace.artifacts = data.artifacts
      }
      if (Array.isArray(data.sources) && data.sources.length) {
        // 只接受来源对象；旧版子代理事件发过编号数组，不能让整数污染列表
        const items = data.sources.filter(
          (src) => src && typeof src === 'object' && !Array.isArray(src),
        )
        if (items.length) {
          streamMsg.sources.push(...items)
          // 卡片顺序跟随 [n] 编号，点击跳转才能定位到正确条目
          streamMsg.sources = sortSources(streamMsg.sources)
        }
      }
      scrollToBottom()
    },
    onSuspended: (data) => {
      // 原生挂起（interrupt）：本条流即将结束；渲染整批卡片/计划卡，收齐决定后一次提交
      closeThinkingSegment()
      setActivity('waiting', '等待人工确认…')
      const pending = {
        conversationId: data.conversation_id || activeId.value,
        msg: streamMsg,
        submitted: false,
        planPending: false,
        questionPending: false,
        // 已提交的问答答案（等其余审批卡凑齐后一并 resume）
        answers: [],
        // 批次戳：客户端轮询 /agent/pending 时据此区分「同一批」与「超时后新挂起的一批」
        batchCreatedAt: Number(data.created_at || 0),
      }
      live.setSuspended(pending)
      pushSuspendedCards(streamMsg, data.ops)
      const questions = data.questions || []
      if (questions.length) {
        pending.questionPending = true
        streamMsg.questionRequest = {
          requests: questions,
          state: 'pending',
          _suspended: true,
        }
      }
      if (data.plan && (data.plan.steps || []).length) {
        pending.planPending = true
        streamMsg.planApproval = {
          text: (data.plan.steps || []).map((s) => String(s)).join('\n'),
          steps: (data.plan.steps || []).map((s) => String(s)),
          summary: data.plan.summary || '',
          state: 'pending',
          _suspended: true,
        }
        focusPlanCard()
      } else if (questions.length) {
        focusQuestionCard()
      } else {
        focusPermissionCard()
      }
      scrollToBottom()
      // 桌面端：挂起等待人工确认时提醒（浏览器/Node 环境自动 no-op）
      desktopShell.notify({ title: '知问', body: '有操作待你确认' })
      desktopShell.attention()
    },
    onStreamDelta: (text) => {
      closeThinkingSegment() // 文本开始输出：本轮思考结束（含工具轮过渡文本）
      pendingText.value += text
      live.setStreamedLen(live.getStreamedLen() + text.length)
      startTypewriter()
    },
    onStreamEnd: (data) => {
      if (data && data.keep) {
        // 正文：已增量显示，后端随后还会发同一段全文 token（供 CLI/评测），
        // 这里标记跳过，避免同一段文字被追加两次
        live.setSkipNextToken(true)
        setActivity('finalizing', '正在整理回答…')
      } else {
        // 工具轮过渡说明：从正文撤回，但归档到工具轨迹的「过程说明」，
        // 避免用户刚看到解释就被吞掉（停止/卡死/报错仍直接撤回）。
        if (data && data.reason === 'tool_round' && streamMsg) {
          const streamed = live.getStreamedLen()
          const pending = pendingText.value || ''
          const fromContent = Math.max(0, streamed - pending.length)
          const contentPart =
            fromContent > 0
              ? (streamMsg.content || '').slice(-fromContent)
              : ''
          const pendingPart = pending.slice(-Math.min(pending.length, streamed))
          const text = (contentPart + pendingPart).trim()
          const trace = streamMsg.tool_trace || []
          const last = trace[trace.length - 1]
          if (text && !(last && last.kind === 'process' && last.text === text)) {
            trace.push({ kind: 'process', text, _open: true })
          }
        }
        if (data && data.reason === 'tool_round') {
          setActivity('thinking', '继续思考…')
        } else {
          clearActivity()
        }
        dropStreamedRound()
      }
    },
    onToken: (token) => {
      if (live.getSkipNextToken()) {
        live.setSkipNextToken(false)
        return
      }
      pendingText.value += token
      startTypewriter()
    },
    onDone: (data) => {
      // done 携带 finalize 去重后的完整来源，作为权威结果覆盖增量拼接：
      // 子代理事件曾把编号数组当来源对象推送，且主/子代理来源可能重复或乱序。
      if (Array.isArray(data?.sources)) {
        streamMsg.sources = sortSources(data.sources)
      }
      closeThinkingSegment()
      clearActivity()
      flushTypewriter()
      streamMsg._streaming = false // 流式结束：切换到完整 Markdown 渲染
      nextTick(() => scrollToBottom(true)) // 渲染切换后重新对齐底部，避免跳动
      if (!streamMsg.content) {
        streamMsg.content = '（未生成回答内容）'
      }
      // 回答完成：拉取输入框「下一步建议」（本地即时版已在视图侧渲染）
      onAnswerComplete?.(streamMsg)
      // 桌面端：窗口未聚焦时发系统通知（主进程会按焦点过滤）
      desktopShell.notify({ title: '知问', body: '回答已完成' })
    },
  }
}
