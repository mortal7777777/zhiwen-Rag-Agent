/**
 * useTypewriter 的行为断言（无浏览器、无后端，Node 直接跑）。
 *
 * 用法：npm run check:typewriter
 * 覆盖：节奏推进（积压自适应）、flush 提交、drop 撤回（缓冲优先/正文兜底）、
 *      resetRound、dispose、消息被清空时的兜底——这些正是拆分前埋在 ChatView
 *      里、无法单测的流式状态机。
 */
import { useTypewriter } from '../src/composables/useTypewriter.js'

const failures = []
function check(name, cond, extra = '') {
  if (cond) {
    console.log(`  ok   ${name}`)
  } else {
    failures.push(name)
    console.log(`  FAIL ${name}${extra ? ` — ${extra}` : ''}`)
  }
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

function makeHarness() {
  let msg = { content: '' }
  let scrolls = 0
  const tw = useTypewriter({
    getStreamMsg: () => msg,
    scrollToBottom: () => {
      scrolls += 1
    },
  })
  return {
    tw,
    get msg() {
      return msg
    },
    setMsg: (v) => {
      msg = v
    },
    scrolls: () => scrolls,
  }
}

console.log('useTypewriter 行为检查')

// 1) 节奏推进：积压自适应，单帧不超过 20 字符
{
  const h = makeHarness()
  h.tw.pendingText.value = 'x'.repeat(200)
  h.tw.start()
  await sleep(30)
  const afterOneFrame = h.msg.content.length
  check('单帧推进 1~20 字符', afterOneFrame >= 1 && afterOneFrame <= 20, `实际 ${afterOneFrame}`)
  await sleep(60)
  check('持续推进（积压被消费）', h.msg.content.length > afterOneFrame)
  check('缓冲区被消费', h.tw.pendingText.value.length < 200)
  check('滚动有节流（隔帧）', h.scrolls() >= 1 && h.scrolls() <= h.msg.content.length)
  h.tw.flush()
}

// 2) flush：一次性提交全部缓冲并停表
{
  const h = makeHarness()
  h.tw.pendingText.value = '完整正文'
  h.tw.start()
  h.tw.flush()
  check('flush 提交缓冲', h.msg.content === '完整正文')
  check('flush 清空缓冲', h.tw.pendingText.value === '')
  const snapshot = h.msg.content
  await sleep(60)
  check('flush 后定时器已停', h.msg.content === snapshot)
}

// 3) drop：缓冲优先扣除，正文不动
{
  const h = makeHarness()
  h.msg.content = '已推送'
  h.tw.pendingText.value = '过渡文本'
  h.tw.setStreamedLen(3) // 本轮 stream_delta 累计推送了 3 个字符
  h.tw.drop()
  check('drop 扣缓冲（4-3 剩 1 字）', h.tw.pendingText.value === '过', h.tw.pendingText.value)
  check('drop 不动正文（缓冲够扣）', h.msg.content === '已推送')
  check('drop 归零计数', h.tw.getStreamedLen() === 0)
}

// 4) drop：缓冲不足时从正文尾部兜底扣除
{
  const h = makeHarness()
  h.msg.content = '正文尾部要撤'
  h.tw.pendingText.value = 'ab'
  h.tw.setStreamedLen(4) // 2 个在缓冲、2 个已进正文
  h.tw.drop()
  check('drop 清空缓冲', h.tw.pendingText.value === '')
  check('drop 从正文尾部扣除差额', h.msg.content === '正文尾部', h.msg.content)
}

// 5) drop：没有 streamedLen 时是 no-op（不误删正文）
{
  const h = makeHarness()
  h.msg.content = '正文'
  h.tw.pendingText.value = 'abc'
  h.tw.setStreamedLen(0)
  h.tw.drop()
  check('drop no-op 不动数据', h.msg.content === '正文' && h.tw.pendingText.value === 'abc')
}

// 6) resetRound：清空本轮全部流式状态
{
  const h = makeHarness()
  h.tw.pendingText.value = 'abc'
  h.tw.setStreamedLen(5)
  h.tw.setSkipNextToken(true)
  h.tw.resetRound()
  check(
    'resetRound 清空缓冲/计数/跳过标记',
    h.tw.pendingText.value === '' &&
      h.tw.getStreamedLen() === 0 &&
      h.tw.getSkipNextToken() === false,
  )
}

// 7) dispose：卸载后不再推进
{
  const h = makeHarness()
  h.tw.pendingText.value = 'x'.repeat(100)
  h.tw.start()
  await sleep(30)
  h.tw.dispose()
  const snapshot = h.msg.content
  await sleep(60)
  check('dispose 停表', h.msg.content === snapshot)
}

// 8) 目标消息被清空时：start 不建表、flush 不抛错
{
  const h = makeHarness()
  h.setMsg(null)
  h.tw.pendingText.value = 'abc'
  h.tw.start()
  await sleep(30)
  check('无消息时不推进', h.tw.pendingText.value === 'abc')
  h.tw.flush()
  // 与拆分前的行为一致：目标消息不存在时只停表、不动缓冲
  // （缓冲由下一轮 resetRound 清理，不会串到别的消息里）
  check('无消息时 flush 只停表、不抛错', h.tw.pendingText.value === 'abc')
}

if (failures.length) {
  console.error(`\n失败 ${failures.length} 项：${failures.join('、')}`)
  process.exit(1)
}
console.log('\n全部通过：打字机/增量流状态机符合预期。')
