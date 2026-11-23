/**
 * 会话域：列表/切换/草稿/消息缓存/重命名/删除/回退。
 *
 * 与视图解耦的钩子：
 * - onLeave()：离开当前会话前（停轮询、清挂起批次）；
 * - onEnter(id)：进入会话后（恢复提示词锁定状态）；
 * - onRestorePending(id)：消息加载后（恢复挂起审批/中断提示）；
 * - onNewChat()：新建会话后（清空提示词状态）；
 * - setTemplateFromSession(v)：切换会话回填模板（不触发持久化写入）。
 */
import { nextTick, ref } from 'vue'
import { ElMessage } from 'element-plus/es/components/message/index'
import { ElMessageBox } from 'element-plus/es/components/message-box/index'
import {
  deleteConversation,
  forkConversation,
  getConversationMessages,
  getTodos,
  listConversations,
  pinConversation,
  renameConversation,
  rewindConversation,
} from '../api'
import { mapHistoryMessage } from './useChatUtils'

export function useChatSessions({
  messages,
  loading,
  question,
  questionInput,
  activeId,
  currentTitle,
  sessionCollapsed,
  sessionMessagesCache,
  messageCacheLimit = 5,
  drafts,
  scrollToBottom,
  hooks = {},
}) {
  const sessions = ref([])

  /** 缓存当前会话消息（LRU：重新插入刷新访问序，超限淘汰最旧）。 */
  function cacheMessages(key) {
    sessionMessagesCache.delete(key)
    sessionMessagesCache.set(key, messages.value)
    while (sessionMessagesCache.size > messageCacheLimit) {
      sessionMessagesCache.delete(sessionMessagesCache.keys().next().value)
    }
  }

  function currentDraftKey() {
    return activeId.value ?? 'new'
  }

  function saveDraft() {
    drafts.value[currentDraftKey()] = question.value
  }

  function restoreDraft() {
    question.value = drafts.value[currentDraftKey()] || ''
  }

  function clearDraft() {
    drafts.value[currentDraftKey()] = ''
  }

  async function loadSessions() {
    try {
      sessions.value = await listConversations()
    } catch (error) {
      ElMessage.error(error.response?.data?.detail || '加载会话失败')
    }
  }

  function startNewChat() {
    // 生成中也允许新建：流式消息对象独立于会话数组，切走不影响后台生成
    saveDraft()
    hooks.onLeave?.()
    if (activeId.value != null) cacheMessages(activeId.value)
    activeId.value = null
    currentTitle.value = '新对话'
    hooks.onNewChat?.()
    messages.value = []
    restoreDraft()
  }

  async function switchSession(session) {
    if (session.id === activeId.value) return
    // 保存当前会话的草稿与内存消息（含生成中的内容），再切换
    saveDraft()
    hooks.onLeave?.()
    if (activeId.value != null) cacheMessages(activeId.value)
    activeId.value = session.id
    currentTitle.value = session.title
    // 窄屏（<1024）会话栏为抽屉模式：选中会话后自动收起（媒体查询见样式末尾）
    if (typeof window !== 'undefined' && window.innerWidth < 1024) {
      sessionCollapsed.value = true
    }
    hooks.setTemplateFromSession?.(session.template_id || 0)
    hooks.onEnter?.(session.id)
    const cached = sessionMessagesCache.get(session.id)
    if (cached) {
      // 本会话有内存缓存（正在生成 / 刚切走）：直接用，不重新拉取，
      // streamMsg 继续写入同一个消息对象，切回即看到完整内容
      messages.value = cached
      scrollToBottom(true)
      // 切走时停掉了轮询/清空了挂起：切回来按后端状态重新挂起
      hooks.onRestorePending?.(session.id)
    } else {
      try {
        await reloadMessages(session.id, { scroll: false })
        // 拉取期间用户可能又切走了：A 的响应/挂起卡不能落到 B 上
        if (activeId.value !== session.id) return
        scrollToBottom(true)
        // 挂起中的审批不在 MySQL 里（finalize 才落库）：刷新/切换后按后端状态恢复审批卡
        hooks.onRestorePending?.(session.id)
      } catch (error) {
        if (activeId.value !== session.id) return
        ElMessage.error(error.response?.data?.detail || '加载历史消息失败')
      }
    }
    if (activeId.value === session.id) restoreDraft()
  }

  /** 重新拉取会话消息（可选带任务清单），供回滚/轮询复用 */
  async function reloadMessages(
    conversationId,
    { withTodos = true, scroll = true } = {},
  ) {
    if (activeId.value !== conversationId) return
    const history = await getConversationMessages(conversationId)
    // await 之后必须再校验一次：快速 A→B 切换时 A 的慢响应不能覆盖 B 的消息
    if (activeId.value !== conversationId) return
    messages.value = history.map(mapHistoryMessage)
    if (withTodos) {
      try {
        const todoRes = await getTodos(conversationId)
        if (activeId.value !== conversationId) return
        const lastAssistant = [...messages.value]
          .reverse()
          .find((m) => m.role === 'assistant')
        if (lastAssistant && todoRes.todos?.length) lastAssistant.todos = todoRes.todos
      } catch {
        // 任务清单读取失败不阻塞会话加载
      }
    }
    if (scroll && activeId.value === conversationId) scrollToBottom()
  }

  async function handleRename(session) {
    try {
      const { value } = await ElMessageBox.prompt('输入新的会话标题', '重命名', {
        inputValue: session.title,
        confirmButtonText: '确定',
        cancelButtonText: '取消',
      })
      const updated = await renameConversation(session.id, value.trim())
      session.title = updated.title
      if (session.id === activeId.value) {
        currentTitle.value = updated.title
      }
    } catch (error) {
      if (error !== 'cancel' && error !== 'close') {
        ElMessage.error(error.response?.data?.detail || '重命名失败')
      }
    }
  }

  async function handleDelete(session) {
    try {
      await ElMessageBox.confirm(
        `确定删除「${session.title}」吗？其全部对话记录将被删除。`,
        '提示',
        { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消' },
      )
    } catch {
      return
    }
    try {
      await deleteConversation(session.id)
      // 清理该会话的内存缓存与草稿
      sessionMessagesCache.delete(session.id)
      delete drafts.value[session.id]
      if (session.id === activeId.value) {
        startNewChat()
      }
      hooks.onSessionDeleted?.(session.id)
      await loadSessions()
      ElMessage.success('会话已删除')
    } catch (error) {
      ElMessage.error(error.response?.data?.detail || '删除失败')
    }
  }

  /** 置顶 / 取消置顶：切换后重拉列表（服务端负责置顶组排序） */
  async function handlePin(session) {
    try {
      await pinConversation(session.id, !session.pinned)
      await loadSessions()
      ElMessage.success(session.pinned ? '已取消置顶' : '已置顶')
    } catch (error) {
      ElMessage.error(error.response?.data?.detail || '置顶操作失败')
    }
  }

  function upsertSession(data) {
    const index = sessions.value.findIndex((s) => s.id === data.conversation_id)
    const item = {
      id: data.conversation_id,
      title: data.title || '新对话',
      message_count: 0,
    }
    if (index >= 0) {
      if (data.title) sessions.value[index].title = data.title
    } else if (data.conversation_id) {
      sessions.value.unshift(item)
    }
  }

  /** 消息级回退：删除该消息及其之后的所有对话，内容放回输入框重新编辑 */
  async function rewindTo(msg) {
    if (loading.value || !activeId.value || !msg.id) return
    try {
      await ElMessageBox.confirm(
        '将删除这条消息及其之后的所有对话记录，并把内容放回输入框，确定回退？',
        '回退确认',
        { type: 'warning', confirmButtonText: '回退', cancelButtonText: '取消' },
      )
    } catch {
      return
    }
    try {
      const result = await rewindConversation(activeId.value, msg.id)
      await reloadMessages(activeId.value, { scroll: false })
      question.value = msg.content || ''
      const extra = result.summary_reset ? '，滚动摘要已重置' : ''
      ElMessage.success(
        `已回退（删除 ${result.removed} 条消息${extra}），内容已放回输入框`,
      )
      nextTick(() => questionInput.value?.focus())
    } catch (error) {
      ElMessage.error(error.response?.data?.detail || '回退失败')
    }
  }

  /**
   * 会话级分支：从该用户消息（含）复制出一个新会话并切过去。
   * 后端保留模板/工作区/提示词锁、重置摘要；消息按原 id 顺序复制，前缀
   * 字节与源会话一致（提示词锁相同才可能继续命中 provider 缓存）。
   */
  async function forkFrom(msg) {
    if (loading.value || !activeId.value || !msg?.id) return
    try {
      const forked = await forkConversation(activeId.value, msg.id)
      await loadSessions()
      await switchSession(forked)
      ElMessage.success(`已创建分支会话（复制 ${forked.message_count} 条消息）`)
    } catch (error) {
      ElMessage.error(error.response?.data?.detail || '创建分支失败')
    }
  }

  return {
    sessions,
    loadSessions,
    startNewChat,
    switchSession,
    handleRename,
    handlePin,
    handleDelete,
    upsertSession,
    rewindTo,
    forkFrom,
    reloadMessages,
    currentDraftKey,
    saveDraft,
    restoreDraft,
    clearDraft,
  }
}
