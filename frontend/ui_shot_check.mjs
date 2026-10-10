// 前端视觉基线截图（阶段 0 工具）。
// 用法：先启动 dev server（npm run dev），再 `node ui_shot_check.mjs`。
// 固定清单：3 页面 × 浅/深 + 设置对话框 + 记忆面板；截图写 ../scratch/check_outputs/frontend_shots/。
// 会打印页面/控制台错误（有错误则退出码非 0），便于"改前 / 改后"对照（见 docs/FRONTEND_REDESIGN_PLAN.md 阶段 0）。
// BASE 环境变量可换端口（默认 http://localhost:5173）。
import { mkdirSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, resolve } from 'node:path'
import { chromium } from 'playwright'

const BASE = process.env.BASE || 'http://localhost:5173'
const HERE = dirname(fileURLToPath(import.meta.url))
const OUT = resolve(HERE, '../scratch/check_outputs/frontend_shots')
mkdirSync(OUT, { recursive: true })

async function launch() {
  for (const channel of ['chrome', 'msedge', undefined]) {
    try {
      return await chromium.launch(channel ? { channel } : {})
    } catch (e) {
      console.log('launch failed', channel || 'bundled', String(e).slice(0, 60))
    }
  }
  throw new Error('no browser available')
}

const pages = [
  ['chat', '/'],
  ['knowledge', '/knowledge'],
  ['runs', '/runs'],
]

const b = await launch()
const ctx = await b.newContext({ viewport: { width: 1440, height: 950 } })
const page = await ctx.newPage()
let errors = 0
page.on('pageerror', (e) => {
  errors += 1
  console.log('PAGEERROR', String(e).slice(0, 160))
})
page.on('console', (m) => {
  if (m.type() === 'error') {
    errors += 1
    console.log('CONSOLE-ERR', m.text().slice(0, 160))
  }
})

for (const theme of ['light', 'dark']) {
  for (const [name, path] of pages) {
    await page.goto(BASE + '/', { waitUntil: 'domcontentloaded' })
    await page.evaluate((t) => localStorage.setItem('rag_theme', t), theme)
    await page.goto(BASE + path, { waitUntil: 'domcontentloaded' })
    await page.waitForTimeout(2300)
    await page.screenshot({ path: `${OUT}/${name}-${theme}.png` })
    console.log('shot:', `${name}-${theme}`)
  }
  // 设置对话框 + 记忆面板（仅聊天页）
  await page.goto(BASE + '/', { waitUntil: 'domcontentloaded' })
  await page.waitForTimeout(1800)
  try {
    await page.locator('.collapse-btn').first().click()
    await page.waitForTimeout(1100)
    await page.screenshot({ path: `${OUT}/settings-${theme}.png` })
    console.log('shot:', `settings-${theme}`)
    await page.keyboard.press('Escape')
    await page.waitForTimeout(500)
  } catch (e) {
    console.log('settings shot failed:', String(e).slice(0, 100))
  }
  try {
    // 「记忆」已收进顶栏「更多」下拉（2026-10-10 步骤 2a）：先开菜单再点菜单项
    await page.locator('.header-more-btn').first().click()
    await page.waitForTimeout(400)
    await page.locator('.el-dropdown__popper:visible .el-dropdown-menu__item:has-text("记忆")').first().click()
    await page.waitForTimeout(1300)
    await page.screenshot({ path: `${OUT}/memory-${theme}.png` })
    console.log('shot:', `memory-${theme}`)
  } catch (e) {
    console.log('memory shot failed:', String(e).slice(0, 100))
  }
}

await b.close()
console.log(`DONE (${errors} page errors) -> ${OUT}`)
if (errors) process.exitCode = 1
