// 计划模式 UI 回归：开关渲染 → 模型自主进入计划模式 → 确认卡可编辑 → 点确认执行
import { chromium } from 'playwright'

const BASE = process.env.BASE || 'http://localhost:5174'
async function launch() {
  // 本地 playwright 版本与已下载的 chromium 版本不匹配，优先复用系统浏览器
  for (const channel of ['chrome', 'msedge', undefined]) {
    try {
      return await chromium.launch(channel ? { channel } : {})
    } catch (e) {
      console.log('launch failed', channel || 'bundled', String(e).slice(0, 80))
    }
  }
  throw new Error('no browser available')
}
const b = await launch()
const ctx = await b.newContext({ viewport: { width: 1440, height: 950 } })
const page = await ctx.newPage()
page.on('pageerror', (e) => console.log('PAGEERROR', String(e).slice(0, 200)))
page.on('console', (m) => {
  if (m.type() === 'error') console.log('CONSOLE_ERR', m.text().slice(0, 200))
})

await page.goto(BASE + '/', { waitUntil: 'domcontentloaded' })
await page.waitForTimeout(2500)

// 1) 新开关是否渲染
const planSwitch = page.locator('.toolbar .el-switch', { hasText: '计划模式' })
console.log('计划模式开关数量:', await planSwitch.count())
console.log('开关初始状态:', await planSwitch.first().getAttribute('class'))

// 2) 发一条明确要求先出计划的消息
const textarea = page.locator('textarea').first()
await textarea.fill('先进入计划模式：给我一份"用 SVG 画一只猫的 HTML 页面"的执行计划，等我确认后再动手，不要直接写文件。')
await page.keyboard.press('Enter')

// 3) 等计划确认卡（后端要探索 + 提交，给足时间）
try {
  await page.waitForSelector('.plan-approval-box', { timeout: 180000 })
  console.log('✅ 计划确认卡已出现')
} catch (e) {
  console.log('❌ 未出现计划确认卡', String(e).slice(0, 120))
  await page.screenshot({ path: 'ui_plan_fail.png', fullPage: true })
  await b.close()
  process.exit(1)
}
await page.waitForTimeout(800)
const cardText = await page.locator('.plan-approval-box').innerText()
console.log('卡片内容:', JSON.stringify(cardText.slice(0, 400)))
await page.screenshot({ path: 'ui_plan_card.png', fullPage: true })

// 4) 键盘路径：卡出现后应自动获得焦点，Ctrl+Enter 确认（不点按钮）
const focusedCard = await page.evaluate(() => {
  const el = document.activeElement
  return el?.classList?.contains('plan-approval-box') ? 'card' : el?.tagName || 'none'
})
console.log('卡片是否自动聚焦:', focusedCard)

// 等本轮 run 结束（按钮解禁）再确认：生成中 loading=true 时会拦住确认
await page.waitForFunction(
  () => {
    const btn = [...document.querySelectorAll('.plan-approval-box button')].find((b) =>
      b.textContent.includes('确认执行'),
    )
    return btn && !btn.disabled
  },
  { timeout: 90000 },
)
console.log('确认按钮已可用（run 已收尾）')

// 编辑步骤（追加一步），再回到卡片主体用 Ctrl+Enter 确认
const box = page.locator('.plan-approval-box textarea').first()
const before = await box.inputValue()
await box.fill(before + '\n最后写一句：由计划模式确认后执行。')
console.log('步骤已编辑:', (await box.inputValue()).split('\n').length, '行')
await page.locator('.plan-approval-box .plan-head').click() // 焦点离开 textarea
await page.keyboard.press('Control+Enter')
await page.waitForTimeout(2500)
await page.screenshot({ path: 'ui_plan_confirmed.png', fullPage: true })

// 5) 确认后应出现「按计划开始执行」的用户消息 + 任务清单
const bodyText = await page.locator('body').innerText()
console.log('确认后的用户消息存在:', bodyText.includes('按计划开始执行'))
console.log('任务清单出现:', bodyText.includes('任务清单') || bodyText.includes('执行计划'))
console.log('确认卡已转为只读步骤列表:', (await page.locator('.plan-approval-box[data-plan-state="confirmed"]').count()) > 0)

// 6) 审批卡键盘：等到 permission 卡片，按数字键 1 批准（焦点不在卡内也应生效）
let permSeen = false
try {
  await page.waitForSelector('.permission-card.pending', { timeout: 120000 })
  permSeen = true
} catch {
  console.log('（本次任务未触发审批卡，跳过审批键盘检查）')
}
if (permSeen) {
  await page.evaluate(() => document.activeElement?.blur?.())
  await page.keyboard.press('1')
  await page.waitForTimeout(1500)
  const stillPending = await page.locator('.permission-card.pending').count()
  console.log('数字键批准成功:', stillPending === 0)
}
await b.close()
console.log('DONE')
