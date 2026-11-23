'use strict'

/** 一次性脚本：离屏渲染「知」logo，生成窗口/托盘/通知用 PNG。
 *  运行：desktop 目录下 node_modules\electron\dist\electron.exe scripts\make_icons.cjs
 */

const { app, BrowserWindow } = require('electron')
const fs = require('fs')
const path = require('path')

const outDir = path.join(__dirname, '..', 'assets')

const HTML = `<!doctype html><html><head><meta charset="utf-8"><style>
html,body{margin:0;padding:0;width:256px;height:256px;background:transparent;overflow:hidden}
.logo{position:relative;width:256px;height:256px;border-radius:56px;
background:linear-gradient(160deg,#f6efdf 0%,#e9dfc9 100%);
box-shadow:inset 0 0 0 2px rgba(213,201,173,.65);
display:flex;align-items:center;justify-content:center;
font-family:"SimSun","Noto Serif SC",serif;font-weight:700;color:#33291c;font-size:148px;letter-spacing:2px;
user-select:none}
.seal{position:absolute;right:34px;bottom:30px;width:30px;height:30px;border-radius:6px;background:#96691f}
</style></head><body><div class="logo">知<div class="seal"></div></div></body></html>`

app.whenReady().then(async () => {
  const win = new BrowserWindow({
    width: 256,
    height: 256,
    show: false,
    frame: false,
    transparent: true,
    webPreferences: { offscreen: true },
  })
  await win.loadURL('data:text/html;charset=utf-8,' + encodeURIComponent(HTML))
  await new Promise((resolve) => setTimeout(resolve, 800))
  const image = await win.webContents.capturePage()
  fs.mkdirSync(outDir, { recursive: true })
  fs.writeFileSync(path.join(outDir, 'icon-256.png'), image.toPNG())
  fs.writeFileSync(
    path.join(outDir, 'tray-32.png'),
    image.resize({ width: 32, height: 32, quality: 'best' }).toPNG()
  )
  fs.writeFileSync(
    path.join(outDir, 'tray-16.png'),
    image.resize({ width: 16, height: 16, quality: 'best' }).toPNG()
  )

  // Windows 安装包图标：多尺寸 PNG 压缩 ICO（Vista+ 支持）
  const sizes = [16, 32, 256]
  const pngs = [
    image.resize({ width: 16, height: 16, quality: 'best' }).toPNG(),
    image.resize({ width: 32, height: 32, quality: 'best' }).toPNG(),
    image.toPNG(),
  ]
  const header = Buffer.alloc(6)
  header.writeUInt16LE(0, 0)
  header.writeUInt16LE(1, 2)
  header.writeUInt16LE(pngs.length, 4)
  const dir = Buffer.alloc(16 * pngs.length)
  let offset = 6 + dir.length
  for (let i = 0; i < pngs.length; i += 1) {
    const size = sizes[i]
    const o = i * 16
    dir.writeUInt8(size >= 256 ? 0 : size, o)
    dir.writeUInt8(size >= 256 ? 0 : size, o + 1)
    dir.writeUInt8(0, o + 2)
    dir.writeUInt8(0, o + 3)
    dir.writeUInt16LE(1, o + 4)
    dir.writeUInt16LE(32, o + 6)
    dir.writeUInt32LE(pngs[i].length, o + 8)
    dir.writeUInt32LE(offset, o + 12)
    offset += pngs[i].length
  }
  const buildDir = path.join(__dirname, '..', 'build')
  fs.mkdirSync(buildDir, { recursive: true })
  fs.writeFileSync(path.join(buildDir, 'icon.ico'), Buffer.concat([header, dir, ...pngs]))
  console.log('icon.ico written to', buildDir)

  console.log('icons written to', outDir)
  app.quit()
})
