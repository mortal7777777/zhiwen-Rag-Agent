/**
 * 输入框「下一步建议」的本地即时版本（零成本、无网络）。
 *
 * 后端会在 done 后用标题小模型生成更贴合的 3 条并替换；这里保证在请求返回前
 * （或模型不可用/关闭时）也有一组可点的追问，不让建议区空白。
 */
export function buildLocalNextSuggestions(message) {
  const trace = Array.isArray(message?.tool_trace) ? message.tool_trace : []
  const names = new Set(trace.map((item) => item?.name).filter(Boolean))
  const artifacts = trace.flatMap((item) =>
    Array.isArray(item?.artifacts) ? item.artifacts : [],
  )
  const sources = Array.isArray(message?.sources) ? message.sources : []
  const out = []
  const add = (text) => {
    if (text && !out.includes(text) && out.length < 3) out.push(text)
  }

  const wrote =
    artifacts.length > 0 ||
    ['write_file', 'edit_file', 'delete_file'].some((name) => names.has(name))

  if (wrote) {
    add('运行并验证产物')
    add('总结这次改动')
    add('继续修改细节')
  } else if (sources.length || names.has('knowledge_base_search')) {
    add('展开讲讲第 1 个来源')
    add('对比这些来源的结论')
    add('整理成表格')
  } else if (names.has('web_search')) {
    add('还有哪些相关信息？')
    add('把要点整理成清单')
    add('给出你的判断')
  } else if (names.has('bash') || names.has('command_tool')) {
    add('把执行结果整理成结论')
    add('下一步该做什么？')
    add('检查潜在风险')
  } else if (
    (Array.isArray(message?.plan) && message.plan.length) ||
    (Array.isArray(message?.todos) && message.todos.length)
  ) {
    add('继续下一步')
    add('检查未完成项')
    add('总结当前进度')
  } else {
    add('展开说明一下')
    add('举个例子')
    add('换个更简短的说法')
  }
  return out
}
