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
            正在上传并索引，请勿关闭页面...
          </template>
          <template v-else>
            拖拽文件到此处，或<em>点击选择</em>
          </template>
        </div>
        <template #tip>
          <div class="el-upload__tip">
            支持 txt / md / csv / doc / docx / xlsx / pdf / epub，上传后自动增量更新索引
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
        <el-table-column label="文件名" min-width="220">
          <template #default="{ row }">
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
        <el-table-column label="操作" width="180" align="center">
          <template #default="{ row }">
            <el-button type="primary" link @click="handlePreview(row)">预览</el-button>
            <el-button type="primary" link @click="openMetaEdit(row)">分类</el-button>
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
import { computed, onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { UploadFilled, Loading } from '@element-plus/icons-vue'
import DocumentViewer from '../components/DocumentViewer.vue'
import {
  deleteDocument,
  getIndexStatus,
  getDocumentMeta,
  listDocuments,
  rebuildIndex,
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
  try {
    const result = await uploadDocuments(files)
    ElMessage.success(`已上传 ${files.length} 个文件并更新索引`)
    // 清空上传组件内部的文件列表
    uploadRef.value?.clearFiles()
    fileList.value = []
    await refresh()
  } catch (error) {
    ElMessage.error(error.response?.data?.detail || '上传失败')
  } finally {
    uploading.value = false
  }
}

async function handleDelete(row) {
  try {
    await ElMessageBox.confirm(`确定删除「${row.name}」吗？删除后会重建索引。`, '提示', {
      type: 'warning',
    })
  } catch {
    return // 用户取消
  }
  try {
    await deleteDocument(row.relative_path)
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

/* 文件名可点击预览 */
.doc-name {
  color: var(--el-color-primary);
  cursor: pointer;
}

.doc-name:hover {
  text-decoration: underline;
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
</style>
