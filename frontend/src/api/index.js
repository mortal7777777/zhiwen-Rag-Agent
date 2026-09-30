import axios from 'axios'

// 统一的后端请求封装：前端只管调用函数，不关心 URL 细节
const client = axios.create({
  baseURL: '/api',
  timeout: 600000, // 首次建索引可能较慢，给足超时时间
})

/** 发送问答请求（旧版 RAG，保留兼容） */
export const chat = (question, history = []) =>
  client.post('/chat', { question, history }).then((res) => res.data)

/** 消费一条 Agent SSE 响应体（/agent/stream 与 /agent/resume 事件协议一致）。 */
async function consumeAgentStream(response, handlers = {}) {
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
 * Agent SSE 流式对话。
 * payload: { question, conversation_id, use_web_search, use_knowledge_base, template_id }
 * handlers: { onSession, onToolStart, onToolResult, onStreamDelta, onStreamEnd,
 *             onSuspended, onToken, onDone, onError }
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
  return consumeAgentStream(response, handlers)
}

/**
 * 恢复挂起的运行（敏感操作审批）。
 * payload: { conversation_id, decisions: [{call_id, approve, reason, remember_session, remember_forever}], stopped }
 * 事件协议与 streamAgentChat 相同：新流续跑同一条回答。
 */
export async function resumeAgentChat(payload = {}, handlers = {}, options = {}) {
  const response = await fetch('/api/agent/resume', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
    signal: options.signal,
  })
  if (!response.ok || !response.body) {
    const detail = await response.json().catch(() => null)
    throw new Error(detail?.detail || `HTTP ${response.status}，请确认后端已启动`)
  }
  return consumeAgentStream(response, handlers)
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

/** 已提取的长期记忆列表（status: active | archived | all） */
export const listMemories = (status = 'active') =>
  client.get('/memories', { params: { status } }).then((res) => res.data)

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

/** 上传多个文件（multipart/form-data）
 *  options: { dryRun, conflictPolicy: 'ask'|'proceed', archiveVersionNo, archiveNote }
 *  冲突时后端返回 409：detail={code:'upload_conflict', files:[{would_replace, suggested_version_no, conflicts}]}
 */
export const uploadDocuments = (files, options = {}) => {
  const form = new FormData()
  files.forEach((file) => form.append('files', file))
  if (options.dryRun) form.append('dry_run', 'true')
  if (options.conflictPolicy) form.append('conflict_policy', options.conflictPolicy)
  if (options.archiveVersionNo) form.append('archive_version_no', options.archiveVersionNo)
  if (options.archiveNote) form.append('archive_note', options.archiveNote)
  return client.post('/documents/upload', form).then((res) => res.data)
}

/** 删除知识库文件；purgeVersions=true 时同时删除其全部历史版本 */
export const deleteDocument = (relativePath, purgeVersions = false) =>
  client
    .delete(`/documents/${encodeURIComponent(relativePath)}`, {
      params: purgeVersions ? { purge_versions: true } : {},
    })
    .then((res) => res.data)

/** 重命名知识库文档（重名 / 非法名后端会拒绝） */
export const renameDocument = (relativePath, newName) =>
  client
    .post('/documents/rename', { relative_path: relativePath, new_name: newName })
    .then((res) => res.data)

/** 某文档的版本列表（当前版本 + 历史版本） */
export const listDocumentVersions = (relativePath) =>
  client
    .get('/documents/versions', { params: { path: relativePath } })
    .then((res) => res.data)

/** 把 source_path 文档归档为 target_path 文档的历史版本 */
export const archiveDocumentVersion = (payload) =>
  client.post('/documents/versions/archive', payload).then((res) => res.data)

/** 恢复某历史版本为当前（当前版本归档为 new_version_no） */
export const restoreDocumentVersion = (versionId, payload) =>
  client.post(`/documents/versions/${versionId}/restore`, payload).then((res) => res.data)

/** 彻底删除某历史版本 */
export const deleteDocumentVersion = (versionId) =>
  client.delete(`/documents/versions/${versionId}`).then((res) => res.data)

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

/** 文档原始字节地址（pdf.js / epub.js / docx-preview 专业查看器用） */
export const getDocumentFileUrl = (relativePath) =>
  `/api/documents/file/${relativePath
    .split('/')
    .map((seg) => encodeURIComponent(seg))
    .join('/')}`

/** 更改知识库数据目录（即时生效并持久化） */
export const setDataDir = (path) =>
  client.put('/documents/data-dir', { path }).then((res) => res.data)

/** 全部文档的自定义分类元数据 */
export const getDocumentMeta = () =>
  client.get('/documents/meta').then((res) => res.data)

/** 保存某个文档的分类/标签/备注 */
export const saveDocumentMeta = (payload) =>
  client.put('/documents/meta', payload).then((res) => res.data)

/** 运行统计：tokens / 缓存命中率 / 估算成本 / 每日趋势。
 *  period: today|3d|7d|all（自然日）；不传则按 days 天 */
export const getRunStats = (days = 7, period = '') =>
  client
    .get('/runs/stats', { params: period ? { period } : { days } })
    .then((res) => res.data)

// ---------------- 设置与技能 ----------------

/** 读取运行时配置（API Key 已脱敏） */
export const getSettings = () => client.get('/settings').then((res) => res.data)

/** 保存运行时配置并立即生效 */
export const saveSettings = (updates) =>
  client.put('/settings', { updates }).then((res) => res.data)

/** 拉取当前激活对话供应商支持的模型列表（OpenAI 兼容 GET /models） */
export const listChatModels = () =>
  client.get('/settings/models').then((res) => res.data)

/** 列出本机某个路径下的子目录（设置页目录选择器用；仅本机可访问） */
export const browseDirs = (path = '') =>
  client.get('/fs/browse', { params: { path }, timeout: 15000 }).then((res) => res.data)

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

export const shareAgentSkill = (dirName, category = '') =>
  client.post('/skills/agent/share', { dir_name: dirName, category }).then((res) => res.data)
export const unshareAgentSkill = (dirName, category = '') =>
  client.post('/skills/agent/unshare', { dir_name: dirName, category }).then((res) => res.data)
export const deleteAgentSkill = (dirName, category = '') =>
  client
    .delete(`/skills/agent/${encodeURIComponent(dirName)}`, { params: { category } })
    .then((res) => res.data)
export const getAgentSkillContent = (dirName, category = '') =>
  client
    .get('/skills/agent/content', { params: { dir_name: dirName, category } })
    .then((res) => res.data)

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
export const connectMcpServers = (ids) =>
  client.post('/mcp/connect', { ids }).then((res) => res.data)

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

/** 该会话挂起中的原生审批（刷新/切换会话后恢复审批卡/提示续跑） */
export const getPendingApproval = (conversationId) =>
  client.get(`/agent/pending/${conversationId}`).then((res) => res.data)

/**
 * 续跑被中断的运行（进程被杀/异常退出留下的未完成一轮）：新 SSE 流续跑。
 * 事件协议与 streamAgentChat 相同。
 */
export async function continueAgentRun(conversationId, handlers = {}, options = {}) {
  const response = await fetch(`/api/agent/continue/${conversationId}`, {
    method: 'POST',
    signal: options.signal,
  })
  if (!response.ok || !response.body) {
    const detail = await response.json().catch(() => null)
    throw new Error(detail?.detail || `HTTP ${response.status}，请确认后端已启动`)
  }
  return consumeAgentStream(response, handlers)
}

/** 丢弃未完成的运行（删除该会话的原生检查点线程），解除新消息闸门 */
export const abandonRun = (conversationId) =>
  client.post(`/agent/abandon/${conversationId}`).then((res) => res.data)

/** 上下文占用统计：消息条数 / token 预算 / 滚动摘要进度 / 缓存命中率 */
export const getAgentContext = (conversationId) =>
  client
    .get(`/agent/context/${conversationId}`, { timeout: 15000 })
    .then((res) => res.data)

/** 手动压缩会话历史：保留近期消息，更早的并入滚动摘要 */
export const compactConversation = (conversationId) =>
  client.post(`/agent/compact/${conversationId}`).then((res) => res.data)

/** 消息级回退：删除该消息及其之后的全部消息（类 Claude Code rewind） */
export const rewindConversation = (conversationId, messageId) =>
  client
    .post(`/conversations/${conversationId}/rewind`, { message_id: messageId })
    .then((res) => res.data)

/** 会话快照时间线（原生检查点，最近 50 条） */
export const getConversationTimeline = (conversationId) =>
  client.get(`/conversations/${conversationId}/timeline`).then((res) => res.data)

/** 回滚会话消息与任务清单到某个快照（下一次提问从该状态继续） */
export const rollbackConversation = (conversationId, checkpointId) =>
  client
    .post(
      `/conversations/${conversationId}/timeline/${encodeURIComponent(checkpointId)}/rollback`,
    )
    .then((res) => res.data)

/** TodoWrite 任务清单：读取/保存某会话的任务 */
export const getTodos = (conversationId) =>
  client.get(`/todos/${conversationId}`).then((res) => res.data)
export const saveTodos = (conversationId, items) =>
  client
    .put(`/todos/${conversationId}`, { items })
    .then((res) => res.data)
