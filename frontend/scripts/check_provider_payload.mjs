/**
 * 供应商载荷映射守门（src/composables/providerPayload.js）。
 *
 * 背景（2026-10-10 供应商页升级）：此前的"白名单式重建"会让新增字段
 * （api_format 等）保存一次就静默丢失，前后端都不报错。这里把映射规则钉死：
 *   - _ 前缀 UI 私有字段绝不进载荷；
 *   - 未知/新增字段原样保留（展开式映射）；
 *   - 掩码 Key、_modelsText、headers 文本按约定转换。
 * 无浏览器、无后端，Node 直接跑（package.json 的 check:payload）。
 */
import { headersToText, providerToPayload } from '../src/composables/providerPayload.js'

const failures = []
function check(name, cond, extra = '') {
  if (cond) {
    console.log(`  ok   ${name}`)
  } else {
    failures.push(name)
    console.log(`  FAIL ${name}${extra ? ` — ${extra}` : ''}`)
  }
}

console.log('供应商载荷映射检查')

const card = {
  id: 'p_1',
  name: 'x',
  type: 'chat',
  base_url: 'https://a.example.com',
  api_key: '****1234',
  model: 'm1',
  models: ['m1'],
  enabled: true,
  note: '',
  api_format: 'anthropic',
  temperature: 0.2,
  max_tokens: 4096,
  thinking_enabled: true,
  thinking_effort: 'max',
  _keyInput: '',
  _modelsText: 'm1, m2',
  _headersText: 'X-Trace: t\n# 注释忽略\n坏行没有冒号\nX-Sec: ****abcd',
  _testing: true,
  _testResult: null,
  _speedResult: null,
}

const payload = providerToPayload(card)
check(
  '无 _ 前缀字段泄漏',
  Object.keys(payload).every((k) => !k.startsWith('_')),
  Object.keys(payload).join(','),
)
check('api_format 保留（今天刚修过的实害）', payload.api_format === 'anthropic')
check('temperature 保留', payload.temperature === 0.2)
check('max_tokens 保留', payload.max_tokens === 4096)
check('掩码 Key 透传（留给后端按 id 还原）', payload.api_key === '****1234')
check(
  '_keyInput 优先于掩码值',
  providerToPayload({ ...card, _keyInput: 'sk-new' }).api_key === 'sk-new',
)
check(
  'models 从 _modelsText 解析',
  JSON.stringify(payload.models) === JSON.stringify(['m1', 'm2']),
)
check(
  'headers 解析且跳过注释/坏行',
  JSON.stringify(payload.headers) ===
    JSON.stringify({ 'X-Trace': 't', 'X-Sec': '****abcd' }),
  JSON.stringify(payload.headers),
)
check(
  'thinking 字段归一',
  payload.thinking_enabled === true && payload.thinking_effort === 'max',
)
check('temperature=0 不被吞', providerToPayload({ ...card, temperature: 0 }).temperature === 0)

const vision = providerToPayload({ ...card, type: 'vision' })
check(
  'vision 剥掉 chat 专属字段',
  ['api_format', 'temperature', 'max_tokens', 'headers'].every((k) => !(k in vision)),
  Object.keys(vision).join(','),
)
check(
  'vision thinking 强制开启',
  vision.thinking_enabled === true && vision.thinking_effort === 'high',
)

check('headersToText 往返', headersToText({ A: '1', B: '2' }) === 'A: 1\nB: 2')
check('空 headers → 空文本', headersToText(null) === '')
check(
  'parseHeadersText 大小为 0 时无 headers 键',
  !('headers' in providerToPayload({ ...card, _headersText: '  ' })),
)

if (failures.length) {
  console.error(`\n失败 ${failures.length} 项：${failures.join('、')}`)
  process.exit(1)
}
console.log('\n全部通过：供应商载荷映射保字段、剥 UI 私有字段、headers 解析正确。')
