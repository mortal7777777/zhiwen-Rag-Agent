/** 知识库范围纯逻辑检查（无浏览器/后端，Node 直接跑）。 */
import {
  buildScopeFromSelection,
  scopePayloadFromScope,
} from '../src/utils/kbScope.js'

const failures = []
function check(name, cond, extra = '') {
  if (cond) console.log(`  ok   ${name}`)
  else {
    failures.push(name)
    console.log(`  FAIL ${name}${extra ? ` — ${extra}` : ''}`)
  }
}

console.log('知识库范围纯逻辑检查')

check('空选择 = 全库', buildScopeFromSelection([]) === null)

{
  const scope = buildScopeFromSelection([
    { type: 'doc', value: 'a.md', label: 'a.md', paths: ['a.md'] },
  ])
  check('单文档保留 doc 类型', scope.type === 'doc' && scope.value === 'a.md')
  const payload = scopePayloadFromScope(scope)
  check('单文档 payload', payload.type === 'doc' && payload.value === 'a.md')
}

{
  const scope = buildScopeFromSelection([
    { type: 'book', value: '书', label: '书', paths: ['书.epub', '书.pdf'] },
  ])
  check('整本书保留 book 类型', scope.type === 'book' && scope.value === '书')
}

{
  const scope = buildScopeFromSelection([
    { type: 'doc', value: 'a.md', label: 'a.md', paths: ['a.md'] },
    { type: 'doc', value: 'b.md', label: 'b.md', paths: ['b.md'] },
    { type: 'book', value: '书', label: '书', paths: ['书.epub', '书.pdf'] },
  ])
  check('多选 = docs 类型', scope.type === 'docs')
  check('多选路径去重且展开整本书', scope.values.join(',') === 'a.md,b.md,书.epub,书.pdf')
  check('多选标签带数量', scope.label.includes('等 3 个'), scope.label)
  const payload = scopePayloadFromScope(scope)
  check('多选 payload 带 values', payload.type === 'docs' && payload.values.length === 4)
}

{
  const scope = buildScopeFromSelection([
    { type: 'doc', value: 'a.md', label: 'A', paths: ['a.md'] },
    { type: 'doc', value: 'b.md', label: 'B', paths: ['b.md'] },
  ])
  check('两项标签用顿号连接', scope.label === 'A、B', scope.label)
}

check('空 scope payload 为 null', scopePayloadFromScope(null) === null)

if (failures.length) {
  console.error(`\n失败 ${failures.length} 项：${failures.join('、')}`)
  process.exit(1)
}
console.log('\n全部通过：知识库范围纯逻辑符合预期。')
