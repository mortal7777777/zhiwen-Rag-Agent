/** 一轮对话的发送/停止/重试：把输入状态组装成 /agent/stream 请求。 */
import { ElMessage } from 'element-plus/es/components/message/index'
import { cancelAgentRun, streamAgentChat } from '../api'
import { nextLocalMsgKey } from './useChatUtils'

export function useChatStream({
  activeId,
  question,
  messages,
  loading,
  setActivity,
  clearActivity,
  turn,
  typewriter,
  attachments,
  prefs,
  templateId,
  kbScope,
  quote,
  projectDir,
  suspension,
  scrollFollow,
  scrollToBottom,
  reloadSessions,
  reloadPrompt,
  clearDraft,
}) {
  function stop() {
    // 方案 A（2026-11-19）：断连不再自动中止——「停止」先显式取消后端运行
    // （cancel 端点置 stop 信号，CLI 同款），再断开本地 SSE；
    // runId 由 session 事件经 createStreamHandlers 捕获到 turn.runId
    if (turn.runId) {
      cancelAgentRun(turn.runId).catch(() => {})
    }
    turn.abortController?.abort()
  }

  /** 重新生成最后一条回答（复用其对应的用户问题） */
  function regenerateLast() {
    if (loading.value) return
    // 必须在 pop 之前校验：挂起中最后一条助手消息带着审批/问答卡，
    // 先删再被 send() 拒绝会丢掉决定入口。
    if (suspension.hasSuspendedPending.value) {
      ElMessage.warning(
        suspension.autoRunning.value
          ? '上一轮还在后台执行，请稍候…'
          : '请先处理待确认的操作（批准或拒绝后自动继续）',
      )
      return
    }
    if (attachments.uploadingAttachments.value) {
      ElMessage.warning('附件还在上传，请稍候再重新生成')
      return
    }
    const last = messages.value[messages.value.length - 1]
    if (!last || last.role !== 'assistant') return
    let userMsg = null
    for (let i = messages.value.length - 2; i >= 0; i--) {
      if (messages.value[i].role === 'user') {
        userMsg = messages.value[i]
        break
      }
    }
    if (!userMsg) return
    messages.value.pop()
    question.value = userMsg.content
    send({ skipUserPush: true })
  }

  function ask(suggestion) {
    question.value = suggestion.text
    send()
  }

  async function send(options = {}) {
    typewriter.flush() // 清理上一轮残留（如异常中断）
    if (attachments.uploadingAttachments.value) {
      ElMessage.warning('附件还在上传，请稍候再发送')
      return
    }
    const hasImages = attachments.attachedImages.value.length > 0
    const hasFiles = attachments.attachedFiles.value.length > 0
    const text =
      question.value.trim() ||
      (hasImages ? '请识别并描述这张图片' : '') ||
      (hasFiles ? '请阅读并分析我上传的文件' : '')
    if (!text || loading.value) return
    if (suspension.hasSuspendedPending.value) {
      ElMessage.warning(
        suspension.autoRunning.value
          ? '上一轮还在后台执行，请稍候…'
          : '请先处理待确认的操作（批准或拒绝后自动继续）',
      )
      return
    }
    const imageDataUrls = attachments.attachedImages.value.map((img) => img.dataUrl)
    const attachmentIds = attachments.attachedFiles.value.map((file) => file.id)
    const fileMeta = attachments.attachedFiles.value.map((file) => ({
      name: file.name,
      size: file.size,
      chars: file.chars,
    }))
    // 先快照引用块：下面会清空输入区，payload 必须用快照
    const quotePayload = quote.value
      ? {
          text: quote.value.text,
          context: quote.value.context || '',
          source: quote.value.source || '',
          page: quote.value.page ?? null,
        }
      : null

    if (!options.skipUserPush) {
      messages.value.push({
        role: 'user',
        content: text,
        images: imageDataUrls,
        files: fileMeta,
        _localKey: nextLocalMsgKey(),
      })
    }
    messages.value.push({
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
      _streaming: true,
      _localKey: nextLocalMsgKey(),
    })
    // 用响应式代理引用消息：typewriter 每帧增量更新 content 时能触发渲染
    turn.streamMsg = messages.value[messages.value.length - 1]
    // 本轮是否已被后端接受（收到 session 事件）：失败时据此决定回滚还是留痕
    turn.sessionReceived = false
    // 失败回滚快照：下面会清空输入区，回滚时原样放回
    const rollbackImages = [...attachments.attachedImages.value]
    const rollbackFiles = [...attachments.attachedFiles.value]
    typewriter.resetRound()
    question.value = ''
    // 发送成功：清空该会话草稿（内容已发出）
    clearDraft()
    attachments.clearAttachments()
    loading.value = true
    setActivity('prepare', '正在理解问题…')
    turn.abortController = new AbortController()
    scrollFollow.value = true
    scrollToBottom(true)

    // 根据开关状态计算后端 tool_mode
    let effectiveMode = 'none'
    if (
      prefs.toolMode.value === 'auto' ||
      (prefs.useWebSearch.value && prefs.useKnowledgeBase.value)
    ) {
      effectiveMode = 'auto'
    } else if (prefs.useWebSearch.value) {
      effectiveMode = 'web'
    } else if (prefs.useKnowledgeBase.value) {
      effectiveMode = 'knowledge'
    }
    // 选择限定范围时强制启用知识库工具（否则后端不会构建检索工具）
    if (kbScope.value) {
      effectiveMode = prefs.useWebSearch.value ? 'auto' : 'knowledge'
    }
    const payload = {
      question: text,
      images: imageDataUrls,
      attachment_ids: attachmentIds,
      conversation_id: activeId.value,
      tool_mode: effectiveMode,
      template_id: templateId.value || null,
      // 会话工作区（project_dir）：新会话由后端建会话时持久化，
      // 已有会话等值时后端 no-op、不同值时跟随更新
      project_dir: projectDir?.value || null,
    }
    if (kbScope.value) {
      payload.kb_scope =
        kbScope.value.type === 'docs'
          ? { type: 'docs', values: [...(kbScope.value.values || [])] }
          : { type: kbScope.value.type, value: kbScope.value.value }
    }
    if (quotePayload) {
      payload.quote = quotePayload
      quote.value = null
    }
    try {
      // 包一层 onSession：标记本轮已被后端接受（失败回滚的判据）
      const handlers = suspension.buildStreamHandlers()
      const originOnSession = handlers.onSession
      if (originOnSession) {
        handlers.onSession = (data) => {
          turn.sessionReceived = true
          originOnSession(data)
        }
      }
      await streamAgentChat(payload, handlers, {
        signal: turn.abortController.signal,
      })
    } catch (error) {
      typewriter.flush()
      if (error.name === 'AbortError' || error.message === 'aborted') {
        // 用户主动停止生成：撤回本轮未完成的正文（与后端"不落半截回答"一致），
        // 早前轮次已确认的正文保留
        typewriter.drop()
        if (turn.streamMsg) {
          turn.streamMsg.content = turn.streamMsg.content || '（已停止生成）'
        }
      } else {
        const detail = error.message || '请求失败，请确认后端服务已启动'
        if (!turn.sessionReceived) {
          // HTTP 层就被拒（409 挂起/中断闸门、400、断网）：本轮没有落库，
          // 回滚刚 push 的消息与输入区快照，避免留下刷新即消失的"幽灵轮次"
          if (
            turn.streamMsg &&
            messages.value[messages.value.length - 1] === turn.streamMsg
          ) {
            const removed = options.skipUserPush ? 1 : 2
            messages.value.splice(messages.value.length - removed, removed)
          }
          turn.streamMsg = null
          question.value = text
          attachments.attachedImages.value.push(...rollbackImages)
          attachments.attachedFiles.value.push(...rollbackFiles)
          if (quotePayload) quote.value = quotePayload
        } else if (turn.streamMsg) {
          turn.streamMsg.content = turn.streamMsg.content || `请求失败：${detail}`
        }
        ElMessage.error(detail)
        // 结构化错误码驱动恢复（后端 X-Zhiwen-Code；此前靠匹配中文文案，改文案即失效）
        if (error.code === 'pending_approval' || error.code === 'run_interrupted') {
          // 后端闸门：该会话还有挂起审批/中断未跑完 → 恢复卡片或提示条
          await suspension.restorePendingApproval(activeId.value)
        }
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

  return { send, stop, regenerateLast, ask }
}
