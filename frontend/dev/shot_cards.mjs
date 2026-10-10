/**
 * 卡片组件的视觉验证：Playwright 截 /dev/preview.html（浅色 + 深色）。
 *
 * 用法（需先 npm run dev；BASE 换端口）：
 *     node dev/shot_cards.mjs
 *     BASE=http://localhost:5173 node dev/shot_cards.mjs
 *
 * 截图写到 ../scratch/check_outputs/frontend_shots/（已 gitignore）。
 * 除了截图，还会在浏览器里断言**计算样式**：卡片从 4000 行视图搬出来后，
 * 最大的风险是 scoped 样式没跟着走（父组件规则不会作用到子组件内部）——
 * 断言 `.plan-approval-*`（组件自有样式）、`.plan-step*`（父组件 :deep 穿透）与
 * `.permission-*` / `.perm-*`（PermissionCard 自有样式，含复用的挂起条）
 * 的关键属性，把"看着没问题"变成可回归的检查。
 *
 * 同时收集页面/控制台错误——有错误即退出码 1（"渲染没崩"是视觉验证的一部分）。
 */
import { mkdirSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { chromium } from 'playwright'

const BASE = process.env.BASE || 'http://localhost:5173'
const HERE = dirname(fileURLToPath(import.meta.url))
const OUT = resolve(HERE, '../../scratch/check_outputs/frontend_shots')
mkdirSync(OUT, { recursive: true })

async function launch() {
  for (const channel of ['chrome', 'msedge', undefined]) {
    try {
      return await chromium.launch(channel ? { channel } : {})
    } catch (err) {
      console.log('launch failed', channel || 'bundled', String(err).slice(0, 60))
    }
  }
  throw new Error('no browser available')
}

let errors = 0
let lightBoxBg = null
let darkBoxBg = null
let lightBorder = null
let darkBorder = null
let lightPermBg = null
let darkPermBg = null
let lightThinkBg = null
let darkThinkBg = null
let lightSourcesBg = null
let darkSourcesBg = null
let lightPlanBg = null
let darkPlanBg = null
let lightPanelBg = null
let darkPanelBg = null
const failures = []
function check(name, cond, extra = '') {
  if (cond) {
    console.log(`  ok   ${name}`)
  } else {
    failures.push(name)
    console.log(`  FAIL ${name}${extra ? ` — ${extra}` : ''}`)
  }
}

const browser = await launch()
// 视口必须 > 1024：SessionList 的侧栏抽屉媒体查询在 ≤1024px 生效会让预览里的侧栏变 fixed
const ctx = await browser.newContext({ viewport: { width: 1280, height: 1100 } })
const page = await ctx.newPage()
page.on('pageerror', (err) => {
  errors += 1
  console.log('PAGEERROR', String(err).slice(0, 200))
})
page.on('console', (m) => {
  if (m.type() === 'error') {
    const text = m.text()
    // "Failed to load resource" 类消息不带 URL（URL 在 m.location()），
    // 真正的资源失败由下面的 response 钩子判定（并过滤 favicon）——
    // 这里只统计 JS 层面的真实报错，避免同一件事被计两次。
    if (text.includes('Failed to load resource')) return
    errors += 1
    console.log('CONSOLE-ERR', text.slice(0, 200))
  }
})
page.on('response', (res) => {
  if (res.status() >= 400 && !res.url().includes('favicon')) {
    errors += 1
    console.log('HTTP-ERR', res.status(), res.url())
  }
})

for (const theme of ['light', 'dark']) {
  await page.goto(`${BASE}/dev/preview.html?theme=${theme}`, { waitUntil: 'networkidle' })
  await page.waitForSelector('.plan-approval-box', { timeout: 15000 })
  await page.waitForSelector('.permission-card.pending', { timeout: 15000 })
  await page.waitForSelector('.tool-trace', { timeout: 15000 })
  await page.waitForSelector('.sources', { timeout: 15000 })
  await page.waitForSelector('.plan-box', { timeout: 15000 })
  await page.waitForSelector('.session-panel', { timeout: 15000 })
  const planBoxes = await page.locator('.plan-box').count()
  const sessionItems = await page.locator('.session-item').count()
  const cards = await page.locator('.plan-approval-box').count()
  const steps = await page.locator('.plan-step').count()
  const buttons = await page.locator('.plan-approval-actions button').count()
  const permCards = await page.locator('.permission-card.pending').count()
  const chips = await page.locator('.permission-resolved-chip').count()
  const thinkBlocks = await page.locator('.think-block').count()
  const toolChips = await page.locator('.tool-chip').count()
  const sourceItems = await page.locator('.source-item').count()
  console.log(
    `${theme}: 卡片 ${cards} / 步骤 ${steps} / 按钮 ${buttons} / 审批卡 ${permCards} / chip ${chips} / 思考段 ${thinkBlocks} / 工具chip ${toolChips} / 来源 ${sourceItems} / 计划盒 ${planBoxes} / 会话项 ${sessionItems}`,
  )

  // ---- 计算样式断言（组件自有样式 + 父组件 :deep 穿透）----
  const styles = await page.evaluate(() => {
    const pick = (sel, props) => {
      const el = document.querySelector(sel)
      if (!el) return null
      const cs = getComputedStyle(el)
      return Object.fromEntries(props.map((p) => [p, cs.getPropertyValue(p).trim()]))
    }
    return {
      box: pick('.plan-approval-box', ['border-radius', 'border-top-color', 'background-color', 'padding']),
      head: pick('.plan-head', ['display', 'color', 'font-weight', 'margin-bottom']),
      actions: pick('.plan-approval-actions', ['display', 'gap']),
      hint: pick('.plan-approval-hint', ['font-size']),
      // 步骤清单已抽成 PlanSteps 组件（样式随组件走）：若样式丢失，
      // li 会退回浏览器默认的 list-item、图标 span 会丢掉圆形尺寸
      step: pick('.plan-step', ['display', 'gap', 'list-style-type']),
      stepIcon: pick('.plan-step-icon', ['width', 'height', 'border-radius']),
      input: pick('.plan-approval-input textarea', ['font-size', 'line-height']),
      // PermissionCard（从 ChatView 抽出）：样式必须随组件走，否则退回按钮/块级默认值
      permBox: pick('.permission-box', ['display', 'gap', 'margin-top']),
      permCard: pick('.permission-card.pending', ['border-radius', 'overflow', 'background-color']),
      permHead: pick('.permission-head', ['display', 'font-weight', 'border-bottom-width']),
      permOpts: pick('.permission-opts', ['display', 'gap']),
      permOpt: pick('.perm-opt', ['display', 'cursor']),
      permNum: pick('.perm-opt .perm-num', ['width', 'height']),
      permNumBase: pick('.perm-opt.primary .perm-num', ['background-color']),
      permNumActive: pick('.perm-opt.active .perm-num', ['background-color']),
      permDel: pick('.perm-diff-line.del', ['background-color']),
      permAdd: pick('.perm-diff-line.add', ['background-color']),
      permChip: pick('.permission-resolved-chip', ['display', 'gap']),
      permBar: pick('.suspension-bar', ['display', 'justify-content', 'border-top-style']),
      // ToolTraceList（从 ChatView 抽出）：思考链 + 工具 chip 自有样式
      trace: pick('.tool-trace', ['display', 'flex-wrap', 'gap', 'margin-top']),
      thinkHead: pick('.think-head', ['display', 'cursor', 'user-select']),
      thinkBody: pick('.think-body', ['border-left-width', 'font-size', 'background-color']),
      thinkLive: pick('.think-body.live', ['max-height', 'overflow-y']),
      toolChip: pick('.tool-chip', ['display', 'padding']),
      toolDetail: pick('.tool-detail', ['border-top-style', 'max-width']),
      toolSummary: pick('.tool-summary', ['overflow', 'text-overflow', 'white-space', 'max-width']),
      toolExpand: pick('.tool-expand-icon .open', ['transform']),
      // SourceList（从 ChatView 抽出）：来源盒与条目自有样式
      sourcesBox: pick('.sources', ['border-radius', 'overflow', 'margin-top', 'background-color']),
      sourcesHead: pick('.sources-head', ['display', 'cursor', 'padding']),
      sourcesArrow: pick('.sources-head .arrow.open', ['transform']),
      sourceItem: pick('.source-item', ['border-top-style', 'padding']),
      sourceNo: pick('.source-no', ['width', 'height']),
      sourceTag: pick('.source-tag', ['background-color']),
      sourceTagWeb: pick('.source-tag.web', ['background-color']),
      sourceTitle: pick('.source-title', ['overflow', 'text-overflow', 'white-space']),
      sourceLink: pick('.source-link', ['display', 'margin-top', 'text-decoration-line']),
      sourceContent: pick('.source-content', ['max-height', 'overflow-y']),
      // TodoPanel（从 ChatView 抽出）：计划盒/清单自有样式；
      // .plan-head 是从上一窗口拆分里补回的回归项（头部 flex/主色/下距）
      planBox: pick('.plan-box', ['margin-bottom', 'padding', 'border-radius', 'background-color']),
      planHead: pick('.plan-head', ['display', 'gap', 'font-weight', 'margin-bottom']),
      planProgress: pick('.plan-progress', ['margin-left', 'border-radius']),
      planTodos: pick('.plan-todos', ['display', 'flex-direction', 'gap']),
      planTodo: pick('.plan-todo:not(.done)', ['display', 'gap', 'padding', 'color']),
      planTodoDone: pick('.plan-todo.done', ['text-decoration-line', 'color']),
      planTodoBox: pick('.plan-todo-box', ['width', 'height', 'border-top-style', 'border-top-width']),
      planTodoText: pick('.plan-todo-text', ['line-height']),
      // SessionList（从 ChatView 抽出）：侧栏自有样式（含随搬的媒体查询，须视口>1024）
      spPanel: pick('.session-panel', ['display', 'flex-direction', 'width', 'overflow', 'background-color']),
      spCollapsed: pick('.session-panel.collapsed', ['width']),
      spCollapsedHead: pick('.session-panel.collapsed .session-head', ['display', 'justify-content']),
      spNewBtn: pick('.new-chat-btn', ['border-radius', 'background-image']),
      spItem: pick('.session-item', ['display', 'padding', 'cursor', 'background-color']),
      spItemActive: pick('.session-item.active', ['background-color']),
      spTitle: pick('.session-item.active .session-title', ['font-weight', 'overflow', 'text-overflow']),
      spCount: pick('.session-count', ['border-radius', 'font-size']),
      spActions: pick('.session-actions', ['display']),
      spToggle: pick('.panel-toggle', ['display', 'height', 'cursor']),
      tokenFontSm: getComputedStyle(document.documentElement).getPropertyValue('--font-sm').trim(),
      tokenPrimary: getComputedStyle(document.documentElement).getPropertyValue('--primary').trim(),
    }
  })
  const tag = `[${theme}]`
  check(`${tag} 卡片圆角=12px`, styles.box?.['border-radius'] === '12px', styles.box?.['border-radius'])
  if (theme === 'light') {
    check(`${tag} 卡片边框=浅色主题主色`, styles.box?.['border-top-color'] === 'rgb(22, 119, 255)', styles.box?.['border-top-color'])
  } else {
    darkBorder = styles.box?.['border-top-color']
    check('[dark] 边框色随主题变化', darkBorder && darkBorder !== lightBorder, `${lightBorder} vs ${darkBorder}`)
  }
  check(`${tag} 卡片 padding=10/14`, styles.box?.padding === '10px 14px', styles.box?.padding)
  check(`${tag} 卡头 flex + 加粗`, styles.head?.display === 'flex' && styles.head?.['font-weight'] === '600', JSON.stringify(styles.head))
  check(`${tag} 按钮行 flex + gap 8`, styles.actions?.display === 'flex' && styles.actions?.gap === '8px', JSON.stringify(styles.actions))
  check(`${tag} 提示行字号=12px`, styles.hint?.['font-size'] === '12px', styles.hint?.['font-size'])
  check(`${tag} PlanSteps 自有样式：li 是 flex（非默认 list-item）`, styles.step?.display === 'flex' && styles.step?.['list-style-type'] === 'none', JSON.stringify(styles.step))
  check(`${tag} PlanSteps 自有样式：序号圆标 18px 圆形`, styles.stepIcon?.width === '18px' && styles.stepIcon?.['border-radius'] === '50%', JSON.stringify(styles.stepIcon))
  check(`${tag} 输入框字号=--font-sm`, styles.input?.['font-size'] === styles.tokenFontSm, `${styles.input?.['font-size']} vs ${styles.tokenFontSm}`)
  // ---- PermissionCard：审批卡（组件自有样式 + 挂起条复制样式）----
  check(`${tag} 审批卡：盒 flex + gap 8`, styles.permBox?.display === 'flex' && styles.permBox?.gap === '8px', JSON.stringify(styles.permBox))
  check(`${tag} 审批卡：卡片圆角=12px + overflow hidden`, styles.permCard?.['border-radius'] === '12px' && styles.permCard?.overflow === 'hidden', JSON.stringify(styles.permCard))
  check(`${tag} 审批卡：卡头 flex + 600 + 1px 下边框`, styles.permHead?.display === 'flex' && styles.permHead?.['font-weight'] === '600' && styles.permHead?.['border-bottom-width'] === '1px', JSON.stringify(styles.permHead))
  check(`${tag} 审批卡：选项行 flex + gap 6`, styles.permOpts?.display === 'flex' && styles.permOpts?.gap === '6px', JSON.stringify(styles.permOpts))
  // .perm-opt 是 flex 行（.permission-opts）的子项：inline-flex 按规范块化为 flex；
  // 样式丢失时会退回按钮默认的 block/inline-block + cursor:default，仍能被这条抓住
  check(`${tag} 审批卡：选项按钮 flex 块化 + 手型`, styles.permOpt?.display === 'flex' && styles.permOpt?.cursor === 'pointer', JSON.stringify(styles.permOpt))
  check(`${tag} 审批卡：序号圆标 16px`, styles.permNum?.width === '16px' && styles.permNum?.height === '16px', JSON.stringify(styles.permNum))
  check(`${tag} 审批卡：active 序号底色与常态不同`, !!styles.permNumActive?.['background-color'] && styles.permNumActive['background-color'] !== styles.permNumBase?.['background-color'], `${styles.permNumBase?.['background-color']} vs ${styles.permNumActive?.['background-color']}`)
  check(`${tag} 审批卡：diff 红删 / 绿增底色可区分且非透明`, !!styles.permDel?.['background-color'] && styles.permDel['background-color'] !== 'rgba(0, 0, 0, 0)' && styles.permDel['background-color'] !== styles.permAdd?.['background-color'], `${styles.permDel?.['background-color']} vs ${styles.permAdd?.['background-color']}`)
  check(`${tag} 审批卡：已决 chip flex + gap 6`, styles.permChip?.display === 'flex' && styles.permChip?.gap === '6px', JSON.stringify(styles.permChip))
  check(`${tag} 审批卡：挂起条 flex + 两端对齐 + 虚线`, styles.permBar?.display === 'flex' && styles.permBar?.['justify-content'] === 'space-between' && styles.permBar?.['border-top-style'] === 'dashed', JSON.stringify(styles.permBar))
  // ---- ToolTraceList：思考链与工具轨迹（组件自有样式）----
  check(`${tag} 轨迹：flex + wrap + gap 6 + 顶距 8`, styles.trace?.display === 'flex' && styles.trace?.['flex-wrap'] === 'wrap' && styles.trace?.gap === '6px' && styles.trace?.['margin-top'] === '8px', JSON.stringify(styles.trace))
  check(`${tag} 轨迹：思考头 flex + 可点击 + 禁选中`, styles.thinkHead?.display === 'flex' && styles.thinkHead?.cursor === 'pointer' && styles.thinkHead?.['user-select'] === 'none', JSON.stringify(styles.thinkHead))
  check(`${tag} 轨迹：思考正文左边框 2px + 12.5px 字号`, styles.thinkBody?.['border-left-width'] === '2px' && styles.thinkBody?.['font-size'] === '12.5px', JSON.stringify(styles.thinkBody))
  check(`${tag} 轨迹：进行中思考限高 132 + 可滚动`, styles.thinkLive?.['max-height'] === '132px' && styles.thinkLive?.['overflow-y'] === 'auto', JSON.stringify(styles.thinkLive))
  // .tool-chip 是 flex 列（.tool-item）的子项：inline-flex 按规范块化为 flex
  check(`${tag} 轨迹：工具 chip flex 块化 + padding 4/10`, styles.toolChip?.display === 'flex' && styles.toolChip?.padding === '4px 10px', JSON.stringify(styles.toolChip))
  check(`${tag} 轨迹：detail 虚线框 + 限宽 560`, styles.toolDetail?.['border-top-style'] === 'dashed' && styles.toolDetail?.['max-width'] === '560px', JSON.stringify(styles.toolDetail))
  check(`${tag} 轨迹：summary 省略号三件套 + 限宽 320`, styles.toolSummary?.overflow === 'hidden' && styles.toolSummary?.['text-overflow'] === 'ellipsis' && styles.toolSummary?.['white-space'] === 'nowrap' && styles.toolSummary?.['max-width'] === '320px', JSON.stringify(styles.toolSummary))
  check(`${tag} 轨迹：展开箭头旋转 180°`, !!styles.toolExpand?.transform?.startsWith('matrix(-1'), styles.toolExpand?.transform)
  // ---- SourceList：引用来源（组件自有样式）----
  check(`${tag} 来源：盒圆角=12 + overflow hidden + 顶距 8`, styles.sourcesBox?.['border-radius'] === '12px' && styles.sourcesBox?.overflow === 'hidden' && styles.sourcesBox?.['margin-top'] === '8px', JSON.stringify(styles.sourcesBox))
  check(`${tag} 来源：头 flex + 可点击 + padding 9/14`, styles.sourcesHead?.display === 'flex' && styles.sourcesHead?.cursor === 'pointer' && styles.sourcesHead?.padding === '9px 14px', JSON.stringify(styles.sourcesHead))
  check(`${tag} 来源：展开态箭头旋转 180°`, !!styles.sourcesArrow?.transform?.startsWith('matrix(-1'), styles.sourcesArrow?.transform)
  check(`${tag} 来源：条目上虚线分隔 + padding 10/0`, styles.sourceItem?.['border-top-style'] === 'dashed' && styles.sourceItem?.padding === '10px 0px', JSON.stringify(styles.sourceItem))
  check(`${tag} 来源：序号圆标 20px`, styles.sourceNo?.width === '20px' && styles.sourceNo?.height === '20px', JSON.stringify(styles.sourceNo))
  check(`${tag} 来源：联网 tag 底色与知识库 tag 不同`, !!styles.sourceTagWeb?.['background-color'] && styles.sourceTagWeb['background-color'] !== styles.sourceTag?.['background-color'], `${styles.sourceTag?.['background-color']} vs ${styles.sourceTagWeb?.['background-color']}`)
  check(`${tag} 来源：标题省略号三件套`, styles.sourceTitle?.overflow === 'hidden' && styles.sourceTitle?.['text-overflow'] === 'ellipsis' && styles.sourceTitle?.['white-space'] === 'nowrap', JSON.stringify(styles.sourceTitle))
  check(`${tag} 来源：正文限高 130 + 可滚动`, styles.sourceContent?.['max-height'] === '130px' && styles.sourceContent?.['overflow-y'] === 'auto', JSON.stringify(styles.sourceContent))
  // ---- TodoPanel：执行计划盒 / 任务清单（组件自有样式）----
  check(`${tag} 计划盒：padding 10/14 + 圆角 12 + 底距 8`, styles.planBox?.padding === '10px 14px' && styles.planBox?.['border-radius'] === '12px' && styles.planBox?.['margin-bottom'] === '8px', JSON.stringify(styles.planBox))
  check(`${tag} 计划盒：头部 flex + 600 + 下距 6（回归恢复项）`, styles.planHead?.display === 'flex' && styles.planHead?.gap === '6px' && styles.planHead?.['font-weight'] === '600' && styles.planHead?.['margin-bottom'] === '6px', JSON.stringify(styles.planHead))
  // margin-left:auto 的计算值会被解析为 px（布局后的 used value）；样式丢失时会退回 0px
  check(`${tag} 计划盒：进度胶囊顶到行尾 + 圆角胶囊`, parseFloat(styles.planProgress?.['margin-left']) > 0 && styles.planProgress?.['border-radius'] === '999px', JSON.stringify(styles.planProgress))
  check(`${tag} 清单：列向 + gap 2`, styles.planTodos?.display === 'flex' && styles.planTodos?.['flex-direction'] === 'column' && styles.planTodos?.gap === '2px', JSON.stringify(styles.planTodos))
  check(`${tag} 清单：条目 flex + padding 5/8`, styles.planTodo?.display === 'flex' && styles.planTodo?.gap === '7px' && styles.planTodo?.padding === '5px 8px', JSON.stringify(styles.planTodo))
  check(`${tag} 清单：已完成删除线 + 变灰`, (styles.planTodoDone?.['text-decoration-line'] || '').includes('line-through') && styles.planTodoDone?.color !== styles.planTodo?.color, `${styles.planTodo?.color} vs ${styles.planTodoDone?.color}`)
  // 1.5px 边框的计算值会被取整（1px/2px），这里用 style + 非零宽判断样式是否在
  check(`${tag} 清单：未完成方框 13px + 有描边`, styles.planTodoBox?.width === '13px' && styles.planTodoBox?.height === '13px' && styles.planTodoBox?.['border-top-style'] === 'solid' && styles.planTodoBox?.['border-top-width'] !== '0px', JSON.stringify(styles.planTodoBox))
  check(`${tag} 清单：正文行高 19.5px（1.5×13）`, styles.planTodoText?.['line-height'] === '19.5px', styles.planTodoText?.['line-height'])
  // ---- SessionList：会话侧栏（组件自有样式 + 随搬的响应式）----
  // 预览视口 1280 ≤ 1440 断点：随组件搬来的媒体查询把宽度降为 208（248 是 >1440 的值）
  check(`${tag} 侧栏：列向 flex + 宽 208（1440 断点生效）+ overflow hidden`, styles.spPanel?.display === 'flex' && styles.spPanel?.['flex-direction'] === 'column' && styles.spPanel?.width === '208px' && styles.spPanel?.overflow === 'hidden', JSON.stringify(styles.spPanel))
  check(`${tag} 侧栏：折叠态宽 56`, styles.spCollapsed?.width === '56px', styles.spCollapsed?.width)
  check(`${tag} 侧栏：折叠头居中`, styles.spCollapsedHead?.display === 'flex' && styles.spCollapsedHead?.['justify-content'] === 'center', JSON.stringify(styles.spCollapsedHead))
  check(`${tag} 侧栏：新建按钮圆角 12 + 渐变底`, styles.spNewBtn?.['border-radius'] === '12px' && (styles.spNewBtn?.['background-image'] || '').includes('linear-gradient'), JSON.stringify(styles.spNewBtn))
  check(`${tag} 侧栏：条目 flex + padding 8/10 + 手型`, styles.spItem?.display === 'flex' && styles.spItem?.padding === '8px 10px' && styles.spItem?.cursor === 'pointer', JSON.stringify(styles.spItem))
  check(`${tag} 侧栏：active 条目有底色且不同于普通项`, !!styles.spItemActive?.['background-color'] && styles.spItemActive['background-color'] !== styles.spItem?.['background-color'], `${styles.spItem?.['background-color']} vs ${styles.spItemActive?.['background-color']}`)
  check(`${tag} 侧栏：active 标题加粗 + 省略号`, styles.spTitle?.['font-weight'] === '600' && styles.spTitle?.overflow === 'hidden' && styles.spTitle?.['text-overflow'] === 'ellipsis', JSON.stringify(styles.spTitle))
  check(`${tag} 侧栏：消息数角标胶囊 + 11px`, styles.spCount?.['border-radius'] === '999px' && styles.spCount?.['font-size'] === '11px', JSON.stringify(styles.spCount))
  // 操作按钮默认隐藏、悬停才显示（display:none 是行为契约）
  check(`${tag} 侧栏：条目操作默认隐藏`, styles.spActions?.display === 'none', styles.spActions?.display)
  check(`${tag} 侧栏：折叠开关 flex + 高 30 + 手型`, styles.spToggle?.display === 'flex' && styles.spToggle?.height === '30px' && styles.spToggle?.cursor === 'pointer', JSON.stringify(styles.spToggle))
  if (theme === 'dark') {
    darkBoxBg = styles.box?.['background-color']
    check('[dark] 主题令牌生效（卡片底色变化）', darkBoxBg !== null && darkBoxBg !== lightBoxBg, `${lightBoxBg} vs ${darkBoxBg}`)
    darkPermBg = styles.permCard?.['background-color']
    check('[dark] 审批卡底色随主题变化', darkPermBg !== null && darkPermBg !== lightPermBg, `${lightPermBg} vs ${darkPermBg}`)
    darkThinkBg = styles.thinkBody?.['background-color']
    check('[dark] 轨迹：思考正文底色随主题变化', darkThinkBg !== null && darkThinkBg !== lightThinkBg, `${lightThinkBg} vs ${darkThinkBg}`)
    darkSourcesBg = styles.sourcesBox?.['background-color']
    check('[dark] 来源：盒底色随主题变化', darkSourcesBg !== null && darkSourcesBg !== lightSourcesBg, `${lightSourcesBg} vs ${darkSourcesBg}`)
    darkPlanBg = styles.planBox?.['background-color']
    check('[dark] 计划盒：底色随主题变化', darkPlanBg !== null && darkPlanBg !== lightPlanBg, `${lightPlanBg} vs ${darkPlanBg}`)
    darkPanelBg = styles.spPanel?.['background-color']
    check('[dark] 侧栏：底色随主题变化', darkPanelBg !== null && darkPanelBg !== lightPanelBg, `${lightPanelBg} vs ${darkPanelBg}`)
  } else {
    lightBoxBg = styles.box?.['background-color']
    lightBorder = styles.box?.['border-top-color']
    lightPermBg = styles.permCard?.['background-color']
    lightThinkBg = styles.thinkBody?.['background-color']
    lightSourcesBg = styles.sourcesBox?.['background-color']
    lightPlanBg = styles.planBox?.['background-color']
    lightPanelBg = styles.spPanel?.['background-color']
  }
  await page.screenshot({
    path: resolve(OUT, `plan_card_${theme}.png`),
    fullPage: true,
  })
  await page.locator('.permission-box').first().screenshot({
    path: resolve(OUT, `perm_card_${theme}.png`),
  })
  await page.locator('.tool-trace').first().screenshot({
    path: resolve(OUT, `tool_trace_${theme}.png`),
  })
  await page.locator('.sources').first().screenshot({
    path: resolve(OUT, `sources_${theme}.png`),
  })
  await page.locator('.shot-todo-panel').first().screenshot({
    path: resolve(OUT, `todo_panel_${theme}.png`),
  })
  await page.locator('.shot-session-list').first().screenshot({
    path: resolve(OUT, `session_list_${theme}.png`),
  })
}

await browser.close()
console.log(`截图输出目录：${OUT}`)
if (failures.length) {
  console.error(`计算样式断言失败 ${failures.length} 项：${failures.join('、')}`)
  process.exit(1)
}
if (errors) {
  console.error(`页面报错 ${errors} 条，视觉验证不通过`)
  process.exit(1)
}
console.log('视觉验证通过：无页面/控制台错误')
