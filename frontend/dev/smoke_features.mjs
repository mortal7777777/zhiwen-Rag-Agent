/** 临时冒烟：运行回放 / 索引健康 / 限定范围（需后端已重启）。 */
import { chromium } from 'playwright'

const BASE = process.env.BASE || 'http://localhost:5173'
const errors = []
const failures = []
const check = (name, cond, extra = '') => {
  if (cond) console.log(`  ok   ${name}`)
  else {
    failures.push(name)
    console.log(`  FAIL ${name}${extra ? ` — ${extra}` : ''}`)
  }
}

async function launch() {
  for (const channel of ['chrome', 'msedge', undefined]) {
    try {
      return await chromium.launch(channel ? { channel } : {})
    } catch {
      /* next */
    }
  }
  throw new Error('no browser available')
}

const browser = await launch()
const page = await browser.newPage({ viewport: { width: 1440, height: 900 } })
page.on('pageerror', (e) => {
  errors.push(String(e))
  console.log('PAGEERROR', String(e).slice(0, 200))
})
page.on('console', (m) => {
  if (m.type() === 'error' && !m.text().includes('Failed to load resource')) {
    errors.push(m.text())
    console.log('CONSOLE-ERR', m.text().slice(0, 200))
  }
})

// 1) 运行回放
await page.goto(`${BASE}/runs`, { waitUntil: 'networkidle' })
await page.waitForSelector('.el-table__row', { timeout: 15000 })
await page.getByRole('button', { name: '回放' }).first().click()
await page.waitForSelector('.replay-body', { timeout: 15000 })
await page.waitForTimeout(600)
check('回放抽屉打开', (await page.locator('.replay-question').innerText()).length > 0)
check('回放指标网格', (await page.locator('.replay-grid .replay-cell').count()) >= 5)
check('逐调用遥测表', (await page.locator('.replay-table .el-table__row').count()) >= 1)
check('工具轨迹区', (await page.getByText(/工具轨迹（/).count()) >= 1)

// 2) 索引健康
await page.goto(`${BASE}/knowledge`, { waitUntil: 'networkidle' })
await page.waitForSelector('.health-row', { timeout: 15000 })
const healthText = await page.locator('.health-row').innerText()
check('索引健康行', healthText.includes('文件') && healthText.includes('最近构建'), healthText.slice(0, 80))

// 3) 问此文档 → 范围 chip
await page.getByRole('button', { name: '问此文档' }).first().click()
await page.waitForSelector('.composer-scope', { timeout: 15000 })
await page.waitForTimeout(600)
const chip = await page.locator('.composer-scope').innerText()
check('文档范围已带入对话', chip.includes('范围：') && !chip.includes('全部'), chip)

// 4) 范围对话框：选择第一个具体范围并清除
await page.locator('.composer-scope').click()
await page.waitForSelector('.scope-option', { timeout: 10000 })
// 文档列表是异步加载的：等第二个选项出现再计数（否则只数到「全部知识库」）
await page.waitForFunction(
  () => document.querySelectorAll('.scope-option').length >= 2,
  { timeout: 10000 },
)
const scopeOptions = await page.locator('.scope-option').count()
check('范围选项列表', scopeOptions >= 2)
await page.locator('.scope-option').nth(1).click()
await page.getByRole('button', { name: '应用', exact: true }).click()
await page.waitForTimeout(300)
const chip2 = await page.locator('.composer-scope').innerText()
check('选择具体范围后 chip 更新', !chip2.includes('全部'), chip2)
await page.locator('.scope-clear').click()
await page.waitForTimeout(200)
check('清除范围', (await page.locator('.composer-scope').innerText()).includes('全部'))

await browser.close()
if (failures.length) {
  console.error(`\n失败 ${failures.length} 项：${failures.join('、')}`)
  process.exit(1)
}
if (errors.length) {
  console.error(`\n页面报错 ${errors.length} 条`)
  process.exit(1)
}
console.log('\n功能冒烟通过：无页面/控制台错误')
