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
 * @param {object} deps.state  只读状态：{ activeId, currentTitle, currentTool, streamMsg, pendingText }
 *                             （前四个是 ref / 普通对象，直接改 .value 或对象属性）
 * @param {object} deps.actions 组件副作用回调：
 *   closeThinkingSegment, dropStreamedRound, flushTypewriter, focusPermissionCard,
 *   focusPlanCard, pushSuspendedCards, scrollToBottom, startTypewriter, toolName, upsertSession
 * @param {object} deps.live   需要**实时读写**的组件级 let 变量（不能用值捕获，否则会读到旧值）：
 *   { getSkipNextToken, setSkipNextToken, getStreamedLen, setStreamedLen, setSuspended }
 */
import { nextTick } from 'vue'

export function createStreamHandlers({ state, actions, live }) {
  const { activeId, currentTitle, currentTool, streamMsg, pendingText } = state
  const {
    closeThinkingSegment,
    dropStreamedRound,
    flushTypewriter,
    focusPermissionCard,
    focusPlanCard,
    pushSuspendedCards,
    scrollToBottom,
    startTypewriter,
    toolName,
    upsertSession,
  } = actions

  return {
    onSession: (data) => {
      activeId.value = data.conversation_id
      if (data.title) currentTitle.value = data.title
      upsertSession(data)
    },
    onTitle: (data) => {
      // 新会话标题是后台生成的，生成完成后推送到前端更新
      if (data.title) {
        currentTitle.value = data.title
        upsertSession({ conversation_id: activeId.value, title: data.title })
      }
    },
    onReasoningDelta: (text) => {
      // 原始思考链增量（仅展示）：按轮分段落进工具轨迹时间线
      if (!text || !streamMsg) return
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
      // 动态运行状态：准备中…/思考中…（tool_start 会覆盖为具体工具名）
      if (data.text) {
        currentTool.value = data.text
        scrollToBottom()
      }
    },
    onHook: (data) => {
      // PreToolUse/PostToolUse 生命周期事件：拦截时给出明显提示
      currentTool.value =
        data.decision === 'deny'
          ? `Hook 拦截：${data.name}`
          : `Hook 触发：${data.name}`
      scrollToBottom()
    },
    onPlanProgress: (data) => {
      // 计划按当前进度展示：已完成/进行中/未开始
      streamMsg.planDone = data.done || 0
      streamMsg.planTotal =
        data.total != null ? data.total : streamMsg.plan.length
      streamMsg.planCurrent = data.current || null
      scrollToBottom()
    },
    onToolStart: (data) => {
      closeThinkingSegment()
      currentTool.value = `${toolName(data.name)}…`
      streamMsg.tool_trace.push({
        name: data.name,
        arguments: data.arguments,
        summary: '',
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
      currentTool.value = '识别图片中…'
    },
    onToolResult: (data) => {
      // 工具完成后保持反馈连续，衔接下一轮思考（避免状态条闪空）
      currentTool.value = '继续思考…'
      const trace = streamMsg.tool_trace.find((t) => t.name === data.name && !t.summary)
      if (trace) {
        trace.summary = data.summary
        // 完整输出（截断版）：工具卡可展开查看
        if (data.detail) trace.detail = data.detail
      }
      if (Array.isArray(data.sources) && data.sources.length) {
        // 只接受来源对象；旧版子代理事件发过编号数组，不能让整数污染列表
        const items = data.sources.filter(
          (src) => src && typeof src === 'object' && !Array.isArray(src),
        )
        if (items.length) streamMsg.sources.push(...items)
      }
      scrollToBottom()
    },
    onSuspended: (data) => {
      // 原生挂起（interrupt）：本条流即将结束；渲染整批卡片/计划卡，收齐决定后一次提交
      closeThinkingSegment()
      currentTool.value = '等待人工确认…'
      const pending = {
        conversationId: data.conversation_id || activeId.value,
        msg: streamMsg,
        submitted: false,
        planPending: false,
      }
      live.setSuspended(pending)
      pushSuspendedCards(streamMsg, data.ops)
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
      } else {
        focusPermissionCard()
      }
      scrollToBottom()
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
      } else {
        // 工具轮过渡文本：整轮撤回
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
        streamMsg.sources = data.sources
      }
      closeThinkingSegment()
      currentTool.value = ''
      flushTypewriter()
      streamMsg._streaming = false // 流式结束：切换到完整 Markdown 渲染
      nextTick(() => scrollToBottom(true)) // 渲染切换后重新对齐底部，避免跳动
      if (!streamMsg.content) {
        streamMsg.content = '（未生成回答内容）'
      }
    },
  }
}
