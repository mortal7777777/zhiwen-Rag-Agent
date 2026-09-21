<template>
  <div class="knowledge-page">
    <!-- 索引状态卡片 -->
    <el-card shadow="never" class="status-card">
      <template #header>
        <div class="header-row">
          <span class="card-title">知识库管理</span>
          <el-button type="warning" plain :loading="rebuilding" @click="handleRebuild">
            重建索引
          </el-button>
        </div>
      </template>

      <div class="status-row">
        <el-tag>索引：{{ status.index_name || '-' }}</el-tag>
        <el-tag type="success">文本块：{{ status.chunk_count ?? '-' }}</el-tag>
        <el-tag type="info">向量条数：{{ status.doc_count ?? '-' }}</el-tag>
        <el-tag v-if="status.error" type="danger">状态异常：{{ status.error }}</el-tag>
      </div>
      <div class="data-dir">
        数据目录：{{ status.data_dir || '-' }}
        <el-button
          size="small"
          type="primary"
          plain
          class="change-dir-btn"
          @click="openChangeDir"
        >
          更改目录
        </el-button>
      </div>
    </el-card>

    <!-- 上传区域 -->
    <el-card shadow="never" class="upload-card">
      <template #header>
        <span class="card-title">上传文档</span>
      </template>
      <el-upload
        ref="uploadRef"
        drag
        multiple
        v-model:file-list="fileList"
        :auto-upload="false"
        accept=".txt,.md,.csv,.doc,.docx,.xlsx,.pdf,.epub"
        :disabled="uploading"
      >
        <el-icon class="el-icon--upload"><UploadFilled /></el-icon>
        <div class="el-upload__text">
          <template v-if="uploading">
            <el-icon class="is-loading"><Loading /></el-icon>
            正在上传并索引（{{ uploadProgress || '准备中...' }}），请勿关闭页面...
          </template>
          <template v-else>
            拖拽文件到此处，或<em>点击选择</em>
          </template>
        </div>
        <template #tip>
          <div class="el-upload__tip">
            支持 txt / md / csv / doc / docx / xlsx / pdf / epub，上传后自动增量更新索引；
            同名重传会提示把旧文件归档为历史版本，重复内容会给出查重警告
          </div>
        </template>
      </el-upload>
      <el-button
        type="primary"
        class="upload-button"
        :disabled="uploading"
        :loading="uploading"
        @click="handleUpload"
      >
        上传并索引
      </el-button>
    </el-card>

    <!-- 文件列表 -->
    <el-card shadow="never">
      <template #header>
        <div class="header-row">
          <span class="card-title">已有文档（{{ filteredDocuments.length }}）</span>
          <el-select
            v-model="categoryFilter"
            clearable
            placeholder="按分类筛选"
            size="small"
            class="category-filter"
          >
            <el-option v-for="c in categories" :key="c" :label="c" :value="c" />
          </el-select>
        </div>
      </template>
      <el-table v-loading="tableLoading" :data="filteredDocuments" stripe>
        <el-table-column label="文件名" min-width="260">
          <template #default="{ row }">
            <el-dropdown trigger="click" @command="(cmd) => handleVersionCommand(cmd, row)">
              <el-button type="primary" link class="version-menu-btn">
                版本<el-icon class="el-icon--right"><ArrowDown /></el-icon>
              </el-button>
              <template #dropdown>
                <el-dropdown-menu>
                  <el-dropdown-item command="manage">版本管理…</el-dropdown-item>
                  <el-dropdown-item command="archive">归档为历史版本…</el-dropdown-item>
                </el-dropdown-menu>
              </template>
            </el-dropdown>
            <span class="doc-name" @click="handlePreview(row)">{{ row.name }}</span>
          </template>
        </el-table-column>
        <el-table-column label="分类" width="120">
          <template #default="{ row }">
            <el-tag v-if="row.category" size="small" type="info">{{ row.category }}</el-tag>
            <span v-else class="no-category">未分类</span>
          </template>
        </el-table-column>
        <el-table-column label="标签" min-width="140" show-overflow-tooltip>
          <template #default="{ row }">
            <span class="doc-tags">{{ row.tags || '-' }}</span>
          </template>
        </el-table-column>
        <el-table-column prop="size" label="大小" width="100">
          <template #default="{ row }">{{ formatSize(row.size) }}</template>
        </el-table-column>
        <el-table-column prop="modified" label="修改时间" width="170" />
        <el-table-column label="操作" width="230" align="center" class-name="ops-cell">
          <template #default="{ row }">
            <el-button type="primary" link @click="handlePreview(row)">预览</el-button>
            <el-button type="primary" link @click="openMetaEdit(row)">分类</el-button>
            <el-button type="primary" link @click="openRename(row)">重命名</el-button>
            <el-button type="danger" link @click="handleDelete(row)">删除</el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <!-- 文档预览：专业查看器（pdf/epub/docx/文本 按格式分发 + 目录 + 位置记忆） -->
    <DocumentViewer ref="docViewerRef" />

    <!-- 分类编辑对话框 -->
    <el-dialog v-model="metaEditVisible" title="文档分类" width="440px" append-to-body>
      <el-form label-width="60px">
        <el-form-item label="文件">
          <span class="meta-file">{{ metaForm.name }}</span>
        </el-form-item>
        <el-form-item label="分类">
          <el-select
            v-model="metaForm.category"
            filterable
            allow-create
            default-first-option
            clearable
            placeholder="选择已有分类或输入新分类"
            style="width: 100%"
          >
            <el-option v-for="c in categories" :key="c" :label="c" :value="c" />
          </el-select>
        </el-form-item>
        <el-form-item label="标签">
          <el-input
            v-model="metaForm.tags"
            placeholder="多个标签用逗号分隔，如：RAG, 教程"
          />
        </el-form-item>
        <el-form-item label="备注">
          <el-input
            v-model="metaForm.notes"
            type="textarea"
            :rows="3"
            placeholder="可选：这篇文档是什么、适合什么时候查"
          />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="metaEditVisible = false">取消</el-button>
        <el-button type="primary" :loading="metaSaving" @click="saveMeta">保存</el-button>
      </template>
    </el-dialog>

    <!-- 重命名对话框 -->
    <el-dialog v-model="renameVisible" title="重命名文档" width="440px" append-to-body>
      <el-form label-width="70px">
        <el-form-item label="原文件">
          <span class="meta-file">{{ renameForm.oldName }}</span>
        </el-form-item>
        <el-form-item label="新文件名">
          <el-input v-model="renameForm.newName" placeholder="含扩展名，如 示例文档.txt" />
        </el-form-item>
        <div class="dir-hint">
          改名后该书会重新切分并嵌入（可能耗时）；与其他文件重名（含归一化重名）会被拒绝。
        </div>
      </el-form>
      <template #footer>
        <el-button @click="renameVisible = false">取消</el-button>
        <el-button type="primary" :loading="renameSaving" @click="saveRename">确定</el-button>
      </template>
    </el-dialog>

    <!-- 归档为历史版本对话框 -->
    <el-dialog v-model="archiveVisible" title="归档为历史版本" width="500px" append-to-body>
      <el-form label-width="100px">
        <el-form-item label="待归档文档">
          <span class="meta-file">{{ archiveForm.sourceName }}</span>
        </el-form-item>
        <el-form-item label="归属主文档">
          <el-select
            v-model="archiveForm.targetPath"
            filterable
            placeholder="选择当前生效的主文档"
            style="width: 100%"
          >
            <el-option
              v-for="d in archiveTargets"
              :key="d.relative_path"
              :label="d.name"
              :value="d.relative_path"
            />
          </el-select>
        </el-form-item>
        <el-form-item label="版本号">
          <el-input v-model="archiveForm.versionNo" placeholder="如 v1 / 初稿（用户自定）" />
        </el-form-item>
        <el-form-item label="备注">
          <el-input v-model="archiveForm.note" placeholder="可选：这版的说明" />
        </el-form-item>
        <div class="dir-hint">
          归档后该文档将从检索中移除（向量块删除），文件保留在 data_versions/，可随时恢复。
        </div>
      </el-form>
      <template #footer>
        <el-button @click="archiveVisible = false">取消</el-button>
        <el-button type="primary" :loading="archiveSaving" @click="saveArchive">确认归档</el-button>
      </template>
    </el-dialog>

    <!-- 版本管理对话框 -->
    <el-dialog v-model="versionsVisible" title="版本管理" width="680px" append-to-body>
      <div v-if="versionsData.current" class="versions-current">
        当前版本：{{ versionsData.name }}（{{ formatSize(versionsData.current.size) }} ·
        {{ versionsData.current.modified }}）
      </div>
      <el-table v-loading="versionsLoading" :data="versionsData.versions" size="small" empty-text="暂无历史版本">
        <el-table-column prop="version_no" label="版本号" width="100" />
        <el-table-column prop="original_name" label="归档文件" min-width="150" show-overflow-tooltip />
        <el-table-column prop="note" label="备注" min-width="110" show-overflow-tooltip>
          <template #default="{ row }">{{ row.note || '-' }}</template>
        </el-table-column>
        <el-table-column label="大小" width="90">
          <template #default="{ row }">{{ formatSize(row.size) }}</template>
        </el-table-column>
        <el-table-column prop="created_at" label="归档时间" width="150" />
        <el-table-column label="操作" width="120" align="center">
          <template #default="{ row }">
            <el-button type="primary" link @click="openRestore(row)">恢复</el-button>
            <el-button type="danger" link @click="handleDeleteVersion(row)">删除</el-button>
          </template>
        </el-table-column>
      </el-table>
      <template #footer>
        <el-button @click="versionsVisible = false">关闭</el-button>
        <el-button type="primary" plain @click="openArchiveFromVersions">
          归档其他文档为历史版本…
        </el-button>
      </template>
    </el-dialog>

    <!-- 恢复历史版本对话框 -->
    <el-dialog v-model="restoreVisible" title="恢复历史版本" width="460px" append-to-body>
      <el-form label-width="120px">
        <el-form-item label="恢复版本">
          <span class="meta-file">{{ restoreForm.versionNo }}</span>
        </el-form-item>
        <el-form-item label="当前版本归档为">
          <el-input v-model="restoreForm.newVersionNo" placeholder="为被替换的当前版本指定版本号" />
        </el-form-item>
        <el-form-item label="备注">
          <el-input v-model="restoreForm.note" placeholder="可选" />
        </el-form-item>
        <div class="dir-hint">
          恢复后该版本重新切分嵌入（可能耗时）；当前版本自动归档为上面指定的版本号。
        </div>
      </el-form>
      <template #footer>
        <el-button @click="restoreVisible = false">取消</el-button>
        <el-button type="primary" :loading="restoreSaving" @click="saveRestore">恢复</el-button>
      </template>
    </el-dialog>

    <!-- 上传查重确认对话框 -->
    <el-dialog
      v-model="uploadConflictVisible"
      title="重复检测提示"
      width="540px"
      append-to-body
      :close-on-click-modal="false"
      @close="onConflictDialogClose"
    >
      <div class="conflict-file">文件：{{ uploadConflictForm.fileName }}</div>
      <div v-for="(c, i) in uploadConflictForm.conflicts" :key="i" class="conflict-item">
        <el-tag :type="conflictTagType(c.kind)" size="small">{{ conflictTagText(c.kind) }}</el-tag>
        <span class="conflict-msg">{{ c.message }}</span>
      </div>
      <template v-if="uploadConflictForm.wouldReplace">
        <el-divider />
        <el-form label-width="110px">
          <el-form-item label="旧版本归档为">
            <el-input v-model="uploadConflictForm.versionNo" placeholder="如 v1" />
          </el-form-item>
          <el-form-item label="备注">
            <el-input v-model="uploadConflictForm.note" placeholder="可选：本次更新说明" />
          </el-form-item>
        </el-form>
        <div class="dir-hint">
          确认后旧文件归档为历史版本（可在「版本管理」里恢复），新文件写入并重建索引。
        </div>
      </template>
      <template #footer>
        <el-button @click="resolveUploadConflict(null)">跳过此文件</el-button>
        <el-button type="primary" @click="resolveUploadConflict(true)">仍要上传</el-button>
      </template>
    </el-dialog>

    <!-- 更改数据目录对话框 -->
    <el-dialog v-model="changeDirVisible" title="更改知识库目录" width="480px" append-to-body>
      <el-form label-width="70px">
        <el-form-item label="当前目录">
          <span class="meta-file">{{ status.data_dir || '-' }}</span>
        </el-form-item>
        <el-form-item label="新目录">
          <el-input
            v-model="changeDirForm.path"
            placeholder="输入绝对路径，如 D:\AI\my_docs；不存在会自动创建"
          />
        </el-form-item>
        <div class="dir-hint">
          切换后旧索引仍指向原目录，需要点击「重建索引」让新目录的文档生效。
        </div>
      </el-form>
      <template #footer>
        <el-button @click="changeDirVisible = false">取消</el-button>
        <el-button type="primary" :loading="changeDirSaving" @click="saveChangeDir">
          切换目录
        </el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { computed, onMounted, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { UploadFilled, Loading, ArrowDown } from '@element-plus/icons-vue'
import DocumentViewer from '../components/DocumentViewer.vue'
import {
  archiveDocumentVersion,
  deleteDocument,
  deleteDocumentVersion,
  getIndexStatus,
  getDocumentMeta,
  listDocuments,
  listDocumentVersions,
  rebuildIndex,
  renameDocument,
  restoreDocumentVersion,
  saveDocumentMeta,
  setDataDir,
  uploadDocuments,
} from '../api'

const documents = ref([])
const status = ref({})
const uploading = ref(false)
const rebuilding = ref(false)
const tableLoading = ref(false)
const uploadRef = ref(null)
const fileList = ref([])

// 分类筛选：'' = 全部
const categoryFilter = ref('')
const categories = ref([])

// 文档预览（公共查看器）
const docViewerRef = ref(null)

// 分类编辑
const metaEditVisible = ref(false)
const metaSaving = ref(false)
const metaForm = ref({ relative_path: '', name: '', category: '', tags: '', notes: '' })

// 更改数据目录
const changeDirVisible = ref(false)
const changeDirSaving = ref(false)
const changeDirForm = ref({ path: '' })

// 上传进度文案（逐文件）
const uploadProgress = ref('')

// 重命名
const renameVisible = ref(false)
const renameSaving = ref(false)
const renameForm = ref({ relativePath: '', oldName: '', newName: '' })

// 归档为历史版本
const archiveVisible = ref(false)
const archiveSaving = ref(false)
const archiveForm = ref({ sourcePath: '', sourceName: '', targetPath: '', versionNo: 'v1', note: '' })
const archiveTargets = computed(() =>
  documents.value.filter((d) => d.relative_path !== archiveForm.value.sourcePath),
)

// 版本管理
const versionsVisible = ref(false)
const versionsLoading = ref(false)
const versionsData = ref({
  doc_relative_path: '',
  name: '',
  current: null,
  suggested_version_no: 'v1',
  versions: [],
})

// 恢复历史版本
const restoreVisible = ref(false)
const restoreSaving = ref(false)
const restoreForm = ref({ versionId: null, versionNo: '', newVersionNo: 'v1', note: '' })

// 上传查重确认
const uploadConflictVisible = ref(false)
const uploadConflictForm = ref({
  fileName: '',
  conflicts: [],
  wouldReplace: false,
  versionNo: 'v1',
  note: '',
})
let uploadConflictResolver = null

const filteredDocuments = computed(() =>
  categoryFilter.value
    ? documents.value.filter((d) => d.category === categoryFilter.value)
    : documents.value,
)

function formatSize(size) {
  if (size < 1024) return `${size} B`
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`
  return `${(size / 1024 / 1024).toFixed(1)} MB`
}

async function refresh() {
  try {
    const [docs, indexStatus] = await Promise.all([listDocuments(), getIndexStatus()])
    documents.value = docs
    status.value = indexStatus
    // 分类选项 = 当前使用的分类（含"未分类"文档不列出）
    categories.value = [...new Set(docs.map((d) => d.category).filter(Boolean))]
  } catch (error) {
    ElMessage.error(error.response?.data?.detail || '获取数据失败，请确认后端已启动')
  }
}

async function handleUpload() {
  // el-upload 组件实例未暴露 uploadFiles（Element Plus 2.9 只暴露
  // abort/submit/clearFiles/handleStart/handleRemove），必须用
  // v-model:file-list 绑定的本地数组读文件
  const files = fileList.value.map((item) => item.raw).filter(Boolean)
  if (!files.length) {
    ElMessage.warning('请先选择要上传的文件')
    return
  }
  uploading.value = true
  let ok = 0
  let skipped = 0
  const failures = []
  try {
    // 逐文件上传：查重冲突（409）时弹确认，逐文件决策互不阻塞
    for (let i = 0; i < files.length; i += 1) {
      uploadProgress.value = `${i + 1}/${files.length} ${files[i].name}`
      try {
        const result = await uploadOne(files[i])
        if (result === 'ok') ok += 1
        else skipped += 1
      } catch (error) {
        failures.push(`${files[i].name}：${error.message || '上传失败'}`)
      }
    }
  } finally {
    uploading.value = false
    uploadProgress.value = ''
    // 清空上传组件内部的文件列表
    uploadRef.value?.clearFiles()
    fileList.value = []
    await refresh()
  }
  const parts = []
  if (ok) parts.push(`成功 ${ok} 个`)
  if (skipped) parts.push(`跳过 ${skipped} 个`)
  if (failures.length) parts.push(`失败 ${failures.length} 个（${failures[0]}）`)
  if (failures.length) ElMessage.warning(parts.join('，'))
  else ElMessage.success(parts.join('，') || '没有可上传的文件')
}

/** 上传单个文件：正常直传；409 查重冲突时弹窗确认，确认后带版本号重传。 */
async function uploadOne(raw) {
  try {
    await uploadDocuments([raw])
    return 'ok'
  } catch (error) {
    const status = error.response?.status
    const detail = error.response?.data?.detail
    if (status !== 409 || !detail || !Array.isArray(detail.files) || !detail.files.length) {
      throw new Error(typeof detail === 'string' ? detail : '上传失败')
    }
    const decision = await openUploadConflict(raw.name, detail.files[0])
    if (!decision) return 'skipped'
    await uploadDocuments([raw], {
      conflictPolicy: 'proceed',
      archiveVersionNo: decision.versionNo,
      archiveNote: decision.note,
    })
    return 'ok'
  }
}

function openUploadConflict(fileName, check) {
  uploadConflictForm.value = {
    fileName,
    conflicts: check.conflicts || [],
    wouldReplace: !!check.would_replace,
    versionNo: check.suggested_version_no || 'v1',
    note: '',
  }
  uploadConflictVisible.value = true
  return new Promise((resolve) => {
    uploadConflictResolver = resolve
  })
}

function resolveUploadConflict(confirm) {
  if (confirm && uploadConflictForm.value.wouldReplace && !uploadConflictForm.value.versionNo.trim()) {
    ElMessage.warning('请先为旧版本填写归档版本号')
    return
  }
  const form = uploadConflictForm.value
  uploadConflictVisible.value = false
  const resolver = uploadConflictResolver
  uploadConflictResolver = null
  if (resolver) {
    resolver(
      confirm ? { versionNo: form.versionNo.trim(), note: form.note.trim() } : null,
    )
  }
}

function onConflictDialogClose() {
  // 点右上角关闭 = 跳过此文件（否则上传循环里的 Promise 会悬挂）
  if (uploadConflictResolver) resolveUploadConflict(null)
}

function conflictTagType(kind) {
  if (kind === 'identical') return 'danger'
  if (kind === 'same_name' || kind === 'near_duplicate') return 'warning'
  return 'info'
}

function conflictTagText(kind) {
  return (
    {
      same_name: '同名',
      identical: '内容相同',
      similar_name: '名称相似',
      near_duplicate: '疑似重复',
    }[kind] || kind
  )
}

async function handleDelete(row) {
  // 先查历史版本数量：有版本时在确认文案里明示将一并删除
  let versionCount = 0
  try {
    const data = await listDocumentVersions(row.relative_path)
    versionCount = (data.versions || []).length
  } catch {
    // 读取失败不阻塞删除
  }
  const versionHint = versionCount
    ? `该文档有 ${versionCount} 个历史版本，将一并删除（不可恢复）。`
    : ''
  try {
    await ElMessageBox.confirm(
      `确定删除「${row.name}」吗？删除后会重建索引。${versionHint}`,
      '提示',
      { type: 'warning' },
    )
  } catch {
    return // 用户取消
  }
  try {
    await deleteDocument(row.relative_path, versionCount > 0)
    ElMessage.success('已删除并重建索引')
    await refresh()
  } catch (error) {
    ElMessage.error(error.response?.data?.detail || '删除失败')
  }
}

async function handleRebuild() {
  try {
    await ElMessageBox.confirm(
      '重建索引会删除旧索引并重新嵌入全部文档，可能耗时较长，确定继续吗？',
      '提示',
      { type: 'warning' },
    )
  } catch {
    return
  }
  rebuilding.value = true
  try {
    status.value = await rebuildIndex()
    ElMessage.success('索引重建完成')
    await refresh()
  } catch (error) {
    ElMessage.error(error.response?.data?.detail || '重建失败')
  } finally {
    rebuilding.value = false
  }
}

function openChangeDir() {
  changeDirForm.value = { path: status.value.data_dir || '' }
  changeDirVisible.value = true
}

async function saveChangeDir() {
  const path = changeDirForm.value.path.trim()
  if (!path) {
    ElMessage.warning('请输入新的目录路径')
    return
  }
  changeDirSaving.value = true
  try {
    const result = await setDataDir(path)
    changeDirVisible.value = false
    ElMessage.success(result.hint || '目录已切换')
    // 切换到新目录后刷新列表（可能为空目录）与状态
    documents.value = []
    await refresh()
    if (documents.value.length) {
      // 新目录有文档但索引未建：引导用户重建
      ElMessage.info('新目录包含文档但索引尚未建立，请点击「重建索引」')
    }
  } catch (error) {
    ElMessage.error(error.response?.data?.detail || '更改目录失败')
  } finally {
    changeDirSaving.value = false
  }
}

function handlePreview(row) {
  docViewerRef.value?.open(row.relative_path)
}

async function openMetaEdit(row) {
  // 已有元数据（备注等）先读全量，避免只有列表里的 category/tags
  let notes = ''
  try {
    const metas = await getDocumentMeta()
    notes = metas?.[row.relative_path]?.notes || ''
  } catch {
    // 读取失败不阻塞编辑
  }
  metaForm.value = {
    relative_path: row.relative_path,
    name: row.name,
    category: row.category || '',
    tags: row.tags || '',
    notes,
  }
  metaEditVisible.value = true
}

async function saveMeta() {
  const form = metaForm.value
  metaSaving.value = true
  try {
    await saveDocumentMeta({
      relative_path: form.relative_path,
      category: (form.category || '').trim(),
      tags: (form.tags || '').trim(),
      notes: form.notes || null,
    })
    metaEditVisible.value = false
    ElMessage.success('分类已保存')
    await refresh()
  } catch (error) {
    ElMessage.error(error.response?.data?.detail || '保存失败')
  } finally {
    metaSaving.value = false
  }
}

// ---------------- 重命名 ----------------

function openRename(row) {
  renameForm.value = { relativePath: row.relative_path, oldName: row.name, newName: row.name }
  renameVisible.value = true
}

async function saveRename() {
  const form = renameForm.value
  const name = form.newName.trim()
  if (!name) {
    ElMessage.warning('请输入新文件名')
    return
  }
  if (name === form.oldName) {
    renameVisible.value = false
    return
  }
  renameSaving.value = true
  try {
    await renameDocument(form.relativePath, name)
    renameVisible.value = false
    ElMessage.success('已重命名并重建索引')
    await refresh()
  } catch (error) {
    const detail = error.response?.data?.detail
    ElMessage.error(typeof detail === 'string' ? detail : '重命名失败')
  } finally {
    renameSaving.value = false
  }
}

// ---------------- 版本管理 ----------------

function handleVersionCommand(cmd, row) {
  if (cmd === 'manage') openVersions(row)
  else openArchive(row)
}

async function loadVersions(relativePath) {
  versionsLoading.value = true
  try {
    versionsData.value = await listDocumentVersions(relativePath)
  } catch (error) {
    ElMessage.error(error.response?.data?.detail || '读取版本列表失败')
  } finally {
    versionsLoading.value = false
  }
}

function openVersions(row) {
  versionsData.value = {
    doc_relative_path: row.relative_path,
    name: row.name,
    current: null,
    suggested_version_no: 'v1',
    versions: [],
  }
  versionsVisible.value = true
  loadVersions(row.relative_path)
}

// ---------------- 归档为历史版本 ----------------

function openArchive(row) {
  archiveForm.value = {
    sourcePath: row.relative_path,
    sourceName: row.name,
    targetPath: '',
    versionNo: 'v1',
    note: '',
  }
  archiveVisible.value = true
}

function openArchiveFromVersions() {
  const rel = versionsData.value.doc_relative_path
  const row = documents.value.find((d) => d.relative_path === rel)
  if (!row) {
    ElMessage.warning('主文档不在当前列表中')
    return
  }
  versionsVisible.value = false
  openArchive(row)
}

// 选定主文档后自动预填建议版本号（用户可改）
watch(
  () => archiveForm.value.targetPath,
  async (target) => {
    if (!target || !archiveVisible.value) return
    try {
      const data = await listDocumentVersions(target)
      archiveForm.value.versionNo = data.suggested_version_no || 'v1'
    } catch {
      // 预填失败保留当前值
    }
  },
)

async function saveArchive() {
  const form = archiveForm.value
  if (!form.targetPath) {
    ElMessage.warning('请选择归属主文档')
    return
  }
  if (!form.versionNo.trim()) {
    ElMessage.warning('请填写版本号')
    return
  }
  archiveSaving.value = true
  try {
    await archiveDocumentVersion({
      source_path: form.sourcePath,
      target_path: form.targetPath,
      version_no: form.versionNo.trim(),
      note: form.note.trim(),
    })
    archiveVisible.value = false
    ElMessage.success(`已归档为《${form.targetPath}》的历史版本`)
    await refresh()
    if (versionsVisible.value) await loadVersions(versionsData.value.doc_relative_path)
  } catch (error) {
    const detail = error.response?.data?.detail
    ElMessage.error(typeof detail === 'string' ? detail : '归档失败')
  } finally {
    archiveSaving.value = false
  }
}

// ---------------- 恢复 / 删除历史版本 ----------------

function openRestore(row) {
  restoreForm.value = {
    versionId: row.id,
    versionNo: row.version_no,
    newVersionNo: versionsData.value.suggested_version_no || 'v1',
    note: '',
  }
  restoreVisible.value = true
}

async function saveRestore() {
  const form = restoreForm.value
  if (!form.newVersionNo.trim()) {
    ElMessage.warning('请为被替换的当前版本指定版本号')
    return
  }
  restoreSaving.value = true
  try {
    const result = await restoreDocumentVersion(form.versionId, {
      new_version_no: form.newVersionNo.trim(),
      note: form.note.trim(),
    })
    restoreVisible.value = false
    ElMessage.success('已恢复历史版本并重建索引')
    await refresh()
    await loadVersions(result.new_relative_path || versionsData.value.doc_relative_path)
  } catch (error) {
    const detail = error.response?.data?.detail
    ElMessage.error(typeof detail === 'string' ? detail : '恢复失败')
  } finally {
    restoreSaving.value = false
  }
}

async function handleDeleteVersion(row) {
  try {
    await ElMessageBox.confirm(
      `确定彻底删除历史版本「${row.version_no}」（${row.original_name}）吗？归档文件将被删除，不可恢复。`,
      '提示',
      { type: 'warning' },
    )
  } catch {
    return
  }
  try {
    const data = await deleteDocumentVersion(row.id)
    versionsData.value = { ...versionsData.value, versions: data.versions || [] }
    ElMessage.success('已删除该历史版本')
  } catch (error) {
    const detail = error.response?.data?.detail
    ElMessage.error(typeof detail === 'string' ? detail : '删除版本失败')
  }
}

onMounted(() => {
  tableLoading.value = true
  refresh().finally(() => {
    tableLoading.value = false
  })
})
</script>

<style scoped>
.knowledge-page {
  height: calc(100vh - 32px);
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: 16px;
}

/* 卡片不被 flex 压缩：文档很多、列表很高时页面整体滚动，
   三张卡片（状态/上传/列表）各自完整显示基本信息 */
.knowledge-page > :deep(.el-card) {
  flex-shrink: 0;
}

.knowledge-page :deep(.el-card) {
  border-radius: 14px;
  border: 1px solid var(--border);
}

.header-row {
  display: flex;
  justify-content: space-between;
  align-items: center;
}

.card-title {
  font-size: 16px;
  font-weight: 600;
}

.status-row {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
}

.data-dir {
  margin-top: 8px;
  color: var(--text-3);
  font-size: 12px;
  display: flex;
  align-items: center;
  gap: 8px;
}

.change-dir-btn {
  flex-shrink: 0;
}

.dir-hint {
  font-size: 12px;
  color: var(--text-3);
  line-height: 1.6;
  background: var(--bg-hover, rgba(128, 128, 128, 0.06));
  border-radius: 8px;
  padding: 8px 10px;
  margin-top: 2px;
}

.upload-button {
  margin-top: 12px;
}

.category-filter {
  width: 160px;
}

/* 文件名前的版本入口 + 可点击预览 */
.version-menu-btn {
  margin-right: 8px;
  flex-shrink: 0;
}

.doc-name {
  color: var(--el-color-primary);
  cursor: pointer;
}

.doc-name:hover {
  text-decoration: underline;
}

/* 操作列不换行：预览 | 分类 | 重命名 | 删除 一行放下 */
.ops-cell .cell {
  white-space: nowrap;
}

.no-category {
  color: var(--text-3);
  font-size: 12px;
}

.doc-tags {
  color: var(--text-3);
  font-size: 12px;
}

.meta-file {
  font-size: 13px;
  color: var(--text-2);
  word-break: break-all;
}

/* 版本管理 / 查重提示 */
.versions-current {
  font-size: 13px;
  color: var(--text-2);
  background: var(--bg-hover, rgba(128, 128, 128, 0.06));
  border-radius: 8px;
  padding: 8px 10px;
  margin-bottom: 10px;
}

.conflict-file {
  font-size: 13px;
  color: var(--text-2);
  margin-bottom: 8px;
  word-break: break-all;
}

.conflict-item {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  padding: 4px 0;
}

.conflict-msg {
  font-size: 13px;
  color: var(--text-2);
  line-height: 1.6;
}
</style>
