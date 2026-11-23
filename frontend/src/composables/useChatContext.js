/** 上下文占用面板（/context · /compact）与会话快照时间线（回滚）。 */
import { computed, ref } from 'vue'
import { ElMessage } from 'element-plus/es/components/message/index'
import { ElMessageBox } from 'element-plus/es/components/message-box/index'
import {
  compactConversation,
  getAgentContext,
  getConversationTimeline,
  rollbackConversation,
} from '../api'
import { formatCheckpointTime } from './useChatUtils'

export function useChatContext({
  activeId,
  loading,
  reloadMessages,
  scrollToBottom,
}) {
  const contextVisible = ref(false)
  const contextStats = ref(null)
  const compacting = ref(false)

  // 下一轮实际发给模型的历史条数（后端 effective_history_messages；
  // 会话累计 message_count 含已并入滚动摘要的部分，两者通常不同）
  const ctxEffectiveMessages = computed(
    () =>
      contextStats.value?.effective_history_messages ??
      contextStats.value?.message_count ??
      0,
  )
  // 下一轮实际发送的 token 估算（压缩切点 + trim 之后；旧后端回退整窗估算）
  const ctxSentTokens = computed(
    () =>
      contextStats.value?.sent_estimate_tokens ??
      contextStats.value?.estimated_tokens ??
      0,
  )
  const ctxWindowTokens = computed(
    () => contextStats.value?.estimated_tokens ?? ctxSentTokens.value,
  )
  // 压缩触发水位（1.5×上限）：窗口超过它后压缩到上限条数，
  // 画进度条按水位对比，避免"70/60"这类超 100% 的显示
  const ctxHighWater = computed(
    () =>
      contextStats.value?.history_high_water_messages ??
      contextStats.value?.history_max_messages ??
      1,
  )

  function barPct(cur, cap) {
    if (!cap) return '0%'
    return `${Math.min(100, Math.round((cur / cap) * 100))}%`
  }

  function fmtCtxTokens(n) {
    if (n == null) return '-'
    if (n >= 1000000) return `${(n / 1000000).toFixed(1)}M`
    if (n >= 1000) return `${(n / 1000).toFixed(1)}k`
    return String(n)
  }

  // 请求序号：关闭/重开对话框时使进行中的请求失效，防止旧请求
  // 超时后误关新打开的对话框（竞态）
  let contextReqId = 0

  async function openContextDialog() {
    if (!activeId.value) return
    const reqId = ++contextReqId
    contextVisible.value = true
    contextStats.value = null
    try {
      const stats = await getAgentContext(activeId.value)
      if (reqId === contextReqId) contextStats.value = stats
    } catch (error) {
      if (reqId !== contextReqId) return // 过期请求（用户已关闭/重开），忽略
      ElMessage.error(error.response?.data?.detail || '读取上下文统计失败')
      contextVisible.value = false
    }
  }

  function onContextClose() {
    // 关闭时作废进行中的请求：15s 超时回调不会再触碰对话框状态
    contextReqId++
  }

  // 右侧会话面板的静默取数：不动对话框、失败不弹错；
  // 独立序号，避免与对话框请求互相作废；await 后校验会话没被切走。
  let panelReqId = 0

  async function loadContextStats(id = activeId.value) {
    if (id == null) return null
    const reqId = ++panelReqId
    try {
      const stats = await getAgentContext(id)
      if (reqId !== panelReqId) return null
      if (String(id) !== String(activeId.value)) return null
      contextStats.value = stats
      return stats
    } catch {
      return null
    }
  }

  const timelineVisible = ref(false)
  const timelineLoading = ref(false)
  const timelineItems = ref([])

  async function openTimeline() {
    if (!activeId.value) return
    timelineVisible.value = true
    timelineLoading.value = true
    try {
      const res = await getConversationTimeline(activeId.value)
      timelineItems.value = res.items || []
    } catch (error) {
      ElMessage.error(error.response?.data?.detail || '读取快照失败')
      timelineItems.value = []
    } finally {
      timelineLoading.value = false
    }
  }

  async function rollbackTo(cp) {
    if (!activeId.value || loading.value) return
    try {
      await ElMessageBox.confirm(
        `回滚把消息历史与任务清单恢复到该快照（${formatCheckpointTime(cp.created_at)}，` +
          `消息 ${cp.messages} 条）；比它新的消息会从会话里删除，仍可从更新的快照回滚回来。确定继续？`,
        '回滚到快照',
        { type: 'warning', confirmButtonText: '回滚', cancelButtonText: '取消' },
      )
    } catch {
      return
    }
    try {
      const res = await rollbackConversation(activeId.value, cp.checkpoint_id)
      await reloadMessages(activeId.value)
      timelineVisible.value = false
      ElMessage.success(`已回滚（恢复 ${res.restored_messages} 条消息）`)
      scrollToBottom(true)
    } catch (error) {
      ElMessage.error(error.response?.data?.detail || '回滚失败')
    }
  }

  async function handleCompact() {
    if (!activeId.value || compacting.value) return
    compacting.value = true
    try {
      const result = await compactConversation(activeId.value)
      if (result.compacted) {
        ElMessage.success(
          `已压缩：保留最近 ${result.kept_messages} 条，${result.summarized_messages} 条并入摘要`,
        )
      } else {
        ElMessage.info(result.reason || '无需压缩')
      }
      contextStats.value = await getAgentContext(activeId.value)
    } catch (error) {
      ElMessage.error(error.response?.data?.detail || '压缩失败')
    } finally {
      compacting.value = false
    }
  }

  return {
    contextVisible,
    contextStats,
    compacting,
    ctxEffectiveMessages,
    ctxSentTokens,
    ctxWindowTokens,
    ctxHighWater,
    barPct,
    fmtCtxTokens,
    openContextDialog,
    loadContextStats,
    onContextClose,
    timelineVisible,
    timelineLoading,
    timelineItems,
    openTimeline,
    formatCheckpointTime,
    rollbackTo,
    handleCompact,
  }
}
