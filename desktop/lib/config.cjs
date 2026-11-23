'use strict'

/** 桌面端配置：userData/config.json（原子写入；坏文件备份后回默认值）。 */

const fs = require('fs')
const path = require('path')
const { app } = require('electron')

const DEFAULTS = Object.freeze({
  version: 1,
  mode: 'local',
  remoteBaseUrl: 'http://127.0.0.1:5173',
  pythonPath: '',
  apiToken: '',
  minimizeToTray: true,
  globalHotkey: 'Control+Alt+Z',
  launchAtLogin: false,
  notifications: true,
  projectRoot: '',
  window: { x: null, y: null, width: 1280, height: 800, maximized: false },
})

function userDataDir() {
  return app.getPath('userData')
}

function configPath() {
  return path.join(userDataDir(), 'config.json')
}

function logsDir() {
  return path.join(userDataDir(), 'logs')
}

function normalize(raw) {
  const c = raw && typeof raw === 'object' ? raw : {}
  const win = c.window && typeof c.window === 'object' ? c.window : {}
  return {
    version: 1,
    mode: c.mode === 'remote' ? 'remote' : 'local',
    remoteBaseUrl:
      typeof c.remoteBaseUrl === 'string' && c.remoteBaseUrl.trim()
        ? c.remoteBaseUrl.trim()
        : DEFAULTS.remoteBaseUrl,
    pythonPath: typeof c.pythonPath === 'string' ? c.pythonPath.trim() : '',
    apiToken: typeof c.apiToken === 'string' ? c.apiToken.trim() : '',
    minimizeToTray: c.minimizeToTray !== false,
    globalHotkey:
      typeof c.globalHotkey === 'string' && c.globalHotkey.trim()
        ? c.globalHotkey.trim()
        : DEFAULTS.globalHotkey,
    launchAtLogin: c.launchAtLogin === true,
    notifications: c.notifications !== false,
    projectRoot: typeof c.projectRoot === 'string' ? c.projectRoot.trim() : '',
    window: {
      x: Number.isFinite(win.x) ? win.x : null,
      y: Number.isFinite(win.y) ? win.y : null,
      width: Number.isFinite(win.width) && win.width >= 800 ? win.width : DEFAULTS.window.width,
      height: Number.isFinite(win.height) && win.height >= 500 ? win.height : DEFAULTS.window.height,
      maximized: win.maximized === true,
    },
  }
}

function load() {
  const file = configPath()
  try {
    return normalize(JSON.parse(fs.readFileSync(file, 'utf8')))
  } catch (err) {
    if (err && err.code !== 'ENOENT') {
      try {
        fs.copyFileSync(file, file + '.bad')
      } catch {
        // 备份失败不影响启动
      }
    }
    return normalize({})
  }
}

function save(patch) {
  const current = load()
  const p = patch && typeof patch === 'object' ? patch : {}
  const next = normalize({
    ...current,
    ...p,
    window: { ...current.window, ...(p.window && typeof p.window === 'object' ? p.window : {}) },
  })
  const file = configPath()
  fs.mkdirSync(path.dirname(file), { recursive: true })
  const tmp = file + '.tmp'
  fs.writeFileSync(tmp, JSON.stringify(next, null, 2), 'utf8')
  fs.renameSync(tmp, file)
  return next
}

/** 远程地址校验：仅 http(s)；非本机地址必须 https（避免明文令牌出网）。返回错误文案或 null。 */
function validateRemoteUrl(value) {
  let url
  try {
    url = new URL(String(value || '').trim())
  } catch {
    return '地址格式不正确（示例：http://127.0.0.1:5173）'
  }
  if (url.protocol !== 'http:' && url.protocol !== 'https:') return '仅支持 http(s) 地址'
  const host = url.hostname.toLowerCase()
  const isLocal = host === 'localhost' || host === '127.0.0.1' || host === '::1' || host === '[::1]'
  if (!isLocal && url.protocol !== 'https:') return '非本机地址必须使用 https'
  return null
}

module.exports = { DEFAULTS, load, save, validateRemoteUrl, configPath, logsDir, userDataDir }
