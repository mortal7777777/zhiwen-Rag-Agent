// 增量流式 UI 回归：
// 1) 正文边生成边出现（首个非空样本应远早于结束，而不是整段一次到达）；
// 2) 不重复显示（对照后端持久化的 assistant 正文长度，增量 + 全文 token 双发不能叠加）；
// 3) 工具轮的过渡思考文本被撤回（峰值文本长度应大于最终，且最终不以其开头）。
// 用法：BASE=http://localhost:5173 node ui_stream_check.mjs
import { mkdirSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { chromium } from 'playwright'

const BASE = process.env.BASE || 'http://localhost:5173'

async function launch() {
  for (const channel of ['chrome', 'msedge', undefined]) {
    try {
      return await chromium.launch(channel ? { channel } : {})
    } catch (e) {
      console.log('launch failed', channel || 'bundled', String(e).slice(0, 80))
    }
  }
  throw new Error('no browser available')
}

const lastAssistantText = () =>
  document.querySelector('.msg.assistant:last-of-type .markdown-body')?.innerText || ''

async function askAndSample(page, question, { maxMs = 120000, stableMs = 4000 } = {}) {
  const textarea = page.locator('textarea').first()
  await textarea.fill(question)
  await page.keyboard.press('Enter')

  const t0 = Date.now()
  const samples = []
  let lastChange = Date.now()
  let prev = ''
  while (Date.now() - t0 < maxMs) {
    await page.waitForTimeout(90)
    const text = await page.evaluate(lastAssistantText)
    const t = Date.now() - t0
    if (text !== prev) {
      samples.push({ t, len: text.length })
      lastChange = Date.now()
      prev = text
    } else if (prev.length > 0 && Date.now() - lastChange > stableMs) {
      break // 稳定 4s：视为本轮结束
    }
  }
  return { samples, finalText: prev }
}

async function persistedAnswer(request, question) {
  // 最新会话（列表按更新时间倒序）里最后一条 assistant 正文 = 权威答案
  const convs = await (await request.get(`${BASE}/api/conversations`)).json()
  const conv = convs[0]
  const msgs = await (await request.get(`${BASE}/api/conversations/${conv.id}/messages`)).json()
  const assistants = msgs.filter((m) => m.role === 'assistant')
  return { convId: conv.id, text: assistants.length ? assistants[assistants.length - 1].content : '' }
}

const b = await launch()
const ctx = await b.newContext({ viewport: { width: 1440, height: 950 } })
const page = await ctx.newPage()
page.on('pageerror', (e) => console.log('PAGEERROR', String(e).slice(0, 200)))
page.on('response', (r) => {
  if (r.status() >= 400) console.log('HTTP', r.status(), r.url())
})
page.on('console', (m) => {
  if (m.type() === 'error') console.log('CONSOLE_ERR', m.text().slice(0, 200))
})

await page.goto(BASE + '/', { waitUntil: 'domcontentloaded' })
await page.waitForTimeout(2500)

let failures = 0
const check = (ok, label, detail) => {
  console.log(`${ok ? '✅' : '❌'} ${label}${detail ? ' — ' + detail : ''}`)
  if (!ok) failures++
}

// ---------- 用例 1：纯回答轮（长一点，便于观察渐进） ----------
{
  const q = '请分四点介绍你自己，每点两句话，总共不少于 150 字。'
  console.log('\n===== 用例 1：纯回答轮 =====')
  const { samples, finalText } = await askAndSample(page, q)
  const finalLen = finalText.length
  const mid = samples.filter((s) => s.len > 0 && s.len < finalLen)
  const firstNonZero = samples.find((s) => s.len > 0)
  const lastSample = samples[samples.length - 1]
  console.log('采样序列(前 10):', samples.slice(0, 10).map((s) => `${s.t}ms:${s.len}`).join(' '))
  console.log(`采样点数=${samples.length} 首字=${firstNonZero?.t}ms 结束=${lastSample?.t}ms`)
  check(mid.length >= 3, '正文渐进出现（不是整段跳变）', `中间态样本 ${mid.length} 个`)
  check(
    firstNonZero && lastSample && lastSample.t - firstNonZero.t > 500,
    '首字明显早于结束',
    `间隔 ${lastSample && firstNonZero ? lastSample.t - firstNonZero.t : '?'}ms`,
  )

  const persisted = await persistedAnswer(page.request, q)
  const ratio = persisted.text.length ? finalLen / persisted.text.length : 0
  console.log(`前端渲染 ${finalLen} 字 / 后端持久化 ${persisted.text.length} 字 = ${ratio.toFixed(2)}`)
  check(ratio > 0.5 && ratio < 1.4, '未重复显示（增量文本与全文 token 未叠加）', `比例 ${ratio.toFixed(2)}`)
}

// ---------- 用例 2：工具轮（先说话再调工具 → 过渡文本应被撤回） ----------
{
  const q = '先用一句话说明你的思路，然后调用 list_dir 查看工作目录，最后告诉我目录里有多少个条目。'
  console.log('\n===== 用例 2：工具轮（过渡文本撤回） =====')
  const { samples, finalText } = await askAndSample(page, q)
  console.log('采样序列:', samples.map((s) => `${s.t}ms:${s.len}`).join(' '))
  // 撤回的签名：中途长度回落，且之后又继续增长。轮内 content 只增不减，
  // 唯一会让长度变短的机制就是撤回；"之后继续增长"用来排除收尾时
  // Markdown 重渲染（去掉 ** 等标记）造成的缩短。
  const dropIdx = samples.findIndex(
    (s, i) =>
      i > 0 &&
      samples[i - 1].len > 0 &&
      s.len < samples[i - 1].len &&
      samples.slice(i + 1).some((later) => later.len > s.len + 10),
  )
  const detail =
    dropIdx > 0
      ? `${samples[dropIdx - 1].len} → ${samples[dropIdx].len} 字（${samples[dropIdx].t}ms）`
      : '未观察到中途回落'
  check(dropIdx > 0, '工具轮过渡文本被撤回', detail)
  check(finalText.length > 50, '工具轮后仍给出正文回答', `${finalText.length} 字`)
}

// ---------- 用例 3：停止生成（撤回本轮未完成正文，与后端"不落半截回答"一致） ----------
{
  const q = '请写一篇 300 字以上的短文，主题是"秋天"。'
  console.log('\n===== 用例 3：停止生成 =====')
  const textarea = page.locator('textarea').first()
  await textarea.fill(q)
  await page.keyboard.press('Enter')

  let grew = 0
  for (let i = 0; i < 300; i++) {
    await page.waitForTimeout(100)
    grew = (await page.evaluate(lastAssistantText)).length
    if (grew > 30) break
  }
  check(grew > 30, '停止前已有增量正文', `${grew} 字`)
  await page.locator('.send-btn.stop-btn').click()
  await page.waitForTimeout(1500)
  const after = await page.evaluate(lastAssistantText)
  console.log('停止后正文:', JSON.stringify(after.slice(0, 60)))
  check(
    after.includes('已停止生成') || after.length < 5,
    '停止后未完成的正文被撤回',
    `${after.length} 字`,
  )
}

const shotPath = fileURLToPath(new URL('../scratch/check_outputs/ui_stream_check.png', import.meta.url))
mkdirSync(new URL('../scratch/check_outputs/', import.meta.url), { recursive: true })
await page.screenshot({ path: shotPath, fullPage: true })
console.log(`\n结果：${failures === 0 ? '全部通过' : failures + ' 项未通过'}`)
await b.close()
process.exit(failures === 0 ? 0 : 1)
