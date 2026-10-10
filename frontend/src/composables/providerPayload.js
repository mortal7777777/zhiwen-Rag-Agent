/**
 * 供应商表单 ↔ 提交载荷的纯映射（供 useModelSettings 与守门脚本共用，2026-10-10）。
 *
 * 为什么要独立成纯函数：此前载荷是"白名单式重建"，新增字段（api_format 等）
 * 保存一次就被静默丢弃（实害）；这组函数把映射规则集中在一处，并有
 * scripts/check_provider_payload.mjs 单测守着。_ 前缀是卡片 UI 私有字段，
 * 一律不进载荷。
 */

/** 卡片私有字段（不属于供应商持久化结构） */
export const UI_FIELD_PREFIX = '_'

/** 卡片/新表单对象 → PUT /settings 的 providers 元素 */
export function providerToPayload(p) {
  const out = {}
  for (const [key, value] of Object.entries(p)) {
    if (key.startsWith(UI_FIELD_PREFIX)) continue
    out[key] = value
  }
  out.api_key = p._keyInput || p.api_key || ''
  out.models =
    p._modelsText != null
      ? String(p._modelsText)
          .split(/[,，]/)
          .map((m) => m.trim())
          .filter(Boolean)
      : [p.model].filter(Boolean)
  out.note = p.note || ''
  if (p.type === 'chat') {
    out.thinking_enabled = p.thinking_enabled !== false
    out.thinking_effort = p.thinking_effort || 'high'
    const headers = parseHeadersText(p._headersText)
    if (Object.keys(headers).length) out.headers = headers
    else delete out.headers
  } else {
    out.thinking_enabled = true
    out.thinking_effort = 'high'
    delete out.api_format
    delete out.temperature
    delete out.max_tokens
    delete out.headers
  }
  return out
}

/** "Key: Value" 每行一条 → headers 对象（空行/# 注释/无冒号行跳过） */
export function parseHeadersText(text) {
  const out = {}
  for (const line of String(text || '').split(/\r?\n/)) {
    const trimmed = line.trim()
    if (!trimmed || trimmed.startsWith('#')) continue
    const idx = trimmed.indexOf(':')
    if (idx <= 0) continue
    const name = trimmed.slice(0, idx).trim()
    const value = trimmed.slice(idx + 1).trim()
    if (name) out[name] = value
  }
  return out
}

/** headers 对象 → 多行文本（敏感值已被后端以 **** 掩码，原样展示即可） */
export function headersToText(headers) {
  if (!headers || typeof headers !== 'object') return ''
  return Object.entries(headers)
    .map(([name, value]) => `${name}: ${value}`)
    .join('\n')
}
