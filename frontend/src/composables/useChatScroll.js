/** 消息区滚动：跟随底部 + 每帧合并滚动，避免高频事件造成抖动。 */
import { ref } from 'vue'

export function useChatScroll({ messagesRef }) {
  // 是否跟随最新内容自动滚动：用户上滑查看时暂停跟随，滚回底部后恢复
  const scrollFollow = ref(true)
  let scrollRaf = null

  function onMessagesScroll() {
    const el = messagesRef.value
    if (!el) return
    const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 100
    scrollFollow.value = atBottom
  }

  function scrollToBottom(force = false) {
    if (scrollRaf) return
    scrollRaf = requestAnimationFrame(() => {
      scrollRaf = null
      if (messagesRef.value && (force || scrollFollow.value)) {
        messagesRef.value.scrollTop = messagesRef.value.scrollHeight
      }
    })
  }

  return { scrollFollow, onMessagesScroll, scrollToBottom }
}
