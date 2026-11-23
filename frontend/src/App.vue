<template>
  <!-- locale 从全局 app.use(ElementPlus) 迁来（按需引入后组件级配置） -->
  <el-config-provider :locale="zhCn">
    <el-container class="app-container">
    <!-- 左侧全局导航（可折叠） -->
    <!-- 收起态的左边缘感应条：鼠标移上来把整列悬浮出来 -->
    <div class="aside-peek" aria-hidden="true" @mouseenter="onPeekEnter"></div>
    <el-aside
      width="212px"
      class="app-aside"
      @mouseleave="onAsideLeave"
    >
      <button type="button" class="logo" aria-label="回到知问首页" @click="$router.push('/')">
        <div class="logo-mark">
          <el-icon :size="20"><component :is="logoIcon" /></el-icon>
        </div>
        <transition name="fade">
          <span v-if="!navCollapsed" class="logo-text">AI 知识助手</span>
        </transition>
      </button>

      <!-- 不用 el-menu 的 collapse：折叠只改侧栏宽度（即时），菜单 DOM/内宽保持
           不变并由侧栏裁切——避免每次展开/收起整棵菜单重渲染（约 100ms 长任务）。
           收起态靠 title 提供原生悬浮提示。 -->
      <!-- 图标随布局切换（见 NAV_ICONS）：导轨偏线性、浮岛偏方块、顶栏偏实心 -->
      <el-menu :default-active="$route.path" router class="app-menu">
        <el-menu-item
          v-for="(item, i) in navRoutes"
          :key="item.path"
          :index="item.path"
          :title="navCollapsed || layoutMode === 'rail' ? item.title : undefined"
        >
          <el-icon :size="18"><component :is="navIcons[i]" /></el-icon>
          <template #title><span class="menu-title">{{ item.label }}</span></template>
        </el-menu-item>
      </el-menu>

      <!-- 单列布局（Claude/Hermes）：会话列表由 ChatView 传送进来，与导航同列 -->
      <!-- 插槽常驻：打开"合并"时 Teleport 的目标必须已经在 DOM 里，
           否则同一次更新里创建目标 + 传送会失败，会话列表要手动刷新才出现 -->
      <div
        id="nav-session-slot"
        class="nav-session-slot"
        :class="{ 'is-off': !singleColumn }"
      ></div>

      <div class="aside-footer">
        <el-tooltip :show-after="1000" content="设置" placement="right" :hide-after="800">
          <button
            type="button"
            class="collapse-btn"
            aria-label="打开设置"
            @click="settingsVisible = true"
          >
            <el-icon :size="18"><Setting /></el-icon>
          </button>
        </el-tooltip>
        <!-- 非对话页没有 chat-header，把 固定/收起 侧栏 放到设置旁边 -->
        <el-tooltip :show-after="1000"
          v-if="!isChatRoute"
          :content="sidebarCollapsed ? '固定侧栏' : '收起侧栏'"
          placement="right"
          :hide-after="800"
        >
          <button
            type="button"
            class="collapse-btn sidebar-pin"
            :class="{ floating: sidebarCollapsed }"
            :aria-label="sidebarCollapsed ? '固定侧栏' : '收起侧栏'"
            @click="toggleSidebarCollapsed"
          >
            <el-icon :size="18">
              <Expand v-if="sidebarCollapsed" />
              <Fold v-else />
            </el-icon>
          </button>
        </el-tooltip>
      </div>
    </el-aside>

    <!-- 拖动调宽：写 --nav-width，各布局用各自 min/max 限幅 -->
    <ResizeHandle
      var-name="--nav-width"
      :min="navWidthMin"
      :max="navWidthMax"
      store-key="rag_nav_width"
      label="拖动调整侧栏宽度"
    />

    <el-main class="app-main">
      <router-view v-slot="{ Component }">
        <transition name="page" mode="out-in">
          <keep-alive :include="keepAliveViews">
            <component :is="Component" />
          </keep-alive>
        </transition>
      </router-view>
    </el-main>

    <!-- ================= 设置：居中圆角卡片 + 背景模糊 ================= -->
    <el-dialog
      v-model="settingsVisible"
      class="settings-dialog"
      modal-class="settings-modal"
      width="min(940px, calc(100vw - 32px))"
      align-center
      :show-close="false"
      append-to-body
    >
      <div class="settings-card">
        <!-- 顶部横向导航 -->
        <header class="settings-topbar">
          <div class="settings-brand">
            <div class="brand-mark">
              <el-icon :size="16"><Setting /></el-icon>
            </div>
            <span class="brand-title">设置</span>
          </div>
          <nav class="settings-tabs">
            <button
              v-for="item in navItems"
              :key="item.key"
              type="button"
              :class="['settings-tab', { active: activeTab === item.key }]"
              :aria-current="activeTab === item.key ? 'true' : null"
              @click="activeTab = item.key"
            >
              <el-tooltip :show-after="1000" :content="item.desc" placement="bottom" :offset="6" :hide-after="800">
                <span class="settings-tab-inner">
                  <el-icon :size="15"><component :is="item.icon" /></el-icon>
                  <span>{{ item.label }}</span>
                </span>
              </el-tooltip>
            </button>
          </nav>
          <el-button circle text class="settings-close" @click="settingsVisible = false">
            <el-icon :size="16"><Close /></el-icon>
          </el-button>
        </header>

        <!-- 右侧内容 -->
        <main class="settings-content">
          <!-- ============ 外观 ============ -->
          <AppearancePane v-show="activeTab === 'appearance'" />

          <!-- ============ 模型与供应商 ============ -->
          <ModelSettingsPane v-show="activeTab === 'models'" />

          <!-- ============ 技能 ============ -->
          <SkillsPane v-show="activeTab === 'skills'" />

          <!-- ============ 工具与集成 ============ -->
          <ToolsPane v-show="activeTab === 'tools'" />

          <!-- ============ 审批与安全 ============ -->
          <ApprovalPane v-show="activeTab === 'approval'" />

          <!-- ============ 检索与联网 ============ -->
          <SearchPane v-show="activeTab === 'search'" />

          <!-- ============ 上下文与记忆 ============ -->
          <MemoryPane v-show="activeTab === 'memory'" />
        </main>
      </div>
    </el-dialog>

    </el-container>
  </el-config-provider>
</template>

<script setup>
import { computed, onMounted, provide, ref, watch } from 'vue'
import { ElMessage } from 'element-plus/es/components/message/index'
import { ElMessageBox } from 'element-plus/es/components/message-box/index'
import zhCn from 'element-plus/es/locale/lang/zh-cn'
import {
  Aim,
  Brush,
  ChatDotRound,
  ChatDotSquare,
  ChatLineRound,
  ChatRound,
  Close,
  Collection,
  Comment,
  Compass,
  Connection,
  Cpu,
  DataAnalysis,
  DataBoard,
  DataLine,
  Document,
  Edit,
  EditPen,
  Expand,
  Files,
  Fold,
  Folder,
  FolderAdd,
  FolderOpened,
  Grid,
  Histogram,
  Lock,
  MagicStick,
  Management,
  Memo,
  MessageBox,
  Notebook,
  Operation,
  PieChart,
  Search,
  Setting,
  Star,
  Sunny,
  Tickets,
  TrendCharts,
} from '@element-plus/icons-vue'
import { getSettings } from './api'
import AppearancePane from './components/settings/AppearancePane.vue'
import ApprovalPane from './components/settings/ApprovalPane.vue'
import MemoryPane from './components/settings/MemoryPane.vue'
import ToolsPane from './components/settings/ToolsPane.vue'
import ModelSettingsPane from './components/settings/ModelSettingsPane.vue'
import SearchPane from './components/settings/SearchPane.vue'
import SkillsPane from './components/settings/SkillsPane.vue'
import ResizeHandle from './components/ResizeHandle.vue'
import { useRoute } from 'vue-router'
import { useAppearance } from './composables/useAppearance'
import { useSettingsForm } from './composables/useSettingsForm'
import { useMcpServers } from './composables/useMcpServers'
import { useModelSettings } from './composables/useModelSettings'
import { useSkillsAdmin } from './composables/useSkillsAdmin'

// 导航恒为"图标 + 文字"：窄屏/悬浮态都靠整列收起（sidebarCollapsed）处理，
// 不再有"只留图标"的中间态（它和整列收起两个图标挨在一起，视觉重复且容易误判）
const navCollapsed = ref(false)
const settingsVisible = ref(false)
const activeTab = ref('appearance')

// 左侧功能导航
const navItems = [
  { key: 'appearance', label: '外观', desc: '主题与内容布局', icon: Brush },
  { key: 'models', label: '模型', desc: '对话 / 视觉 / 嵌入 / 重排 + 供应商', icon: Cpu },
  { key: 'skills', label: '技能', desc: '本机 Skills 管理', icon: MagicStick },
  { key: 'tools', label: '工具', desc: '受控执行 / 子代理 / 计划模式 / MCP', icon: Connection },
  { key: 'approval', label: '审批', desc: '敏感操作确认 / 命令白名单 / 沙箱', icon: Lock },
  { key: 'search', label: '检索', desc: '联网搜索 / 知识库检索参数', icon: Search },
  { key: 'memory', label: '记忆', desc: '上下文窗口 / 长期记忆 / 项目记忆 / 检查点', icon: Memo },
]

// ---------------- 外观 ----------------
// 主题/内容宽度/界面风格的状态与即时应用抽到 composables/useAppearance.js
// （设置页外观面板经 provide/inject 共用同一实例）。
const appearance = useAppearance()
provide('settingsAppearance', appearance)
// 模板里要用布局值：顶层解构 ref，模板自动解包（对象内的 ref 不会）
const layoutMode = appearance.layoutMode

// 导航项：图标按布局取不同一套，视觉上能区分"我是哪种布局"
const navRoutes = [
  { path: '/', label: '知问 ZhiWen', title: '知问 ZhiWen' },
  { path: '/knowledge', label: '知识库', title: '知识库' },
  { path: '/runs', label: '运行记录', title: '运行记录' },
  { path: '/prompts', label: '提示词', title: '提示词' },
]
const NAV_ICONS = {
  standard: [ChatDotRound, FolderOpened, DataAnalysis, Operation],
  rail: [ChatLineRound, Files, TrendCharts, Notebook],
  island: [ChatDotSquare, Collection, Histogram, EditPen],
  topbar: [Comment, Folder, DataLine, Tickets],
  claude: [ChatRound, Document, PieChart, Edit],
  hermes: [MessageBox, FolderAdd, DataBoard, Management],
}
const navIcons = computed(() => NAV_ICONS[appearance.layoutMode.value] || NAV_ICONS.standard)
const LOGO_ICONS = {
  standard: MagicStick,
  rail: Grid,
  island: Aim,
  topbar: Compass,
  claude: Star,
  hermes: Sunny,
}
const logoIcon = computed(() => LOGO_ICONS[appearance.layoutMode.value] || MagicStick)
// 这几个布局宽度固定：导轨是常驻图标条，Claude/Hermes 是整幅侧栏——不再渲染收起按钮
const FIXED_WIDTH_LAYOUTS = ['rail', 'claude', 'hermes']
const canCollapse = computed(() => !FIXED_WIDTH_LAYOUTS.includes(layoutMode.value))
// Claude / Hermes：导航与会话列表同列（ChatView 把会话列表 teleport 进插槽）
const SINGLE_COLUMN_LAYOUTS = ['claude', 'hermes']
// 合并开关打开时，任何布局都走"导航 + 会话列表同列"（这才是"合并"的含义；
// 之前只对 claude/hermes 生效，其它布局下合并开关会把侧栏压成 64px 图标条而
// 会话列表仍在另一列 —— 于是"合并不了、图标点不到"）。
const singleColumn = computed(
  () => SINGLE_COLUMN_LAYOUTS.includes(layoutMode.value) || !!mergedSidebar.value,
)
const { sidebarCollapsed, mergedSidebar } = appearance

// 对话页的侧栏开关在 chat-header 里；其余页面放在设置图标旁
const appRoute = useRoute()
const isChatRoute = computed(() => appRoute.path === '/' || appRoute.name === 'chat')

/* 收起后的"悬浮展开"改由 JS 控制：
   CSS :hover 版本下，收起按钮就在侧栏内部，按下瞬间仍是 hover 态，
   侧栏立刻弹回来 —— 表现就是"点了没反应，图标/侧栏还在"。
   现在只有指针进入左侧感应条才展开，指针离开侧栏即关闭。 */
const peekOpen = ref(false)
let peekSuppressed = false

function syncPeek() {
  document.documentElement.dataset.sidebarPeek = peekOpen.value ? '1' : '0'
}

function onPeekEnter() {
  if (!sidebarCollapsed.value || peekSuppressed) return
  peekOpen.value = true
  syncPeek()
}

function onAsideLeave() {
  peekOpen.value = false
  peekSuppressed = false
  syncPeek()
}

function toggleSidebarCollapsed(event) {
  // 收起后按钮仍持有焦点会让侧栏停在原位，点完主动失焦
  if (event?.currentTarget instanceof HTMLElement) event.currentTarget.blur()
  if (!sidebarCollapsed.value) {
    peekOpen.value = false
    peekSuppressed = true
    syncPeek()
  }
  sidebarCollapsed.value = !sidebarCollapsed.value
  if (!sidebarCollapsed.value) {
    peekOpen.value = false
    peekSuppressed = false
    syncPeek()
  }
}

// 收起后：指针离开侧栏区域就解除压制；悬浮态下指针移出侧栏则收回
// （el-aside 的 mouseleave 在某些位移/过渡时不可靠，统一用文档级指针位置判断）
function onDocPointerMove(event) {
  const aside = document.querySelector('.app-aside')
  if (!aside) return
  const inside = aside.contains(event.target)
  if (peekSuppressed && !inside) peekSuppressed = false
  if (peekOpen.value && sidebarCollapsed.value) {
    const rect = aside.getBoundingClientRect()
    const inPeek = event.clientX <= 14
    if (!inside && !inPeek && event.clientX > rect.right) {
      peekOpen.value = false
      syncPeek()
    }
  }
}
// 拖动范围按布局给：导轨收得最窄，Hermes 最宽
const NAV_WIDTH_RANGE = {
  standard: [176, 340],
  rail: [56, 132],
  island: [176, 340],
  topbar: [56, 132],
  claude: [232, 400],
  hermes: [248, 420],
}
const navWidthMin = computed(() => (NAV_WIDTH_RANGE[layoutMode.value] || [176, 340])[0])
const navWidthMax = computed(() => (NAV_WIDTH_RANGE[layoutMode.value] || [176, 340])[1])

// 路由视图缓存：切回来不重新加载（数据由各视图 onActivated 静默刷新）
const keepAliveViews = ['ChatView', 'RunsView', 'KnowledgeView']

// ---------------- 模型与供应商 ----------------
// 模型与供应商域抽到 composables/useModelSettings.js（提供 settingsModel 域实例）
const settingsModel = useModelSettings(() => loadSettings())
provide('settingsModel', settingsModel)

// ---------------- 工具与集成（表单域） ----------------
// toolsForm / 保存 / 测试按钮抽到 composables/useSettingsForm.js
// （工具/审批/检索/记忆四页共用， provide 注入）
const settingsForm = useSettingsForm(() => loadSettings())
provide('settingsForm', settingsForm)
// MCP 服务器管理抽到 composables/useMcpServers.js（工具面板经 inject 使用）
const settingsMcp = useMcpServers()
provide('settingsMcp', settingsMcp)
// 技能管理抽到 composables/useSkillsAdmin.js（技能面板经 inject 使用）
const settingsSkills = useSkillsAdmin()
provide('settingsSkills', settingsSkills)

// 跨组件打开设置指定标签（如 ChatView 的「管理供应商…」→ 模型面板）
function openSettingsTab(key = 'models') {
  activeTab.value = key
  settingsVisible.value = true
}
provide('openSettingsTab', openSettingsTab)

async function loadSettings() {
  try {
    const snapshot = await getSettings()
    settingsModel.applySnapshot(snapshot)
    settingsForm.applyEditable(snapshot.editable || {})
  } catch (error) {
    ElMessage.error(error.response?.data?.detail || '加载设置失败')
  }
}


watch(settingsVisible, (visible) => {
  if (visible) {
    loadSettings()
    settingsSkills.refreshSkills()
    settingsMcp.loadMcp()
  }
})

// 折叠状态持久化：仅记录用户显式切换（初始窄屏强制值不落盘，见 navCollapsed 声明）
watch(navCollapsed, (value) => {
  try {
    localStorage.setItem('rag_nav_collapsed', value ? '1' : '0')
  } catch {
    // ignore
  }
})

appearance.init()

onMounted(() => {
  document.addEventListener('pointermove', onDocPointerMove)
  appearance.mount()
  window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', appearance.applyTheme)
})
</script>

<!-- 全局（非 scoped）样式：拆到 styles/（P3·⑤），载入顺序即书写顺序 -->
<style src="./styles/app-nav.css"></style>
<style src="./styles/layout.css"></style>
<style src="./styles/view-variants.css"></style>
<style src="./styles/settings.css"></style>
<style src="./styles/inspector.css"></style>
<style src="./styles/style-signatures.css"></style>
