/** 知识库文档与版本、索引维护、预览/元数据、运行统计与建议。 */

import client from './client.js'

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
