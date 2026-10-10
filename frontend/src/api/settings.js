/** 设置、模型列表、目录浏览、技能、MCP、项目记忆、受控执行与技能沙箱。 */

import client from './client.js'

// ---------------- 设置与技能 ----------------

/** 读取运行时配置（API Key 已脱敏） */
export const getSettings = () => client.get('/settings').then((res) => res.data)

/** 保存运行时配置并立即生效 */
export const saveSettings = (updates) =>
  client.put('/settings', { updates }).then((res) => res.data)

/** 拉取当前激活对话供应商支持的模型列表（OpenAI 兼容 GET /models） */
export const listChatModels = () =>
  client.get('/settings/models').then((res) => res.data)

// ---------------- 供应商增强（预设 / 测试连接 / 按卡片拉模型 / 测速） ----------------

/** 内置供应商预设（cc-switch 式「从预设开始」） */
export const getProviderPresets = () =>
  client.get('/settings/provider-presets').then((res) => res.data)

/** 测试单个供应商配置（支持未保存草稿；恒 200，由返回的 ok 判别成败） */
export const testProvider = (cfg) =>
  client.post('/settings/providers/test', cfg).then((res) => res.data)

/** 按指定供应商配置拉模型列表（每张卡片各拉各的；掩码 Key 需带 id） */
export const listProviderModels = (cfg) =>
  client.post('/settings/providers/models', cfg).then((res) => res.data)

/** 对全部已启用对话供应商并发测延迟（服务端已存配置，Key 不过浏览器） */
export const speedtestProviders = () =>
  client.post('/settings/providers/speedtest', {}).then((res) => res.data)

/** 导出供应商配置（默认不含密钥；includeKeys=true 时含明文，仅本地使用） */
export const exportProviders = (includeKeys = false) =>
  client
    .get('/settings/providers/export', { params: { include_keys: includeKeys ? 1 : 0 } })
    .then((res) => res.data)

/** 导入供应商配置（同 id 覆盖、空/掩码 Key 保留旧值、新 id 掩码拒绝） */
export const importProviders = (payload) =>
  client.post('/settings/providers/import', payload).then((res) => res.data)

/** 快捷切换激活对话供应商（只清对话缓存，不触发嵌入/重排重载） */
export const switchChatProvider = (providerId) =>
  client.post('/settings/switch-chat-provider', { provider_id: providerId }).then((res) => res.data)

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
