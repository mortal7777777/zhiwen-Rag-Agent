/**
 * 输入区偏好持久化：自动/手动模式、联网/知识库开关、模板、侧栏折叠。
 * 存 localStorage，组件重建后恢复；浏览器禁用存储时静默忽略。
 */
import { ref, watch } from 'vue'

const PREFS_KEY = 'rag_chat_prefs'
const PANEL_KEY = 'rag_session_collapsed'

export function useChatPreferences({ templateId, sessionCollapsed }) {
  // auto=自动（两者都启用）/ manual=手动
  const toolMode = ref('auto')
  const useWebSearch = ref(false)
  const useKnowledgeBase = ref(false)

  function loadPrefs() {
    try {
      const raw = localStorage.getItem(PREFS_KEY)
      if (raw) {
        const prefs = JSON.parse(raw)
        if (prefs.toolMode === 'auto' || prefs.toolMode === 'manual') {
          toolMode.value = prefs.toolMode
        }
        if (toolMode.value === 'auto') {
          useWebSearch.value = true
          useKnowledgeBase.value = true
        } else {
          useWebSearch.value = !!prefs.useWebSearch
          useKnowledgeBase.value = !!prefs.useKnowledgeBase
        }
        if (typeof prefs.templateId === 'number') {
          templateId.value = prefs.templateId
        }
      }
      sessionCollapsed.value = localStorage.getItem(PANEL_KEY) === '1'
    } catch {
      // 浏览器禁用 localStorage 时忽略，不影响使用
    }
  }

  function savePrefs() {
    try {
      localStorage.setItem(
        PREFS_KEY,
        JSON.stringify({
          toolMode: toolMode.value,
          useWebSearch: useWebSearch.value,
          useKnowledgeBase: useKnowledgeBase.value,
          templateId: templateId.value,
        }),
      )
      localStorage.setItem(PANEL_KEY, sessionCollapsed.value ? '1' : '0')
    } catch {
      // ignore
    }
  }

  watch(toolMode, (value) => {
    // 打开自动 = 联网/知识库都开启；关闭自动 = 都不开启（可再单独勾选）
    if (value === 'auto') {
      useWebSearch.value = true
      useKnowledgeBase.value = true
    } else {
      useWebSearch.value = false
      useKnowledgeBase.value = false
    }
  })

  watch(
    [toolMode, useWebSearch, useKnowledgeBase, templateId, sessionCollapsed],
    savePrefs,
  )

  return { toolMode, useWebSearch, useKnowledgeBase, loadPrefs }
}
