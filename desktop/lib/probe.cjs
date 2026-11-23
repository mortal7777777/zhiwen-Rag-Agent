'use strict'

/** 探活与解释器探测（全部只读，不修改任何状态）。 */

const fs = require('fs')
const path = require('path')
const http = require('http')
const https = require('https')
const net = require('net')
const { execFile, spawnSync } = require('child_process')

function joinUrl(base, pathname) {
  return String(base || '').replace(/\/+$/, '') + pathname
}

function httpGetJson(urlString, timeoutMs = 1500) {
  return new Promise((resolve) => {
    let url
    try {
      url = new URL(urlString)
    } catch {
      return resolve({ ok: false, error: 'bad-url' })
    }
    const mod = url.protocol === 'https:' ? https : http
    const req = mod.get(url, { timeout: timeoutMs, headers: { accept: 'application/json' } }, (res) => {
      const chunks = []
      let size = 0
      res.on('data', (c) => {
        size += c.length
        if (size <= 64 * 1024) chunks.push(c)
      })
      res.on('end', () => {
        let body = null
        try {
          body = JSON.parse(Buffer.concat(chunks).toString('utf8'))
        } catch {
          // 非 JSON 响应按失败处理
        }
        resolve({ ok: true, statusCode: res.statusCode, body })
      })
    })
    req.on('timeout', () => req.destroy(new Error('timeout')))
    req.on('error', (err) => resolve({ ok: false, error: err.message }))
  })
}

async function probeLivez(baseUrl, timeoutMs = 1500) {
  const r = await httpGetJson(joinUrl(baseUrl, '/api/livez'), timeoutMs)
  return { ok: r.ok && r.statusCode === 200 && !!r.body && r.body.status === 'ok', raw: r }
}

async function probeReadyz(baseUrl, timeoutMs = 3000) {
  const r = await httpGetJson(joinUrl(baseUrl, '/api/readyz'), timeoutMs)
  return { ok: r.ok && r.statusCode === 200, statusCode: r.statusCode, body: r.body, error: r.error }
}

/** TCP 连接探测：能建立连接即视为端口被监听。 */
function portInUse(port, host = '127.0.0.1', timeoutMs = 400) {
  return new Promise((resolve) => {
    const socket = net.connect({ port, host })
    let settled = false
    const done = (value) => {
      if (settled) return
      settled = true
      socket.destroy()
      resolve(value)
    }
    socket.setTimeout(timeoutMs)
    socket.once('connect', () => done(true))
    socket.once('timeout', () => done(false))
    socket.once('error', () => done(false))
  })
}

/** 解析 netstat 找出监听指定端口的 PID（只用于错误提示，不用于杀进程）。 */
function findPidOnPort(port) {
  return new Promise((resolve) => {
    execFile('netstat', ['-ano'], { windowsHide: true, maxBuffer: 8 * 1024 * 1024 }, (err, stdout) => {
      if (err) return resolve(null)
      const pattern = new RegExp(`:${port}\\s+\\S+\\s+LISTENING\\s+(\\d+)`, 'i')
      for (const line of String(stdout).split(/\r?\n/)) {
        const m = line.match(pattern)
        if (m) return resolve(Number(m[1]))
      }
      resolve(null)
    })
  })
}

/**
 * 解释器探测顺序：配置 pythonPath > ZHIWEN_PYTHON > 仓库根 .zhiwen-python 首行 > 本机默认 conda 路径 > PATH。
 * 配置里显式写了但不存在时直接报该路径（避免悄悄换用别的解释器）。
 */
function detectPython(config, repoRoot) {
  const configured = config && typeof config.pythonPath === 'string' ? config.pythonPath.trim() : ''
  if (configured) {
    return { path: configured, exists: fs.existsSync(configured), source: '桌面端配置' }
  }
  const candidates = []
  if (process.env.ZHIWEN_PYTHON) {
    candidates.push({ source: '环境变量 ZHIWEN_PYTHON', value: process.env.ZHIWEN_PYTHON.trim() })
  }
  try {
    const line = fs
      .readFileSync(path.join(repoRoot, '.zhiwen-python'), 'utf8')
      .split(/\r?\n/)[0]
      .replace(/^\uFEFF/, '')
      .trim()
    if (line) candidates.push({ source: '.zhiwen-python', value: line })
  } catch {
    // 未配置该文件
  }
  candidates.push({ source: '默认路径', value: 'D:\\conda_envs\\pytorch_env\\python.exe' })
  for (const c of candidates) {
    if (fs.existsSync(c.value)) return { path: c.value, exists: true, source: c.source }
  }
  return { path: 'python', exists: true, source: 'PATH（待验证）' }
}

function checkPythonDeps(pythonPath) {
  try {
    const r = spawnSync(pythonPath, ['-c', 'import fastapi, uvicorn'], {
      timeout: 3000,
      windowsHide: true,
      stdio: 'ignore',
    })
    if (r.error) return { ok: false, error: r.error.message }
    return r.status === 0 ? { ok: true } : { ok: false, error: 'exit ' + r.status }
  } catch (err) {
    return { ok: false, error: err.message }
  }
}

/** 解析 .env.local（与 run.py 同语义：跳过注释/空行，首个 = 分割，去成对引号）。 */
function readEnvLocal(envFile) {
  const out = {}
  let text = ''
  try {
    text = fs.readFileSync(envFile, 'utf8')
  } catch {
    return out
  }
  for (const raw of text.split(/\r?\n/)) {
    const line = raw.trim()
    if (!line || line.startsWith('#') || !line.includes('=')) continue
    const idx = line.indexOf('=')
    const key = line.slice(0, idx).trim()
    let value = line.slice(idx + 1).trim()
    if (
      (value.startsWith('"') && value.endsWith('"')) ||
      (value.startsWith("'") && value.endsWith("'"))
    ) {
      value = value.slice(1, -1)
    }
    if (key) out[key] = value
  }
  return out
}

/**
 * 进入界面前要写入 localStorage 的令牌，以及它来自哪里。
 * 优先级：进程环境 > backend/.env.local > 桌面端配置（与后端实际启用值对齐，避免注入错令牌被前端清掉）。
 */
function resolveEffectiveToken(config, repoRoot) {
  if (process.env.ZHIWEN_API_TOKEN) {
    return { token: process.env.ZHIWEN_API_TOKEN, source: '环境变量 ZHIWEN_API_TOKEN' }
  }
  const fileEnv = readEnvLocal(path.join(repoRoot, 'backend', '.env.local'))
  if (fileEnv.ZHIWEN_API_TOKEN) {
    return { token: fileEnv.ZHIWEN_API_TOKEN, source: 'backend/.env.local' }
  }
  if (config && config.apiToken) {
    return { token: config.apiToken, source: '桌面端配置' }
  }
  return { token: '', source: '' }
}

module.exports = {
  probeLivez,
  probeReadyz,
  portInUse,
  findPidOnPort,
  detectPython,
  checkPythonDeps,
  readEnvLocal,
  resolveEffectiveToken,
}
