/**
 * 头部快捷切换：供应商 / 思考强度 / 敏感操作权限模式。
 * 读设置快照，切换走轻量端点（只清对话缓存，不触发嵌入/重排重载）。
 * 流式生成中禁用。
 */
import { computed, inject, ref } from 'vue'
import { ElMessage } from 'element-plus/es/components/message/index'
import {
  getSettings,
  switchChatProvider,
  switchChatThinking,
  switchToolPermissionMode,
} from '../api'

const PERMISSION_LABELS = {
  ask: '每次确认',
  acceptedits: '自动接受编辑',
  allow: '自动批准',
}
const THINKING_LABELS = { low: '低', medium: '中', high: '高', max: '最高' }
const THINKING_OPTIONS = [
  { value: 'low', label: '低' },
  { value: 'medium', label: '中' },
  { value: 'high', label: '高（默认）' },
  { value: 'max', label: '最高' },
]

export function useQuickSwitch() {
  const quickProviders = ref([])
  const quickActiveId = ref('')
  const quickModel = ref('')
  const quickBusy = ref(false)
  const quickPermissionMode = ref('ask')
  const quickThinkingEnabled = ref(true)
  const quickThinkingEffort = ref('high')
  const modeBusy = ref(false)
  const thinkingBusy = ref(false)
  const openSettingsTab = inject('openSettingsTab', null)

  const quickActiveName = computed(
    () => quickProviders.value.find((p) => p.id === quickActiveId.value)?.name || '',
  )
  const quickPermissionLabel = computed(
    () => PERMISSION_LABELS[quickPermissionMode.value] || PERMISSION_LABELS.ask,
  )
  const quickThinkingLabel = computed(() => {
    if (!quickThinkingEnabled.value) return '关闭'
    return THINKING_LABELS[quickThinkingEffort.value] || '高'
  })

  async function loadQuickProviders() {
    try {
      const snapshot = await getSettings()
      quickProviders.value = (snapshot.providers || []).filter(
        (p) => p.type === 'chat' && p.enabled,
      )
      quickActiveId.value =
        snapshot.active_chat_provider || quickProviders.value[0]?.id || ''
      quickPermissionMode.value =
        (snapshot.editable || {}).tool_permission_mode || 'ask'
      syncQuickModel()
    } catch {
      /* 后端未就绪时静默，不影响对话主流程 */
    }
  }

  function syncQuickModel() {
    const active = quickProviders.value.find((p) => p.id === quickActiveId.value)
    quickModel.value = active?.model || ''
    quickThinkingEnabled.value = active ? active.thinking_enabled !== false : true
    quickThinkingEffort.value = active?.thinking_effort || 'high'
  }

  async function onQuickSwitch(command) {
    if (!command) return
    if (command === '__manage__') {
      openSettingsTab?.('models')
      return
    }
    if (command === quickActiveId.value) return
    quickBusy.value = true
    try {
      const res = await switchChatProvider(command)
      quickActiveId.value = res.active_chat_provider || command
      syncQuickModel()
      ElMessage.success(
        `已切换到 ${quickActiveName.value || command}（对后续消息生效）`,
      )
    } catch (error) {
      ElMessage.error(error.response?.data?.detail || '切换失败')
      await loadQuickProviders()
    } finally {
      quickBusy.value = false
    }
  }

  async function onPermissionModeSwitch(mode) {
    if (!mode || modeBusy.value || mode === quickPermissionMode.value) return
    modeBusy.value = true
    try {
      const res = await switchToolPermissionMode(mode)
      quickPermissionMode.value = res.tool_permission_mode || mode
      ElMessage.success(
        `权限模式：${PERMISSION_LABELS[quickPermissionMode.value] || mode}（对后续消息生效）`,
      )
    } catch (error) {
      ElMessage.error(error.response?.data?.detail || '切换权限模式失败')
      await loadQuickProviders()
    } finally {
      modeBusy.value = false
    }
  }

  async function onThinkingSwitch(command) {
    if (!command || thinkingBusy.value) return
    const enabled = command !== 'off'
    const effort = enabled ? command : quickThinkingEffort.value
    if (enabled && quickThinkingEnabled.value && quickThinkingEffort.value === effort) {
      return
    }
    if (!enabled && !quickThinkingEnabled.value) return
    thinkingBusy.value = true
    try {
      const res = await switchChatThinking(
        enabled
          ? { thinking_enabled: true, thinking_effort: effort }
          : { thinking_enabled: false },
      )
      quickThinkingEnabled.value = res.thinking_enabled !== false
      quickThinkingEffort.value = res.thinking_effort || effort
      const label = quickThinkingEnabled.value
        ? THINKING_LABELS[quickThinkingEffort.value] || quickThinkingEffort.value
        : '关闭'
      ElMessage.success(`思考：${label}（对后续消息生效）`)
    } catch (error) {
      ElMessage.error(error.response?.data?.detail || '切换思考强度失败')
      await loadQuickProviders()
    } finally {
      thinkingBusy.value = false
    }
  }

  return {
    quickProviders,
    quickActiveId,
    quickModel,
    quickBusy,
    quickActiveName,
    quickPermissionMode,
    quickPermissionLabel,
    quickThinkingEnabled,
    quickThinkingEffort,
    quickThinkingLabel,
    thinkingOptions: THINKING_OPTIONS,
    modeBusy,
    thinkingBusy,
    loadQuickProviders,
    onQuickSwitch,
    onPermissionModeSwitch,
    onThinkingSwitch,
  }
}
