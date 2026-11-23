/**
 * 挂起批次（HITL）：审批卡 / 计划确认 / 用户提问的统一状态机。
 *
 * 关键约束与视图拆分前一致：
 * - 一次 interrupt 收齐本轮全部待决定项，决定收齐后一次性 /agent/resume；
 * - 续跑可能再次挂起（新批次），提交时只清理"本次提交的那一批"；
 * - 审批卡支持数字键/方向键/Enter/Esc，焦点不在卡内时不响应 Enter（防误批）。
 */
import {
  computed,
  nextTick,
  onActivated,
  onBeforeUnmount,
  onDeactivated,
  onMounted,
  ref,
} from 'vue'
import { ElMessage } from 'element-plus/es/components/message/index'
import { createStreamHandlers } from './useStreamHandlers'
import {
  collectSuspendedCards,
  findPendingPermission,
  firstPendingPermission,
  hasPendingSuspension,
  markSuspendedCardsStale,
  pushSuspendedCards,
  resetSuspendedCards,
} from './useSuspensionCards'
import {
  abandonRun,
  continueAgentRun,
  getPendingApproval,
  resumeAgentChat,
} from '../api'
import { nextLocalMsgKey, toolName } from './useChatUtils'

export function useSuspension({
  activeId,
  currentTitle,
  loading,
  setActivity,
  clearActivity,
  messages,
  questionInput,
  turn,
  typewriter,
  closeThinkingSegment,
  scrollToBottom,
  upsertSession,
  reloadSessions,
  reloadPrompt,
  reloadMessages,
  onAnswerComplete,
}) {
  // 原生挂起（interrupt）的审批批次：{ conversationId, msg, submitted }
  let suspended = null
  // pending 轮询定时器：服务端 permission_timeout 触发自动拒绝时，客户端要能感知
  let approvalWatchId = null
  // 审批超时后后端在无人值守续跑：期间锁住输入，避免用户立刻追问撞 409
  const autoRunState = ref(false)

  function buildStreamHandlers() {
    return createStreamHandlers({
      // turn 注入给 onSession 捕获 run_id（「停止」按钮走 /agent/cancel 需要）
      state: {
        activeId,
        currentTitle,
        streamMsg: turn.streamMsg,
        pendingText: typewriter.pendingText,
        turn,
      },
      actions: {
        clearActivity,
        setActivity,
        closeThinkingSegment,
        dropStreamedRound: typewriter.drop,
        flushTypewriter: typewriter.flush,
        focusPermissionCard,
        focusPlanCard,
        focusQuestionCard,
        pushSuspendedCards,
        scrollToBottom,
        startTypewriter: typewriter.start,
        toolName,
        upsertSession,
        onAnswerComplete,
      },
      live: {
        getSkipNextToken: typewriter.getSkipNextToken,
        setSkipNextToken: typewriter.setSkipNextToken,
        getStreamedLen: typewriter.getStreamedLen,
        setStreamedLen: typewriter.setStreamedLen,
        setSuspended: (v) => {
          suspended = v
          if (v) startApprovalWatch()
          else stopApprovalWatch()
        },
      },
    })
  }

  /** 挂起批次是否还有未决定的操作/计划/问题（用于禁用输入框/发送键） */
  const hasSuspendedPending = computed(
    () => autoRunState.value || hasPendingSuspension(messages),
  )

  /** 停止审批批次轮询 */
  function stopApprovalWatch() {
    if (approvalWatchId) {
      window.clearInterval(approvalWatchId)
      approvalWatchId = null
    }
  }

  /** 开始轮询当前挂起批次：服务端 permission_timeout 到点会自动拒绝 */
  function startApprovalWatch() {
    stopApprovalWatch()
    if (!suspended) return
    approvalWatchId = window.setInterval(pollPendingBatch, 2000)
  }

  /** 离开当前会话：停轮询并清掉挂起批次 */
  function reset() {
    stopApprovalWatch()
    suspended = null
    autoRunState.value = false
  }

  async function decidePermission(p, optionIndex) {
    if (!p || p.status !== 'pending') return
    const approve = optionIndex !== 1
    const remember = optionIndex === 2
    // 记录决定；整批决定齐了由 maybeSubmitSuspension 一次性提交（/agent/resume）
    p._decision = {
      call_id: p.id,
      approve,
      reason: p.reason || '',
      remember_session: false,
      remember_forever: !!remember && approve,
    }
    p.status = approve ? 'approved' : 'denied'
    if (remember && approve) {
      p.reason = (p.reason ? `${p.reason}；` : '') + '已记住，下次不再询问'
    }
    await maybeSubmitSuspension()
  }

  function focusPermissionCard() {
    const p = firstPendingPermission(messages)
    if (!p) return
    const active = document.activeElement
    // 用户正在输入问题时不强抢焦点（审批卡片仍在，点击即可进入选项卡）
    const typing = active && active.tagName === 'TEXTAREA' && active.value?.length > 0
    if (typing) return
    p._focusIndex = 0
    nextTick(() => {
      const card = document.querySelector(`[data-perm-id="${p.id}"] .permission-opts`)
      card?.focus()
    })
  }

  function focusPlanCard() {
    nextTick(() => {
      const card = document.querySelector('.plan-approval-box[data-plan-state="pending"]')
      if (!card) return
      if (document.activeElement === questionInput.value) {
        if ((questionInput.value?.value?.length || 0) > 0) return
      }
      card.focus?.()
    })
  }

  function focusQuestionCard() {
    nextTick(() => {
      const card = document.querySelector(
        '.question-card[data-question-state="pending"]',
      )
      if (!card) return
      if (document.activeElement === questionInput.value) {
        if ((questionInput.value?.value?.length || 0) > 0) return
      }
      card.focus?.()
    })
  }

  // ChatView 被 keep-alive 保活：切到知识库/运行页时全局键盘监听仍在。
  // 只有当前视图激活且审批卡真的渲染在 DOM 里时才响应，避免"看不见卡片却回车批准"。
  const viewActive = ref(true)
  onActivated(() => {
    viewActive.value = true
  })
  onDeactivated(() => {
    viewActive.value = false
  })

  /** 全局键盘：数字键/Enter 在当前可见审批卡上可用；方向键/Esc 只在卡片内生效。 */
  function onPermissionKeydown(event) {
    if (!viewActive.value) return
    if (
      typeof document !== 'undefined' &&
      !document.querySelector('.permission-card.pending')
    ) {
      return
    }
    const tag = event.target?.tagName
    if (tag === 'INPUT' || tag === 'TEXTAREA') return
    // 问答卡内键盘归卡片自己处理（同批次可能同时有审批卡与问题卡）
    if (event.target?.closest?.('.question-card')) return
    const cardEl = event.target.closest?.('[data-perm-id]')
    const p = cardEl
      ? findPendingPermission(messages, cardEl.getAttribute('data-perm-id'))
      : firstPendingPermission(messages)
    if (!p) return
    const count = p.rememberable ? 3 : 2
    if (event.key >= '1' && event.key <= String(count)) {
      event.preventDefault()
      decidePermission(p, Number(event.key) - 1)
      return
    }
    if (event.key === 'Enter') {
      // 焦点在选项按钮上时交给原生 click；其余位置确认当前聚焦项
      if (event.target.closest?.('.perm-opt')) return
      event.preventDefault()
      decidePermission(p, p._focusIndex ?? 0)
      return
    }
    if (!cardEl) return
    if (event.key === 'ArrowDown' || event.key === 'ArrowRight') {
      event.preventDefault()
      p._focusIndex = (p._focusIndex + 1) % count
    } else if (event.key === 'ArrowUp' || event.key === 'ArrowLeft') {
      event.preventDefault()
      p._focusIndex = (p._focusIndex + count - 1) % count
    } else if (event.key === 'Escape') {
      event.preventDefault()
      questionInput.value?.focus()
    }
  }

  /** 计划确认：把（可编辑的）计划作为决定经 /agent/resume 提交，同一 run 继续 */
  function confirmPlan(msg) {
    const approval = msg.planApproval
    if (!approval || loading.value) return
    const steps = String(approval.text || '')
      .split('\n')
      .map((s) => s.trim())
      .filter(Boolean)
    if (!steps.length) {
      ElMessage.warning('计划不能为空')
      return
    }
    approval.steps = steps
    approval.state = 'confirmed'
    if (!suspended || suspended.submitted) return
    suspended.planPending = false
    suspended.submitted = true
    runResumeStream(
      suspended.conversationId,
      collectSuspendedCards(messages)
        .map((c) => c._decision)
        .filter(Boolean),
      false,
      { approved: true, steps, reason: '' },
      suspended.answers || [],
    )
  }

  function cancelPlan(msg) {
    const approval = msg.planApproval
    if (!approval) return
    if (approval._suspended && suspended && !suspended.submitted) {
      approval.state = 'cancelled'
      suspended.planPending = false
      suspended.submitted = true
      runResumeStream(
        suspended.conversationId,
        collectSuspendedCards(messages)
          .map((c) => c._decision)
          .filter(Boolean),
        false,
        { approved: false, steps: [], reason: '用户取消了该计划' },
        suspended.answers || [],
      )
      return
    }
    approval.state = 'cancelled'
  }

  /** 计划卡键盘：Enter / Ctrl+Enter 确认，Esc 取消（输入框内 Enter 保留换行语义） */
  function onPlanCardKeydown(msg, event) {
    const approval = msg?.planApproval
    if (!approval || approval.state !== 'pending') return
    const inTextarea = event.target?.tagName === 'TEXTAREA'
    if ((event.ctrlKey || event.metaKey) && event.key === 'Enter') {
      event.preventDefault()
      confirmPlan(msg)
    } else if (event.key === 'Escape') {
      event.preventDefault()
      cancelPlan(msg)
    } else if (event.key === 'Enter' && !inTextarea) {
      event.preventDefault()
      confirmPlan(msg)
    }
  }

  /** 全部卡片都已决定 → 一次性提交，开恢复流续跑同一条回答 */
  async function maybeSubmitSuspension() {
    if (!suspended || suspended.submitted) return
    if (suspended.planPending) return
    if (suspended.questionPending) return
    const cards = collectSuspendedCards(messages)
    if (cards.some((c) => !c._decision)) return
    if (!cards.length && !(suspended.answers || []).length) return
    suspended.submitted = true
    await runResumeStream(
      suspended.conversationId,
      cards.map((c) => c._decision),
      false,
      null,
      suspended.answers || [],
    )
  }

  /** 提交问答卡答案：审批卡都已决定时立即续跑，否则等剩余的卡 */
  async function submitQuestions(msg, answers) {
    const request = msg?.questionRequest
    if (!request || request.state !== 'pending') return
    request.state = 'answered'
    if (!suspended || suspended.submitted) return
    suspended.questionPending = false
    suspended.answers = answers || []
    await maybeSubmitSuspension()
  }

  /** 挂起期间点「全部拒绝并停止」：全拒 + stopped */
  async function stopSuspended() {
    if (!suspended || suspended.submitted) return
    const cards = collectSuspendedCards(messages)
    for (const c of cards) {
      if (!c._decision) {
        c._decision = { call_id: c.id, approve: false, reason: '用户停止了回答' }
        c.status = 'denied'
      }
    }
    for (const msg of messages.value) {
      const request = msg.questionRequest
      if (request && request._suspended && request.state === 'pending') {
        request.state = 'cancelled'
        request._suspended = false
      }
    }
    const plan = suspended.planPending
      ? { approved: false, steps: [], reason: '用户停止了回答' }
      : null
    suspended.submitted = true
    await runResumeStream(
      suspended.conversationId,
      cards.map((c) => c._decision),
      true,
      plan,
      suspended.answers || [],
    )
  }

  /** 提交决定后恢复挂起运行：新 SSE 流继续写回同一条助手消息 */
  async function runResumeStream(
    conversationId,
    decisions,
    stopped,
    plan = null,
    answers = null,
  ) {
    if (!conversationId || loading.value) return
    stopApprovalWatch()
    const submittedBatch = suspended
    typewriter.flush()
    if (submittedBatch && submittedBatch.msg) turn.streamMsg = submittedBatch.msg
    loading.value = true
    setActivity('resume', '正在继续…')
    turn.abortController = new AbortController()
    // resume 流是否已被后端接受（收到 session）：失败回滚的判据
    turn.sessionReceived = false
    let rolledBack = false
    let needsReconcile = false
    const body = {
      conversation_id: conversationId,
      decisions,
      stopped: !!stopped,
    }
    if (plan) body.plan = plan
    if (answers && answers.length) body.answers = answers
    try {
      const handlers = buildStreamHandlers()
      const originOnSession = handlers.onSession
      if (originOnSession) {
        handlers.onSession = (data) => {
          turn.sessionReceived = true
          originOnSession(data)
        }
      }
      await resumeAgentChat(body, handlers, {
        signal: turn.abortController.signal,
      })
    } catch (error) {
      typewriter.flush()
      if (error.name === 'AbortError' || error.message === 'aborted') {
        typewriter.drop()
        if (turn.streamMsg) {
          turn.streamMsg.content = turn.streamMsg.content || '（已停止生成）'
        }
      } else if (!turn.sessionReceived && submittedBatch) {
        // HTTP 层失败：决定可能根本没送达后端。整批回滚为待决定并重启轮询，
        // 用户直接重试提交（此前只能靠再发一条消息撞 409 才能恢复入口）
        resetSuspendedCards(messages)
        const msg = submittedBatch.msg
        submittedBatch.submitted = false
        submittedBatch.answers = []
        submittedBatch.planPending = !!(
          msg.planApproval &&
          msg.planApproval._suspended &&
          msg.planApproval.state === 'pending'
        )
        submittedBatch.questionPending = !!(
          msg.questionRequest &&
          msg.questionRequest._suspended &&
          msg.questionRequest.state === 'pending'
        )
        suspended = submittedBatch
        rolledBack = true
        startApprovalWatch()
        ElMessage.error(error.message || '提交失败，请重试')
        if (submittedBatch.planPending) focusPlanCard()
        else if (submittedBatch.questionPending) focusQuestionCard()
        else focusPermissionCard()
      } else {
        const detail = error.message || '恢复执行失败，请确认后端服务已启动'
        ElMessage.error(detail)
        if (turn.streamMsg) {
          turn.streamMsg.content = turn.streamMsg.content || `恢复失败：${detail}`
        }
        // 流中途失败：运行状态以后端为准（可能已中断/又挂起一批），
        // loading 释放后按服务端状态重建
        needsReconcile = true
      }
    } finally {
      turn.abortController = null
      loading.value = false
      clearActivity()
      if (turn.streamMsg) turn.streamMsg._streaming = false
      // 续跑过程中可能又挂起了新批次（onSuspended 已替换 suspended）：不能清掉，
      // 否则新卡片无法提交。只清掉本次提交的那一批。
      if (!rolledBack && suspended === submittedBatch) suspended = null
      if (!suspended) stopApprovalWatch()
      await reloadSessions()
      scrollToBottom()
      if (needsReconcile) await restorePendingApproval(conversationId)
    }
  }

  /**
   * 刷新/切换会话后按后端状态恢复未完成的一轮。
   * 挂起/中断的一轮还没落库（finalize 才写 MySQL），不恢复的话会话会被闸门挡住。
   */
  async function restorePendingApproval(conversationId) {
    if (!conversationId || loading.value) return
    try {
      const res = await getPendingApproval(conversationId)
      if (!res) return
      // 幂等：先清掉上一次恢复出的提示条/挂起卡，避免同一批次重复渲染
      messages.value = messages.value.filter((m) => !m._unfinishedBar)
      for (const m of messages.value) {
        m.permissions = (m.permissions || []).filter((p) => !p._suspended)
        if (m.planApproval && m.planApproval._suspended) m.planApproval = null
        if (m.questionRequest && m.questionRequest._suspended) m.questionRequest = null
      }
      if (!res.pending && !res.interrupted) {
        // 方案 A（2026-11-19）：上一轮还在后台跑（断连不中止，刷新回来接状态）。
        // 挂「后台生成中」占位气泡 + 复用审批超时的轮询等它落库；期间输入锁住
        // （autoRunning），停止按钮可用（run_id 来自 /agent/pending）。
        if (res.active && res.run_id) {
          let target = messages.value[messages.value.length - 1]
          if (!target || target.role !== 'assistant') {
            target = {
              role: 'assistant',
              content: '',
              sources: [],
              tool_trace: [],
              permissions: [],
              todos: [],
              plan: [],
              planTotal: 0,
              planDone: 0,
              planCurrent: null,
              _sourcesOpen: false,
              _streaming: false,
              _localKey: nextLocalMsgKey(),
            }
            messages.value.push(target)
          }
          turn.streamMsg = target
          turn.runId = res.run_id
          loading.value = true
          scrollToBottom()
          try {
            await waitAutoRunThenReload(conversationId)
          } finally {
            loading.value = false
            scrollToBottom()
          }
        }
        return
      }
      let target = [...messages.value].reverse().find((m) => m.role === 'assistant')
      if (!target || res.interrupted) {
        target = {
          role: 'assistant',
          content: '',
          sources: [],
          tool_trace: [],
          permissions: [],
          todos: [],
          plan: [],
          planTotal: 0,
          planDone: 0,
          planCurrent: null,
          _sourcesOpen: false,
          _streaming: false,
          _localKey: nextLocalMsgKey(),
        }
        messages.value.push(target)
      }
      target.permissions = target.permissions || []
      turn.streamMsg = target
      if (res.interrupted) {
        // 进程被杀/异常退出留下的半成品：给「继续执行/丢弃」入口
        stopApprovalWatch()
        target._unfinishedBar = true
        scrollToBottom()
        return
      }
      suspended = {
        conversationId,
        msg: target,
        submitted: false,
        planPending: false,
        questionPending: false,
        answers: [],
        batchCreatedAt: Number(res.created_at || 0),
      }
      pushSuspendedCards(target, res.ops)
      if ((res.questions || []).length) {
        suspended.questionPending = true
        target.questionRequest = {
          requests: res.questions,
          state: 'pending',
          _suspended: true,
        }
      }
      if (res.plan && (res.plan.steps || []).length) {
        suspended.planPending = true
        target.planApproval = {
          text: (res.plan.steps || []).map((s) => String(s)).join('\n'),
          steps: (res.plan.steps || []).map((s) => String(s)),
          summary: res.plan.summary || '',
          state: 'pending',
          _suspended: true,
        }
        focusPlanCard()
      } else if ((res.questions || []).length) {
        focusQuestionCard()
      } else {
        focusPermissionCard()
      }
      startApprovalWatch()
      scrollToBottom()
    } catch {
      // 恢复失败不阻塞会话加载
    }
  }

  /** 审批超时后后端在独立流里继续跑：等它结束或再次挂起，然后刷新界面 */
  async function waitAutoRunThenReload(conversationId) {
    autoRunState.value = true
    setActivity('waiting', '等待后端自动处理…')
    try {
      for (let i = 0; i < 300; i += 1) {
        await new Promise((resolve) => setTimeout(resolve, 2000))
        if (activeId.value !== conversationId || suspended) return
        let state
        try {
          state = await getPendingApproval(conversationId)
        } catch {
          continue
        }
        if (state.pending) {
          await restorePendingApproval(conversationId)
          return
        }
        if (state.active) continue
        if (state.interrupted) {
          await restorePendingApproval(conversationId)
          return
        }
        await reloadMessages(conversationId)
        return
      }
    } finally {
      autoRunState.value = false
      clearActivity()
    }
  }

  /** 挂起期间轮询后端状态：超时自动拒绝 / 新批次 / 中断 */
  async function pollPendingBatch() {
    if (loading.value) return
    const batch = suspended
    if (!batch || batch.submitted) {
      stopApprovalWatch()
      return
    }
    if (activeId.value !== batch.conversationId) {
      stopApprovalWatch()
      return
    }
    let state
    try {
      state = await getPendingApproval(batch.conversationId)
    } catch {
      return
    }
    if (!suspended || suspended !== batch || batch.submitted) return

    if (state.pending) {
      const prev = Number(batch.batchCreatedAt || 0)
      const next = Number(state.created_at || 0)
      if (prev && next && Math.abs(prev - next) > 1e-6) {
        ElMessage.warning('该批操作已自动处理，收到新一批待确认操作')
        stopApprovalWatch()
        suspended = null
        await restorePendingApproval(batch.conversationId)
      }
      return
    }

    // 挂起消失：本批已被处理（最常见：permission_timeout 自动拒绝）。
    // 看门狗续跑期间 active=True 且 interrupted=True 会同时成立：必须先看 active。
    stopApprovalWatch()
    const active = !!state.active
    const reason = active
      ? '等待确认超时，已自动取消（后端继续执行中）'
      : state.interrupted
        ? '运行已中断'
        : '等待确认超时，已自动取消'
    markSuspendedCardsStale(messages, reason)
    suspended = null
    if (active) {
      ElMessage.warning('审批已超时自动取消，后端正在继续执行…')
      await waitAutoRunThenReload(batch.conversationId)
      return
    }
    if (state.interrupted) {
      ElMessage.warning('审批已结束，运行处于中断状态')
      await restorePendingApproval(batch.conversationId)
      return
    }
    ElMessage.warning('审批已超时自动取消，正在刷新结果')
    await reloadMessages(batch.conversationId)
  }

  /** 续跑被中断的运行：新 SSE 流渲染进提示条所在气泡 */
  async function continueInterrupted(msg) {
    if (!activeId.value || loading.value) return
    typewriter.flush()
    turn.streamMsg = msg
    msg._unfinishedBar = false
    loading.value = true
    setActivity('resume', '正在继续执行…')
    turn.abortController = new AbortController()
    try {
      await continueAgentRun(activeId.value, buildStreamHandlers(), {
        signal: turn.abortController.signal,
      })
    } catch (error) {
      typewriter.flush()
      if (error.name === 'AbortError' || error.message === 'aborted') {
        typewriter.drop()
        if (turn.streamMsg) {
          turn.streamMsg.content = turn.streamMsg.content || '（已停止生成）'
        }
      } else {
        const detail = error.message || '续跑失败，请确认后端服务已启动'
        ElMessage.error(detail)
        msg._unfinishedBar = true
      }
    } finally {
      turn.abortController = null
      loading.value = false
      clearActivity()
      if (turn.streamMsg) turn.streamMsg._streaming = false
      await reloadSessions()
      await reloadPrompt()
      scrollToBottom()
    }
  }

  /** 丢弃未完成的运行：删除原生检查点线程，解除闸门 */
  async function abandonInterrupted(msg) {
    if (!activeId.value || loading.value) return
    try {
      await abandonRun(activeId.value)
      messages.value = messages.value.filter((m) => m !== msg)
      ElMessage.success('已丢弃未完成的运行，可以继续对话')
      scrollToBottom()
    } catch (error) {
      ElMessage.error(error.response?.data?.detail || '丢弃失败，请重试')
    }
  }

  onMounted(() => {
    window.addEventListener('keydown', onPermissionKeydown, true)
  })
  onBeforeUnmount(() => {
    window.removeEventListener('keydown', onPermissionKeydown, true)
    stopApprovalWatch()
  })

  return {
    autoRunning: autoRunState,
    hasSuspendedPending,
    buildStreamHandlers,
    decidePermission,
    confirmPlan,
    cancelPlan,
    onPlanCardKeydown,
    submitQuestions,
    stopSuspended,
    restorePendingApproval,
    continueInterrupted,
    abandonInterrupted,
    reset,
  }
}
