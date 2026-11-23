'use strict'

/** 一次性脚本：渲染图标候选（A 形态 × 三套主题配色/字体 + 原蓝紫备选）。
 *  运行：node_modules\electron\dist\electron.exe scripts\make_icon_candidates.cjs
 */

const { app, BrowserWindow } = require('electron')
const fs = require('fs')
const path = require('path')

const HTML = `<!doctype html><html><head><meta charset="utf-8"><style>
*{box-sizing:border-box}
html,body{overflow:hidden}
body{margin:0;width:100vw;height:100vh;background:#12151b;display:flex;align-items:center;justify-content:center;gap:36px;
font-family:"Microsoft YaHei","Segoe UI",sans-serif}
.wrap{display:flex;flex-direction:column;align-items:center;gap:14px}
.icon{width:256px;height:256px;position:relative;overflow:hidden;border-radius:56px}
.label{color:#9aa4b2;font-size:19px;letter-spacing:1px;white-space:nowrap}
.zhi{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;font-weight:700}
.seal{position:absolute}
/* A 书斋暖读：纸色 + 宋体墨字 + 琥珀印记 */
.salon{background:linear-gradient(160deg,#f6efdf 0%,#e9dfc9 100%);box-shadow:inset 0 0 0 2px rgba(213,201,173,.65)}
.salon .zhi{color:#33291c;font-family:'SimSun','Noto Serif SC',serif;font-size:148px;letter-spacing:2px}
.salon .seal{right:34px;bottom:30px;width:30px;height:30px;border-radius:6px;background:#96691f}
/* B 素墨：纯灰阶 + 直角 + 雅黑 */
.mono{background:#fafaf8;border-radius:10px;box-shadow:inset 0 0 0 2px #d2d2cf}
.mono .zhi{color:#161616;font-family:'Microsoft YaHei',sans-serif;font-size:150px}
.mono .seal{right:34px;bottom:30px;width:26px;height:26px;border-radius:2px;background:#2b2b2b}
/* C 炭火：近黑褐底 + 炉火渐变楷体 */
.ember{background:radial-gradient(320px 260px at 30% 18%,#2a1408 0%,#160800 70%);box-shadow:inset 0 0 0 2px rgba(255,216,176,.10)}
.ember .zhi{background:linear-gradient(135deg,#ffd8b0 0%,#f08a2a 100%);-webkit-background-clip:text;background-clip:text;color:transparent;
font-family:'KaiTi','Microsoft YaHei',sans-serif;font-size:156px;filter:drop-shadow(0 0 24px rgba(240,138,42,.35))}
/* D 原蓝紫：现有方案备选 */
.blue{background:linear-gradient(135deg,#409EFF 0%,#7C5CFF 100%)}
.blue::before{content:"";position:absolute;inset:0;background:radial-gradient(360px 240px at 24% 12%,rgba(255,255,255,.28),transparent 60%)}
.blue .zhi{color:#fff;font-size:150px;text-shadow:0 4px 24px rgba(0,0,0,.18)}
</style></head><body>
<div class="wrap"><div class="icon salon"><div class="zhi">知</div><div class="seal"></div></div><div class="label">A · 书斋暖读（宋体）</div></div>
<div class="wrap"><div class="icon mono"><div class="zhi">知</div><div class="seal"></div></div><div class="label">B · 素墨（雅黑）</div></div>
<div class="wrap"><div class="icon ember"><div class="zhi">知</div></div><div class="label">C · 炭火（楷体）</div></div>
<div class="wrap"><div class="icon blue"><div class="zhi">知</div></div><div class="label">D · 原蓝紫（备选）</div></div>
</body></html>`

app.whenReady().then(async () => {
  const win = new BrowserWindow({
    width: 1320,
    height: 400,
    show: false,
    frame: false,
    transparent: true,
    webPreferences: { offscreen: true },
  })
  await win.loadURL('data:text/html;charset=utf-8,' + encodeURIComponent(HTML))
  await new Promise((resolve) => setTimeout(resolve, 900))
  const image = await win.webContents.capturePage()
  const outDir = path.join(__dirname, '..', '..', 'scratch', 'check_outputs', 'icons')
  fs.mkdirSync(outDir, { recursive: true })
  fs.writeFileSync(path.join(outDir, 'contact-sheet-v2.png'), image.toPNG())
  console.log('contact sheet v2 written to', outDir)
  app.quit()
})
