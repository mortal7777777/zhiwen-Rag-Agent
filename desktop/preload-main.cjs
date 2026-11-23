'use strict'

/** 主窗口薄桥：只暴露出站能力（通知 / 任务栏提醒 / 运行态），不提供任何可读 IPC。 */

const { contextBridge, ipcRenderer } = require('electron')

// 让页面 CSS 知道自己在桌面壳里（--titlebar-h / 标题条样式）。
// preload 运行于文档极早期，documentElement 可能尚未创建，DOMContentLoaded 再兜底。
function markDesktop() {
  try {
    document.documentElement.dataset.desktop = '1'
  } catch {
    // 忽略
  }
}
if (document.documentElement) markDesktop()
document.addEventListener('DOMContentLoaded', markDesktop, { once: true })

contextBridge.exposeInMainWorld('zhiwenShell', {
  notify: (payload) =>
    ipcRenderer.send('shell:notify', {
      title: String((payload && payload.title) || '知问'),
      body: String((payload && payload.body) || ''),
    }),
  attention: () => ipcRenderer.send('shell:attention'),
  setBusy: (active) => ipcRenderer.send('shell:set-busy', !!active),
  setTitleBarTheme: (payload) =>
    ipcRenderer.send('shell:titlebar', {
      color: String((payload && payload.color) || ''),
      symbolColor: String((payload && payload.symbolColor) || ''),
    }),
  // 应用设置面板
  getDesktopConfig: () => ipcRenderer.invoke('shell:get-desktop-config'),
  saveDesktopConfig: (patch) => ipcRenderer.invoke('shell:save-desktop-config', patch || {}),
  checkForUpdates: () => ipcRenderer.invoke('shell:check-updates'),
  getUpdateState: () => ipcRenderer.invoke('shell:get-update-state'),
  openUpdatePage: () => ipcRenderer.invoke('shell:open-update-page'),
  installUpdate: () => ipcRenderer.invoke('shell:install-update'),
  onUpdateState: (callback) => {
    if (typeof callback !== 'function') return () => {}
    const handler = (_event, state) => callback(state)
    ipcRenderer.on('shell:update-state', handler)
    return () => ipcRenderer.removeListener('shell:update-state', handler)
  },
  openConnectionSettings: () => ipcRenderer.invoke('shell:open-connection-settings'),
  openLogs: () => ipcRenderer.invoke('shell:open-logs'),
})
