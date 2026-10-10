// 供应商页升级功能回归：预设填充 / 测试连接徽标 / 模型管理对话框（2026-10-10）。
// 用法：node ui_provider_check.mjs（需 dev server 5173 + 后端 8000）
// 只读：不点「保存」「添加」，对话框一律取消。截图写 scratch/check_outputs/。
import { mkdirSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { chromium } from 'playwright'

const BASE = process.env.BASE || 'http://localhost:5173'
const OUT = fileURLToPath(new URL('../scratch/check_outputs/', import.meta.url))
mkdirSync(OUT, { recursive: true })

const results = []
function ok(name, cond, extra = '') {
  results.push([name, !!cond])
  console.log(cond ? `  ok   ${name}` : `  FAIL ${name}${extra ? ` — ${extra}` : ''}`)
}

// 设置面板本身也是 el-dialog（正文里还有「管理模型」等按钮文本），按标题元素过滤
function visibleDialog(titlePart) {
  return page
    .locator('.el-dialog:visible')
    .filter({ has: page.locator('.el-dialog__title', { hasText: titlePart }) })
    .first()
}

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

const ctx = await browser.newContext({ viewport: { width: 1440, height: 950 } })
const page = await ctx.newPage()
page.on('pageerror', (e) => console.log('PAGEERROR', String(e).slice(0, 160)))
await page.goto(BASE + '/', { waitUntil: 'domcontentloaded' })
await page.waitForTimeout(1500)

// 打开设置 → 模型标签
await page.locator('.aside-footer .collapse-btn').first().click()
await page.waitForSelector('.settings-card', { timeout: 10000 })
await page.locator('.settings-tab', { hasText: '模型' }).first().click()
await page.waitForTimeout(800)

// 1) 测试连接：OpenCode Go 卡片的「测试」→ 徽标
const card = page.locator('.provider-card', { hasText: 'OpenCode Go' }).first()
await card.scrollIntoViewIfNeeded()
await card.locator('button', { hasText: '测试' }).click()
const badge = card.locator('.probe-badge').first()
await badge.waitFor({ timeout: 45000 })
const badgeText = (await badge.textContent()).trim()
ok('测试连接产生徽标', /[✓✗]/.test(badgeText), badgeText)
console.log('  badge =', badgeText)
await page.locator('.settings-card').screenshot({ path: OUT + 'provider_test_badge.png' })

// 2) 预设填充：添加对话框选「OpenCode Go」预设
await page.locator('.section-head button', { hasText: '添加供应商' }).first().click()
const dialog = visibleDialog('添加对话模型供应商')
await dialog.waitFor({ timeout: 5000 })
await dialog.locator('.el-form-item', { hasText: '从预设开始' }).locator('.el-select').click()
await page.locator('.el-select-dropdown__item:visible', { hasText: 'OpenCode Go' }).first().click()
await page.waitForTimeout(400)
const baseVal = await dialog.locator('.el-form-item', { hasText: '接口地址' }).locator('input').inputValue()
ok('预设填充 base_url', baseVal === 'https://opencode.ai/zen/go/v1', baseVal)
const effortVisible = await dialog.locator('.el-form-item', { hasText: '接口格式' }).count()
ok('对话框含接口格式字段', effortVisible > 0)
await dialog.getByRole('button', { name: '取消' }).click()
await page.waitForTimeout(400)

// 3) 模型管理对话框：打开 → 自动拉取 → 勾选框出现
await card.locator('button', { hasText: '管理模型' }).click()
const mdialog = visibleDialog('管理模型')
await mdialog.waitFor({ timeout: 5000 })
// 等在线列表到位（≥2 才算拉取成功；超时则记录实际数量）
await page
  .waitForFunction(
    () => document.querySelectorAll('.el-dialog .model-dialog-item').length >= 2,
    { timeout: 45000 },
  )
  .catch(() => {})
const n = await mdialog.locator('.model-dialog-item').count()
ok('模型管理拉取到候选', n >= 2, `count=${n}`)
await mdialog.screenshot({ path: OUT + 'provider_model_dialog.png' })
await mdialog.getByRole('button', { name: '取消' }).click()

// 4) 测速全部：触发后每卡出现 ⚡ 徽标（等待所有完成）
await page.locator('.section-head button', { hasText: '测速全部' }).click()
await page
  .waitForFunction(
    () => document.querySelectorAll('.probe-badge').length >= 2,
    { timeout: 90000 },
  )
  .catch(() => {})
const speedBadges = await page.locator('.provider-card .probe-badge').count()
ok('测速产生徽标', speedBadges >= 2, `count=${speedBadges}`)
await page.locator('.settings-card').screenshot({ path: OUT + 'provider_speedtest.png' })

// 5) 聊天页快捷切换 chip：切到另一个供应商再切回（不改变最终激活项）
await page.goto(BASE + '/', { waitUntil: 'domcontentloaded' })
await page.waitForTimeout(1500)
const chip = page.locator('.model-switch-chip').first()
await chip.waitFor({ timeout: 10000 })
const chipText = (await chip.textContent()).replace(/\s+/g, ' ').trim()
ok('聊天页出现供应商 chip', chipText.length > 0, chipText)
console.log('  chip =', chipText)

await chip.click()
const items = page.locator('.el-dropdown-menu__item:visible')
await items.first().waitFor({ timeout: 5000 })
const itemCount = await items.count()
const norm = (s) => s.replace(/\s+/g, '')
let originalName = ''
let switched = false
for (let i = 0; i < itemCount; i++) {
  const text = (await items.nth(i).textContent()).trim()
  if (text.includes('管理供应商')) continue
  const name = text.split(' · ')[0].trim()
  if (norm(chipText).includes(norm(name))) {
    originalName = name
    continue // 当前项（下拉里是 disabled）
  }
  if (!switched) {
    await items.nth(i).click()
    switched = true
  }
}
ok('下拉可切换供应商', switched, `original=${originalName}`)
await page.waitForTimeout(1500)
const chipText2 = (await chip.textContent()).replace(/\s+/g, ' ').trim()
ok('切换后 chip 更新', switched && chipText2 !== chipText, chipText2)
console.log('  chip after =', chipText2)

// 切回原供应商
if (switched && originalName) {
  await chip.click()
  await page.waitForTimeout(400)
  await page.locator('.el-dropdown-menu__item:visible', { hasText: originalName }).first().click()
  await page.waitForTimeout(1500)
  const chipText3 = (await chip.textContent()).replace(/\s+/g, ' ').trim()
  ok('已切回原供应商', norm(chipText3).includes(norm(originalName)), chipText3)
  console.log('  chip restored =', chipText3)
}

await ctx.close()
await browser.close()
const failed = results.filter(([, pass]) => !pass)
console.log(failed.length ? `\n失败 ${failed.length} 项` : '\n全部通过')
process.exit(failed.length ? 1 : 0)
