'use strict'

/** 主窗口薄桥：只暴露出站能力（通知 / 任务栏提醒 / 运行态），不提供任何可读 IPC。 */

const { contextBridge, ipcRenderer } = require('electron')

contextBridge.exposeInMainWorld('zhiwenShell', {
  notify: (payload) =>
    ipcRenderer.send('shell:notify', {
      title: String((payload && payload.title) || '知问'),
      body: String((payload && payload.body) || ''),
    }),
  attention: () => ipcRenderer.send('shell:attention'),
  setBusy: (active) => ipcRenderer.send('shell:set-busy', !!active),
})
