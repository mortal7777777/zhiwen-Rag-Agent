/** Agent 会话流与运行控制：流式问答/恢复/续跑、挂起审批、上下文与快照、任务清单。 */

import client from './client.js'
import { streamRequest } from './sse.js'

/**
 * Agent SSE 流式对话。
 * payload: { question, conversation_id, use_web_search, use_knowledge_base, template_id }
 * handlers: { onSession, onToolStart, onToolResult, onStreamDelta, onStreamEnd,
 *             onSuspended, onToken, onDone, onError }
 * options:  { signal } 传入 AbortController.signal 可中止请求（停止生成）
 */
export async function streamAgentChat(payload = {}, handlers = {}, options = {}) {
  return streamRequest('/api/agent/stream', payload, handlers, options)
}

/**
 * 恢复挂起的运行（敏感操作审批）。
 * payload: { conversation_id, decisions: [{call_id, approve, reason, remember_session, remember_forever}], stopped }
 * 事件协议与 streamAgentChat 相同：新流续跑同一条回答。
 */
export async function resumeAgentChat(payload = {}, handlers = {}, options = {}) {
  return streamRequest('/api/agent/resume', payload, handlers, options)
}


/** 该会话挂起中的原生审批（刷新/切换会话后恢复审批卡/提示续跑） */
export const getPendingApproval = (conversationId) =>
  client.get(`/agent/pending/${conversationId}`).then((res) => res.data)

/**
 * 续跑被中断的运行（进程被杀/异常退出留下的未完成一轮）：新 SSE 流续跑。
 * 事件协议与 streamAgentChat 相同。
 */
export async function continueAgentRun(conversationId, handlers = {}, options = {}) {
  return streamRequest(`/api/agent/continue/${conversationId}`, {}, handlers, options)
}

/** 丢弃未完成的运行（删除该会话的原生检查点线程），解除新消息闸门 */
export const abandonRun = (conversationId) =>
  client.post(`/agent/abandon/${conversationId}`).then((res) => res.data)

/** 上下文占用统计：消息条数 / token 预算 / 滚动摘要进度 / 缓存命中率 */
export const getAgentContext = (conversationId) =>
  client
    .get(`/agent/context/${conversationId}`, { timeout: 15000 })
    .then((res) => res.data)

/** 手动压缩会话历史：保留近期消息，更早的并入滚动摘要 */
export const compactConversation = (conversationId) =>
  client.post(`/agent/compact/${conversationId}`).then((res) => res.data)

/** 消息级回退：删除该消息及其之后的全部消息（类 Claude Code rewind） */
export const rewindConversation = (conversationId, messageId) =>
  client
    .post(`/conversations/${conversationId}/rewind`, { message_id: messageId })
    .then((res) => res.data)

/** 会话快照时间线（原生检查点，最近 50 条） */
export const getConversationTimeline = (conversationId) =>
  client.get(`/conversations/${conversationId}/timeline`).then((res) => res.data)

/** 回滚会话消息与任务清单到某个快照（下一次提问从该状态继续） */
export const rollbackConversation = (conversationId, checkpointId) =>
  client
    .post(
      `/conversations/${conversationId}/timeline/${encodeURIComponent(checkpointId)}/rollback`,
    )
    .then((res) => res.data)

/** TodoWrite 任务清单：读取/保存某会话的任务 */
export const getTodos = (conversationId) =>
  client.get(`/todos/${conversationId}`).then((res) => res.data)
export const saveTodos = (conversationId, items) =>
  client
    .put(`/todos/${conversationId}`, { items })
    .then((res) => res.data)
