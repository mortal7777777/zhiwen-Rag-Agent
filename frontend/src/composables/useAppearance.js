/**
 * 外观状态：主题 / 内容宽度 / 界面风格（从 App.vue 抽出，2026-10-09 设置页拆分）。
 *
 * 全局生效：直接写 documentElement（class/dataset/CSS 变量）+ localStorage 持久化，
 * 因此由 App.vue 持有**唯一实例**（启动时 init() 读取 + mount() 应用），设置页
 * 外观面板通过 inject('settingsAppearance') 读写同一份状态。
 * 选项清单（themeOptions 等）属于展示数据，留在面板组件里。
 */
import { ref, watch } from 'vue'

const THEME_KEY = 'rag_theme'
const WIDTH_KEY = 'rag_content_width'
const STYLE_KEY = 'rag_style'

export function useAppearance() {
  const theme = ref('system')
  const contentWidth = ref('1200px')
  const styleMode = ref('default')

  function applyStyle() {
    if (styleMode.value === 'default') {
      delete document.documentElement.dataset.style
    } else {
      document.documentElement.dataset.style = styleMode.value
    }
  }

  function resolveDark() {
    if (theme.value === 'dark') return true
    if (theme.value === 'light') return false
    return window.matchMedia('(prefers-color-scheme: dark)').matches
  }

  function applyTheme() {
    const dark = resolveDark()
    document.documentElement.classList.toggle('dark', dark)
    document.documentElement.dataset.theme = dark ? 'dark' : 'light'
  }

  function applyWidth() {
    document.documentElement.style.setProperty(
      '--chat-content-width',
      contentWidth.value,
    )
  }

  watch(styleMode, () => {
    applyStyle()
    localStorage.setItem(STYLE_KEY, styleMode.value)
  })
  watch(theme, () => {
    applyTheme()
    localStorage.setItem(THEME_KEY, theme.value)
  })
  watch(contentWidth, () => {
    applyWidth()
    localStorage.setItem(WIDTH_KEY, contentWidth.value)
  })

  /** 启动时从 localStorage 读取（旧版 px 档位迁移到按比例档位）。 */
  function init() {
    try {
      theme.value = localStorage.getItem(THEME_KEY) || 'system'
      styleMode.value = localStorage.getItem(STYLE_KEY) || 'default'
      const savedWidth = localStorage.getItem(WIDTH_KEY)
      // 旧版 px 档位迁移到按比例档位
      const legacyMap = { '960px': '55%', '1200px': '70%', '1440px': '85%', none: '100%' }
      contentWidth.value = legacyMap[savedWidth] || savedWidth || '70%'
    } catch {
      // ignore
    }
  }

  /** onMounted 时应用初始外观。 */
  function mount() {
    applyTheme()
    applyWidth()
    applyStyle()
  }

  return { theme, contentWidth, styleMode, applyTheme, applyWidth, applyStyle, init, mount }
}
