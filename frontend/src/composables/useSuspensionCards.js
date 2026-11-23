/**
 * 挂起批次（HITL）的卡片数据层：把 interrupt 载荷映射成视图卡片，
 * 并提供跨消息查找/清理。无响应式依赖之外的副作用，状态机在 useSuspension.js。
 */
import { formatArgs, toolName } from './useChatUtils'

/** 把挂起的待审批项渲染成卡片（挂起事件与刷新恢复共用同一形态） */
export function pushSuspendedCards(msg, ops) {
  for (const op of ops || []) {
    msg.permissions.push({
      id: op.call_id,
      tool: op.name,
      summary: op.summary || `${toolName(op.name)} 需要确认`,
      args_text: formatArgs(op.arguments),
      args: op.arguments || {},
      status: 'pending',
      reason: '',
      risk: op.risk === 'high' ? 'high' : '',
      risk_reason: op.risk_reason || '',
      // 服务端已按「命令类且非高危」给出 rememberable；旧服务回退到名称判断
      rememberable:
        op.rememberable !== undefined
          ? !!op.rememberable
          : op.name === 'bash' || op.name === 'command_tool',
      _focusIndex: 0,
      _suspended: true,
      _decision: null,
    })
  }
}

/** 当前挂起批次的全部卡片（跨消息收集，按 _suspended 标记） */
export function collectSuspendedCards(messages) {
  const out = []
  for (const msg of messages.value) {
    for (const p of msg.permissions || []) {
      if (p._suspended) out.push(p)
    }
  }
  return out
}

/** 挂起批次是否还有未决定的操作/计划/问题（用于禁用输入框/发送键） */
export function hasPendingSuspension(messages) {
  return messages.value.some(
    (m) =>
      (m.permissions || []).some((p) => p._suspended && !p._decision) ||
      (m.planApproval &&
        m.planApproval._suspended &&
        m.planApproval.state === 'pending') ||
      (m.questionRequest &&
        m.questionRequest._suspended &&
        m.questionRequest.state === 'pending'),
  )
}

/** 待审批的请求：当前查看会话的消息里第一个 pending 卡片。 */
export function firstPendingPermission(messages) {
  if (!messages.value) return null
  for (const msg of messages.value) {
    const p = (msg.permissions || []).find((x) => x.status === 'pending')
    if (p) return p
  }
  return null
}

export function findPendingPermission(messages, id) {
  if (!id || !messages.value) return null
  for (const msg of messages.value) {
    const p = (msg.permissions || []).find(
      (x) => x.id === id && x.status === 'pending',
    )
    if (p) return p
  }
  return null
}

/** 把当前挂起卡片标记为「已被服务端处理」（超时自动拒绝/另一端处理） */
export function markSuspendedCardsStale(messages, reason) {
  for (const msg of messages.value) {
    for (const p of msg.permissions || []) {
      if (p._suspended && p.status === 'pending') {
        p.status = 'denied'
        p.reason = p.reason || reason
        p._decision = null
        p._suspended = false
      }
    }
    const plan = msg.planApproval
    if (plan && plan._suspended && plan.state === 'pending') {
      plan.state = 'cancelled'
      plan._suspended = false
    }
    const request = msg.questionRequest
    if (request && request._suspended && request.state === 'pending') {
      request.state = 'cancelled'
      request._suspended = false
    }
  }
}

/**
 * resume 提交失败（HTTP 层，决定未送达后端）：把本批卡片回滚为待决定，
 * 用户可直接重试提交，而不是靠再发一条消息撞 409 才恢复入口。
 * 只回滚仍属挂起批次（_suspended）的卡片；已确认/取消的计划与已答的问题一并复位。
 */
export function resetSuspendedCards(messages) {
  for (const msg of messages.value) {
    for (const p of msg.permissions || []) {
      if (p._suspended && p._decision) {
        p.status = 'pending'
        p._decision = null
        // 用户填写的备注保留，重试时不必重敲
      }
    }
    const plan = msg.planApproval
    if (plan && plan._suspended && plan.state !== 'pending') {
      plan.state = 'pending'
    }
    const request = msg.questionRequest
    if (request && request._suspended && request.state === 'answered') {
      request.state = 'pending'
    }
  }
}
