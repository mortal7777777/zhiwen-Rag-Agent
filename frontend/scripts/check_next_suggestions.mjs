/**
 * 输入框「下一步建议」本地启发式检查（Node，无浏览器/后端）。
 * 锁定：按最后一轮工具轨迹/产物/来源选择不同建议，最多 3 条且不重复。
 */
import { buildLocalNextSuggestions } from '../src/utils/nextSuggestions.js'

const failures = []
function check(name, cond, extra = '') {
  if (cond) console.log(`  ok   ${name}`)
  else {
    failures.push(name)
    console.log(`  FAIL ${name}${extra ? ` — ${extra}` : ''}`)
  }
}

console.log('下一步建议本地启发式检查')

{
  const items = buildLocalNextSuggestions({
    tool_trace: [
      { name: 'write_file', artifacts: [{ path: 'out/a.py', action: 'created' }] },
      { name: 'bash', summary: 'ok' },
    ],
  })
  check('写文件后的建议优先验证与改动', items.includes('运行并验证产物') && items.includes('总结这次改动'))
  check('建议数量不超过 3', items.length === 3, JSON.stringify(items))
}

{
  const items = buildLocalNextSuggestions({
    sources: [{ index: 1, type: 'kb' }],
    tool_trace: [{ name: 'knowledge_base_search' }],
  })
  check('知识库来源后建议展开来源', items.includes('展开讲讲第 1 个来源'))
  check('知识库来源后建议整理表格', items.includes('整理成表格'))
}

{
  const web = buildLocalNextSuggestions({ tool_trace: [{ name: 'web_search' }] })
  check('联网搜索后建议相关资讯', web.includes('还有哪些相关信息？'))

  const bash = buildLocalNextSuggestions({ tool_trace: [{ name: 'bash' }] })
  check('命令执行后建议整理结论', bash.includes('把执行结果整理成结论'))

  const plan = buildLocalNextSuggestions({
    plan: ['第一步'],
    tool_trace: [{ kind: 'thinking', text: 'x' }],
  })
  check('计划/清单后建议继续下一步', plan.includes('继续下一步'))
}

{
  const plain = buildLocalNextSuggestions({ content: '普通回答' })
  check('普通回答使用通用建议', plain.includes('展开说明一下'))
  check('没有轨迹也不重复', new Set(plain).size === plain.length)
  check('空消息也有兜底', buildLocalNextSuggestions(null).length === 3)
}

if (failures.length) {
  console.error(`\n失败 ${failures.length} 项：${failures.join('、')}`)
  process.exit(1)
}
console.log('\n全部通过：下一步建议的本地启发式符合预期。')
