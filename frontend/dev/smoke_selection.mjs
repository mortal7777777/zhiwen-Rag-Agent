/** 临时冒烟：多选范围 + 查看器划选（文本/PDF）→ 对话引用。 */
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

async function openDocRow(pattern) {
  const row = page.locator('.el-table__row').filter({ hasText: pattern }).first()
  await row.waitFor({ timeout: 15000 })
  const name = (await row.innerText()).split('\n')[0]
  await row.getByRole('button', { name: '预览' }).click()
  return name
}

async function selectIn(selector, minLen = 2) {
  return page.evaluate(
    ({ selector, minLen }) => {
      // PDF 懒渲染会同时存在多个文本层，挑第一个有文字节点的
      const roots = [...document.querySelectorAll(selector)]
      let node = null
      for (const root of roots) {
        const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT)
        while (walker.nextNode()) {
          const text = (walker.currentNode.textContent || '').trim()
          if (text.length >= minLen) {
            node = walker.currentNode
            break
          }
        }
        if (node) break
      }
      if (!node) return false
      const len = Math.min(40, node.textContent.length)
      const range = document.createRange()
      range.setStart(node, 0)
      range.setEnd(node, len)
      const sel = window.getSelection()
      sel.removeAllRanges()
      sel.addRange(range)
      node.parentElement.dispatchEvent(
        new MouseEvent('mouseup', { bubbles: true }),
      )
      return true
    },
    { selector, minLen },
  )
}

// 1) 文本查看器划选 → 解释
await page.goto(`${BASE}/knowledge`, { waitUntil: 'networkidle' })
await page.waitForSelector('.el-table__row', { timeout: 15000 })
const txtName = await openDocRow(/\.txt/i)
await page.waitForSelector('.tv-scroll', { timeout: 20000 })
await page.waitForTimeout(600)
check('文本查看器已打开', (await page.locator('.tv-scroll').count()) === 1)
check('文本选区已建立', await selectIn('.tv-scroll', 20))
await page.waitForSelector('.dv-selection-bar', { timeout: 5000 })
check('划选操作条出现', (await page.locator('.dv-selection-bar').count()) === 1)
await page.getByRole('button', { name: '解释', exact: true }).click()
await page.waitForURL(`${BASE}/`, { timeout: 10000 })
await page.waitForSelector('.quote-chip', { timeout: 10000 })
const quoteText = await page.locator('.quote-chip-text').innerText()
const scopeText = await page.locator('.composer-scope').innerText()
const questionText = await page.locator('.input-box textarea').inputValue()
check('引用 chip 已带入', quoteText.length > 0)
check('范围自动限定到当前文档', !scopeText.includes('全部'), scopeText)
check('问题预填为解释', questionText.includes('解释'), questionText)
check('引用来源正确', scopeText.toLowerCase().includes('.txt'), scopeText)

// 1b) 划选不覆盖已有草稿（用侧栏导航保持 SPA 状态；page.goto 会整页重载）
await page.locator('.input-box textarea').fill('我的草稿')
await page
  .locator('.app-menu .el-menu-item')
  .filter({ hasText: '知识库' })
  .click()
await page.waitForSelector('.el-table__row', { timeout: 15000 })
await openDocRow(/\.txt/i)
await page.waitForSelector('.tv-scroll', { timeout: 20000 })
await page.waitForTimeout(500)
await selectIn('.tv-scroll', 20)
await page.waitForSelector('.dv-selection-bar', { timeout: 5000 })
await page.getByRole('button', { name: '解释', exact: true }).click()
await page.waitForURL(`${BASE}/`, { timeout: 10000 })
await page.waitForSelector('.quote-chip', { timeout: 10000 })
const draftAfter = await page.locator('.input-box textarea').inputValue()
check('划选不覆盖已有草稿', draftAfter === '我的草稿', draftAfter)

// 1c) 范围按会话隔离：切走回全库，新建对话恢复原范围
await page.locator('.session-item').first().click()
await page.waitForTimeout(1200)
check(
  '切换会话后范围回到全库',
  (await page.locator('.composer-scope').innerText()).includes('全部'),
)
await page.locator('.new-chat-btn').click()
await page.waitForTimeout(800)
check(
  '新建对话恢复新会话的范围',
  (await page.locator('.composer-scope').innerText())
    .toLowerCase()
    .includes('.txt'),
)

// 2) 多选范围
await page.locator('.composer-scope').click()
await page.waitForSelector('.scope-option', { timeout: 10000 })
await page.waitForFunction(
  () => document.querySelectorAll('.scope-option').length >= 2,
  { timeout: 10000 },
)
await page.locator('.scope-option').nth(0).click()
await page.locator('.scope-option').nth(1).click()
await page.getByRole('button', { name: '应用', exact: true }).click()
await page.waitForTimeout(400)
const multiScope = await page.locator('.composer-scope').innerText()
check('多选范围生效', multiScope.includes('、') || multiScope.includes('等'), multiScope)

// 3) PDF 文本层划选 → 翻译
await page.goto(`${BASE}/knowledge`, { waitUntil: 'networkidle' })
await page.waitForSelector('.el-table__row', { timeout: 15000 })
await openDocRow(/\.pdf/i)
await page.waitForSelector('.pv-text-layer span', { timeout: 30000 })
await page.waitForFunction(
  () => document.querySelectorAll('.pv-text-layer span').length > 10,
  { timeout: 30000 },
)
await page.waitForTimeout(800)
const spanCount = await page.locator('.pv-text-layer span').count()
check('PDF 文本层已渲染', spanCount > 0, String(spanCount))
check('PDF 选区已建立', await selectIn('.pv-text-layer', 2))
await page.waitForSelector('.dv-selection-bar', { timeout: 5000 })
await page.getByRole('button', { name: '翻译', exact: true }).click()
await page.waitForURL(`${BASE}/`, { timeout: 10000 })
await page.waitForSelector('.quote-chip', { timeout: 10000 })
const pdfQuestion = await page.locator('.input-box textarea').inputValue()
check('PDF 划选带入引用', (await page.locator('.quote-chip').count()) === 1)
check('问题预填为翻译', pdfQuestion.includes('翻译'), pdfQuestion)

await browser.close()
if (failures.length) {
  console.error(`\n失败 ${failures.length} 项：${failures.join('、')}`)
  process.exit(1)
}
if (errors.length) {
  console.error(`\n页面报错 ${errors.length} 条`)
  process.exit(1)
}
console.log('\n划选冒烟通过：无页面/控制台错误')
