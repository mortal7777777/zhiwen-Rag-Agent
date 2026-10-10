/** Agent SSE 传输层：帧解析 + 统一的流式请求封装（三个流式入口共用）。 */


/** 消费一条 Agent SSE 响应体（/agent/stream 与 /agent/resume 事件协议一致）。
 *
 * 导出以便单测（frontend/scripts/check_api_sse.mjs）：帧解析对"半帧"很敏感，
 * 历史上是靠肉眼观测发现的（后端聚合推送 + 分片），现在有测试守着。
 */
export async function consumeAgentStream(response, handlers = {}) {
  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''

  while (true) {
    const { done, value } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })

    // SSE 帧之间用空行分隔，解析完整帧、保留半帧
    const frames = buffer.split('\n\n')
    buffer = frames.pop()
    for (const frame of frames) {
      if (!frame.startsWith('data: ')) continue
      let payload
      try {
        payload = JSON.parse(frame.slice(6))
      } catch {
        continue
      }
      const { event, data } = payload
      if (event === 'session' && handlers.onSession) {
        handlers.onSession(data)
      } else if (event === 'title' && handlers.onTitle) {
        handlers.onTitle(data)
      } else if (event === 'status' && handlers.onStatus) {
        handlers.onStatus(data)
      } else if (event === 'reasoning_delta' && handlers.onReasoningDelta) {
        handlers.onReasoningDelta(data)
      } else if (event === 'plan_progress' && handlers.onPlanProgress) {
        handlers.onPlanProgress(data)
      } else if (event === 'plan' && handlers.onPlan) {
        // 计划步骤（prepare 播种 / 计划确认后重发）：渲染消息卡片里的步骤清单
        handlers.onPlan(data)
      } else if (event === 'vision' && handlers.onVision) {
        handlers.onVision(data)
      } else if (event === 'tool_start' && handlers.onToolStart) {
        handlers.onToolStart(data)
      } else if (event === 'tool_result' && handlers.onToolResult) {
        handlers.onToolResult(data)
      } else if (event === 'run_suspended' && handlers.onSuspended) {
        // 敏感操作挂起：本条流到此结束（后端等待决定），前端收齐决定后走 resume
        handlers.onSuspended(data)
      } else if (event === 'hook' && handlers.onHook) {
        handlers.onHook(data)
      } else if (event === 'todos' && handlers.onTodos) {
        handlers.onTodos(data)
      } else if (event === 'stream_delta' && handlers.onStreamDelta) {
        // 正文增量推送（后端按窗口聚合；工具轮会再发 stream_end 撤回）
        handlers.onStreamDelta(data)
      } else if (event === 'stream_end' && handlers.onStreamEnd) {
        // keep=true：增量文本即最终正文（随后的 token 是全文，需跳过避免重复）
        // keep=false：工具轮过渡文本，撤回已推送的部分
        handlers.onStreamEnd(data)
      } else if (event === 'token' && handlers.onToken) {
        handlers.onToken(data)
      } else if (event === 'done' && handlers.onDone) {
        handlers.onDone(data)
      } else if (event === 'error') {
        throw new Error(data?.message || data)
      }
    }
  }
}

/**
 * 统一的流式请求：POST JSON → 交给 consumeAgentStream 分派事件。
 *
 * 三个流式入口（问答 / 恢复挂起 / 续跑中断）共用本函数，避免各写一遍
 * fetch + 错误解析（此前是三份近似重复的代码，改错误提示要改三处）。
 */
export async function streamRequest(url, payload = {}, handlers = {}, options = {}) {
  const response = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload ?? {}),
    signal: options.signal,
  })
  if (!response.ok || !response.body) {
    const detail = await response.json().catch(() => null)
    throw new Error(detail?.detail || `HTTP ${response.status}，请确认后端已启动`)
  }
  return consumeAgentStream(response, handlers)
}
