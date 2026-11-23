/** 引用溯源：来源卡片 → 文档查看器；正文 [n] 角标 → 对应来源条目。 */
import { nextTick, ref } from 'vue'
import { ElMessage } from 'element-plus/es/components/message/index'
import { listDocuments } from '../api'

export function useChatViewer({ messages }) {
  // 知识库文档列表缓存（旧索引来源无 relative_path 时按文件名反查）
  let kbDocCache = { at: 0, list: [] }
  const docViewerRef = ref(null)

  async function openSourceDoc(src) {
    let path = src.relative_path
    if (!path) {
      try {
        if (Date.now() - kbDocCache.at > 60_000) {
          kbDocCache.list = await listDocuments()
          kbDocCache.at = Date.now()
        }
        const match = kbDocCache.list.find((d) => d.name === src.source)
        if (!match) {
          ElMessage.warning('未在知识库找到该文档（可能已删除；重建索引后可带精确路径）')
          return
        }
        path = match.relative_path
      } catch {
        ElMessage.error('获取知识库文档列表失败')
        return
      }
    }
    docViewerRef.value?.open(path, {
      page: src.page ?? null,
      anchorText: (src.content || '').slice(0, 120),
    })
  }

  function handleMessageClick(event) {
    const citeEl = event.target.closest('[data-cite]')
    if (!citeEl) return
    const msgEl = event.target.closest('.msg')
    if (!msgEl) return
    const index = Number(msgEl.dataset.msgIndex)
    const msg = messages.value[index]
    if (!msg) return
    const no = Number(citeEl.dataset.cite)
    if (!Array.isArray(msg.sources) || msg.sources.length === 0 || no < 1) return

    // 按来源自身的 index 定位（数组下标不一定等于引用编号）
    let pos = msg.sources.findIndex((src) => Number(src?.index) === no)
    // 仅旧数据（整份列表都没有 index）才退回「第 n 条」；有编号时乱猜会跳错卡片
    const anyIndexed = msg.sources.some((src) => Number.isFinite(Number(src?.index)))
    if (pos < 0 && !anyIndexed && no <= msg.sources.length) pos = no - 1
    if (pos < 0) return

    msg._sourcesOpen = true
    nextTick(() => {
      const items = msgEl.querySelectorAll('.source-item')
      const target = items[pos]
      if (!target) return
      target.scrollIntoView({ behavior: 'smooth', block: 'center' })
      target.classList.add('flash')
      setTimeout(() => target.classList.remove('flash'), 1600)
    })
  }

  return { docViewerRef, openSourceDoc, handleMessageClick }
}
