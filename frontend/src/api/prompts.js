/** 系统提示词版本、片段与会话锁定。 */

import client from './client.js'

// ---------------- 模板版本 ----------------

export const listTemplateVersions = (id, includeContent = true) =>
  client
    .get(`/templates/${id}/versions`, {
      params: { include_content: includeContent },
    })
    .then((res) => res.data)

export const getTemplateVersion = (id, versionId) =>
  client.get(`/templates/${id}/versions/${versionId}`).then((res) => res.data)

export const restoreTemplateVersion = (id, payload) =>
  client.post(`/templates/${id}/restore`, payload).then((res) => res.data)

export const lintTemplate = (payload) =>
  client.post('/templates/lint', payload).then((res) => res.data)

export const cloneTemplate = (id) =>
  client.post(`/templates/${id}/clone`).then((res) => res.data)

export const getDefaultTemplate = () =>
  client.get('/templates/default').then((res) => res.data)

export const archiveTemplate = (id) =>
  client.post(`/templates/${id}/archive`).then((res) => res.data)

export const unarchiveTemplate = (id) =>
  client.post(`/templates/${id}/unarchive`).then((res) => res.data)

export const saveTemplateDraft = (id, payload) =>
  client.put(`/templates/${id}/draft`, payload).then((res) => res.data)

export const publishTemplateDraft = (id, payload = {}) =>
  client.post(`/templates/${id}/publish-draft`, payload).then((res) => res.data)

export const discardTemplateDraft = (id) =>
  client.delete(`/templates/${id}/draft`).then((res) => res.data)

// ---------------- 系统片段 ----------------

export const listPromptFragments = () =>
  client.get('/prompts/fragments').then((res) => res.data)

export const updatePromptFragment = (key, payload) =>
  client.patch(`/prompts/fragments/${key}`, payload).then((res) => res.data)

export const lintPromptFragment = (key, payload) =>
  client.post(`/prompts/fragments/${key}/lint`, payload).then((res) => res.data)

export const previewPrompt = (payload) =>
  client.post('/prompts/preview', payload).then((res) => res.data)

export const normalizePrompt = (payload) =>
  client.post('/prompts/normalize', payload).then((res) => res.data)

export const auditPrompts = () =>
  client.get('/prompts/audit').then((res) => res.data)

export const savePromptFragmentDraft = (key, payload) =>
  client.put(`/prompts/fragments/${key}/draft`, payload).then((res) => res.data)

export const publishPromptFragmentDraft = (key, payload = {}) =>
  client
    .post(`/prompts/fragments/${key}/publish-draft`, payload)
    .then((res) => res.data)

export const discardPromptFragmentDraft = (key) =>
  client.delete(`/prompts/fragments/${key}/draft`).then((res) => res.data)

export const listPromptFragmentVersions = (key, includeContent = true) =>
  client
    .get(`/prompts/fragments/${key}/versions`, {
      params: { include_content: includeContent },
    })
    .then((res) => res.data)

export const getPromptFragmentVersion = (key, versionId) =>
  client
    .get(`/prompts/fragments/${key}/versions/${versionId}`)
    .then((res) => res.data)

export const restorePromptFragment = (key, payload) =>
  client.post(`/prompts/fragments/${key}/restore`, payload).then((res) => res.data)

export const resetPromptFragment = (key, payload = {}) =>
  client.post(`/prompts/fragments/${key}/reset`, payload).then((res) => res.data)

// ---------------- 会话锁定 ----------------

export const getConversationPrompt = (id) =>
  client.get(`/conversations/${id}/prompt`).then((res) => res.data)

export const applyLatestConversationPrompt = (id) =>
  client.post(`/conversations/${id}/prompt/apply-latest`).then((res) => res.data)

// ---------------- 影响面 / 批量升级 ----------------

export const getPromptImpact = (limit = 200) =>
  client.get('/prompts/impact', { params: { limit } }).then((res) => res.data)

export const applyLatestBatch = (conversationIds = null) =>
  client
    .post('/prompts/apply-latest-batch', {
      conversation_ids: conversationIds,
    })
    .then((res) => res.data)

export const pinConversationPrompt = (id, payload) =>
  client.post(`/conversations/${id}/prompt/pin`, payload).then((res) => res.data)

// ---------------- 导入导出 / 试跑 ----------------

export const exportPrompts = () =>
  client.get('/prompts/export').then((res) => res.data)

export const importPrompts = (data) =>
  client.post('/prompts/import', { data }).then((res) => res.data)

export const runPromptAB = (payload) =>
  client.post('/prompts/trials/ab', payload).then((res) => res.data)

export const runPromptEval = (payload) =>
  client.post('/prompts/trials/eval', payload).then((res) => res.data)

export const listPromptTrials = (params = {}) =>
  client.get('/prompts/trials', { params }).then((res) => res.data)
