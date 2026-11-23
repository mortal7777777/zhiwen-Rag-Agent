'use strict'

/**
 * boot 状态机：检查环境 → （local）启动后端 → 等待 livez → 进入界面 / （remote）探活 → 进入界面。
 * 只做编排，具体能力来自 probe / Sidecar / createStaticServer。
 */

const fs = require('fs')
const path = require('path')

const { Sidecar } = require('./sidecar.cjs')
const { createStaticServer } = require('./server.cjs')

const BACKEND_BASE = 'http://127.0.0.1:8000'
const WAIT_TIMEOUT_MS = 120_000
const POLL_INTERVAL_MS = 1_000
const REMOTE_PROBE_TIMEOUT_MS = 30_000

const delay = (ms) => new Promise((resolve) => setTimeout(resolve, ms))

class BootFlow {
  constructor(opts) {
    this.getConfig = opts.config
    this.probe = opts.probe
    this.validateRemoteUrl = opts.validateRemoteUrl
    this.getPaths = opts.paths || (() => ({
      repoRoot: opts.repoRoot,
      distDir: opts.distDir,
      backendDir: opts.backendDir,
    }))
    this.logsDir = opts.logsDir
    this.sidecar = null
    this.sidecarBackendDir = ''
    this.onState = opts.onState || (() => {})
    this.onReady = opts.onReady || (() => {})
    this.onBackendLost = opts.onBackendLost || (() => {})
    this.sidecar = null
    this.staticServer = null
    this.running = false
    this.runId = 0
    this.online = false
    this.ownsBackend = false
    this.quitting = false
    this.effectiveToken = ''
    this.lastExitInfo = null
  }

  /** 路径（仓库根/dist/backend）可能随设置变化，sidecar 按当前 backendDir 惰性创建。 */
  ensureSidecar() {
    const paths = this.getPaths()
    if (!this.sidecar || this.sidecarBackendDir !== paths.backendDir) {
      this.sidecar = new Sidecar({ backendDir: paths.backendDir, logsDir: this.logsDir })
      this.sidecar.onExit = (info) => this.handleSidecarExit(info)
      this.sidecarBackendDir = paths.backendDir
    }
    return this.sidecar
  }

  emit(phase, message, extra = {}) {
    this.onState({ phase, message, ...extra })
  }

  async run(mode) {
    if (this.running) return
    this.running = true
    this.runId += 1
    const runId = this.runId
    this.online = false
    this.lastExitInfo = null
    try {
      await this.resetForRun()
      if (runId !== this.runId) return
      if (mode === 'remote') await this.runRemote(runId)
      else await this.runLocal(runId)
    } catch (err) {
      this.fail('启动失败', err && err.message ? err.message : String(err))
    } finally {
      if (runId === this.runId) this.running = false
    }
  }

  async resetForRun() {
    if (this.ownsBackend) {
      this.ownsBackend = false
      if (this.sidecar) await this.sidecar.stop()
      await this.waitPortFree(5000)
    }
  }

  async runLocal(runId) {
    const config = this.getConfig()
    const fake = process.env.ZHIWEN_FAKE_STATE || ''
    const paths = this.getPaths()
    if (!paths.repoRoot) {
      return this.fail(
        '未配置项目根目录',
        '打包版本的本机模式需要知道知问仓库位置（含 backend 的 rag_knowledge_base 目录）。请打开「连接设置」填写项目根目录，或改用「远程服务器」模式。'
      )
    }
    this.effectiveToken = this.probe.resolveEffectiveToken(config, paths.repoRoot).token

    this.emit('checking', '检查本地环境…')
    await delay(150)

    if (fake === 'dist-missing' || !fs.existsSync(path.join(paths.distDir, 'index.html'))) {
      return this.fail(
        '前端构建产物不存在',
        `未找到 ${path.join(paths.distDir, 'index.html')}。\n请先在 frontend 目录执行 npm run build，然后重试。`
      )
    }

    const live = await this.probe.probeLivez(BACKEND_BASE, 1500)
    if (live.ok) {
      this.emit('checking', '检测到已运行的知问后端，直接复用（退出桌面端不会关闭它）')
      this.ownsBackend = false
      return this.finishLocal(runId)
    }

    const occupied = fake === 'port-busy' ? true : await this.probe.portInUse(8000)
    if (occupied) {
      const pid = fake === 'port-busy' ? 9999 : await this.probe.findPidOnPort(8000)
      return this.fail(
        '端口 8000 已被其他进程占用',
        `占用进程 PID：${pid == null ? '未知' : pid}（对方未通过知问健康检查，桌面端不会自动结束别人的进程）。\n可运行 stop_all.bat 清理，或改用「远程服务器」模式。`
      )
    }

    const py = this.probe.detectPython(config, paths.repoRoot)
    if (!py.exists) {
      return this.fail(
        '找不到 Python 解释器',
        `检测到的路径：${py.path}\n请设置环境变量 ZHIWEN_PYTHON，或编辑仓库根 .zhiwen-python 指向本机 python.exe。`
      )
    }
    this.emit('checking', `Python 解释器：${py.path}（来源：${py.source}）`)
    const deps = this.probe.checkPythonDeps(py.path)
    if (!deps.ok) {
      return this.fail(
        'Python 依赖不完整（缺 fastapi / uvicorn）',
        `${py.path}\n请运行：${py.path} -m pip install -r ${path.join(paths.backendDir, 'requirements.txt')}\n（${deps.error || 'import 检查失败'}）`
      )
    }

    const envLocal = this.probe.readEnvLocal(path.join(paths.backendDir, '.env.local'))
    const extraEnv = {}
    if (!process.env.ZHIWEN_API_TOKEN && envLocal.ZHIWEN_API_TOKEN == null && config.apiToken) {
      extraEnv.ZHIWEN_API_TOKEN = config.apiToken
    }

    this.emit('starting', '正在启动本机后端（uvicorn）…')
    try {
      this.ensureSidecar().start({ pythonPath: py.path, envBase: envLocal, extraEnv })
    } catch (err) {
      return this.fail('启动后端失败', err && err.message ? err.message : String(err))
    }
    this.ownsBackend = true

    this.emit('waiting', '等待后端就绪（/api/livez）…')
    const startedAt = Date.now()
    const timeoutMs = fake === 'spawn-timeout' ? 6000 : WAIT_TIMEOUT_MS
    let announced = 0
    while (this.runId === runId) {
      if (this.lastExitInfo) {
        const info = this.lastExitInfo
        const classified = this.ensureSidecar().classifyEarlyExit()
        const suffix =
          info.code != null
            ? `（退出码 ${info.code}）`
            : info.error
              ? `（${info.error.message}）`
              : ''
        return this.fail('后端进程提前退出' + suffix, classified.hint || '请查看日志尾部定位原因。', {
          logTail: classified.tail,
        })
      }
      const r = await this.probe.probeLivez(BACKEND_BASE, 1200)
      if (r.ok) break
      if (this.lastExitInfo) continue
      const waited = Date.now() - startedAt
      if (waited > timeoutMs) {
        return this.fail(
          '后端未在预期时间内就绪',
          '请检查日志尾部；常见原因：MySQL/OpenSearch 未启动、依赖缺失、端口冲突。',
          { logTail: this.ensureSidecar().getLogTail(50) }
        )
      }
      const secs = Math.floor(waited / 1000)
      if (secs >= announced + 5) {
        announced = secs
        this.emit('waiting', `等待后端就绪…已等待 ${secs}s`)
      }
      await delay(POLL_INTERVAL_MS)
    }
    if (this.runId !== runId) return

    const rz = await this.probe.probeReadyz(BACKEND_BASE, 3000)
    if (this.runId !== runId) return
    const readyzDegraded = rz.ok ? null : rz.statusCode || 'unreachable'
    if (readyzDegraded) {
      this.emit('checking', '后端进程已就绪，但 MySQL / OpenSearch 未就绪（可进入后重试）')
      await delay(400)
    }
    return this.finishLocal(runId, { readyzDegraded })
  }

  async runRemote(runId) {
    const config = this.getConfig()
    const urlError = this.validateRemoteUrl(config.remoteBaseUrl)
    if (urlError) return this.fail('服务器地址无效', urlError)
    const base = String(config.remoteBaseUrl).replace(/\/+$/, '')
    this.effectiveToken = config.apiToken || ''
    this.emit('probing', `正在连接 ${base} …`)
    const startedAt = Date.now()
    while (this.runId === runId) {
      const r = await this.probe.probeLivez(base, 2500)
      if (r.ok) {
        this.emit('loading', '正在打开知问界面…')
        this.onReady({ url: base, token: this.effectiveToken, readyzDegraded: null })
        return
      }
      if (Date.now() - startedAt > REMOTE_PROBE_TIMEOUT_MS) {
        return this.fail(
          '无法连接到服务器',
          `30 秒内未能通过健康检查：${base}/api/livez\n请确认服务已启动、地址与协议正确（非本机地址必须使用 https）。`
        )
      }
      await delay(1000)
    }
  }

  async finishLocal(runId, extra = {}) {
    let url = ''
    try {
      if (!this.staticServer) {
        this.staticServer = createStaticServer({ distDir: this.getPaths().distDir })
      }
      const started = await this.staticServer.start()
      url = started.url
    } catch {
      return this.fail(
        '本地端口 17800 被占用',
        '桌面端需要固定端口加载界面（保证登录状态与界面偏好不丢失）。请释放 17800 端口，或改用「远程服务器」模式。'
      )
    }
    if (this.runId !== runId) return
    if (!this.effectiveToken) {
      this.effectiveToken = this.probe.resolveEffectiveToken(
        this.getConfig(),
        this.getPaths().repoRoot
      ).token
    }
    this.emit('loading', '正在打开知问界面…', { readyzDegraded: extra.readyzDegraded || null })
    this.onReady({ url, token: this.effectiveToken, readyzDegraded: extra.readyzDegraded || null })
  }

  fail(message, detail, extra = {}) {
    if (this.ownsBackend) {
      this.ownsBackend = false
      if (this.sidecar) this.sidecar.stop().catch(() => {})
    }
    this.emit('error', message, { detail: detail || '', ...extra })
  }

  handleSidecarExit(info) {
    if (!this.ownsBackend || this.quitting) return
    if (this.online) {
      this.onBackendLost(info)
      return
    }
    this.lastExitInfo = info
  }

  reportLoadError(message, detail) {
    this.running = false
    this.online = false
    this.emit('error', message, { detail: detail || '' })
  }

  markOnline() {
    this.online = true
  }

  async shutdown() {
    this.quitting = true
    if (this.staticServer) {
      try {
        await this.staticServer.stop()
      } catch {
        // 忽略
      }
    }
    if (this.ownsBackend) {
      if (this.sidecar) await this.sidecar.stop()
      await this.waitPortFree(5000)
    }
  }

  async waitPortFree(timeoutMs) {
    const deadline = Date.now() + timeoutMs
    while (Date.now() < deadline) {
      if (!(await this.probe.portInUse(8000))) return true
      await delay(200)
    }
    return false
  }
}

module.exports = { BootFlow }
