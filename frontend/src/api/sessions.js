/** 会话、提示词模板、长期记忆。 */

import client from './client.js'

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
