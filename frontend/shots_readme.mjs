/**
 * README 截图脚本：抓 5 张界面图到 ../docs/screenshots/。
 * 前置：vite dev（默认 5173，走本地后端数据）或 Docker 前端已运行。
 * 用法：node shots_readme.mjs                        # http://localhost:5173
 *       BASE=http://localhost:5174 node shots_readme.mjs
 */
import { chromium } from 'playwright'
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const BASE = process.env.BASE || 'http://localhost:5173'
// ONLY=03-knowledge 只重拍单张（其余步骤跳过），便于局部更新
const ONLY = process.env.ONLY || ''
const OUT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../docs/screenshots')
fs.mkdirSync(OUT, { recursive: true })

const browser = await chromium.launch()
const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 1 })
// 固定浅色 + 书斋暖读风格（应用从 localStorage 读 rag_theme / rag_style）
await ctx.addInitScript(() => {
  localStorage.setItem('rag_theme', 'light')
  localStorage.setItem('rag_style', 'salon')
})
const page = await ctx.newPage()
const log = (m) => console.log('·', m)

async function shot(name) {
  await page.screenshot({ path: path.join(OUT, `${name}.png`) })
  log(`saved ${name}.png`)
}

await page.goto(BASE, { waitUntil: 'domcontentloaded' })
await page.waitForTimeout(2500)

// ONLY 未指定时全拍；指定时只跑对应步骤（便于单张重拍）
function want(name) {
  return !ONLY || ONLY === name
}

async function step(name, fn) {
  if (!want(name)) return
  try {
    await fn()
  } catch (e) {
    log(`${name}: ` + String(e.message).split('\n')[0])
  }
  await shot(name)
}

// 1) 欢迎页：新建对话 → 三条建议卡
await step('01-chat-welcome', async () => {
  await page.getByRole('button', { name: '新建对话' }).click()
  await page.waitForSelector('.suggestions .suggestion', { timeout: 30000 })
  await page.waitForTimeout(1200)
})

// 2) Agent 对话：现场提问一个知识库问题，等回答与来源卡片完成
await step('02-agent-chat', async () => {
  const input = page.getByPlaceholder(/输入你的问题/)
  await input.fill('《人类简史》主要讲了什么？')
  await input.press('Enter')
  await page.waitForSelector('.sources', { timeout: 120000 })
  await page.waitForSelector('.stop-btn', { state: 'detached', timeout: 180000 })
  await page.waitForTimeout(1500)
})

// 3) 知识库
await step('03-knowledge', async () => {
  await page.locator('.app-menu .el-menu-item', { hasText: '知识库' }).click()
  await page.waitForTimeout(3000)
  // 「数据目录」行会显示本机绝对路径（含用户名/目录结构）：只替换文本节点，保留「更改目录」按钮
  await page.evaluate(() => {
    const box = document.querySelector('.data-dir')
    if (!box) return
    for (const n of box.childNodes) {
      if (n.nodeType === Node.TEXT_NODE && n.nodeValue.includes('数据目录')) {
        n.nodeValue = ' 数据目录：D:\\zhiwen\\data '
      }
    }
  })
})

// 4) 运行记录
await step('04-runs', async () => {
  await page.locator('.app-menu .el-menu-item', { hasText: '运行记录' }).click()
  await page.waitForTimeout(3000)
})

// 5) 设置（模型面板）
await step('05-settings', async () => {
  await page.locator('.aside-footer .collapse-btn').first().click()
  await page.waitForSelector('.el-dialog', { timeout: 10000 })
  await page.waitForTimeout(800)
  await page.locator('.el-dialog').getByText('模型', { exact: true }).first().click()
  await page.waitForTimeout(1200)
})

await browser.close()
log('done → ' + OUT)
