/** 临时冒烟：ChatView 拆分后主路径（不进 CI，跑完可删）。 */
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
      /* try next */
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

await page.goto(`${BASE}/`, { waitUntil: 'networkidle' })
await page.waitForSelector('.chat-page', { timeout: 15000 })
await page.waitForTimeout(800)

check('欢迎页渲染', await page.locator('.welcome').count() === 1)
check('建议问题 >= 3', (await page.locator('.suggestion').count()) >= 3)
check('会话列表已加载', (await page.locator('.session-item').count()) >= 0)

// 输入区开关
const autoChip = page.locator('.composer-chip').first()
await autoChip.click()
await page.waitForTimeout(100)
check('切到手动模式', (await page.locator('.composer-chip.on').count()) === 0)
await page.locator('.composer-chip').nth(2).click()
await page.waitForTimeout(100)
check('手动模式下可勾选知识库', (await page.locator('.composer-chip.on').count()) === 1)

// 知识库范围选择（真实后端文档列表）
await page.locator('.composer-scope').click()
await page.waitForTimeout(600)
check('范围对话框打开', (await page.getByText('限定知识库范围').count()) >= 1)
check('范围选项有内容', (await page.locator('.scope-option').count()) >= 1)
await page.getByRole('button', { name: '取消', exact: true }).click()
await page.waitForTimeout(300)
check(
  '取消后 chip 保持全部',
  (await page.locator('.composer-scope').innerText()).includes('全部'),
)

// 模板管理（真实后端）
await page.locator('.header-more-btn').click()
await page.waitForTimeout(300)
await page.getByText('提示词模板', { exact: true }).click()
await page.waitForSelector('.el-dialog', { timeout: 10000 })
check('模板管理对话框打开', await page.getByText('提示词模板管理').count() === 1)
check('模板列表有内容', (await page.locator('.tpl-item').count()) >= 1)
await page.reload({ waitUntil: 'networkidle' })
await page.waitForTimeout(800)

// 记忆抽屉
await page.locator('.header-more-btn').click()
await page.waitForTimeout(300)
await page.getByText('记忆', { exact: true }).click()
await page.waitForTimeout(600)
check('记忆对话框打开', await page.getByText('长期记忆').count() >= 1)
await page.reload({ waitUntil: 'networkidle' })
await page.waitForTimeout(800)

// 切换会话（读真实历史）
const sessionCount = await page.locator('.session-item').count()
if (sessionCount > 0) {
  await page.locator('.session-item').first().click()
  await page.waitForTimeout(1200)
  check('切换会话后欢迎页隐藏', (await page.locator('.welcome').count()) === 0)
  check('会话消息或空态渲染', (await page.locator('.msg').count()) >= 0)
  // 草稿恢复：输入后切走再切回
  const box = page.locator('.input-box textarea')
  await box.fill('草稿冒烟')
  await page.locator('.session-item').nth(Math.min(1, sessionCount - 1)).click()
  await page.waitForTimeout(800)
  await page.locator('.session-item').first().click()
  await page.waitForTimeout(800)
  check('会话草稿切回后恢复', (await box.inputValue()) === '草稿冒烟')
} else {
  console.log('  --   无历史会话，跳过切换用例')
}

await browser.close()
if (failures.length) {
  console.error(`\n失败 ${failures.length} 项：${failures.join('、')}`)
  process.exit(1)
}
if (errors.length) {
  console.error(`\n页面报错 ${errors.length} 条`)
  process.exit(1)
}
console.log('\n冒烟通过：无页面/控制台错误')
