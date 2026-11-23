'use strict'

/** boot 页逻辑：纯浏览器 JS，只经 window.zhiwenDesktop 桥与主进程通信。 */

const bridge = window.zhiwenDesktop
const $ = (id) => document.getElementById(id)

const PHASE_STEP = { checking: 0, starting: 1, probing: 1, waiting: 2, loading: 3, online: 4 }

const state = { mode: 'local', busy: false, phaseStep: 0 }
const settingsMode = new URLSearchParams(window.location.search).get('settings') === '1'
const startLabel = settingsMode ? '保存并应用' : '启动知问'

function applyMode() {
  document.querySelectorAll('.mode').forEach((el) => {
    const selected = el.dataset.mode === state.mode
    el.classList.toggle('selected', selected)
    el.setAttribute('aria-checked', String(selected))
  })
  document.querySelectorAll('[data-for]').forEach((el) => {
    el.hidden = el.dataset.for !== state.mode
  })
  const serverStep = document.querySelector('.step[data-step="1"] .label')
  if (serverStep) serverStep.textContent = state.mode === 'remote' ? '连接服务器' : '启动后端'
}

function setBusy(busy) {
  state.busy = busy
  $('btnStart').disabled = busy
  document.querySelectorAll('.mode').forEach((el) => {
    el.disabled = busy
  })
  document.querySelectorAll('.fields input').forEach((el) => {
    el.disabled = busy
  })
  if (busy) $('btnStart').textContent = '启动中…'
  else if ($('btnStart').textContent === '启动中…') $('btnStart').textContent = startLabel
}

function showSteps(show) {
  $('steps').hidden = !show
}

function resetSteps() {
  state.phaseStep = 0
  document.querySelectorAll('.step').forEach((el) => el.classList.remove('done', 'active', 'error'))
  $('statusLine').textContent = ''
}

function setPhase(phase, message) {
  const idx = PHASE_STEP[phase]
  document.querySelectorAll('.step').forEach((el) => {
    const i = Number(el.dataset.step)
    el.classList.remove('done', 'active', 'error')
    if (phase === 'error') {
      if (i < state.phaseStep) el.classList.add('done')
      return
    }
    if (idx == null) return
    if (i < idx) el.classList.add('done')
    else if (i === idx) el.classList.add(phase === 'online' ? 'done' : 'active')
  })
  if (idx != null) state.phaseStep = Math.min(idx, 3)
  if (message) $('statusLine').textContent = message
}

function markErrorStep() {
  const el = document.querySelector('.step[data-step="' + Math.min(state.phaseStep, 3) + '"]')
  if (el) {
    el.classList.remove('active', 'done')
    el.classList.add('error')
  }
}

function showError(title, detail, logTail) {
  $('errorBox').hidden = false
  $('errorTitle').textContent = title || '启动失败'
  $('errorDetail').textContent = detail || ''
  const pre = $('errorLog')
  if (logTail) {
    pre.hidden = false
    pre.textContent = logTail
  } else {
    pre.hidden = true
    pre.textContent = ''
  }
}

function hideError() {
  $('errorBox').hidden = true
}

function handleState(s) {
  if (!s || !s.phase) return
  // 状态可能来自外部触发（如 ZHIWEN_AUTOSTART），此时也要把步骤区亮出来
  if ($('steps').hidden && s.phase !== 'error') {
    showSteps(true)
    resetSteps()
    if (s.phase !== 'online') setBusy(true)
  }
  if (s.phase === 'error') {
    setPhase('error', s.message)
    markErrorStep()
    showError(s.message, s.detail, s.logTail)
    state.busy = false
    setBusy(false)
    $('btnStart').textContent = '重试'
    return
  }
  hideError()
  setPhase(s.phase, s.message)
  if (s.phase === 'online') {
    setBusy(false)
    $('statusLine').textContent = '已就绪，正在进入…'
  }
}

function renderBadges(detected) {
  const wrap = $('envBadges')
  wrap.innerHTML = ''
  const add = (cls, text) => {
    const span = document.createElement('span')
    span.className = 'badge' + (cls ? ' ' + cls : '')
    span.textContent = text
    wrap.appendChild(span)
  }
  if (detected.python) {
    if (detected.python.exists) add('ok', 'Python：' + detected.python.path)
    else add('bad', 'Python 未找到：' + detected.python.path)
  }
  if (detected.distExists) add('ok', '前端构建产物已就绪')
  else add('bad', '前端构建产物缺失（frontend 运行 npm run build）')
  const backendMap = {
    running: ['ok', '后端：已在运行（将直接复用）'],
    occupied: ['bad', '后端：8000 被占用'],
    free: ['', '后端：8000 空闲'],
  }
  const backend = backendMap[detected.backend] || ['', '后端：未知']
  add(backend[0], backend[1])
  if (detected.staticPortInUse) add('bad', '端口 17800 被占用')
}

function renderHints(detected) {
  const py = detected.python
  $('pythonHint').textContent = py ? `当前探测：${py.path}（来源：${py.source}）` : ''
  $('projectRootHint').textContent = detected.packaged
    ? detected.projectRoot
      ? `当前生效：${detected.projectRoot}`
      : '打包版本需要指定仓库目录（含 backend）；开发模式会自动使用仓库'
    : `开发模式自动使用：${detected.projectRoot || ''}`
  $('tokenHint').textContent = detected.tokenSource
    ? `已检测到后端令牌（来源：${detected.tokenSource}），将自动写入界面`
    : '后端未配置令牌时可留空'
}

async function start() {
  if (!bridge || state.busy) return
  hideError()
  showSteps(true)
  resetSteps()
  setBusy(true)
  try {
    const saved = await bridge.saveConfig({
      mode: state.mode,
      remoteBaseUrl: $('remoteBaseUrl').value.trim(),
      pythonPath: $('pythonPath').value.trim(),
      projectRoot: $('projectRoot').value.trim(),
      apiToken: $('apiToken').value,
      launchAtLogin: $('launchAtLogin').checked,
      notifications: $('notifications').checked,
      globalHotkey: $('globalHotkey').value.trim(),
    })
    if (!saved || !saved.ok) {
      showError('配置有问题', (saved && saved.error) || '未知错误')
      state.busy = false
      setBusy(false)
      return
    }
    const res = await bridge.start(state.mode)
    if (!res || !res.ok) {
      state.busy = false
      setBusy(false)
      showError('无法启动', '当前正在启动中，请稍候再试。')
    }
  } catch (err) {
    state.busy = false
    setBusy(false)
    showError('启动失败', String(err && err.message ? err.message : err))
  }
}

async function init() {
  if (!bridge) {
    showError('桌面桥未加载', 'preload 脚本未能注入，请检查安装是否完整。')
    return
  }
  try {
    const snapshot = await bridge.getState()
    const cfg = snapshot.config || {}
    state.mode = cfg.mode === 'remote' ? 'remote' : 'local'
    $('remoteBaseUrl').value = cfg.remoteBaseUrl || ''
    $('pythonPath').value = cfg.pythonPath || ''
    $('projectRoot').value = cfg.projectRoot || ''
    $('apiToken').value = cfg.apiToken || ''
    $('launchAtLogin').checked = !!cfg.launchAtLogin
    $('notifications').checked = cfg.notifications !== false
    $('globalHotkey').value = cfg.globalHotkey || ''
    renderBadges(snapshot.detected || {})
    renderHints(snapshot.detected || {})
  } catch (err) {
    showError('无法读取桌面端状态', String(err && err.message ? err.message : err))
  }
  applyMode()
  if (settingsMode) {
    document.title = '连接设置 - 知问'
    $('settingsNote').hidden = false
    $('btnStart').textContent = startLabel
    $('btnQuit').textContent = '关闭'
  }
  bridge.onState(handleState)
  $('btnStart').addEventListener('click', start)
  $('btnLogs').addEventListener('click', () => bridge.openLogs())
  $('btnQuit').addEventListener('click', () =>
    settingsMode ? bridge.closeWindow() : bridge.quit()
  )
  document.querySelectorAll('.mode').forEach((el) => {
    el.addEventListener('click', () => {
      if (state.busy) return
      state.mode = el.dataset.mode === 'remote' ? 'remote' : 'local'
      applyMode()
    })
  })
  document.addEventListener('keydown', (event) => {
    if (event.key !== 'Enter') return
    const tag = document.activeElement && document.activeElement.tagName
    if (state.busy || tag === 'INPUT') return
    event.preventDefault()
    start()
  })
}

init()
