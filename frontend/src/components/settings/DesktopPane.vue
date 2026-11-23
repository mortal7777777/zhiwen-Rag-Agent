<template>
  <div class="settings-pane">
    <div class="pane-head">
      <div class="pane-title">应用设置</div>
      <div class="pane-sub">版本与更新 · 启动项 · 通知与托盘 · 本机后端</div>
    </div>

    <div v-if="!available" class="settings-section">
      <div class="provider-hint">这些设置只在桌面客户端内可用（浏览器访问时不可修改）。</div>
    </div>

    <template v-else>
      <div class="settings-section">
        <div class="settings-label">版本与更新</div>
        <div class="generic-form">
          <div class="generic-row">
            <span class="generic-label">当前版本</span>
            <span class="desktop-value">
              v{{ version }}
              <span v-if="!packaged" class="desktop-tag">开发模式</span>
            </span>
          </div>
          <div class="generic-row">
            <span class="generic-label">更新状态</span>
            <span :class="['desktop-value', 'update-status', 'is-' + update.status]">
              <span class="update-dot" aria-hidden="true"></span>
              {{ updateStatusText }}
            </span>
          </div>
          <div
            v-if="update.status === 'downloading' || update.status === 'downloaded'"
            class="update-progress"
          >
            <el-progress :percentage="update.progress" :stroke-width="6" :show-text="false" />
            <span>{{ update.progress }}%</span>
          </div>
          <div class="generic-row">
            <span class="generic-label">启动时检查更新</span>
            <el-switch
              :model-value="form.autoCheckUpdates"
              @change="(value) => apply({ autoCheckUpdates: value })"
            />
          </div>
        </div>
        <div class="provider-hint">{{ updateHint }}</div>
        <el-space wrap>
          <el-button size="small" :loading="updateBusy" :disabled="updateBusy" @click="checkUpdates">
            检查更新
          </el-button>
          <el-button size="small" @click="openUpdatePage">打开发布页</el-button>
          <el-button
            v-if="update.status === 'downloaded'"
            size="small"
            type="primary"
            @click="installUpdate"
          >
            重启并安装
          </el-button>
        </el-space>
      </div>

      <div class="settings-section">
        <div class="settings-label">启动与窗口</div>
        <div class="generic-form">
          <div class="generic-row">
            <span class="generic-label">开机自动启动</span>
            <el-switch
              :model-value="form.launchAtLogin"
              @change="(value) => apply({ launchAtLogin: value })"
            />
          </div>
          <div class="generic-row">
            <span class="generic-label">关闭窗口时收进托盘</span>
            <el-switch
              :model-value="form.minimizeToTray"
              @change="(value) => apply({ minimizeToTray: value })"
            />
          </div>
          <div class="generic-row">
            <span class="generic-label">全局快捷键</span>
            <div class="desktop-inline">
              <el-input
                v-model="hotkeyDraft"
                size="small"
                placeholder="Control+Alt+Z"
                spellcheck="false"
              />
              <el-button size="small" @click="saveHotkey">保存</el-button>
            </div>
          </div>
        </div>
        <div class="provider-hint">
          用于唤出/隐藏窗口（默认 Control+Alt+Z）；留空表示不注册，被其他应用占用时会提示并恢复旧值。
        </div>
      </div>

      <div class="settings-section">
        <div class="settings-label">通知</div>
        <div class="generic-form">
          <div class="generic-row">
            <span class="generic-label">系统通知（回答完成 / 待确认）</span>
            <el-switch
              :model-value="form.notifications"
              @change="(value) => apply({ notifications: value })"
            />
          </div>
        </div>
      </div>

      <div class="settings-section">
        <div class="settings-label">本机后端</div>
        <div class="generic-form">
          <div class="generic-row">
            <span class="generic-label">项目根目录</span>
            <div class="desktop-inline wide">
              <el-input
                v-model="projectRootDraft"
                size="small"
                placeholder="例如 D:\work\rag_knowledge_base"
                spellcheck="false"
              />
              <el-button size="small" @click="saveProjectRoot">保存</el-button>
            </div>
          </div>
        </div>
        <div class="provider-hint">
          打包版启动本机后端需要知道知问仓库位置（含 backend 目录）；开发模式自动使用仓库目录。
          <template v-if="effectiveRoot">当前生效：{{ effectiveRoot }}</template>
        </div>
      </div>

      <div class="settings-section">
        <div class="settings-label">维护</div>
        <el-space wrap>
          <el-button size="small" @click="openConnectionSettings">连接设置…</el-button>
          <el-button size="small" @click="openLogs">打开日志目录</el-button>
        </el-space>
        <div class="provider-hint">
          连接设置可切换本机/远程后端与访问令牌；卸载请使用系统「应用与功能」里的知问条目。
        </div>
      </div>
    </template>
  </div>
</template>

<script setup>
/**
 * 设置页 · 应用设置（仅桌面客户端生效）。
 * 配置由主进程持有（userData/config.json），经 window.zhiwenShell 读写；
 * 浏览器环境下显示提示，不引入任何 Electron 依赖。
 */
import { computed, onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus/es/components/message/index'

const shell = typeof window !== 'undefined' ? window.zhiwenShell : null
const available = !!(shell && typeof shell.getDesktopConfig === 'function')

const version = ref('')
const packaged = ref(false)
const effectiveRoot = ref('')
const hotkeyDraft = ref('')
const projectRootDraft = ref('')
const checking = ref(false)
const form = reactive({
  launchAtLogin: false,
  notifications: true,
  minimizeToTray: true,
  autoCheckUpdates: true,
})
const update = reactive({
  status: 'idle',
  currentVersion: '',
  version: '',
  progress: 0,
  message: '',
  checkedAt: 0,
})

const updateStatusText = computed(() => {
  switch (update.status) {
    case 'checking':
      return update.message || '正在检查…'
    case 'up-to-date':
      return `已是最新版本（v${update.currentVersion || version.value || '—'}）`
    case 'available':
      return update.message || `发现新版本${update.version ? ' v' + update.version : ''}`
    case 'downloading':
      return `正在下载${update.version ? ' v' + update.version : ''} · ${update.progress}%`
    case 'downloaded':
      return `v${update.version || ''} 已下载，重启后安装`
    case 'error':
      return update.message || '检查失败'
    default:
      return '尚未检查'
  }
})

const updateHint = computed(() => {
  if (!packaged.value) {
    return '开发模式不会执行真实更新；打包版会从 GitHub Releases 检查并后台下载，失败时查询发布页兜底。'
  }
  if (update.status === 'error') {
    return '自动更新失败时会查询 GitHub Releases 作为兜底；也可以直接打开发布页下载安装包。'
  }
  return '打包版从 GitHub Releases 检查新版本并后台下载；发现更新会提示下载进度，退出后自动安装。'
})

const updateBusy = computed(
  () =>
    checking.value ||
    update.status === 'checking' ||
    update.status === 'downloading'
)

function syncUpdateState(state) {
  if (!state || typeof state !== 'object') return
  if (state.status) update.status = String(state.status)
  if ('currentVersion' in state) update.currentVersion = state.currentVersion || ''
  if ('version' in state) update.version = state.version || ''
  if ('progress' in state) update.progress = Number(state.progress) || 0
  if ('message' in state) update.message = state.message || ''
  if ('checkedAt' in state) update.checkedAt = Number(state.checkedAt) || 0
}

async function load() {
  if (!available) return
  try {
    const res = await shell.getDesktopConfig()
    version.value = res?.version || ''
    packaged.value = !!res?.packaged
    effectiveRoot.value = res?.effectiveProjectRoot || ''
    Object.assign(form, res?.config || {})
    hotkeyDraft.value = form.globalHotkey || ''
    projectRootDraft.value = form.projectRoot || ''
    const state = await shell.getUpdateState?.()
    if (state) syncUpdateState(state)
  } catch {
    ElMessage.error('读取桌面设置失败')
  }
}

async function apply(patch) {
  if (!available) return false
  try {
    const res = await shell.saveDesktopConfig(patch)
    if (!res || !res.ok) {
      ElMessage.error((res && res.error) || '保存失败')
      return false
    }
    Object.assign(form, res.config || {})
    hotkeyDraft.value = form.globalHotkey || ''
    projectRootDraft.value = form.projectRoot || ''
    return true
  } catch {
    ElMessage.error('保存失败')
    return false
  }
}

async function saveHotkey() {
  if (await apply({ globalHotkey: hotkeyDraft.value })) ElMessage.success('快捷键已保存')
}

async function saveProjectRoot() {
  if (await apply({ projectRoot: projectRootDraft.value })) ElMessage.success('项目根目录已保存')
}

async function checkUpdates() {
  if (!available || checking.value) return
  checking.value = true
  try {
    const state = await shell.checkForUpdates?.()
    if (state) syncUpdateState(state)
  } catch {
    ElMessage.error('检查更新失败')
  } finally {
    checking.value = false
  }
}

function openUpdatePage() {
  shell?.openUpdatePage?.()
}

async function installUpdate() {
  try {
    const res = await shell?.installUpdate?.()
    if (res && res.ok === false) ElMessage.error(res.error || '无法安装更新')
  } catch {
    ElMessage.error('无法安装更新')
  }
}

function openConnectionSettings() {
  shell?.openConnectionSettings?.()
}

function openLogs() {
  shell?.openLogs?.()
}

let stopUpdateState = null
onMounted(async () => {
  await load()
  stopUpdateState = shell?.onUpdateState?.((state) => syncUpdateState(state)) || null
})
onBeforeUnmount(() => {
  if (stopUpdateState) stopUpdateState()
  stopUpdateState = null
})
</script>

<style scoped>
.desktop-value {
  color: var(--text-1);
  font-size: var(--font-sm);
}

.desktop-tag {
  margin-left: 8px;
  padding: 1px 8px;
  border: 1px solid var(--border);
  border-radius: 999px;
  color: var(--text-3);
  font-size: 11px;
}

.desktop-inline {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 260px;
}

.desktop-inline.wide {
  min-width: 420px;
}

.update-status {
  display: inline-flex;
  align-items: center;
  gap: 8px;
}

.update-dot {
  width: 7px;
  height: 7px;
  flex-shrink: 0;
  border-radius: 50%;
  background: var(--text-3);
}

.update-status.is-checking .update-dot {
  background: var(--primary);
  animation: update-pulse 1.2s var(--ease-in-out) infinite;
}

.update-status.is-up-to-date .update-dot,
.update-status.is-downloaded .update-dot {
  background: var(--success);
}

.update-status.is-available .update-dot,
.update-status.is-downloading .update-dot {
  background: var(--warning);
}

.update-status.is-error .update-dot {
  background: var(--danger);
}

.update-progress {
  display: flex;
  align-items: center;
  gap: 10px;
  max-width: 380px;
  margin: 2px 0 6px;
}

.update-progress :deep(.el-progress) {
  flex: 1;
}

.update-progress span {
  flex-shrink: 0;
  font-size: var(--font-2xs);
  font-variant-numeric: tabular-nums;
  color: var(--text-3);
}

@keyframes update-pulse {
  0%,
  100% {
    opacity: 1;
    box-shadow: 0 0 0 0 color-mix(in srgb, var(--primary) 40%, transparent);
  }
  50% {
    opacity: 0.65;
    box-shadow: 0 0 0 4px transparent;
  }
}
</style>
