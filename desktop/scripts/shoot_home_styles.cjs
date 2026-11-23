'use strict'

/** 主页样式全面体检：客户端内容区尺寸（扣除标题条） × 四种主页样式 × 主题/风格。
 *  运行：node_modules\electron\dist\electron.exe scripts\shoot_home_styles.cjs
 *  输出：scratch/check_outputs/home-audit/
 */

const { app, BrowserWindow } = require('electron')
const fs = require('fs')
const path = require('path')

const OUT = path.join(__dirname, '..', '..', 'scratch', 'check_outputs', 'home-audit')
const BASE = process.env.ZHIWEN_DEV_URL || 'http://localhost:5174/'
const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms))

async function reloadWith(win, script) {
  const loaded = new Promise((resolve) => win.webContents.once('did-finish-load', resolve))
  await win.webContents.executeJavaScript(`${script}; location.reload(); true`)
  await loaded
  await wait(1300)
}

async function shot(win, label) {
  const info = await win.webContents.executeJavaScript(`(() => {
    const w = document.querySelector('.welcome')
    const s = document.querySelector('.suggestions')
    const t = document.querySelector('.welcome-title')
    const m = document.querySelector('.chat-main')
    return {
      viewport: [document.documentElement.clientWidth, document.documentElement.clientHeight],
      welcome: w ? { sh: w.scrollHeight, ch: w.clientHeight } : null,
      suggestions: s ? { h: Math.round(s.getBoundingClientRect().height), w: Math.round(s.getBoundingClientRect().width) } : null,
      title: t ? (t.textContent || '').slice(0, 24) : null,
      mainW: m ? Math.round(m.getBoundingClientRect().width) : null,
    }
  })()`)
  console.log(label, JSON.stringify(info))
  fs.writeFileSync(path.join(OUT, `${label}.png`), (await win.webContents.capturePage()).toPNG())
}

app.whenReady().then(async () => {
  fs.mkdirSync(OUT, { recursive: true })
  // 1140x603 ≈ 643 窗口内容区扣除 40px 品牌标题条后的高度
  const win = new BrowserWindow({ width: 1140, height: 603, show: false, webPreferences: { offscreen: true } })
  await win.loadURL(BASE)
  await wait(1200)

  const combos = [
    ['dark', 'default'],
    ['light', 'salon'],
    ['dark', 'salon'],
    ['light', 'mono'],
  ]
  for (const [theme, style] of combos) {
    for (const home of ['classic', 'studio', 'minimal', 'hero']) {
      await reloadWith(
        win,
        "localStorage.setItem('rag_content_width','100%');" +
          `localStorage.setItem('rag_theme','${theme}');` +
          `localStorage.setItem('rag_style','${style}');` +
          `localStorage.setItem('rag_home_style','${home}')`
      )
      await shot(win, `${theme}-${style}-${home}`)
    }
  }

  console.log('done')
  app.quit()
})
