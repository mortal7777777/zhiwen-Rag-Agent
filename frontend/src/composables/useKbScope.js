/**
 * 知识库检索范围（单文档 / 整本书 / 多文档）。
 *
 * 书籍分组用文件名去掉扩展名近似（与后端 match_book_paths 的"书名匹配
 * 文件名"口径一致）；同名多格式（epub+pdf）合并成一个"整本书"选项。
 * 多选时发送 type=docs + 展开后的相对路径列表。
 */
import { computed, ref } from 'vue'
import { listDocuments } from '../api'
import {
  buildScopeFromSelection,
  scopePayloadFromScope,
} from '../utils/kbScope'

export function useKbScope() {
  const kbScope = ref(null) // { type: doc|book|docs, value?, values?, label }
  const scopeDialogVisible = ref(false)
  const scopeQuery = ref('')
  const scopeDraft = ref([])
  const scopeDocs = ref([])
  let scopeDocsPromise = null

  /** 文档列表按需加载（范围弹窗与输入框 @ 引用共用，只请求一次）。 */
  function ensureScopeDocs() {
    if (scopeDocs.value.length) return Promise.resolve(scopeDocs.value)
    if (!scopeDocsPromise) {
      scopeDocsPromise = listDocuments()
        .then((list) => {
          scopeDocs.value = Array.isArray(list) ? list : []
          return scopeDocs.value
        })
        .catch(() => {
          scopeDocs.value = []
          return scopeDocs.value
        })
        .finally(() => {
          scopeDocsPromise = null
        })
    }
    return scopeDocsPromise
  }

  async function openScopeDialog() {
    initDraft()
    scopeDialogVisible.value = true
    await ensureScopeDocs()
  }

  function initDraft() {
    const scope = kbScope.value
    if (!scope) {
      scopeDraft.value = []
    } else if (scope.type === 'docs') {
      scopeDraft.value = (scope.values || []).map((v) => `doc:${v}`)
    } else if (scope.type === 'doc') {
      scopeDraft.value = [`doc:${scope.value}`]
    } else {
      scopeDraft.value = [`book:${scope.value}`]
    }
  }

  const scopeOptions = computed(() => {
    const groups = new Map()
    for (const doc of scopeDocs.value) {
      const stem = String(doc.name || '').replace(/\.[^.]+$/, '')
      if (!stem) continue
      if (!groups.has(stem)) groups.set(stem, [])
      groups.get(stem).push(doc)
    }
    const options = []
    for (const [stem, docs] of groups) {
      if (docs.length > 1) {
        options.push({
          key: `book:${stem}`,
          type: 'book',
          value: stem,
          label: stem,
          meta: `整本书 · ${docs.length} 个文件`,
          paths: docs.map((d) => d.relative_path),
        })
      }
      for (const doc of docs) {
        options.push({
          key: `doc:${doc.relative_path}`,
          type: 'doc',
          value: doc.relative_path,
          label: doc.name,
          meta: doc.relative_path,
          paths: [doc.relative_path],
        })
      }
    }
    return options.sort((a, b) => a.label.localeCompare(b.label, 'zh'))
  })

  const filteredScopeOptions = computed(() => {
    const q = scopeQuery.value.trim().toLowerCase()
    if (!q) return scopeOptions.value
    return scopeOptions.value.filter(
      (opt) =>
        opt.label.toLowerCase().includes(q) ||
        String(opt.meta || '').toLowerCase().includes(q),
    )
  })

  function clearDraft() {
    scopeDraft.value = []
  }

  function applyScope() {
    const selected = scopeOptions.value.filter((opt) =>
      scopeDraft.value.includes(opt.key),
    )
    kbScope.value = buildScopeFromSelection(selected)
    scopeDialogVisible.value = false
    scopeQuery.value = ''
  }

  function clearScope() {
    kbScope.value = null
  }

  /** 输入框 @ 引用：把文档加入检索范围（单文档→doc，多个→docs；书范围会被替换）。 */
  function addDocPaths(docs) {
    const labelOf = (path) => {
      const hit = scopeDocs.value.find((d) => d.relative_path === path)
      if (hit?.name) return hit.name
      return String(path).split(/[\\/]/).pop() || path
    }
    const current = kbScope.value
    const options = []
    const seen = new Set()
    if (current?.type === 'doc') {
      options.push({
        type: 'doc',
        value: current.value,
        label: current.label,
        paths: [current.value],
      })
      seen.add(current.value)
    } else if (current?.type === 'docs') {
      for (const path of current.values || []) {
        if (!path || seen.has(path)) continue
        seen.add(path)
        options.push({ type: 'doc', value: path, label: labelOf(path), paths: [path] })
      }
    }
    for (const doc of docs || []) {
      const path = doc?.relative_path
      if (!path || seen.has(path)) continue
      seen.add(path)
      options.push({
        type: 'doc',
        value: path,
        label: doc.name || labelOf(path),
        paths: [path],
      })
    }
    kbScope.value = buildScopeFromSelection(options)
  }

  function scopePayload() {
    return scopePayloadFromScope(kbScope.value)
  }

  return {
    kbScope,
    scopeDocs,
    scopeDialogVisible,
    scopeQuery,
    scopeDraft,
    filteredScopeOptions,
    openScopeDialog,
    ensureScopeDocs,
    addDocPaths,
    clearDraft,
    applyScope,
    clearScope,
    scopePayload,
  }
}
