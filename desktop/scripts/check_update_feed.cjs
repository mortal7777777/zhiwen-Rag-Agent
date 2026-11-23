'use strict'

/**
 * 诊断：用 Electron net（同主进程更新兜底链路）查询 GitHub Releases。
 * 运行：node_modules\electron\dist\electron.exe scripts\check_update_feed.cjs
 * 需要本机已登录的网络环境；只读，不发布任何东西。
 */

const { app, net, session } = require('electron')
const path = require('path')

const updates = require(path.join(__dirname, '..', 'lib', 'updates.cjs'))
const pkg = require(path.join(__dirname, '..', 'package.json'))
const REPO = 'mortal7777777/zhiwen-Rag-Agent'
const API = `https://api.github.com/repos/${REPO}/releases?per_page=30`

function requestJson(url, sessionRef, timeoutMs = 10000) {
  return new Promise((resolve, reject) => {
    let settled = false
    const finish = (fn, value) => {
      if (settled) return
      settled = true
      clearTimeout(timer)
      fn(value)
    }
    const options = { url, method: 'GET' }
    if (sessionRef) options.session = sessionRef
    const request = net.request(options)
    request.setHeader('User-Agent', 'zhiwen-desktop-diagnostic')
    request.setHeader('Accept', 'application/vnd.github+json')
    const timer = setTimeout(() => {
      try {
        request.abort()
      } catch {
        // 忽略
      }
      finish(reject, new Error('请求超时'))
    }, timeoutMs)
    request.on('response', (response) => {
      const chunks = []
      response.on('data', (chunk) => chunks.push(Buffer.from(chunk)))
      response.on('end', () => {
        const body = Buffer.concat(chunks).toString('utf8')
        if (response.statusCode >= 200 && response.statusCode < 300) {
          try {
            finish(resolve, JSON.parse(body))
          } catch {
            finish(reject, new Error('返回格式异常'))
          }
        } else {
          finish(reject, new Error(`GitHub HTTP ${response.statusCode}`))
        }
      })
    })
    request.on('error', (err) => finish(reject, err))
    request.end()
  })
}

async function fetchJson(url) {
  try {
    return await requestJson(url, null)
  } catch (firstErr) {
    console.log(`系统代理失败（${firstErr.message}），改用直连重试…`)
    const direct = session.fromPartition('zhiwen-update-diag')
    await direct.setProxy({ mode: 'direct' })
    return requestJson(url, direct)
  }
}

app.whenReady().then(async () => {
  try {
    const releases = await fetchJson(API)
    const desktopReleases = releases.filter((item) => updates.isDesktopRelease(item))
    console.log(`当前版本：v${pkg.version}`)
    console.log(`Release 总数：${releases.length}，其中桌面 Release：${desktopReleases.length}`)
    for (const item of desktopReleases.slice(0, 5)) {
      const assets = item.assets.map((asset) => asset.name).join(', ')
      console.log(`  ${item.tag_name}${item.prerelease ? '（预发布）' : ''}${item.draft ? '（草稿）' : ''}: ${assets}`)
    }
    const newer = updates.pickNewerRelease(releases, pkg.version)
    console.log(newer ? `可更新到：v${newer.version}` : '没有比当前版本更新的桌面安装包')
  } catch (err) {
    console.error('查询失败：', err && err.message ? err.message : err)
    process.exitCode = 1
  } finally {
    app.quit()
  }
})
