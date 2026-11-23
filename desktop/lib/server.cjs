'use strict'

/**
 * 本地同源服务器（127.0.0.1:17800，固定端口保证 localStorage 的 origin 稳定）：
 * - 静态服务 frontend/dist（SPA fallback 到 index.html）
 * - /api/* 反向代理到 127.0.0.1:8000（流式透传，SSE 不缓冲）
 */

const fs = require('fs')
const path = require('path')
const http = require('http')

const STATIC_PORT = 17800
const BACKEND_PORT = 8000

const HOP_BY_HOP = new Set([
  'connection',
  'keep-alive',
  'proxy-authenticate',
  'proxy-authorization',
  'te',
  'trailer',
  'transfer-encoding',
  'upgrade',
])

const MIME = {
  '.html': 'text/html; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.mjs': 'text/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.json': 'application/json; charset=utf-8',
  '.map': 'application/json; charset=utf-8',
  '.svg': 'image/svg+xml',
  '.png': 'image/png',
  '.jpg': 'image/jpeg',
  '.jpeg': 'image/jpeg',
  '.gif': 'image/gif',
  '.webp': 'image/webp',
  '.ico': 'image/x-icon',
  '.woff': 'font/woff',
  '.woff2': 'font/woff2',
  '.ttf': 'font/ttf',
  '.otf': 'font/otf',
  '.wasm': 'application/wasm',
  '.txt': 'text/plain; charset=utf-8',
  '.pdf': 'application/pdf',
}

function createStaticServer({ distDir }) {
  const distRoot = path.resolve(distDir)
  let startPromise = null

  const server = http.createServer((req, res) => {
    try {
      if (req.url === '/api' || req.url.startsWith('/api/') || req.url.startsWith('/api?')) {
        return proxy(req, res)
      }
      return serveStatic(req, res)
    } catch {
      if (!res.headersSent) {
        res.writeHead(500, { 'content-type': 'text/plain; charset=utf-8' })
        res.end('desktop static server internal error')
      }
    }
  })
  server.requestTimeout = 0
  server.headersTimeout = 60_000
  server.keepAliveTimeout = 65_000
  server.timeout = 0
  server.on('clientError', (_err, socket) => {
    try {
      socket.destroy()
    } catch {
      // 忽略
    }
  })

  function serveStatic(req, res) {
    if (req.method !== 'GET' && req.method !== 'HEAD') {
      res.writeHead(405, { allow: 'GET, HEAD' })
      return res.end()
    }
    let pathname
    try {
      pathname = decodeURIComponent(new URL(req.url, 'http://desktop.local').pathname)
    } catch {
      res.writeHead(400)
      return res.end()
    }
    const rel = pathname === '/' || pathname === '' ? 'index.html' : pathname.replace(/^\/+/, '')
    let file = path.resolve(distRoot, rel)
    if (!file.startsWith(distRoot + path.sep)) {
      res.writeHead(403)
      return res.end()
    }
    let stat = null
    try {
      stat = fs.statSync(file)
      if (!stat.isFile()) stat = null
    } catch {
      stat = null
    }
    let servedRel = rel
    if (!stat) {
      const wantsHtml =
        path.extname(pathname) === '' || String(req.headers.accept || '').includes('text/html')
      if (!wantsHtml) {
        res.writeHead(404)
        return res.end('not found')
      }
      file = path.join(distRoot, 'index.html')
      servedRel = 'index.html'
      try {
        stat = fs.statSync(file)
      } catch {
        res.writeHead(404)
        return res.end('index.html missing (run npm run build in frontend)')
      }
    }
    const ext = path.extname(file).toLowerCase()
    res.writeHead(200, {
      'content-type': MIME[ext] || 'application/octet-stream',
      'cache-control': servedRel.startsWith('assets/') ? 'public, max-age=31536000, immutable' : 'no-store',
      'content-length': stat.size,
    })
    if (req.method === 'HEAD') return res.end()
    const stream = fs.createReadStream(file)
    stream.on('error', () => res.destroy())
    stream.pipe(res)
  }

  function proxy(req, res) {
    const headers = {}
    for (const [rawKey, value] of Object.entries(req.headers)) {
      const key = rawKey.toLowerCase()
      if (HOP_BY_HOP.has(key) || key === 'origin') continue
      headers[rawKey] = value
    }
    headers.host = `127.0.0.1:${BACKEND_PORT}`
    const upstream = http.request(
      { host: '127.0.0.1', port: BACKEND_PORT, method: req.method, path: req.url, headers },
      (up) => {
        const resHeaders = {}
        for (const [rawKey, value] of Object.entries(up.headers)) {
          if (!HOP_BY_HOP.has(rawKey.toLowerCase())) resHeaders[rawKey] = value
        }
        res.writeHead(up.statusCode || 502, resHeaders)
        // SSE/大响应：立即把响应头吐给客户端，之后整段 pipe 不缓冲
        res.flushHeaders()
        up.pipe(res)
      }
    )
    upstream.setTimeout(0)
    upstream.on('error', (err) => {
      if (!res.headersSent) {
        res.writeHead(502, { 'content-type': 'application/json; charset=utf-8' })
        res.end(JSON.stringify({ detail: 'desktop proxy: backend unreachable (' + err.message + ')' }))
      } else {
        res.destroy()
      }
    })
    req.on('aborted', () => upstream.destroy())
    res.on('close', () => {
      if (!res.writableEnded) upstream.destroy()
    })
    req.pipe(upstream)
  }

  return {
    start() {
      if (!startPromise) {
        startPromise = new Promise((resolve, reject) => {
          const onError = (err) => {
            startPromise = null
            reject(err)
          }
          server.once('error', onError)
          server.listen(STATIC_PORT, '127.0.0.1', () => {
            server.removeListener('error', onError)
            resolve({ port: STATIC_PORT, url: `http://127.0.0.1:${STATIC_PORT}` })
          })
        })
      }
      return startPromise
    },
    stop() {
      return new Promise((resolve) => {
        try {
          server.close(() => resolve())
        } catch {
          resolve()
        }
      })
    },
  }
}

module.exports = { createStaticServer, STATIC_PORT }
