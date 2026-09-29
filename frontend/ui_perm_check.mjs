// 审批卡回归（原生挂起版）：
// 1) 敏感操作触发 interrupt，本条流收尾并渲染待决定卡片（挂起横幅在）；
// 2) 焦点不在卡内时数字键也能批准（全局键盘）；
// 3) 唯一卡片决定后自动经 /agent/resume 开第二条流，续跑写回同一条回答。
import { mkdirSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
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

// 触发一次写文件（会挂起并弹审批卡）
await page.locator('textarea').first().fill(
  '新建文件 kb_perm_key_test.txt，内容一行：审批卡键盘测试。不要问我确认细节，直接调用工具。',
)
await page.keyboard.press('Enter')

await page.waitForSelector('.permission-card.pending', { timeout: 180000 })
console.log('✅ 审批卡已出现（挂起）')
const summary = await page.locator('.permission-card.pending .permission-summary').first().innerText()
console.log('卡片内容:', summary.slice(0, 80))

// 挂起横幅：等全部卡片决定后自动继续
const barCount = await page.locator('.suspension-bar').count()
console.log('挂起提示条存在:', barCount > 0)

// 关键场景：焦点不在卡片内（模拟用户点了别处 / 在输入框打了草稿后又离开）
await page.evaluate(() => document.activeElement?.blur?.())
const activeTag = await page.evaluate(() => document.activeElement?.tagName || 'BODY')
console.log('当前焦点:', activeTag)

// 数字键 1 = 批准（全局生效，不需要先点卡片）→ 决定收齐后自动提交并续跑
await page.keyboard.press('1')
await page
  .waitForFunction(() => document.querySelectorAll('.permission-card.pending').length === 0, null, {
    timeout: 30000,
  })
  .then(() => console.log('✅ 数字键批准生效，卡片已收起'))
  .catch(() => console.log('✘ 卡片未收起（批准失败或出现新一批审批）'))

// 恢复流续跑：同一条气泡里出现正文（模型汇报执行结果）
const gotAnswer = await page
  .waitForFunction(
    () => {
      const nodes = document.querySelectorAll('.msg.assistant .markdown-body')
      const last = nodes[nodes.length - 1]
      return last && last.innerText.length > 10
    },
    null,
    { timeout: 180000 },
  )
  .then(() => true)
  .catch(() => false)
console.log('恢复流产出正文:', gotAnswer)

const shot = fileURLToPath(new URL('../scratch/check_outputs/ui_perm_check.png', import.meta.url))
mkdirSync(new URL('../scratch/check_outputs/', import.meta.url), { recursive: true })
await page.screenshot({ path: shot, fullPage: false })
await b.close()
console.log('DONE')
