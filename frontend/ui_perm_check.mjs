// 审批卡键盘回归：焦点不在卡内时数字键也能批准（用户反馈"有时键盘确认不了"）
import { chromium } from 'playwright'

const BASE = process.env.BASE || 'http://localhost:5173'

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

const b = await launch()
const page = await (await b.newContext({ viewport: { width: 1440, height: 950 } })).newPage()
page.on('pageerror', (e) => console.log('PAGEERROR', String(e).slice(0, 160)))

await page.goto(BASE + '/', { waitUntil: 'domcontentloaded' })
await page.waitForTimeout(2500)

// 触发一次写文件（会弹审批卡）
await page.locator('textarea').first().fill(
  '新建文件 kb_perm_key_test.txt，内容一行：审批卡键盘测试。不要问我确认细节，直接调用工具。',
)
await page.keyboard.press('Enter')

await page.waitForSelector('.permission-card.pending', { timeout: 180000 })
console.log('✅ 审批卡已出现')
const summary = await page.locator('.permission-card.pending .permission-summary').first().innerText()
console.log('卡片内容:', summary.slice(0, 80))

// 关键场景：焦点不在卡片内（模拟用户点了别处 / 在输入框打了草稿后又离开）
await page.evaluate(() => document.activeElement?.blur?.())
const activeTag = await page.evaluate(() => document.activeElement?.tagName || 'BODY')
console.log('当前焦点:', activeTag)

// 数字键 1 = 批准（全局生效，不需要先点卡片）
await page.keyboard.press('1')
await page.waitForTimeout(2000)
const pendingLeft = await page.locator('.permission-card.pending').count()
console.log('数字键批准成功:', pendingLeft === 0)

// 收尾：确认文件真的被写入
await page.waitForTimeout(4000)
await page.screenshot({ path: 'ui_perm_after.png', fullPage: false })
await b.close()
console.log('DONE')
