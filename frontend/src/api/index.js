import axios from 'axios'

// 统一的后端请求封装：前端只管调用函数，不关心 URL 细节
const client = axios.create({
  baseURL: '/api',
  timeout: 600000, // 首次建索引可能较慢，给足超时时间
})

/** 发送问答请求（旧版 RAG，保留兼容） */
export const chat = (question, history = []) =>
  client.post('/chat', { question, history }).then((res) => res.data)

/**
 * Agent SSE 流式对话。
 * payload: { question, conversation_id, use_web_search, use_knowledge_base, template_id }
 * handlers: { onSession, onToolStart, onToolResult, onToken, onDone, onError }
 * options:  { signal } 传入 AbortController.signal 可中止请求（停止生成）
 */
export async function streamAgentChat(payload = {}, handlers = {}, options = {}) {
  const response = await fetch('/api/agent/stream', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
    signal: options.signal,
  })
  if (!response.ok || !response.body) {
    const detail = await response.json().catch(() => null)
    throw new Error(detail?.detail || `HTTP ${response.status}，请确认后端已启动`)
  }

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
      } else if (event === 'reasoning' && handlers.onReasoning) {
        handlers.onReasoning(data)
      } else if (event === 'plan_progress' && handlers.onPlanProgress) {
        handlers.onPlanProgress(data)
      } else if (event === 'vision' && handlers.onVision) {
        handlers.onVision(data)
      } else if (event === 'tool_start' && handlers.onToolStart) {
        handlers.onToolStart(data)
      } else if (event === 'tool_result' && handlers.onToolResult) {
        handlers.onToolResult(data)
      } else if (event === 'permission_request' && handlers.onPermission) {
        handlers.onPermission(data)
      } else if (event === 'permission_resolved' && handlers.onPermissionResolved) {
        handlers.onPermissionResolved(data)
      } else if (event === 'hook' && handlers.onHook) {
        handlers.onHook(data)
      } else if (event === 'todos' && handlers.onTodos) {
        handlers.onTodos(data)
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

// ---------------- 会话（MySQL 记忆） ----------------

/** 会话列表（最近更新在前） */
export const listConversations = () =>
  client.get('/conversations').then((res) => res.data)

/** 新建会话 */
export const createConversation = (payload = {}) =>
  client
    .post(
      '/conversations',
      typeof payload === 'string' ? { title: payload } : payload,
    )
    .then((res) => res.data)

/** 更新会话：重命名 / 绑定模板 */
export const renameConversation = (id, payload) =>
  client
    .patch(
      `/conversations/${id}`,
      typeof payload === 'string' ? { title: payload } : payload,
    )
    .then((res) => res.data)

/** 删除会话 */
export const deleteConversation = (id) =>
  client.delete(`/conversations/${id}`).then((res) => res.data)

/** 加载某个会话的历史消息 */
export const getConversationMessages = (id) =>
  client.get(`/conversations/${id}/messages`).then((res) => res.data)

// ---------------- 提示词模板 ----------------

/** 模板列表 */
export const listTemplates = () =>
  client.get('/templates').then((res) => res.data)

/** 新建自定义模板 */
export const createTemplate = (payload) =>
  client.post('/templates', payload).then((res) => res.data)

/** 编辑自定义模板 */
export const updateTemplate = (id, payload) =>
  client.patch(`/templates/${id}`, payload).then((res) => res.data)

/** 删除自定义模板 */
export const deleteTemplate = (id) =>
  client.delete(`/templates/${id}`).then((res) => res.data)

// ---------------- 长期记忆 ----------------

/** 已提取的长期记忆列表 */
export const listMemories = () =>
  client.get('/memories').then((res) => res.data)

/** 手动新增长期记忆 */
export const addMemory = (payload) =>
  client.post('/memories', payload).then((res) => res.data)

/** 编辑长期记忆（内容/分类） */
export const updateMemory = (id, payload) =>
  client.patch(`/memories/${id}`, payload).then((res) => res.data)

/** 删除一条长期记忆 */
export const deleteMemory = (id) =>
  client.delete(`/memories/${id}`).then((res) => res.data)

/** 手动触发记忆整合整理 */
export const consolidateMemories = () =>
  client.post('/memories/consolidate').then((res) => res.data)

// ---------------- 知识库（原有功能） ----------------

/** 获取知识库文件列表 */
export const listDocuments = () =>
  client.get('/documents').then((res) => res.data)

/** 上传多个文件（multipart/form-data） */
export const uploadDocuments = (files) => {
  const form = new FormData()
  files.forEach((file) => form.append('files', file))
  return client.post('/documents/upload', form).then((res) => res.data)
}

/** 删除知识库文件 */
export const deleteDocument = (relativePath) =>
  client.delete(`/documents/${encodeURIComponent(relativePath)}`).then((res) => res.data)

/** 强制重建索引 */
export const rebuildIndex = () =>
  client.post('/index/rebuild').then((res) => res.data)

/** 获取索引状态 */
export const getIndexStatus = () =>
  client.get('/index/status').then((res) => res.data)

/** 预览知识库文档（md 按原文，pdf/docx/epub 等提取文本） */
export const previewDocument = (relativePath) =>
  client
    .get('/documents/preview', { params: { path: relativePath } })
    .then((res) => res.data)

/** 全部文档的自定义分类元数据 */
export const getDocumentMeta = () =>
  client.get('/documents/meta').then((res) => res.data)

/** 保存某个文档的分类/标签/备注 */
export const saveDocumentMeta = (payload) =>
  client.put('/documents/meta', payload).then((res) => res.data)

/** 运行统计：tokens / 缓存命中率 / 估算成本 / 每日趋势 */
export const getRunStats = (days = 7) =>
  client.get('/runs/stats', { params: { days } }).then((res) => res.data)

// ---------------- 设置与技能 ----------------

/** 读取运行时配置（API Key 已脱敏） */
export const getSettings = () => client.get('/settings').then((res) => res.data)

/** 保存运行时配置并立即生效 */
export const saveSettings = (updates) =>
  client.put('/settings', { updates }).then((res) => res.data)

/** 列出本机可复用的 Agent 技能（include_hidden=true 时同时返回已隐藏项） */
export const listSkills = (includeHidden = false) =>
  client
    .get('/skills', { params: { include_hidden: includeHidden } })
    .then((res) => res.data)

/** 语义检索技能 */
export const searchSkills = (q) =>
  client.get('/skills/search', { params: { q } }).then((res) => res.data)

/** 保存技能偏好：enabled=本助手启用的技能，hidden=从本助手移除的技能 */
export const saveSkillPrefs = (enabled, hidden) =>
  client.put('/skills', { enabled, hidden }).then((res) => res.data)

/** 恢复默认精选手集 */
export const resetSkillPrefs = () =>
  client.post('/skills/reset').then((res) => res.data)

/** 每日建议（知识库 / 热点 / 通用） */
export const getSuggestions = () =>
  client.get('/suggestions').then((res) => res.data)

/** Agent 运行记录（决策可观测性） */
export const listRuns = (params = {}) =>
  client.get('/runs', { params }).then((res) => res.data)

/** 读取某次运行的完整 trace */
export const getRunTrace = (runId) =>
  client.get(`/runs/${runId}/trace`).then((res) => res.data)

// ---------------- 高级功能（P0/P1） ----------------

/** MCP 服务器配置 */
export const listMcpServers = () => client.get('/mcp').then((res) => res.data)
export const saveMcpServers = (servers) =>
  client.put('/mcp', { servers }).then((res) => res.data)
export const testMcpServer = (cfg) =>
  client.post('/mcp/test', cfg).then((res) => res.data)

/** 文件型项目记忆（AGENTS.md） */
export const getProjectMemory = () =>
  client.get('/project-memory').then((res) => res.data)
export const exportProjectMemory = () =>
  client.post('/project-memory/export').then((res) => res.data)

/** 受控执行工具（设置页测试） */
export const executeTool = (payload) =>
  client.post('/tools/execute', payload).then((res) => res.data)

/** 技能沙箱：预览/执行技能命令 */
export const getSkillCommands = (name) =>
  client.get(`/skills/${encodeURIComponent(name)}/commands`).then((res) => res.data)
export const runSkillCommand = (name, command) =>
  client.post(`/skills/${encodeURIComponent(name)}/run`, { command }).then((res) => res.data)

/** 人工确认（HITL）：批准/拒绝一次敏感操作；remember=永久记住，rememberSession=本会话记住 */
export const resolvePermission = (
  requestId,
  approve,
  reason = '',
  remember = false,
  rememberSession = false,
) =>
  client
    .post(`/agent/permission/${encodeURIComponent(requestId)}/resolve`, {
      approve,
      reason,
      remember,
      remember_session: rememberSession,
    })
    .then((res) => res.data)

/** 当前待人工确认的审批列表 */
export const listPendingPermissions = () =>
  client.get('/agent/permissions').then((res) => res.data)

/** 上下文占用统计：消息条数 / token 预算 / 滚动摘要进度 */
export const getAgentContext = (conversationId) =>
  client.get(`/agent/context/${conversationId}`).then((res) => res.data)

/** 手动压缩会话历史：保留近期消息，更早的并入滚动摘要 */
export const compactConversation = (conversationId) =>
  client.post(`/agent/compact/${conversationId}`).then((res) => res.data)

/** 消息级回退：删除该消息及其之后的全部消息（类 Claude Code rewind） */
export const rewindConversation = (conversationId, messageId) =>
  client
    .post(`/conversations/${conversationId}/rewind`, { message_id: messageId })
    .then((res) => res.data)

/** TodoWrite 任务清单：读取/保存某会话的任务 */
export const getTodos = (conversationId) =>
  client.get(`/todos/${conversationId}`).then((res) => res.data)
export const saveTodos = (conversationId, items) =>
  client
    .put(`/todos/${conversationId}`, { items })
    .then((res) => res.data)
