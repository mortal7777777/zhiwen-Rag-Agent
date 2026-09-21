// 扫描全部书：TOC 质量 + 中间项点击跳转精度
import { chromium } from 'playwright'
const BASE = process.env.BASE || 'http://localhost:5273'
const b = await chromium.launch()
const ctx = await b.newContext({ viewport: { width: 1440, height: 900 } })
const page = await ctx.newPage()
page.on('pageerror', (e) => console.log('PAGEERROR', String(e).slice(0, 150)))

await page.goto(BASE + '/knowledge')
await page.waitForSelector('.el-table', { timeout: 20000 })
await page.waitForTimeout(600)
// 知识库页状态卡片文本
const statusText = await page.evaluate(() => {
  const el = document.querySelector('.status-area, .index-status, [class*=status]')
  return el ? el.innerText.slice(0, 400) : null
})
console.log('状态卡片:', JSON.stringify(statusText))

const rows = await page.locator('.el-table__row').count()
console.log('文档数:', rows)
for (let r = 0; r < rows; r++) {
  const name = await page.locator('.el-table__row').nth(r).locator('.doc-name').textContent()
  await page.locator('.el-table__row').nth(r).locator('button', { hasText: '预览' }).click()
  await page.waitForSelector('.doc-viewer-drawer .tv-scroll', { timeout: 30000 }).catch(() => {
    console.log(`--- ${name}: 无 tv-scroll（可能非文本格式）`)
    return
  })
  await page.waitForTimeout(1600)
  const info = await page.evaluate(() => {
    const tocItems = [...document.querySelectorAll('.dv-toc-item')]
    const labels = tocItems.map((e) => e.textContent.trim())
    const badCount = labels.filter((l) => /^(正文|第\d+段)$/.test(l)).length
    return { total: labels.length, labels: labels.slice(0, 24), badCount }
  })
  console.log(`\n=== ${name} === TOC ${info.total} 项，噪音 ${info.badCount}`)
  console.log(info.labels.join(' | '))
  // 跳转精度：点中间一项
  if (info.total > 3) {
    const mid = Math.floor(info.total / 2)
    const label = info.labels[mid]
    await page.locator('.dv-toc-item').nth(mid).click()
    await page.waitForTimeout(1600)
    const j = await page.evaluate(() => {
      const sc = document.querySelector('.doc-viewer-drawer .tv-scroll')
      const act = document.querySelector('.dv-toc-item.active')
      const flash = document.querySelector('.viewer-flash')
      const fr = flash?.getBoundingClientRect()
      const cr = sc.getBoundingClientRect()
      return {
        top: Math.round(sc.scrollTop),
        active: act?.textContent.trim(),
        flashInView: fr ? fr.bottom >= cr.top && fr.top < cr.bottom : false,
      }
    })
    console.log(`   点击 [${label}] -> top=${j.top} active=${j.active} flashInView=${j.flashInView}`)
  }
  await page.keyboard.press('Escape')
  await page.waitForTimeout(700)
}
await b.close()
