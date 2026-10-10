<template>
  <el-container class="app-container">
    <!-- 左侧全局导航（可折叠） -->
    <el-aside :width="navCollapsed ? '64px' : '212px'" class="app-aside">
      <div class="logo" @click="$router.push('/')">
        <div class="logo-mark">
          <el-icon :size="20"><MagicStick /></el-icon>
        </div>
        <transition name="fade">
          <span v-if="!navCollapsed" class="logo-text">AI 知识助手</span>
        </transition>
      </div>

      <el-menu
        :default-active="$route.path"
        router
        :collapse="navCollapsed"
        :collapse-transition="false"
        class="app-menu"
      >
        <el-menu-item index="/">
          <el-icon :size="18"><ChatDotRound /></el-icon>
          <template #title>知问 ZhiWen</template>
        </el-menu-item>
        <el-menu-item index="/knowledge">
          <el-icon :size="18"><FolderOpened /></el-icon>
          <template #title>知识库</template>
        </el-menu-item>
        <el-menu-item index="/runs">
          <el-icon :size="18"><DataAnalysis /></el-icon>
          <template #title>运行记录</template>
        </el-menu-item>
      </el-menu>

      <div class="aside-footer">
        <el-tooltip content="设置" placement="right">
          <div class="collapse-btn" @click="settingsVisible = true">
            <el-icon :size="18"><Setting /></el-icon>
          </div>
        </el-tooltip>
        <el-tooltip :content="navCollapsed ? '展开导航' : '收起导航'" placement="right">
          <div class="collapse-btn" @click="navCollapsed = !navCollapsed">
            <el-icon :size="18">
              <Expand v-if="navCollapsed" />
              <Fold v-else />
            </el-icon>
          </div>
        </el-tooltip>
      </div>
    </el-aside>

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
            <div
              v-for="item in navItems"
              :key="item.key"
              :class="['settings-tab', { active: activeTab === item.key }]"
              @click="activeTab = item.key"
            >
              <el-tooltip :content="item.desc" placement="bottom" :offset="6">
                <span class="settings-tab-inner">
                  <el-icon :size="15"><component :is="item.icon" /></el-icon>
                  <span>{{ item.label }}</span>
                </span>
              </el-tooltip>
            </div>
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
</template>

<script setup>
import { computed, onMounted, provide, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  Brush,
  ChatDotRound,
  Close,
  Connection,
  Cpu,
  DataAnalysis,
  Expand,
  Fold,
  FolderOpened,
  Lock,
  MagicStick,
  Memo,
  Search,
  Setting,
} from '@element-plus/icons-vue'
import { getSettings } from './api'
import AppearancePane from './components/settings/AppearancePane.vue'
import ApprovalPane from './components/settings/ApprovalPane.vue'
import MemoryPane from './components/settings/MemoryPane.vue'
import ToolsPane from './components/settings/ToolsPane.vue'
import ModelSettingsPane from './components/settings/ModelSettingsPane.vue'
import SearchPane from './components/settings/SearchPane.vue'
import SkillsPane from './components/settings/SkillsPane.vue'
import { useAppearance } from './composables/useAppearance'
import { useSettingsForm } from './composables/useSettingsForm'
import { useMcpServers } from './composables/useMcpServers'
import { useModelSettings } from './composables/useModelSettings'
import { useSkillsAdmin } from './composables/useSkillsAdmin'

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

// 记住导航折叠状态
try {
  navCollapsed.value = localStorage.getItem('rag_nav_collapsed') === '1'
} catch {
  // ignore
}
appearance.init()

watch(navCollapsed, (value) => {
  try {
    localStorage.setItem('rag_nav_collapsed', value ? '1' : '0')
  } catch {
    // ignore
  }
})

onMounted(() => {
  appearance.mount()
  window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', appearance.applyTheme)
})
</script>

<style>
/* ==================== 全局导航 ==================== */
.app-container {
  height: 100%;
}

.app-aside {
  display: flex;
  flex-direction: column;
  background: var(--bg-sidebar);
  border-right: 1px solid var(--border);
  transition: width 0.25s ease;
  overflow: hidden;
}

.logo {
  display: flex;
  align-items: center;
  gap: 10px;
  height: 60px;
  padding: 0 16px;
  cursor: pointer;
  flex-shrink: 0;
}

.logo-mark {
  width: 34px;
  height: 34px;
  flex-shrink: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  border-radius: var(--radius-md);
  color: var(--text-on-brand);
  background: var(--primary-grad);
  box-shadow: var(--shadow-pop);
}

.logo-text {
  font-size: var(--font-lg);
  font-weight: 700;
  letter-spacing: 0.5px;
  color: var(--text-1);
  white-space: nowrap;
}

.app-menu {
  flex: 1;
  border-right: none;
  padding: 4px 10px;
  overflow-y: auto;
}

.app-menu:not(.el-menu--collapse) {
  width: 100%;
}

.app-menu .el-menu-item {
  height: 44px;
  margin-bottom: 4px;
  border-radius: var(--radius-md);
  color: var(--text-2);
}

.app-menu .el-menu-item:hover {
  background: var(--bg-hover);
  color: var(--primary);
}

.app-menu .el-menu-item.is-active {
  background: var(--bg-active);
  color: var(--primary);
  font-weight: 600;
}

.app-menu.el-menu--collapse {
  width: 64px;
}

.app-menu.el-menu--collapse .el-menu-item {
  justify-content: center;
  padding: 0 !important;
}

.app-menu.el-menu--collapse .el-menu-item .el-tooltip__trigger {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 100%;
}

.app-menu.el-menu--collapse .el-menu-item .el-icon {
  margin: 0 !important;
}

.aside-footer {
  display: flex;
  justify-content: center;
  gap: 4px;
  padding: 12px 0;
  border-top: 1px solid var(--border);
  flex-shrink: 0;
}

.collapse-btn {
  width: 36px;
  height: 36px;
  display: flex;
  align-items: center;
  justify-content: center;
  border-radius: var(--radius-md);
  color: var(--text-3);
  cursor: pointer;
  transition: all 0.2s;
}

.collapse-btn:hover {
  background: var(--bg-hover);
  color: var(--primary);
}

.app-main {
  padding: 16px;
  background: var(--bg-app);
  overflow: hidden;
}

.fade-enter-active,
.fade-leave-active {
  transition: opacity 0.2s;
}

.fade-enter-from,
.fade-leave-to {
  opacity: 0;
}

/* 路由切换过渡：短时长 + 仅合成层属性（GPU），避免切换卡顿 */
.page-enter-active {
  transition: opacity 0.16s ease, transform 0.16s cubic-bezier(0.22, 1, 0.36, 1);
}

.page-leave-active {
  transition: opacity 0.1s ease;
}

.page-enter-from {
  opacity: 0;
  transform: translateY(6px);
}

.page-leave-to {
  opacity: 0;
}

/* ==================== 设置：居中圆角卡片 ==================== */
.settings-modal {
  backdrop-filter: blur(6px) saturate(1.1);
  -webkit-backdrop-filter: blur(6px) saturate(1.1);
  background: rgba(12, 14, 18, 0.42) !important;
}

.settings-dialog {
  border-radius: var(--radius-lg);
  overflow: hidden;
  padding: 0;
  background: var(--bg-panel);
  border: 1px solid var(--border);
  box-shadow: 0 24px 64px rgba(0, 0, 0, 0.22);
}

.settings-dialog .el-dialog__header {
  display: none;
}

.settings-dialog .el-dialog__body {
  padding: 0;
}

.settings-card {
  display: flex;
  flex-direction: column;
  height: 640px;
}

/* ---- 顶部横向导航 ---- */
.settings-topbar {
  flex-shrink: 0;
  display: flex;
  align-items: center;
  gap: 16px;
  padding: 12px 20px;
  border-bottom: 1px solid var(--border);
  background: var(--bg-card-2);
}

.settings-brand {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-shrink: 0;
}

.brand-mark {
  width: 30px;
  height: 30px;
  flex-shrink: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  border-radius: var(--radius-sm);
  color: var(--text-on-brand);
  background: var(--primary-grad);
}

.brand-title {
  font-size: var(--font-lg);
  font-weight: 700;
  color: var(--text-1);
}

.settings-tabs {
  flex: 1;
  display: flex;
  gap: 6px;
  justify-content: center;
}

.settings-tab {
  padding: 0;
  border: none;
  background: transparent;
  cursor: pointer;
  border-radius: var(--radius-md);
}

.settings-tab-inner {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 8px 16px;
  border-radius: var(--radius-md);
  font-size: var(--font-base);
  color: var(--text-2);
  transition: all 0.2s;
  user-select: none;
  white-space: nowrap;
}

.settings-tab:hover .settings-tab-inner {
  background: var(--bg-hover);
  color: var(--primary);
}

.settings-tab.active .settings-tab-inner {
  background: var(--primary-grad);
  color: var(--text-on-brand);
  font-weight: 600;
  box-shadow: var(--shadow-pop);
}

.settings-close {
  flex-shrink: 0;
  color: var(--text-3);
}

/* ---- 右侧内容 ---- */
.settings-content {
  flex: 1;
  min-width: 0;
  overflow-y: auto;
  padding: 24px 28px 28px;
}

.pane-head {
  margin-bottom: 18px;
}

.pane-title {
  font-size: var(--font-xl);
  font-weight: 700;
  color: var(--text-1);
}

.pane-sub {
  margin-top: 3px;
  font-size: var(--font-sm);
  color: var(--text-3);
}

.settings-section {
  margin-bottom: 22px;
}

.settings-label {
  font-size: var(--font-sm);
  font-weight: 600;
  color: var(--text-2);
  margin-bottom: 10px;
}

.section-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 8px;
}

.section-head .settings-label {
  margin-bottom: 0;
}

.section-actions {
  display: flex;
  align-items: center;
  gap: 8px;
}

.settings-tip {
  margin-top: 10px;
  font-size: var(--font-xs);
  color: var(--text-3);
  line-height: 1.7;
}

.settings-save-row {
  padding-top: 4px;
}

.tools-test-row {
  display: flex;
  gap: 8px;
  margin-top: 10px;
}

.memory-path {
  font-size: var(--font-xs);
  color: var(--text-3);
  margin-bottom: 10px;
  word-break: break-all;
}

.memory-textarea :deep(.el-textarea__inner) {
  font-family: Consolas, Monaco, monospace;
  font-size: var(--font-sm);
  line-height: 1.6;
}

/* ---- 外观 ---- */
.theme-options {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 10px;
}

.theme-card {
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  padding: 10px;
  cursor: pointer;
  background: var(--bg-card-2);
  transition: all 0.2s;
}

.theme-card:hover {
  border-color: var(--primary-2);
}

.theme-card.active {
  border-color: var(--primary);
  background: var(--bg-active);
  box-shadow: 0 0 0 2px var(--primary-soft);
}

.theme-preview {
  height: 52px;
  border-radius: var(--radius-sm);
  display: flex;
  overflow: hidden;
  border: 1px solid var(--border);
  margin-bottom: 8px;
}

.theme-preview.light {
  background: #f3f4f8;
}

.theme-preview.dark {
  background: #0e1014;
}

.theme-preview.system {
  background: linear-gradient(135deg, #f3f4f8 50%, #0e1014 50%);
}

.preview-side {
  width: 26%;
  background: rgba(127, 127, 127, 0.14);
  border-right: 1px solid rgba(127, 127, 127, 0.2);
}

.preview-main {
  flex: 1;
  padding: 8px;
  display: flex;
  flex-direction: column;
  gap: 5px;
}

.preview-line {
  height: 4px;
  border-radius: var(--radius-xs);
  background: var(--primary);
  opacity: 0.75;
  width: 70%;
}

.preview-line.short {
  width: 45%;
  opacity: 0.35;
}

.preview-bubble {
  align-self: flex-end;
  width: 38%;
  height: 12px;
  border-radius: var(--radius-sm) var(--radius-sm) var(--radius-xs) var(--radius-sm);
  background: var(--accent);
  opacity: 0.7;
}

.theme-meta {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.theme-name {
  font-size: var(--font-sm);
  color: var(--text-1);
}

.theme-check {
  color: var(--primary);
  font-size: var(--font-lg);
}

.width-segmented {
  width: 100%;
}

.width-segmented :deep(.el-segmented__item) {
  flex: 1;
}

.width-feedback {
  margin-top: 12px;
  display: flex;
  align-items: center;
  gap: 12px;
}

.width-preview-track {
  flex: 1;
  height: 8px;
  border-radius: var(--radius-pill);
  background: var(--bg-hover);
  border: 1px solid var(--border);
  overflow: hidden;
}

.width-preview-bar {
  height: 100%;
  border-radius: var(--radius-pill);
  background: var(--primary-grad);
  transition: width 0.25s ease;
}

.width-value {
  flex-shrink: 0;
  font-size: var(--font-xs);
  color: var(--text-3);
  min-width: 56px;
  text-align: right;
}

/* ---- 模型供应商 ---- */
.provider-empty {
  padding: 18px 12px;
  text-align: center;
  font-size: var(--font-sm);
  color: var(--text-3);
  border: 1px dashed var(--border-strong);
  border-radius: var(--radius-md);
  background: var(--bg-card-2);
}

.provider-card {
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  padding: 12px;
  margin-bottom: 10px;
  background: var(--bg-card-2);
  transition: all 0.2s;
}

.provider-card:hover {
  border-color: var(--border-strong);
}

.provider-card.inactive {
  opacity: 0.72;
}

.provider-head {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 10px;
}

.provider-radio :deep(.el-radio__label) {
  font-size: var(--font-xs);
  color: var(--text-3);
  padding-left: 4px;
}

.provider-name {
  font-size: var(--font-base);
  font-weight: 600;
  color: var(--text-1);
}

.provider-flex {
  flex: 1;
}

.provider-del {
  margin-left: 2px;
}

/* 供应商测试/测速徽标（成功绿 / 失败红，颜色全走令牌） */
.probe-badge {
  font-size: var(--font-xs);
  padding: 1px 8px;
  border-radius: 10px;
  border: 1px solid var(--border);
  white-space: nowrap;
}

.probe-badge.ok {
  color: var(--success);
  border-color: var(--success);
  background: color-mix(in srgb, var(--success) 12%, transparent);
}

.probe-badge.fail {
  color: var(--danger);
  border-color: var(--danger);
  background: var(--danger-bg);
}

.provider-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 10px;
}

.provider-grid .field {
  display: flex;
  flex-direction: column;
  gap: 5px;
}

.provider-grid .field.wide {
  grid-column: 1 / -1;
}

.provider-grid .field label {
  font-size: var(--font-xs);
  color: var(--text-3);
}

.provider-grid :deep(.el-input__wrapper),
.provider-grid :deep(.el-select__wrapper) {
  background: var(--bg-chat);
  box-shadow: 0 0 0 1px var(--border) inset;
}

.provider-grid :deep(.el-input__inner) {
  color: var(--text-1);
}

.generic-form {
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  padding: 6px 12px;
  background: var(--bg-card-2);
}

.generic-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 9px 0;
  border-bottom: 1px solid var(--border);
}

.generic-row:last-child {
  border-bottom: none;
}

.generic-sub {
  margin: 12px 0 2px;
  padding-bottom: 4px;
  border-bottom: 1px dashed var(--border);
  font-size: var(--font-xs);
  font-weight: 600;
  color: var(--text-2);
}

.generic-label {
  font-size: var(--font-sm);
  color: var(--text-2);
  flex-shrink: 0;
}

.generic-select {
  width: 210px;
}

.generic-hint {
  font-size: var(--font-xs);
  color: var(--text-3);
  line-height: 1.4;
}

/* ---- 技能 ---- */
.skill-master {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 12px 14px;
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  background: var(--bg-card-2);
  margin-bottom: 14px;
}

.skill-master-title {
  font-size: var(--font-base);
  font-weight: 600;
  color: var(--text-1);
}

.skill-master-desc {
  margin-top: 3px;
  font-size: var(--font-xs);
  color: var(--text-3);
}

.skill-filter-row {
  display: flex;
  gap: 8px;
  margin-bottom: 10px;
}

.skill-search {
  flex: 1;
}

.skill-group-select {
  width: 140px;
  flex-shrink: 0;
}

.skill-source-filter {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-bottom: 12px;
}

.source-chip {
  padding: 4px 12px;
  border-radius: var(--radius-pill);
  font-size: var(--font-xs);
  color: var(--text-2);
  background: var(--bg-card-2);
  border: 1px solid var(--border);
  cursor: pointer;
  transition: all 0.2s;
}

.source-chip:hover {
  color: var(--primary);
  border-color: var(--primary-2);
}

.source-chip.active {
  color: var(--primary);
  background: var(--bg-active);
  border-color: var(--primary);
  font-weight: 600;
}

.skill-summary {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 10px;
}

.skill-summary-text {
  font-size: var(--font-xs);
  color: var(--text-3);
}

.skills-loading,
.agent-banner {
  display: flex;
  align-items: center;
  gap: 6px;
  margin: 0 0 12px;
  padding: 8px 12px;
  border: 1px solid var(--el-color-warning-light-5);
  border-radius: 8px;
  background: var(--el-color-warning-light-9);
  color: var(--el-color-warning-dark-2);
  font-size: 13px;
  cursor: pointer;
}
.agent-banner:hover {
  border-color: var(--el-color-warning-light-3);
}
.skill-date {
  font-size: 11px;
  color: var(--text-3);
}
.agent-preview {
  margin: 0;
  max-height: 60vh;
  overflow: auto;
  padding: 12px 14px;
  border-radius: 8px;
  background: var(--el-fill-color-light);
  color: var(--el-text-color-primary);
  font-size: 12.5px;
  line-height: 1.7;
  white-space: pre-wrap;
  word-break: break-word;
}

.skills-empty {
  padding: 26px 0;
  text-align: center;
  color: var(--text-3);
  font-size: var(--font-sm);
}

.skill-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.skill-item {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 10px 12px;
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  background: var(--bg-card-2);
  transition: border-color 0.2s;
}

.skill-item:hover {
  border-color: var(--border-strong);
}

.skill-item.muted {
  opacity: 0.66;
}

.skill-icon {
  width: 34px;
  height: 34px;
  flex-shrink: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  border-radius: var(--radius-md);
  color: var(--primary);
  background: var(--bg-active);
}

.skill-main {
  flex: 1;
  min-width: 0;
}

.skill-head {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
}

.skill-name {
  font-weight: 600;
  font-size: var(--font-base);
  color: var(--text-1);
}

.skill-desc {
  margin-top: 4px;
  font-size: var(--font-xs);
  color: var(--text-3);
  line-height: 1.5;
  overflow: hidden;
  text-overflow: ellipsis;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
}

.skill-ops {
  display: flex;
  align-items: center;
  gap: 4px;
  flex-shrink: 0;
}

.skill-remove {
  color: var(--text-3);
}

.skill-remove:hover {
  color: var(--danger);
}

.hidden-head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  margin: 18px 0 10px;
}

.hidden-head .settings-label {
  margin-bottom: 0;
}

.hidden-hint {
  font-size: var(--font-xs);
  color: var(--text-3);
}

.skill-footer {
  display: flex;
  justify-content: space-between;
  margin-top: 12px;
}
/* ---- 界面风格卡 ---- */
.style-swatch {
  height: 52px;
  border-radius: var(--radius-sm);
  display: flex;
  overflow: hidden;
  border: 1px solid var(--border);
  margin-bottom: 8px;
}

.style-swatch .sw-side {
  width: 26%;
  border-right: 1px solid rgba(127, 127, 127, 0.16);
}

.style-swatch .sw-body {
  flex: 1;
  padding: 9px 10px;
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.style-swatch .sw-line {
  height: 4px;
  border-radius: var(--radius-xs);
  width: 62%;
  opacity: 0.85;
}

.style-swatch .sw-line.short {
  width: 38%;
  opacity: 0.45;
}

.style-swatch.default {
  background: #f3f4f8;
}

.style-swatch.default .sw-side {
  background: #ffffff;
}

.style-swatch.default .sw-line {
  background: #1677ff;
}

.style-swatch.quiet {
  background: #f7f6f3;
}

.style-swatch.quiet .sw-side {
  background: #fbfaf8;
}

.style-swatch.quiet .sw-line {
  background: #1f2126;
}

.style-swatch.salon {
  background: #ece5d8;
}

.style-swatch.salon .sw-side {
  background: #f2ecdf;
}

.style-swatch.salon .sw-line {
  background: #96691f;
}

/* 标题字体随风格（默认无衬线；书斋为宋体系） */
.pane-title {
  font-family: var(--font-display);
}

/* ---- MCP 面板增强 ---- */
.mcp-badge {
  flex-shrink: 0;
  font-size: var(--font-2xs);
  padding: 1px 8px;
  border-radius: 999px;
  background: var(--bg-hover);
  color: var(--text-3);
  white-space: nowrap;
}

.mcp-badge.ok {
  color: var(--success-text, var(--success));
  background: color-mix(in srgb, var(--success) 12%, transparent);
}

.mcp-badge.warn {
  color: var(--warning-text, var(--warning));
  background: color-mix(in srgb, var(--warning) 14%, transparent);
}

.mcp-badge.bad {
  color: var(--danger-text, var(--danger));
  background: color-mix(in srgb, var(--danger) 10%, transparent);
}

.mcp-cmd {
  margin-top: 6px;
  font-family: Consolas, Monaco, monospace;
  font-size: var(--font-xs);
  color: var(--text-3);
  word-break: break-all;
}

.mcp-env {
  margin-top: 2px;
  font-size: var(--font-2xs);
  color: var(--text-3);
}

.mcp-tools {
  margin-top: 8px;
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
}

.mcp-tool-chip {
  font-family: Consolas, Monaco, monospace;
  font-size: var(--font-2xs);
  color: var(--text-2);
  background: var(--bg-hover);
  border-radius: var(--radius-xs);
  padding: 1px 6px;
}

</style>
