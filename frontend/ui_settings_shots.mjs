// 设置页整页截图（7 个标签 × 浅深两主题）：改设置页 UI 后整体回归/对比用。
// 用法：PHASE=before node ui_settings_shots.mjs  /  PHASE=after node ui_settings_shots.mjs
//   截图写 ../scratch/check_outputs/settings_<PHASE>/；跨 phase 用像素比对法比较。
// 依赖 dev server(5173) + 后端(8000)；只读，不触发任何保存。
import { mkdirSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { chromium } from 'playwright'

const BASE = process.env.BASE || 'http://localhost:5173'
const PHASE = process.env.PHASE || 'before'
const OUT = fileURLToPath(new URL(`../scratch/check_outputs/settings_${PHASE}/`, import.meta.url))
mkdirSync(OUT, { recursive: true })

let browser
for (const channel of ['chrome', 'msedge', undefined]) {
  try {
    browser = await chromium.launch(channel ? { channel } : {})
    break
  } catch {
    /* next */
  }
}
if (!browser) throw new Error('no browser')

const TABS = ['外观', '模型', '技能', '工具', '审批', '检索', '记忆']

for (const theme of ['light', 'dark']) {
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 950 } })
  await ctx.addInitScript((t) => {
    try {
      localStorage.setItem('rag_theme', t)
    } catch {
      /* ignore */
    }
  }, theme)
  const page = await ctx.newPage()
  page.on('pageerror', (e) => console.log('PAGEERROR', String(e).slice(0, 160)))
  await page.goto(BASE + '/', { waitUntil: 'domcontentloaded' })
  await page.waitForTimeout(2000)

  // 打开设置（左下角齿轮 = aside-footer 第一个按钮）
  await page.locator('.aside-footer .collapse-btn').first().click()
  await page.waitForSelector('.settings-card', { timeout: 10000 })
  await page.waitForTimeout(1200) // 等 loadSettings/refreshSkills/loadMcp 落地

  for (const tab of TABS) {
    await page.locator('.settings-tab', { hasText: tab }).first().click()
    await page.waitForTimeout(700)
    const file = `${tab}_${theme}.png`
    await page.locator('.settings-card').screenshot({ path: OUT + file })
    console.log('shot', file)
  }
  await ctx.close()
}
await browser.close()
console.log('输出目录:', OUT)
