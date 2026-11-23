/**
 * ChatView 纯函数工具（无 Vue 状态、无 IO）。
 *
 * 抽出的目的：这些映射/格式化逻辑此前埋在 4300 行视图里，无法单测；
 * 现在由 scripts/check_chat_utils.mjs 在 Node 里直接断言。
 */

/** 本地消息的稳定 key 序列（历史消息用数据库 id，本地构造的用这个）：
 * 消息列表 v-for 的 key 不能用数组下标——rewind 在中间 splice 后下标全
 * 位移，Vue 按 key 复用组件会让 MarkdownContent 等内部状态串台。 */
let LOCAL_MSG_SEQ = 0
export function nextLocalMsgKey() {
  LOCAL_MSG_SEQ += 1
  return `local-${LOCAL_MSG_SEQ}`
}

const TOOL_LABELS = {
  knowledge_base_search: '知识库检索',
  web_search: '联网搜索',
  image_to_text: '图片识别',
  read_file: '读取文件',
  list_dir: '查看目录',
  grep_search: '搜索代码',
  write_file: '写入文件',
  edit_file: '编辑文件',
  delete_file: '删除文件',
  bash: '执行命令',
  command_tool: '执行命令',
  file_tool: '文件操作',
  todo_update: '任务清单',
  subagent: '子代理',
  add_document: '添加知识库文档',
  skill_view: '查看技能',
  write_skill: '沉淀技能',
  dispatch_subtasks: '并行子任务',
  enter_plan_mode: '进入计划模式',
  exit_plan_mode: '提交计划',
  ask_user_question: '向你提问',
  verify: '验证结果',
}

export function toolName(name) {
  if (name && name.startsWith('mcp_')) {
    return `MCP · ${name.slice(4).replace(/_/g, ' ')}`
  }
  return TOOL_LABELS[name] || name
}

/** 把审批参数格式化成多行展示文本（预览大段内容，避免刷屏） */
export function formatArgs(args) {
  if (!args || !Object.keys(args).length) return ''
  const lines = []
  for (const [k, v] of Object.entries(args)) {
    if (v === '' || v == null) continue
    const text = typeof v === 'string' ? v : JSON.stringify(v)
    const preview =
      text.length > 160 ? `${text.slice(0, 160)}…（共 ${text.length} 字符）` : text
    lines.push(`${k}: ${preview}`)
  }
  return lines.join('\n')
}

export function formatFileSize(bytes) {
  const n = Number(bytes) || 0
  if (n >= 1024 * 1024) return `${(n / (1024 * 1024)).toFixed(1)}MB`
  if (n >= 1024) return `${Math.round(n / 1024)}KB`
  return `${n}B`
}

export function formatCount(n) {
  const v = Number(n) || 0
  return v >= 10000 ? `${(v / 10000).toFixed(1)}万` : String(v)
}

/** 来源按引用编号排序：并行工具按完成顺序入账，索引常与 [n] 顺序不一致。 */
export function sortSources(sources) {
  const order = (src) => {
    const n = Number(src?.index)
    return Number.isFinite(n) ? n : Number.MAX_SAFE_INTEGER
  }
  return [...(sources || [])].sort((a, b) => order(a) - order(b))
}

/** 后端历史消息 → 前端消息对象（含 id：消息级回退要用） */
export function mapHistoryMessage(m) {
  return {
    id: m.id,
    role: m.role,
    content: m.content,
    tool_trace: (m.tool_trace || []).map((t) => ({ ...t, _open: false })),
    sources: sortSources(m.sources),
    permissions: [],
    todos: [],
    plan: [],
    planTotal: 0,
    _sourcesOpen: false,
    _streaming: false,
  }
}

export function isFailed(content) {
  return content && content.startsWith('请求失败')
}

export function formatCheckpointTime(ts) {
  if (!ts) return '—'
  try {
    return new Date(ts).toLocaleString('zh-CN', { hour12: false })
  } catch {
    return String(ts)
  }
}

export function formatMemoryDate(v) {
  if (!v) return '未知'
  const d = new Date(v)
  if (Number.isNaN(d.getTime())) return '未知'
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(
    d.getDate(),
  ).padStart(2, '0')}`
}

export const MEMORY_CATEGORIES = {
  profile: '用户画像',
  preference: '喜好偏好',
  project: '工作项目',
  decision: '重要决定',
  lesson: '教训与应对',
  other: '其他',
}

export function categoryLabel(key) {
  return MEMORY_CATEGORIES[key] || key
}

export function categoryTagType(key) {
  return (
    {
      profile: 'primary',
      preference: 'success',
      project: 'warning',
      decision: 'danger',
      lesson: 'info',
      other: 'info',
    }[key] || 'info'
  )
}
