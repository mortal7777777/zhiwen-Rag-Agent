'use strict'

/** boot 窗口的窄桥：只暴露启动流程需要的 IPC，主窗口不加载本脚本。 */

const { contextBridge, ipcRenderer } = require('electron')

function markDesktop() {
  try {
    document.documentElement.dataset.desktop = '1'
  } catch {
    // 极早期 DOM 未就绪时忽略
  }
}
if (document.documentElement) markDesktop()
document.addEventListener('DOMContentLoaded', markDesktop, { once: true })

contextBridge.exposeInMainWorld('zhiwenDesktop', {
  getState: () => ipcRenderer.invoke('boot:get-state'),
  saveConfig: (patch) => ipcRenderer.invoke('boot:save-config', patch),
  start: (mode) => ipcRenderer.invoke('boot:start', { mode }),
  openLogs: () => ipcRenderer.invoke('boot:open-logs'),
  closeWindow: () => ipcRenderer.invoke('boot:close-window'),
  quit: () => ipcRenderer.invoke('boot:quit'),
  onState: (callback) => {
    const handler = (_event, state) => callback(state)
    ipcRenderer.on('boot:state', handler)
    return () => ipcRenderer.removeListener('boot:state', handler)
  },
})
