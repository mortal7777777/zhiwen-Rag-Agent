'use strict'

/** 布局体检：按桌面客户端真实窗口尺寸截六种布局 + 各板块页面，并输出水平溢出信息。
 *  运行：node_modules\electron\dist\electron.exe scripts\shoot_layout_audit.cjs
 *  输出：scratch/check_outputs/layout/
 */

const { app, BrowserWindow } = require('electron')
const fs = require('fs')
const path = require('path')

const OUT = path.join(__dirname, '..', '..', 'scratch', 'check_outputs', 'layout')
const BASE = process.env.ZHIWEN_DEV_URL || 'http://localhost:5173/'
const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms))

async function reloadWith(win, script) {
  const loaded = new Promise((resolve) => win.webContents.once('did-finish-load', resolve))
  await win.webContents.executeJavaScript(`${script}; location.reload(); true`)
  await loaded
  await wait(1300)
}

async function report(win, label) {
  const info = await win.webContents.executeJavaScript(`(() => {
    const de = document.documentElement
    const main = document.querySelector('.chat-main, .knowledge-page, .page-shell, #app > div')
    const scrollers = [...document.querySelectorAll('*')]
      .filter((el) => {
        const s = getComputedStyle(el)
        return /(auto|scroll)/.test(s.overflowY) && el.scrollHeight > el.clientHeight + 4
      })
      .slice(0, 6)
      .map((el) => ({
        cls: String(el.className || el.tagName).slice(0, 40),
        sh: el.scrollHeight,
        ch: el.clientHeight,
      }))
    return {
      overflowX: de.scrollWidth - de.clientWidth,
      viewport: [de.clientWidth, de.clientHeight],
      mainWidth: main ? Math.round(main.getBoundingClientRect().width) : null,
      scrollers,
    }
  })()`)
  console.log(label, JSON.stringify(info))
  fs.writeFileSync(path.join(OUT, `${label}.png`), (await win.webContents.capturePage()).toPNG())
}

app.whenReady().then(async () => {
  fs.mkdirSync(OUT, { recursive: true })
  const win = new BrowserWindow({
    width: 1140,
    height: 643,
    show: false,
    webPreferences: { offscreen: true },
  })
  await win.loadURL(BASE)
  await wait(1200)

  const base =
    "localStorage.setItem('rag_content_width','100%');" +
    "localStorage.setItem('rag_theme','dark');" +
    "localStorage.setItem('rag_style','default');" +
    "localStorage.setItem('rag_home_style','classic');" +
    "localStorage.setItem('rag_sidebar_merged','0');"

  for (const layout of ['standard', 'rail', 'island', 'topbar', 'claude', 'hermes']) {
    await reloadWith(win, `${base}localStorage.setItem('rag_layout','${layout}')`)
    await report(win, `chat-${layout}`)
  }

  // 各板块页面（默认布局）
  await reloadWith(win, `${base}localStorage.setItem('rag_layout','standard')`)
  for (const [route, label] of [
    ['knowledge', 'page-knowledge'],
    ['runs', 'page-runs'],
    ['prompts', 'page-prompts'],
  ]) {
    await win.loadURL(BASE.replace(/\/$/, '') + '/' + route)
    await wait(1500)
    await report(win, label)
  }

  // 页面级视图变体（断点按内容区宽度）
  const variants = [
    ['knowledge', 'rag_kb_view', 'split', 'knowledge-split'],
    ['knowledge', 'rag_kb_view', 'dense', 'knowledge-dense'],
    ['runs', 'rag_runs_view', 'dense', 'runs-dense'],
    ['runs', 'rag_runs_view', 'focus', 'runs-focus'],
    ['prompts', 'rag_pw_view', 'editor', 'prompts-editor'],
    ['prompts', 'rag_pw_view', 'preview', 'prompts-preview'],
  ]
  for (const [route, key, value, label] of variants) {
    await win.loadURL(BASE.replace(/\/$/, '') + '/' + route)
    await wait(700)
    await reloadWith(win, `localStorage.setItem('${key}','${value}')`)
    await report(win, label)
  }

  console.log('done')
  app.quit()
})
