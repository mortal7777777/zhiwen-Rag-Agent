/** 欢迎页建议问题：后端生成（知识库 / 热点 / 最近任务），失败时回退静态三条。 */
import { markRaw, ref } from 'vue'
import { Collection, Clock, MagicStick, Position } from '@element-plus/icons-vue'
import { getSuggestions } from '../api'

const suggestionIcons = {
  kb: Collection,
  news: Position,
  general: MagicStick,
  recent: Clock,
}
const suggestionDescs = {
  kb: '检索知识库文档，回答附带出处',
  news: '联网检索最新资讯',
  general: '通用对话、写作与润色',
  recent: '接着最近的任务继续',
}

export function useSuggestions() {
  const suggestions = ref([])

  async function loadSuggestions() {
    try {
      const items = await getSuggestions()
      suggestions.value = items.map((item) => ({
        // markRaw：图标是组件对象，进深响应式 ref 会触发 Vue 的
        // "Component made reactive" 警告（实测聊天页 3 条/次）
        icon: markRaw(suggestionIcons[item.type] || MagicStick),
        text: item.text,
        desc: suggestionDescs[item.type] || '直接开始对话',
      }))
    } catch {
      suggestions.value = [
        {
          icon: markRaw(Collection),
          text: '知识库里的文档主要讲了什么？',
          desc: suggestionDescs.kb,
        },
        { icon: markRaw(MagicStick), text: '帮我写一封简洁的工作周报', desc: suggestionDescs.general },
        { icon: markRaw(Position), text: '今天有什么值得关注的新闻？', desc: suggestionDescs.news },
      ]
    }
  }

  return { suggestions, loadSuggestions }
}
