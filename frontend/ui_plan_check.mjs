// 计划模式 UI 回归：开关渲染 → 模型自主进入计划模式 → 挂起渲染确认卡 →
// 编辑后 Ctrl+Enter 确认（同一 run 经 /agent/resume 继续）→ 首个写入再挂起 → 数字键批准
// 慢供应商下 180s 不够探索+提交：CHECK_TIMEOUT_MS=420000 node ui_plan_check.mjs
import { mkdirSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { chromium } from 'playwright'

const T = (v) => Number(process.env.CHECK_TIMEOUT_MS || v)
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
const shotDir = new URL('../scratch/check_outputs/', import.meta.url)
mkdirSync(shotDir, { recursive: true })
const shot = (name) => ({ path: fileURLToPath(new URL(name, shotDir)) })
page.on('pageerror', (e) => console.log('PAGEERROR', String(e).slice(0, 200)))
page.on('console', (m) => {
  if (m.type() === 'error') console.log('CONSOLE_ERR', m.text().slice(0, 200))
})

await page.goto(BASE + '/', { waitUntil: 'domcontentloaded' })
await page.waitForTimeout(2500)

// 1) 计划模式开关：在设置页「工具与集成 → 计划模式」（聊天工具栏已无此开关）
const planAllowed = await page.evaluate(async () => {
  const r = await fetch('/api/settings')
  const cfg = await r.json()
  return (cfg.editable || {}).plan_mode_allowed !== false
})
console.log('计划模式开关（设置页）:', planAllowed ? '开启' : '关闭')
if (!planAllowed) {
  console.log('✘ 计划模式已在设置页关闭，后端不注册 enter/exit 工具，无法跑本流程')
  await b.close()
  process.exit(1)
}

// 2) 发一条明确要求先出计划的消息
const textarea = page.locator('textarea').first()
await textarea.fill('先进入计划模式：给我一份"用 SVG 画一只猫的 HTML 页面"的执行计划，等我确认后再动手，不要直接写文件。')
await page.keyboard.press('Enter')

// 3) 等计划确认卡（后端要探索 + 提交，给足时间）
try {
  await page.waitForSelector('.plan-approval-box', { timeout: T(180000) })
  console.log('✅ 计划确认卡已出现')
} catch (e) {
  console.log('❌ 未出现计划确认卡', String(e).slice(0, 120))
  await page.screenshot({ ...shot('ui_plan_fail.png'), fullPage: true })
  await b.close()
  process.exit(1)
}
await page.waitForTimeout(800)
const cardText = await page.locator('.plan-approval-box').innerText()
console.log('卡片内容:', JSON.stringify(cardText.slice(0, 400)))
await page.screenshot({ ...shot('ui_plan_card.png'), fullPage: true })

// 4) 键盘路径：卡出现后应自动获得焦点，Ctrl+Enter 确认（不点按钮）
const focusedCard = await page.evaluate(() => {
  const el = document.activeElement
  return el?.classList?.contains('plan-approval-box') ? 'card' : el?.tagName || 'none'
})
console.log('卡片是否自动聚焦:', focusedCard)

// 等本轮 run 结束（按钮解禁）再确认：生成中 loading=true 时会拦住确认。
// 注意 waitForFunction 第二参是"传给页面函数的 arg"，options 必须放第三参
// （原写法把 {timeout} 当 arg 传了，实际一直用默认 30s——2026-10-10 修复）。
await page.waitForFunction(
  () => {
    const btn = [...document.querySelectorAll('.plan-approval-box button')].find((b) =>
      b.textContent.includes('确认执行'),
    )
    return btn && !btn.disabled
  },
  null,
  { timeout: T(90000) },
)
console.log('确认按钮已可用（挂起收流，等用户决定）')

// 编辑步骤（追加一步），再回到卡片主体用 Ctrl+Enter 确认
const box = page.locator('.plan-approval-box textarea').first()
const before = await box.inputValue()
await box.fill(before + '\n最后写一句：由计划模式确认后执行。')
console.log('步骤已编辑:', (await box.inputValue()).split('\n').length, '行')
await page.locator('.plan-approval-box .plan-head').click() // 焦点离开 textarea
await page.keyboard.press('Control+Enter')
await page.waitForTimeout(2500)
await page.screenshot({ ...shot('ui_plan_confirmed.png'), fullPage: true })

// 5) 确认后同一 run 继续（原生挂起路径：不再重发新 run）：
//    exit_plan_mode 的合成结果回填 + 任务清单播种 + 不再新增用户消息
const bodyText = await page.locator('body').innerText()
console.log('同一 run 继续（exit_plan_mode 结果已回填）:', bodyText.includes('计划已确认'))
console.log('任务清单出现:', bodyText.includes('任务清单') || bodyText.includes('执行计划'))
console.log('确认卡已转为只读步骤列表:', (await page.locator('.plan-approval-box[data-plan-state="confirmed"]').count()) > 0)
console.log('未追加"按计划开始执行"用户消息:', !bodyText.includes('按计划开始执行'))

// 6) 审批卡键盘：等到 permission 卡片，按数字键 1 批准（焦点不在卡内也应生效）
let permSeen = false
try {
  await page.waitForSelector('.permission-card.pending', { timeout: T(120000) })
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
