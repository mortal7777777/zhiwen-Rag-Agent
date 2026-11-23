/**
 * 外观状态：主题 / 内容宽度 / 界面风格 / 布局 / 字体（从 App.vue 抽出，2026-10-09）。
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
const LAYOUT_KEY = 'rag_layout'
const FONT_KEY = 'rag_font'
const SIDEBAR_KEY = 'rag_sidebar_merged'
const SIDEBAR_COLLAPSED_KEY = 'rag_sidebar_collapsed'
const HOME_KEY = 'rag_home_style'
const GROUP_WS_KEY = 'rag_group_sessions_by_workspace'

export function useAppearance() {
  const theme = ref('system')
  const contentWidth = ref('100%')
  const styleMode = ref('default')
  const layoutMode = ref('standard')
  const fontMode = ref('auto')
  // 会话列表与导航是否合成一条侧栏（图标条 + 列表）
  const mergedSidebar = ref(false)
  // 单列布局（Claude/Hermes）整列收起，鼠标移回左边缘再悬浮出来
  const sidebarCollapsed = ref(false)
  // 主页（欢迎页）样式：classic / minimal / hero / studio
  const homeStyle = ref('classic')
  // 会话列表按工作区（会话 project_dir）分组；默认开启
  const groupSessionsByWorkspace = ref(true)

  function applyStyle() {
    if (styleMode.value === 'default') {
      delete document.documentElement.dataset.style
    } else {
      document.documentElement.dataset.style = styleMode.value
    }
    syncDesktopTitlebar()
  }

  /** 布局总是写到 dataset：html[data-layout="standard"] 也要能命中。 */
  function applyLayout() {
    document.documentElement.dataset.layout = layoutMode.value
  }

  /** 字体同理：auto 时由 theme.css 按 data-style 解析成该风格的默认字体。 */
  function applyFont() {
    document.documentElement.dataset.font = fontMode.value
  }

  /** split = 导航与会话列表两条栏；merged = 合成一条（导轨 + 列表贴身）。 */
  function applyHomeStyle() {
    document.documentElement.dataset.home = homeStyle.value
  }

  function applySidebar() {
    document.documentElement.dataset.sidebar = mergedSidebar.value ? 'merged' : 'split'
    // 收起态只在单列布局下有意义，其它布局忽略
    document.documentElement.dataset.sidebarCollapsed = sidebarCollapsed.value ? '1' : '0'
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
    syncDesktopTitlebar()
  }

  /** 桌面端隐藏式标题栏：把当前主题实际的底色/符号色同步给主进程（浏览器 no-op）。 */
  function syncDesktopTitlebar() {
    try {
      const shell = typeof window !== 'undefined' ? window.zhiwenShell : undefined
      if (!shell || typeof shell.setTitleBarTheme !== 'function') return
      const styles = getComputedStyle(document.documentElement)
      const color = styles.getPropertyValue('--bg-app').trim()
      const symbolColor = styles.getPropertyValue('--text-2').trim()
      if (color) shell.setTitleBarTheme({ color, symbolColor })
    } catch {
      // 非桌面环境忽略
    }
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
  watch(layoutMode, () => {
    applyLayout()
    localStorage.setItem(LAYOUT_KEY, layoutMode.value)
  })
  watch(fontMode, () => {
    applyFont()
    localStorage.setItem(FONT_KEY, fontMode.value)
  })
  watch(mergedSidebar, () => {
    applySidebar()
    applyHomeStyle()
    localStorage.setItem(SIDEBAR_KEY, mergedSidebar.value ? '1' : '0')
  })
  watch(homeStyle, (value) => {
    document.documentElement.dataset.home = value
    localStorage.setItem(HOME_KEY, value)
  })
  watch(groupSessionsByWorkspace, (value) => {
    localStorage.setItem(GROUP_WS_KEY, value ? '1' : '0')
  })

  watch(sidebarCollapsed, () => {
    document.documentElement.dataset.sidebarCollapsed = sidebarCollapsed.value ? '1' : '0'
    localStorage.setItem(SIDEBAR_COLLAPSED_KEY, sidebarCollapsed.value ? '1' : '0')
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
      layoutMode.value = localStorage.getItem(LAYOUT_KEY) || 'standard'
      fontMode.value = localStorage.getItem(FONT_KEY) || 'auto'
      mergedSidebar.value = localStorage.getItem(SIDEBAR_KEY) === '1'
      sidebarCollapsed.value = localStorage.getItem(SIDEBAR_COLLAPSED_KEY) === '1'
      // 旧值/坏值回落到 classic：样式名参与 html[data-home] 选择器，必须可枚举
      const savedHome = localStorage.getItem(HOME_KEY) || 'classic'
      homeStyle.value = ['classic', 'minimal', 'hero', 'studio'].includes(savedHome)
        ? savedHome
        : 'classic'
      // 默认开启的布尔项：只有显式存过 '0' 才关闭
      groupSessionsByWorkspace.value = localStorage.getItem(GROUP_WS_KEY) !== '0'
      const savedWidth = localStorage.getItem(WIDTH_KEY)
      // 旧版 px 档位迁移到按比例档位；旧默认（70% / 1200px）归位到全宽
      const legacyMap = {
        '960px': '55%',
        '1440px': '85%',
        none: '100%',
        '1200px': '100%',
        '70%': '100%',
      }
      contentWidth.value = legacyMap[savedWidth] || savedWidth || '100%'
    } catch {
      // ignore
    }
  }

  /** onMounted 时应用初始外观。 */
  function mount() {
    applyTheme()
    applyWidth()
    applyStyle()
    applyLayout()
    applyFont()
    applySidebar()
    applyHomeStyle()
  }

  return {
    theme,
    contentWidth,
    styleMode,
    layoutMode,
    fontMode,
    mergedSidebar,
    sidebarCollapsed,
    homeStyle,
    groupSessionsByWorkspace,
    applyTheme,
    applyWidth,
    applyStyle,
    applyLayout,
    applyFont,
    applySidebar,
    init,
    mount,
  }
}