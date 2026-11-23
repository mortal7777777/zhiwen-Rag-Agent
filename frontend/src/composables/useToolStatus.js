/** 运行状态条：相位 + 当前活动 + 已持续秒数，相位内部轮换文案。 */
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { pickActivityLabel } from '../utils/activityCopy'

export function useToolStatus() {
  const currentTool = ref('') // 当前活动明细（工具名/阶段文案）
  const statusPhase = ref('') // prepare / thinking / tool / finalizing…
  const statusElapsed = ref(0)
  let statusTickId = null

  function startTicker() {
    if (!statusTickId) {
      statusTickId = window.setInterval(() => {
        statusElapsed.value += 1
      }, 1000)
    }
  }

  function stopTicker() {
    if (statusTickId) {
      window.clearInterval(statusTickId)
      statusTickId = null
    }
  }

  /** 事件驱动地更新相位；只有相位/文案变化才重置计时（流式增量不重置）。 */
  function setActivity(phase, text) {
    const nextPhase = phase || 'thinking'
    const nextText = text || ''
    const changed =
      nextPhase !== statusPhase.value || nextText !== currentTool.value
    statusPhase.value = nextPhase
    currentTool.value = nextText
    if (changed) statusElapsed.value = 0
    if (nextText) startTicker()
    else stopTicker()
  }

  function clearActivity() {
    currentTool.value = ''
    statusPhase.value = ''
    statusElapsed.value = 0
    stopTicker()
  }

  // 兼容仍有直接写 currentTool 的调用：写入即启动计时
  watch(currentTool, (val) => {
    if (val) startTicker()
    else stopTicker()
  })

  onBeforeUnmount(stopTicker)

  const activityLabel = computed(() =>
    currentTool.value
      ? pickActivityLabel(statusPhase.value, currentTool.value, statusElapsed.value)
      : '',
  )

  return {
    currentTool,
    statusPhase,
    statusElapsed,
    activityLabel,
    setActivity,
    clearActivity,
  }
}
