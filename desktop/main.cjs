'use strict'

/** 知问桌面端主进程：单实例、boot 窗口 → 主窗口、安全策略、IPC、退出时序。 */

const { app, BrowserWindow, Menu, Notification, Tray, dialog, globalShortcut, ipcMain, nativeImage, powerSaveBlocker, shell } = require('electron')
const { autoUpdater } = require('electron-updater')
const fs = require('fs')
const path = require('path')

const config = require('./lib/config.cjs')
const probe = require('./lib/probe.cjs')
const { BootFlow } = require('./lib/bootflow.cjs')

const APP_ID = 'com.zhiwen.desktop'
const BOOT_STATE_CHANNEL = 'boot:state'
const devRepoRoot = path.resolve(__dirname, '..')
const SHOT_DIR = process.env.ZHIWEN_SHOT_DIR || ''
const ICON_256 = path.join(__dirname, 'assets', 'icon-256.png')
const TRAY_ICON = path.join(__dirname, 'assets', 'tray-32.png')

// 统一 dev/打包版本的 userData 路径（%APPDATA%\zhiwen-desktop），配置互通
app.setName('zhiwen-desktop')
app.setAppUserModelId(APP_ID)

let currentConfig = null
let bootWindow = null
let mainWindow = null
let bootflow = null
let quitting = false
let autoQuitScheduled = false
let tray = null
let busyActive = false
let powerBlockerId = null
let lastFlowPhase = 'idle'
let lastFlowMessage = ''
let suppressQuitOnClose = false
let allowedShellOrigin = ''
let activeHotkey = ''
let pendingConvId = null
let updateInfo = { checking: false, manual: false, version: '', progress: 0, downloaded: false }

const gotLock = app.requestSingleInstanceLock()
if (!gotLock) {
  app.quit()
} else {
  app.on('second-instance', (_event, argv) => {
    showAppWindow()
    const link = parseDeepLink(argv)
    if (link) applyDeepLink(link)
  })

  app.whenReady().then(() => {
    currentConfig = config.load()
    installAppMenu()
    bootflow = createBootFlow()
    registerIpc()
    createTray()
    createBootWindow()
    registerGlobalHotkey(currentConfig.globalHotkey)
    applyLoginItem(currentConfig.launchAtLogin)
    registerProtocolClient()
    setupAutoUpdater()
    const initialLink = parseDeepLink(process.argv)
    if (initialLink) pendingConvId = initialLink.conv
    if (process.env.ZHIWEN_AUTOSTART === '1') {
      bootWindow.webContents.once('did-finish-load', () => {
        setTimeout(() => {
          if (bootflow) bootflow.run(currentConfig.mode)
        }, 300)
      })
    }
  })
}

app.on('window-all-closed', () => {
  app.quit()
})

app.on('will-quit', () => {
  try {
    globalShortcut.unregisterAll()
  } catch {
    // 忽略
  }
  activeHotkey = ''
  if (powerBlockerId != null) {
    try {
      powerSaveBlocker.stop(powerBlockerId)
    } catch {
      // 忽略
    }
    powerBlockerId = null
  }
  if (tray) {
    try {
      tray.destroy()
    } catch {
      // 忽略
    }
    tray = null
  }
})

// 退出时序：自己拉起的后端先收进程树（等端口释放），attached 的后端不杀
app.on('before-quit', (event) => {
  if (quitting) return
  quitting = true
  if (bootflow && bootflow.ownsBackend) {
    event.preventDefault()
    bootflow
      .shutdown()
      .catch(() => {})
      .finally(() => app.quit())
    setTimeout(() => app.quit(), 8000)
  }
})

function createBootFlow() {
  return new BootFlow({
    config: () => currentConfig,
    probe,
    validateRemoteUrl: config.validateRemoteUrl,
    paths: () => resolveProjectPaths(),
    logsDir: config.logsDir(),
    onState: handleFlowState,
    onReady: ({ url, token }) => openMainWindow(url, token),
    onBackendLost: handleBackendLost,
  })
}

/**
 * 仓库路径解析：
 * - 开发模式（electron .）：仓库 = desktop 的上一级，dist = frontend/dist。
 * - 打包模式：dist 随包（resources/frontend-dist）；本机后端需要用户在设置里指定
 *   项目根目录（含 backend 的 rag_knowledge_base），因为 torch/Paddle 无法随包分发。
 */
function resolveProjectPaths() {
  if (!app.isPackaged) {
    return {
      repoRoot: devRepoRoot,
      distDir: path.join(devRepoRoot, 'frontend', 'dist'),
      backendDir: path.join(devRepoRoot, 'backend'),
    }
  }
  const root =
    currentConfig && typeof currentConfig.projectRoot === 'string'
      ? currentConfig.projectRoot.trim()
      : ''
  return {
    repoRoot: root,
    distDir: path.join(process.resourcesPath, 'frontend-dist'),
    backendDir: root ? path.join(root, 'backend') : '',
  }
}

function createBootWindow({ settings = false } = {}) {
  bootWindow = new BrowserWindow({
    width: 760,
    height: 680,
    minWidth: 640,
    minHeight: 560,
    show: false,
    autoHideMenuBar: true,
    title: '知问 ZhiWen',
    icon: ICON_256,
    backgroundColor: '#0f1115',
    webPreferences: {
      preload: path.join(__dirname, 'preload.cjs'),
      contextIsolation: true,
      sandbox: true,
      nodeIntegration: false,
      spellcheck: false,
    },
  })
  bootWindow.loadFile(
    path.join(__dirname, 'boot', 'index.html'),
    settings ? { query: { settings: '1' } } : undefined
  )
  bootWindow.once('ready-to-show', () => bootWindow.show())
  bootWindow.webContents.setWindowOpenHandler(({ url }) => {
    safeOpenExternal(url)
    return { action: 'deny' }
  })
  bootWindow.on('closed', () => {
    bootWindow = null
  })
}

function handleFlowState(state) {
  console.log('[state]', state.phase, '-', state.message)
  lastFlowPhase = state.phase
  lastFlowMessage = state.message || ''
  updateTrayTooltip()
  if (bootWindow && !bootWindow.isDestroyed()) {
    bootWindow.webContents.send(BOOT_STATE_CHANNEL, state)
  }
  if (state.phase === 'error') {
    shot('boot-error', 120)
    maybeAutoQuitAfterError()
  } else {
    // boot 窗口在进入主界面后马上关闭，截图延迟必须小
    shot('boot-' + state.phase, 60)
  }
}

function maybeAutoQuitAfterError() {
  const ms = Number(process.env.ZHIWEN_AUTO_QUIT_MS || 0)
  if (!ms || autoQuitScheduled) return
  autoQuitScheduled = true
  setTimeout(() => app.quit(), 2500)
}

function openMainWindow(url, token) {
  if (mainWindow && !mainWindow.isDestroyed()) return
  const winCfg = currentConfig.window || {}
  const options = {
    width: winCfg.width || 1280,
    height: winCfg.height || 800,
    minWidth: 960,
    minHeight: 640,
    show: false,
    autoHideMenuBar: true,
    title: '知问 ZhiWen',
    icon: ICON_256,
    backgroundColor: '#ffffff',
    webPreferences: {
      preload: path.join(__dirname, 'preload-main.cjs'),
      contextIsolation: true,
      sandbox: true,
      nodeIntegration: false,
      spellcheck: false,
      // 最小化/被遮挡时不要节流渲染：SSE 增量与打字机不能断
      backgroundThrottling: false,
    },
  }
  if (Number.isFinite(winCfg.x) && Number.isFinite(winCfg.y)) {
    options.x = winCfg.x
    options.y = winCfg.y
  }
  mainWindow = new BrowserWindow(options)
  if (winCfg.maximized) mainWindow.maximize()

  let allowedOrigin = ''
  try {
    allowedOrigin = new URL(url).origin
  } catch {
    // url 由 bootflow 保证合法
  }
  allowedShellOrigin = allowedOrigin
  const wc = mainWindow.webContents
  wc.setWindowOpenHandler(({ url: target }) => {
    safeOpenExternal(target)
    return { action: 'deny' }
  })
  const guardNavigation = (event, target) => {
    let origin = ''
    try {
      origin = new URL(target).origin
    } catch {
      // 非法 URL 一律拦下
    }
    if (origin && origin === allowedOrigin) return
    event.preventDefault()
    safeOpenExternal(target)
  }
  wc.on('will-navigate', guardNavigation)
  wc.on('will-redirect', guardNavigation)

  let injected = false
  let shown = false
  let destroyingForError = false

  const reveal = () => {
    if (shown || !mainWindow || mainWindow.isDestroyed()) return
    shown = true
    mainWindow.show()
    mainWindow.focus()
    updateTrayTooltip()
    if (bootflow) bootflow.markOnline()
    if (bootWindow && !bootWindow.isDestroyed()) bootWindow.close()
    shot('main-online')
    if (pendingConvId) {
      const convId = pendingConvId
      pendingConvId = null
      navigateToConversation(convId)
    }
    // 稳态截图（等首屏设置/会话数据加载完成）
    setTimeout(() => shot('main-steady', 0), 3000)
    scheduleAutoQuit()
  }

  wc.on('did-finish-load', async () => {
    if (!injected) {
      injected = true
      if (token) {
        const script =
          `(function(){try{var k='zhiwen_api_token';` +
          `if(localStorage.getItem(k)!==${JSON.stringify(token)}){` +
          `localStorage.setItem(k,${JSON.stringify(token)});return true}return false}catch(e){return false}})()`
        try {
          const changed = await wc.executeJavaScript(script, false)
          if (changed) {
            wc.reload()
            return
          }
        } catch {
          // 注入失败时按无令牌继续（前端 401 弹窗兜底）
        }
      }
    }
    reveal()
  })

  wc.on('did-fail-load', (_event, code, description, validatedURL, isMainFrame) => {
    if (!isMainFrame || code === -3 || shown) return
    destroyingForError = true
    if (bootflow) bootflow.reportLoadError('界面加载失败', `${description}（${validatedURL || url}）`)
    if (mainWindow && !mainWindow.isDestroyed()) mainWindow.destroy()
    mainWindow = null
  })

  wc.on('render-process-gone', (_event, details) => {
    const reason = details && details.reason ? details.reason : 'unknown'
    if (shown) {
      const win = mainWindow && !mainWindow.isDestroyed() ? mainWindow : null
      dialog
        .showMessageBox(win, { type: 'error', title: '知问', message: '界面进程异常退出', detail: '原因：' + reason })
        .catch(() => {})
      return
    }
    destroyingForError = true
    if (bootflow) bootflow.reportLoadError('界面进程启动失败', '原因：' + reason)
    if (mainWindow && !mainWindow.isDestroyed()) mainWindow.destroy()
    mainWindow = null
  })

  mainWindow.on('focus', () => mainWindow && !mainWindow.isDestroyed() && mainWindow.flashFrame(false))
  mainWindow.on('close', (event) => {
    persistBounds(mainWindow)
    // 关闭窗口 = 收进托盘（sidecar 与生成任务继续跑），真正退出走托盘菜单
    if (!quitting && !destroyingForError && currentConfig.minimizeToTray !== false) {
      event.preventDefault()
      mainWindow.hide()
      updateTrayTooltip()
    }
  })
  mainWindow.on('closed', () => {
    mainWindow = null
    if (suppressQuitOnClose) {
      suppressQuitOnClose = false
      return
    }
    if (!quitting && !destroyingForError) app.quit()
  })

  mainWindow.loadURL(url)

  // 开发验证：确认主窗口薄桥可用，并走一遍通知/运行态链路
  if (process.env.ZHIWEN_VERIFY_BRIDGE === '1') {
    setTimeout(async () => {
      try {
        const ok = await wc.executeJavaScript(
          "typeof window.zhiwenShell === 'object' && !!window.zhiwenShell"
        )
        console.log('[verify] shell bridge:', ok)
        if (ok) {
          await wc.executeJavaScript(
            "window.zhiwenShell.notify({ title: '知问测试', body: '通知链路验证' }); window.zhiwenShell.setBusy(true)"
          )
          setTimeout(() => {
            wc.executeJavaScript("window.zhiwenShell.setBusy(false)").catch(() => {})
          }, 1500)
          // 关闭到托盘：close 应被拦截为 hide，应用不退出
          setTimeout(() => {
            try {
              mainWindow.close()
              setTimeout(() => {
                const win = mainWindow && !mainWindow.isDestroyed() ? mainWindow : null
                console.log('[verify] close-to-tray:', win ? !win.isVisible() : 'window-gone', 'tray:', !!tray)
                if (win) {
                  win.show()
                  win.focus()
                }
              }, 600)
            } catch (err) {
              console.log('[verify] close-to-tray failed', err && err.message ? err.message : err)
            }
          }, 2600)
        }
      } catch (err) {
        console.log('[verify] failed', err && err.message ? err.message : err)
      }
    }, 1200)
  }

  // 开发验证：设置窗口（settings 模式）+ 深链解析
  if (process.env.ZHIWEN_VERIFY_P1 === '1') {
    setTimeout(() => {
      openSettingsWindow()
      setTimeout(async () => {
        try {
          if (SHOT_DIR && bootWindow && !bootWindow.isDestroyed()) {
            const image = await bootWindow.capturePage()
            fs.mkdirSync(SHOT_DIR, { recursive: true })
            fs.writeFileSync(path.join(SHOT_DIR, `settings-window-${Date.now()}.png`), image.toPNG())
            console.log('[verify] settings window shot')
          }
        } catch (err) {
          console.log('[verify] settings shot failed', err && err.message ? err.message : err)
        }
        const link = parseDeepLink(['zhiwen://chat?conv=42'])
        console.log('[verify] deeplink parse:', JSON.stringify(link))
        applyDeepLink(link)
        // 设置页「保存并应用」重连：主窗应被销毁后重建（boot 流程就绪后自动关本窗）
        setTimeout(() => {
          if (bootWindow && !bootWindow.isDestroyed()) {
            bootWindow.webContents
              .executeJavaScript("document.getElementById('btnStart').click(); true")
              .catch(() => {})
            console.log('[verify] settings apply clicked')
          }
        }, 1500)
      }, 1000)
    }, 6000)
  }
}

function handleBackendLost(info) {
  const detail = info && info.code != null ? `退出码：${info.code}` : info && info.error ? info.error.message : ''
  notifyUser('本机后端已退出', (detail ? detail + '，' : '') + '正在回到启动页，可重连', { force: true })
  // 应用内重连：销毁主窗、回到 boot 页并重新跑启动流程（不重启整个应用）
  if (bootWindow && !bootWindow.isDestroyed()) {
    if (bootflow) bootflow.run(currentConfig.mode)
    return
  }
  suppressQuitOnClose = true
  if (mainWindow && !mainWindow.isDestroyed()) mainWindow.destroy()
  mainWindow = null
  createBootWindow()
  setTimeout(() => {
    if (bootflow) bootflow.run(currentConfig.mode)
  }, 300)
}

function persistBounds(win) {
  if (!win || win.isDestroyed()) return
  try {
    const maximized = win.isMaximized()
    const b = maximized ? win.getNormalBounds() : win.getBounds()
    currentConfig = config.save({ window: { x: b.x, y: b.y, width: b.width, height: b.height, maximized } })
  } catch {
    // 窗口位置记忆失败不阻断退出
  }
}

function safeOpenExternal(target) {
  try {
    const u = new URL(target)
    if (u.protocol === 'http:' || u.protocol === 'https:') shell.openExternal(target)
  } catch {
    // 非 http(s) 链接不打开
  }
}

function shot(name, delayMs = 300) {
  if (!SHOT_DIR) return
  const win = mainWindow && !mainWindow.isDestroyed() ? mainWindow : bootWindow
  if (!win || win.isDestroyed()) return
  setTimeout(async () => {
    if (!win || win.isDestroyed()) {
      console.log('[shot] skipped (window closed):', name)
      return
    }
    try {
      const image = await win.capturePage()
      fs.mkdirSync(SHOT_DIR, { recursive: true })
      fs.writeFileSync(path.join(SHOT_DIR, `${name}-${Date.now()}.png`), image.toPNG())
      console.log('[shot] saved:', name)
    } catch (err) {
      console.log('[shot] failed:', name, err && err.message ? err.message : err)
    }
  }, delayMs)
}

function scheduleAutoQuit() {
  if (autoQuitScheduled) return
  const ms = Number(process.env.ZHIWEN_AUTO_QUIT_MS || 0)
  if (!ms) return
  autoQuitScheduled = true
  setTimeout(() => app.quit(), ms)
}

function installAppMenu() {
  const isDev = !app.isPackaged
  const viewItems = [
    {
      label: '重新加载',
      accelerator: 'CmdOrCtrl+R',
      click: () => {
        const win = focusedWindow()
        if (win) win.reload()
      },
    },
    { type: 'separator' },
    { role: 'zoomIn', label: '放大' },
    { role: 'zoomOut', label: '缩小' },
    { role: 'resetZoom', label: '实际大小' },
    { type: 'separator' },
    { role: 'togglefullscreen', label: '全屏' },
  ]
  if (isDev) {
    viewItems.push({ type: 'separator' })
    viewItems.push({
      label: '开发者工具',
      accelerator: 'F12',
      click: () => {
        const win = focusedWindow()
        if (win) win.webContents.toggleDevTools()
      },
    })
  }
  Menu.setApplicationMenu(
    Menu.buildFromTemplate([
      {
        label: '知问',
        submenu: [
          { label: '连接设置…', click: () => openSettingsWindow() },
          { label: '检查更新…', click: () => checkForUpdates(true) },
          { type: 'separator' },
          { label: '打开日志目录', click: () => shell.openPath(config.logsDir()) },
          { type: 'separator' },
          { role: 'quit', label: '退出' },
        ],
      },
      {
        label: '编辑',
        submenu: [
          { role: 'undo', label: '撤销' },
          { role: 'redo', label: '重做' },
          { type: 'separator' },
          { role: 'cut', label: '剪切' },
          { role: 'copy', label: '复制' },
          { role: 'paste', label: '粘贴' },
          { role: 'selectAll', label: '全选' },
        ],
      },
      { label: '视图', submenu: viewItems },
      {
        label: '帮助',
        submenu: [
          {
            label: '关于知问桌面端',
            click: () =>
              dialog.showMessageBox({
                type: 'info',
                title: '知问 ZhiWen',
                message: '知问 ZhiWen 桌面客户端',
                detail: `版本 v${app.getVersion()}`,
              }),
          },
        ],
      },
    ])
  )
}

function focusedWindow() {
  const win = BrowserWindow.getFocusedWindow()
  if (win && !win.isDestroyed()) return win
  if (mainWindow && !mainWindow.isDestroyed()) return mainWindow
  if (bootWindow && !bootWindow.isDestroyed()) return bootWindow
  return null
}

function openSettingsWindow() {
  if (bootWindow && !bootWindow.isDestroyed()) {
    bootWindow.show()
    bootWindow.focus()
    return
  }
  createBootWindow({ settings: true })
}

/** 注册全局热键（空字符串 = 取消）；失败时尝试恢复原热键并返回 false。 */
function registerGlobalHotkey(accel) {
  const previous = activeHotkey
  try {
    if (activeHotkey) globalShortcut.unregister(activeHotkey)
  } catch {
    // 忽略
  }
  activeHotkey = ''
  if (!accel) return true
  try {
    const ok = globalShortcut.register(accel, () => toggleAppWindow())
    if (!ok) throw new Error('register returned false')
    activeHotkey = accel
    console.log('[hotkey] registered', accel)
    return true
  } catch (err) {
    console.log('[hotkey] failed', accel, err && err.message ? err.message : err)
    if (previous) {
      try {
        if (globalShortcut.register(previous, () => toggleAppWindow())) activeHotkey = previous
      } catch {
        // 恢复失败则保持无热键
      }
    }
    return false
  }
}

function toggleAppWindow() {
  const win = mainWindow && !mainWindow.isDestroyed() ? mainWindow : bootWindow
  if (!win || win.isDestroyed()) {
    showAppWindow()
    return
  }
  if (win.isVisible() && win.isFocused()) {
    win.hide()
  } else {
    if (win.isMinimized()) win.restore()
    win.show()
    win.focus()
  }
}

function applyLoginItem(enabled) {
  try {
    app.setLoginItemSettings({
      openAtLogin: !!enabled,
      path: process.execPath,
      args: process.defaultApp ? [path.resolve(process.argv[1])] : [],
    })
    console.log('[login-item]', !!enabled)
  } catch (err) {
    console.log('[login-item] failed', err && err.message ? err.message : err)
  }
}

function setupAutoUpdater() {
  if (!app.isPackaged) {
    console.log('[updater] dev mode: auto update disabled')
    return
  }
  autoUpdater.autoDownload = true
  autoUpdater.autoInstallOnAppQuit = true
  autoUpdater.logger = {
    info: (m) => console.log('[updater]', m),
    warn: (m) => console.log('[updater]', m),
    error: (m) => console.log('[updater]', m),
    debug: () => {},
  }
  autoUpdater.on('checking-for-update', () => {
    updateInfo.checking = true
    updateTrayTooltip()
  })
  autoUpdater.on('update-available', (info) => {
    updateInfo.version = info && info.version ? info.version : ''
    notifyUser('发现新版本 ' + updateInfo.version, '正在后台下载更新…', { force: true })
    updateTrayTooltip()
  })
  autoUpdater.on('update-not-available', () => {
    if (updateInfo.manual) {
      notifyUser('已是最新版本', `当前 v${app.getVersion()}`, { force: true })
    }
    updateInfo = { ...updateInfo, checking: false, manual: false }
    updateTrayTooltip()
  })
  autoUpdater.on('download-progress', (progress) => {
    updateInfo.progress = Math.round((progress && progress.percent) || 0)
    if (mainWindow && !mainWindow.isDestroyed()) {
      mainWindow.setProgressBar(Math.min(1, (progress && progress.percent ? progress.percent : 0) / 100))
    }
    updateTrayTooltip()
  })
  autoUpdater.on('update-downloaded', (info) => {
    updateInfo.downloaded = true
    updateInfo.progress = 100
    if (mainWindow && !mainWindow.isDestroyed()) mainWindow.setProgressBar(-1)
    updateTrayTooltip()
    const choice = dialog.showMessageBoxSync({
      type: 'info',
      title: '知问',
      message: '新版本已下载',
      detail: `版本 ${info && info.version ? info.version : ''} 已就绪，重启后生效。`,
      buttons: ['立即重启并更新', '稍后'],
      defaultId: 0,
      cancelId: 1,
    })
    if (choice === 0) {
      try {
        autoUpdater.quitAndInstall(false, true)
      } catch (err) {
        console.log('[updater] quitAndInstall failed', err && err.message ? err.message : err)
        app.quit()
      }
    }
  })
  autoUpdater.on('error', (err) => {
    console.log('[updater] error', err && err.message ? err.message : err)
    if (updateInfo.manual) {
      notifyUser('检查更新失败', (err && err.message) || '网络或更新源不可用', { force: true })
    }
    updateInfo = { ...updateInfo, checking: false, manual: false }
    updateTrayTooltip()
  })
  // 启动 15 秒后静默检查一次（不阻塞界面）
  setTimeout(() => checkForUpdates(false), 15000)
}

function checkForUpdates(manual) {
  if (!app.isPackaged) {
    if (manual) {
      dialog.showMessageBox({
        type: 'info',
        title: '检查更新',
        message: '开发模式不支持自动更新',
        detail: '请使用安装后的打包版本（桌面快捷方式启动）。',
      })
    }
    return
  }
  updateInfo = { ...updateInfo, checking: true, manual: !!manual }
  updateTrayTooltip()
  autoUpdater.checkForUpdates().catch((err) => {
    console.log('[updater] check failed', err && err.message ? err.message : err)
  })
}

function registerProtocolClient() {
  try {
    if (process.defaultApp && process.argv.length >= 2) {
      app.setAsDefaultProtocolClient('zhiwen', process.execPath, [path.resolve(process.argv[1])])
    } else {
      app.setAsDefaultProtocolClient('zhiwen')
    }
    console.log('[protocol] registered zhiwen://')
  } catch (err) {
    console.log('[protocol] failed', err && err.message ? err.message : err)
  }
}

/** 解析 zhiwen://chat?conv=<id>（也接受 zhiwen://open?conv=<id>）。 */
function parseDeepLink(argv) {
  const url = (argv || []).find((arg) => typeof arg === 'string' && arg.startsWith('zhiwen://'))
  if (!url) return null
  try {
    const parsed = new URL(url)
    const conv = Number(parsed.searchParams.get('conv'))
    if (Number.isFinite(conv) && conv > 0) return { conv }
  } catch {
    // 非法 URL 忽略
  }
  return null
}

function applyDeepLink(link) {
  if (!link || !link.conv) return
  if (mainWindow && !mainWindow.isDestroyed() && allowedShellOrigin) {
    navigateToConversation(link.conv)
    return
  }
  pendingConvId = link.conv
}

function navigateToConversation(convId) {
  if (!mainWindow || mainWindow.isDestroyed() || !allowedShellOrigin) return
  const target = `${allowedShellOrigin}/?conv=${convId}`
  console.log('[deeplink] navigate ->', target)
  mainWindow.loadURL(target)
}

function createTray() {
  try {
    let image = nativeImage.createFromPath(TRAY_ICON)
    if (image.isEmpty()) image = nativeImage.createFromPath(ICON_256)
    if (image.isEmpty()) {
      console.log('[tray] icon missing, tray disabled')
      return
    }
    tray = new Tray(image)
    tray.setToolTip(trayStatusText())
    tray.on('click', () => showAppWindow())
    tray.on('double-click', () => showAppWindow())
    tray.on('right-click', () => tray.popUpContextMenu(buildTrayMenu()))
    console.log('[tray] ready')
  } catch (err) {
    console.log('[tray] failed', err && err.message ? err.message : err)
  }
}

function buildTrayMenu() {
  return Menu.buildFromTemplate([
    { label: '打开知问', click: () => showAppWindow() },
    { label: '连接设置…', click: () => openSettingsWindow() },
    { label: '检查更新', click: () => checkForUpdates(true) },
    { label: trayStatusText(), enabled: false },
    { type: 'separator' },
    { label: '打开日志目录', click: () => shell.openPath(config.logsDir()) },
    { type: 'separator' },
    { label: '退出', click: () => app.quit() },
  ])
}

function trayStatusText() {
  const modeLabel = currentConfig && currentConfig.mode === 'remote' ? '远程模式' : '本地模式'
  const phaseMap = {
    idle: '未启动',
    checking: '检查环境',
    starting: '启动后端',
    probing: '连接服务器',
    waiting: '等待就绪',
    loading: '打开界面',
    online: '已连接',
    error: '连接失败',
  }
  const phase = phaseMap[lastFlowPhase] || lastFlowPhase
  let extra = busyActive ? ' · 生成中' : ''
  if (updateInfo.downloaded) extra += ' · 更新就绪'
  else if (updateInfo.progress > 0) extra += ` · 更新下载 ${updateInfo.progress}%`
  else if (updateInfo.checking) extra += ' · 检查更新'
  return `知问 ZhiWen · ${modeLabel} · ${phase}${extra}`
}

function updateTrayTooltip() {
  if (!tray) return
  try {
    tray.setToolTip(trayStatusText())
  } catch {
    // 托盘已销毁时忽略
  }
}

function showAppWindow() {
  const win = mainWindow && !mainWindow.isDestroyed() ? mainWindow : bootWindow
  if (win && !win.isDestroyed()) {
    if (win.isMinimized()) win.restore()
    win.show()
    win.focus()
    return
  }
  createBootWindow()
  setTimeout(() => {
    if (bootflow) bootflow.run(currentConfig.mode)
  }, 300)
}

function notifyUser(title, body, { force = false } = {}) {
  if (currentConfig && currentConfig.notifications === false) return
  const win = mainWindow && !mainWindow.isDestroyed() ? mainWindow : null
  const focused = win && win.isVisible() && win.isFocused()
  if (!force && focused) return
  try {
    const notification = new Notification({ title, body, icon: ICON_256 })
    notification.on('click', () => showAppWindow())
    notification.show()
  } catch (err) {
    console.log('[notify] failed', err && err.message ? err.message : err)
  }
}

function setBusyState(active) {
  busyActive = active
  console.log('[busy]', active)
  if (mainWindow && !mainWindow.isDestroyed()) {
    mainWindow.setProgressBar(active ? 2 : -1)
  }
  if (active) {
    if (powerBlockerId == null) powerBlockerId = powerSaveBlocker.start('prevent-display-sleep')
  } else if (powerBlockerId != null) {
    try {
      powerSaveBlocker.stop(powerBlockerId)
    } catch {
      // 忽略
    }
    powerBlockerId = null
  }
  updateTrayTooltip()
}

async function collectDetection() {
  const paths = resolveProjectPaths()
  const py = probe.detectPython(currentConfig, paths.repoRoot)
  const distExists = fs.existsSync(path.join(paths.distDir, 'index.html'))
  const live = await probe.probeLivez('http://127.0.0.1:8000', 1200)
  let backend = 'free'
  if (live.ok) backend = 'running'
  else if (await probe.portInUse(8000)) backend = 'occupied'
  const token = probe.resolveEffectiveToken(currentConfig, paths.repoRoot)
  return {
    python: py,
    distExists,
    backend,
    tokenSource: token.source,
    projectRoot: paths.repoRoot,
    packaged: app.isPackaged,
    // 主界面运行时 17800 是桌面端自己占用的，不算冲突
    staticPortInUse: !(bootflow && bootflow.online) && (await probe.portInUse(17800)),
  }
}

function fromBoot(event) {
  return !!bootWindow && !bootWindow.isDestroyed() && event.sender === bootWindow.webContents
}

function registerIpc() {
  ipcMain.handle('boot:get-state', async (event) => {
    if (!fromBoot(event)) throw new Error('forbidden')
    return {
      config: currentConfig,
      detected: await collectDetection(),
      fakeState: process.env.ZHIWEN_FAKE_STATE || '',
    }
  })

  ipcMain.handle('boot:save-config', (event, patch) => {
    if (!fromBoot(event)) throw new Error('forbidden')
    const p = patch && typeof patch === 'object' ? patch : {}
    const nextMode = p.mode === 'remote' ? 'remote' : 'local'
    const nextUrl =
      typeof p.remoteBaseUrl === 'string' ? p.remoteBaseUrl.trim() : currentConfig.remoteBaseUrl
    if (nextMode === 'remote') {
      const err = config.validateRemoteUrl(nextUrl)
      if (err) return { ok: false, error: err, config: currentConfig }
    }
    const nextHotkey =
      typeof p.globalHotkey === 'string' ? p.globalHotkey.trim() : currentConfig.globalHotkey
    if (nextHotkey !== currentConfig.globalHotkey && !registerGlobalHotkey(nextHotkey)) {
      return { ok: false, error: '全局快捷键无效或已被其他应用占用', config: currentConfig }
    }
    currentConfig = config.save({
      mode: nextMode,
      remoteBaseUrl: nextUrl,
      pythonPath: typeof p.pythonPath === 'string' ? p.pythonPath.trim() : currentConfig.pythonPath,
      apiToken: typeof p.apiToken === 'string' ? p.apiToken : currentConfig.apiToken,
      launchAtLogin:
        typeof p.launchAtLogin === 'boolean' ? p.launchAtLogin : currentConfig.launchAtLogin,
      notifications:
        typeof p.notifications === 'boolean' ? p.notifications : currentConfig.notifications,
      projectRoot:
        typeof p.projectRoot === 'string' ? p.projectRoot.trim() : currentConfig.projectRoot,
      globalHotkey: nextHotkey,
    })
    applyLoginItem(currentConfig.launchAtLogin)
    return { ok: true, error: '', config: currentConfig }
  })

  ipcMain.handle('boot:start', (event, payload) => {
    if (!fromBoot(event)) throw new Error('forbidden')
    if (!bootflow || bootflow.running) return { ok: false }
    const mode = payload && payload.mode === 'remote' ? 'remote' : 'local'
    currentConfig = config.save({ mode })
    // 设置页应用：先销毁旧主窗，再由启动流程创建新窗口（不退出应用）
    if (mainWindow && !mainWindow.isDestroyed()) {
      suppressQuitOnClose = true
      mainWindow.destroy()
      mainWindow = null
    }
    bootflow.run(mode)
    return { ok: true }
  })

  ipcMain.handle('boot:open-logs', async (event) => {
    if (!fromBoot(event)) throw new Error('forbidden')
    try {
      fs.mkdirSync(config.logsDir(), { recursive: true })
      await shell.openPath(config.logsDir())
    } catch {
      // 打开失败静默
    }
    return { ok: true }
  })

  ipcMain.handle('boot:quit', (event) => {
    if (!fromBoot(event)) throw new Error('forbidden')
    app.quit()
    return { ok: true }
  })

  ipcMain.handle('boot:close-window', (event) => {
    if (!fromBoot(event)) throw new Error('forbidden')
    if (bootWindow && !bootWindow.isDestroyed()) bootWindow.close()
    return { ok: true }
  })

  // 主窗口薄桥（preload-main.cjs）：只接收出站指令，且必须来自主窗口的合法 origin
  ipcMain.on('shell:notify', (event, payload) => {
    if (!fromMain(event)) return
    const title = String((payload && payload.title) || '知问').slice(0, 80)
    const body = String((payload && payload.body) || '').slice(0, 240)
    console.log('[shell] notify:', title, '-', body)
    notifyUser(title, body)
  })
  ipcMain.on('shell:attention', (event) => {
    if (!fromMain(event)) return
    if (mainWindow && !mainWindow.isDestroyed()) {
      mainWindow.flashFrame(true)
      updateTrayTooltip()
    }
  })
  ipcMain.on('shell:set-busy', (event, active) => {
    if (!fromMain(event)) return
    setBusyState(!!active)
  })
}

function fromMain(event) {
  if (!mainWindow || mainWindow.isDestroyed() || event.sender !== mainWindow.webContents) return false
  const frameUrl = event.senderFrame ? event.senderFrame.url : ''
  return !allowedShellOrigin || frameUrl.startsWith(allowedShellOrigin)
}
