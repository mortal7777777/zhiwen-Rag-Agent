'use strict'

/** 本机后端 sidecar：spawn uvicorn、日志落盘/轮转、早退识别、进程树清理。 */

const fs = require('fs')
const path = require('path')
const { spawn, spawnSync } = require('child_process')

const BACKEND_PORT = 8000
const LOG_KEEP = 5

function rotateLogs(logFile) {
  try {
    if (!fs.existsSync(logFile)) return
    try {
      fs.rmSync(`${logFile}.${LOG_KEEP}`, { force: true })
    } catch {
      // 删除旧文件失败不阻断
    }
    for (let i = LOG_KEEP - 1; i >= 1; i -= 1) {
      const from = `${logFile}.${i}`
      if (fs.existsSync(from)) {
        try {
          fs.renameSync(from, `${logFile}.${i + 1}`)
        } catch {
          // 轮转失败保留原样
        }
      }
    }
    fs.renameSync(logFile, `${logFile}.1`)
  } catch {
    // 轮转失败不影响追加写
  }
}

class Sidecar {
  constructor({ backendDir, logsDir }) {
    this.backendDir = backendDir
    this.logsDir = logsDir
    this.logFile = path.join(logsDir, 'backend.log')
    this.child = null
    this.exited = true
    this.onExit = null
  }

  /**
   * @param {object} opts
   * @param {string} opts.pythonPath
   * @param {Record<string,string>} [opts.envBase]   .env.local 解析结果（低优先级）
   * @param {Record<string,string>} [opts.extraEnv]  额外注入（高优先级）
   */
  start({ pythonPath, envBase, extraEnv }) {
    fs.mkdirSync(this.logsDir, { recursive: true })
    rotateLogs(this.logFile)
    const fd = fs.openSync(this.logFile, 'a')
    fs.writeSync(
      fd,
      `\n[desktop] ${new Date().toISOString()} spawn: ${pythonPath} -m uvicorn app.main:app --host 127.0.0.1 --port ${BACKEND_PORT}\n`
    )
    // 合并语义同 run.py：.env.local 只补缺失键，不覆盖已有环境变量
    const childEnv = { ...(envBase || {}), ...process.env, ...(extraEnv || {}), PYTHONUNBUFFERED: '1' }
    this.exited = false
    this.child = spawn(
      pythonPath,
      ['-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', String(BACKEND_PORT)],
      {
        cwd: this.backendDir,
        windowsHide: true,
        detached: false,
        stdio: ['ignore', fd, fd],
        env: childEnv,
      }
    )
    try {
      fs.closeSync(fd)
    } catch {
      // fd 已复制进子进程，父进程关闭失败可忽略
    }
    this.child.on('error', (err) => {
      this.exited = true
      if (this.onExit) this.onExit({ error: err, code: null, signal: null })
    })
    this.child.on('exit', (code, signal) => {
      this.exited = true
      if (this.onExit) this.onExit({ error: null, code, signal })
    })
    return this.child.pid
  }

  isRunning() {
    return !!this.child && !this.exited
  }

  getPid() {
    return this.child ? this.child.pid : null
  }

  /** 读取日志尾部（默认最后 64KB 中的 50 行）。 */
  getLogTail(maxLines = 50, maxBytes = 64 * 1024) {
    let text = ''
    try {
      const stat = fs.statSync(this.logFile)
      const start = Math.max(0, stat.size - maxBytes)
      const fd = fs.openSync(this.logFile, 'r')
      const buf = Buffer.alloc(stat.size - start)
      fs.readSync(fd, buf, 0, buf.length, start)
      fs.closeSync(fd)
      text = buf.toString('utf8')
    } catch {
      return ''
    }
    return text.split(/\r?\n/).slice(-maxLines).join('\n')
  }

  classifyEarlyExit() {
    const tail = this.getLogTail(80)
    if (/10048|address already in use|only one usage of each socket address/i.test(tail)) {
      return { hint: '端口 8000 已被占用（可能是上一次残留的后端进程，可运行 stop_all.bat 清理）', tail }
    }
    if (/InstanceLockError|backend\.lock|单实例/i.test(tail)) {
      return { hint: '检测到另一个知问后端实例已持有单实例锁（同一时间只允许一个后端）', tail }
    }
    if (/ModuleNotFoundError|No module named/i.test(tail)) {
      const m = tail.match(/No module named '?([\w.]+)'?/)
      return { hint: `Python 环境缺少依赖${m ? '：' + m[1] : ''}（检查解释器与 requirements.txt）`, tail }
    }
    return { hint: '', tail }
  }

  /** 仅当进程由本壳拉起且仍在运行时才杀（taskkill /T 连 MCP 等子进程一起收）。 */
  stop({ waitMs = 5000 } = {}) {
    return new Promise((resolve) => {
      if (!this.child || this.exited) return resolve({ stopped: false })
      const pid = this.getPid()
      try {
        spawnSync('taskkill', ['/F', '/T', '/PID', String(pid)], { windowsHide: true, stdio: 'ignore' })
      } catch {
        // taskkill 不可用时依赖进程自然退出
      }
      const deadline = Date.now() + waitMs
      const timer = setInterval(() => {
        if (!this.isRunning() || Date.now() > deadline) {
          clearInterval(timer)
          resolve({ stopped: true })
        }
      }, 200)
    })
  }
}

module.exports = { Sidecar, BACKEND_PORT }
