/**
 * 打字机 + 增量流的三个开关（从 ChatView 抽出，2026-10-09）。
 *
 * 职责：
 *   - `pendingText`  后端 token/增量的缓冲区（不直接进消息，按节奏刷入）；
 *   - `start/flush`  24ms 一帧、单帧 1~20 字符（积压多时自适应加速）的节奏控制；
 *   - `drop`         工具轮过渡文本 / 停止生成时，把本轮已推送字符从流的尾部撤回
 *                    （先扣缓冲，不足再从正文尾部扣）；
 *   - `streamedLen` / `skipNextToken` 是本轮流式状态，必须**实时读写**（不能被值捕获）。
 *
 * 依赖注入（不 import 组件）：`getStreamMsg()` 取当前助手消息对象（每轮流开始前被
 * 指向正确的气泡）、`scrollToBottom()` 是滚动副作用。
 *
 * 可单测：`node frontend/scripts/check_typewriter.mjs`
 */
import { ref } from 'vue'

export function useTypewriter({ getStreamMsg, scrollToBottom }) {
  const pendingText = ref('')
  let timer = null
  // 本轮 stream_delta 已推送字符数（工具轮撤回用）
  let streamedLen = 0
  // 「跳过随后全文 token」标记（正文轮已增量显示，token 是同一段全文）
  let skipNextToken = false

  function flush() {
    if (timer) {
      clearInterval(timer)
      timer = null
    }
    const msg = getStreamMsg()
    if (msg && pendingText.value) {
      msg.content += pendingText.value
      pendingText.value = ''
      scrollToBottom()
    }
  }

  // 撤回本轮增量推送的字符：已推送的字符在流的最尾部，
  // 先扣未显示的缓冲区，不足的部分再从消息正文尾部扣
  function drop() {
    const msg = getStreamMsg()
    if (!msg || !streamedLen) {
      streamedLen = 0
      return
    }
    const fromPending = Math.min(streamedLen, pendingText.value.length)
    pendingText.value = pendingText.value.slice(
      0,
      pendingText.value.length - fromPending,
    )
    const rest = streamedLen - fromPending
    if (rest > 0) {
      msg.content = msg.content.slice(0, Math.max(0, msg.content.length - rest))
    }
    streamedLen = 0
  }

  function start() {
    const msg = getStreamMsg()
    if (timer || !msg) return
    let scrollCounter = 0
    timer = setInterval(() => {
      const current = getStreamMsg()
      if (!pendingText.value) {
        flush()
        return
      }
      if (!current) {
        flush()
        return
      }
      // 24ms 一帧，单帧最多 20 字符：
      // - 稳态约 35~70 字符/秒，有清晰打字机观感；
      // - 后端增量流式时积压持续增长，上限 20（≈800 字符/秒）保证打字机
      //   不落后于生成速度，避免末尾一次性倾泻
      const n = Math.max(1, Math.min(20, Math.round(pendingText.value.length / 10)))
      current.content += pendingText.value.slice(0, n)
      pendingText.value = pendingText.value.slice(n)
      // 滚动节流：隔帧滚动一次，避免每 24ms 触发滚动导致底部抖动闪烁
      scrollCounter = (scrollCounter + 1) % 2
      if (scrollCounter === 0) scrollToBottom()
    }, 24)
  }

  /** 新一轮开始前清空残留（异常中断/切会话时用） */
  function resetRound() {
    pendingText.value = ''
    streamedLen = 0
    skipNextToken = false
  }

  /** 组件卸载：停掉定时器，避免回调打到已销毁的组件上 */
  function dispose() {
    if (timer) {
      clearInterval(timer)
      timer = null
    }
  }

  return {
    pendingText,
    start,
    flush,
    drop,
    resetRound,
    dispose,
    getStreamedLen: () => streamedLen,
    setStreamedLen: (v) => {
      streamedLen = v
    },
    getSkipNextToken: () => skipNextToken,
    setSkipNextToken: (v) => {
      skipNextToken = v
    },
  }
}
