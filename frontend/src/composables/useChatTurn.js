/**
 * 单轮流式状态：目标助手消息、打字机与共享取消句柄。
 *
 * streamMsg 用可变对象持有（而不是 ref）：send / resume / continue 都可能在
 * 流进行中切换目标消息，处理器通过 getter 每次读最新值。
 */
import { reactive } from 'vue'
import { useTypewriter } from './useTypewriter'

export function useChatTurn({ scrollToBottom }) {
  // reactive：runId 由 SSE session 事件写入，子代理取消按钮要能响应变化
  const turn = reactive({
    streamMsg: null,
    abortController: null,
    runId: null,
  })

  const typewriter = useTypewriter({
    getStreamMsg: () => turn.streamMsg,
    scrollToBottom,
  })

  /** 结束当前打开的思考段（文本开始输出 / 工具开始 / 收尾时调用） */
  function closeThinkingSegment() {
    const msg = turn.streamMsg
    if (!msg || !msg.tool_trace) return
    const trace = msg.tool_trace
    const seg = trace[trace.length - 1]
    if (seg && seg.kind === 'thinking' && !seg.done) {
      seg.done = true
      seg.duration = Math.max(
        1,
        Math.round((Date.now() - (seg._startedAt || Date.now())) / 1000),
      )
    }
  }

  return {
    turn,
    typewriter,
    pendingText: typewriter.pendingText,
    flushTypewriter: typewriter.flush,
    dropStreamedRound: typewriter.drop,
    startTypewriter: typewriter.start,
    closeThinkingSegment,
  }
}
