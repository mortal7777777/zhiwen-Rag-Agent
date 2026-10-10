/**
 * 设置表单字段一致性守门（B 拆分后新增，2026-10-09）。
 *
 * 四页共用表单（useSettingsForm.js）里同一个字段出现在三处：初始值、后端快照
 * 映射（applyEditable）、保存载荷（saveToolsSettings）。漏掉任何一处都是
 * "设置项静默不可改/保存丢失"类事故，且没有运行时报错。本脚本静态比对：
 *   ① 三处键集合必须完全相等；
 *   ② 键集合 ⊆ 后端 EDITABLE_KEYS（_EDITABLE_LEGACY ∪ _SPECS，
 *      见 backend/app/runtime_config.py——后端键集合另有快照测试守着）。
 *
 * 数据源是纯文本解析（无需后端/网络），可进 CI 与 pre-commit。
 */
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'

const SRC = fileURLToPath(new URL('../src/composables/useSettingsForm.js', import.meta.url))
const BACKEND = fileURLToPath(new URL('../../backend/app/runtime_config.py', import.meta.url))

let failures = 0
function check(name, cond, extra = '') {
  if (cond) {
    console.log(`  ok   ${name}`)
  } else {
    failures += 1
    console.log(`  FAIL ${name}${extra ? ` — ${extra}` : ''}`)
  }
}

const src = readFileSync(SRC, 'utf8')

function keysOf(block) {
  return new Set([...block.matchAll(/^\s*([a-z0-9_]+)\s*:/gm)].map((m) => m[1]))
}

function block(re, label) {
  const m = src.match(re)
  if (!m) {
    console.log(`  FAIL 未能在 useSettingsForm.js 找到 ${label} 块`)
    failures += 1
    return ''
  }
  return m[1]
}

const initKeys = keysOf(block(/const toolsForm = ref\(\{([\s\S]*?)\n  \}\)/, 'toolsForm 初始值'))
const applyKeys = keysOf(
  block(/function applyEditable\(editable\) \{([\s\S]*?)\n  \}/, 'applyEditable 映射'),
)
const saveKeys = keysOf(
  block(/await saveSettings\(\{([\s\S]*?)\n      \}\)/, 'saveToolsSettings 载荷'),
)

const sort = (s) => [...s].sort()
const eq = (a, b) => JSON.stringify(sort(a)) === JSON.stringify(sort(b))

check('三处键集合存在且非空', initKeys.size > 0 && applyKeys.size > 0 && saveKeys.size > 0)
check('初始值 == applyEditable', eq(initKeys, applyKeys), diff(initKeys, applyKeys))
check('初始值 == 保存载荷', eq(initKeys, saveKeys), diff(initKeys, saveKeys))
if (failures === 0) {
  console.log(`  ok   表单字段数 = ${initKeys.size}`)
}

// ---- 后端键集合（_EDITABLE_LEGACY ∪ _SPECS）----
const py = readFileSync(BACKEND, 'utf8')
const legacyBlock = py.match(/_EDITABLE_LEGACY = \{([\s\S]*?)\n\}/)
const backendKeys = new Set([
  ...[...py.matchAll(/"([a-z0-9_]+)"\s*:\s*FieldSpec\(/g)].map((m) => m[1]),
  ...(legacyBlock ? [...legacyBlock[1].matchAll(/"([a-z0-9_]+)"/g)].map((m) => m[1]) : []),
])
check('后端键集合可解析（>30）', backendKeys.size > 30, `got ${backendKeys.size}`)

const orphan = sort(initKeys).filter((k) => !backendKeys.has(k))
check('表单字段 ⊆ 后端 EDITABLE_KEYS', orphan.length === 0, orphan.join(', '))

function diff(a, b) {
  const onlyA = sort(a).filter((k) => !b.has(k))
  const onlyB = sort(b).filter((k) => !a.has(k))
  return `仅左: ${onlyA.join(',')}；仅右: ${onlyB.join(',')}`
}

if (failures) {
  console.error(`设置表单字段一致性检查失败 ${failures} 项`)
  process.exit(1)
}
console.log('全部通过：设置表单三处键集合一致，且都在后端 EDITABLE_KEYS 内。')
