/** 子代理类型注册表（Phase A 6.1/6.2）：列表、编辑、重置。 */

import client from './client.js'

export const listAgentTypes = () => client.get('/agent-types').then((res) => res.data)

export const getAgentTypeMeta = () =>
  client.get('/agent-types/meta').then((res) => res.data)

export const createAgentType = (payload) =>
  client.post('/agent-types', payload).then((res) => res.data)

export const updateAgentType = (key, updates) =>
  client.patch(`/agent-types/${encodeURIComponent(key)}`, updates).then((res) => res.data)

export const resetAgentType = (key) =>
  client.post(`/agent-types/${encodeURIComponent(key)}/reset`).then((res) => res.data)

export const deleteAgentType = (key) =>
  client.delete(`/agent-types/${encodeURIComponent(key)}`).then((res) => res.data)
