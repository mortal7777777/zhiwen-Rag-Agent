'use strict'

/** 开发截图：用离屏 Electron 打开本地 vite（5173），切换主页样式各拍一张。
 *  运行：node_modules\electron\dist\electron.exe scripts\shoot_home_variants.cjs
 *  输出：scratch/check_outputs/home/<variant>.png
 */

const { app, BrowserWindow } = require('electron')
const fs = require('fs')
const path = require('path')

const OUT = path.join(__dirname, '..', '..', 'scratch', 'check_outputs', 'home')
const URL = process.env.ZHIWEN_DEV_URL || 'http://localhost:5173/'
const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms))

async function reloadWith(win, script) {
  const loaded = new Promise((resolve) => win.webContents.once('did-finish-load', resolve))
  await win.webContents.executeJavaScript(`${script}; location.reload(); true`)
  await loaded
  await wait(3200)
}

app.whenReady().then(async () => {
  fs.mkdirSync(OUT, { recursive: true })
  const win = new BrowserWindow({
    width: 1440,
    height: 900,
    show: false,
    webPreferences: { offscreen: true },
  })
  await win.loadURL(URL)
  await wait(1600)

  const base =
    "localStorage.setItem('rag_content_width','100%');" +
    "localStorage.setItem('rag_theme','dark');" +
    "localStorage.setItem('rag_style','default');"

  for (const name of ['classic', 'studio', 'minimal', 'hero']) {
    await reloadWith(win, `${base}localStorage.setItem('rag_home_style','${name}')`)
    const ratio = await win.webContents.executeJavaScript(
      "(() => { const m = document.querySelector('.chat-column'); if (!m || !m.parentElement) return null; return +(m.getBoundingClientRect().width / m.parentElement.getBoundingClientRect().width).toFixed(3) })()"
    )
    fs.writeFileSync(path.join(OUT, `${name}.png`), (await win.webContents.capturePage()).toPNG())
    console.log(name, 'width-ratio=', ratio)
  }

  // 浅色书斋暖读风格下的工作台，验证主题兼容
  await reloadWith(
    win,
    "localStorage.setItem('rag_home_style','studio');" +
      "localStorage.setItem('rag_theme','light');" +
      "localStorage.setItem('rag_style','salon');" +
      "localStorage.setItem('rag_content_width','100%')"
  )
  fs.writeFileSync(
    path.join(OUT, 'studio-salon-light.png'),
    (await win.webContents.capturePage()).toPNG()
  )
  console.log('done')
  app.quit()
})
