/** 知识库范围选择的纯逻辑（无 Vue / 无 IO，供 composable 与 Node 单测共用）。 */

/**
 * 把范围弹窗里选中的选项转成请求用的 scope。
 * - 0 项 → null（全库）
 * - 1 项 → 保留 doc/book 类型
 * - 多项 → type=docs + 展开后的相对路径（去重）
 */
export function buildScopeFromSelection(selected) {
  if (!selected || !selected.length) return null
  if (selected.length === 1) {
    const opt = selected[0]
    return { type: opt.type, value: opt.value, label: opt.label }
  }
  const paths = []
  for (const opt of selected) {
    for (const path of opt.paths || [opt.value]) {
      if (path && !paths.includes(path)) paths.push(path)
    }
  }
  const labels = selected.map((opt) => opt.label)
  const label =
    labels.length <= 2
      ? labels.join('、')
      : `${labels.slice(0, 2).join('、')} 等 ${labels.length} 个`
  return { type: 'docs', values: paths, label }
}

/** 前端 scope → /agent/stream 的 kb_scope 字段（不传 label）。 */
export function scopePayloadFromScope(scope) {
  if (!scope) return null
  if (scope.type === 'docs') {
    return { type: 'docs', values: [...(scope.values || [])] }
  }
  return { type: scope.type, value: scope.value }
}
