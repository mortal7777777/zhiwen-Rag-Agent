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
      <div class="data-dir">数据目录：{{ status.data_dir || '-' }}</div>
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
        <span class="card-title">已有文档（{{ documents.length }}）</span>
      </template>
      <el-table v-loading="tableLoading" :data="documents" stripe>
        <el-table-column prop="name" label="文件名" min-width="240" />
        <el-table-column prop="size" label="大小" width="120">
          <template #default="{ row }">{{ formatSize(row.size) }}</template>
        </el-table-column>
        <el-table-column prop="modified" label="修改时间" width="180" />
        <el-table-column label="操作" width="100" align="center">
          <template #default="{ row }">
            <el-button type="danger" link @click="handleDelete(row)">删除</el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-card>
  </div>
</template>

<script setup>
import { onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { UploadFilled, Loading } from '@element-plus/icons-vue'
import {
  deleteDocument,
  getIndexStatus,
  listDocuments,
  rebuildIndex,
  uploadDocuments,
} from '../api'

const documents = ref([])
const status = ref({})
const uploading = ref(false)
const rebuilding = ref(false)
const tableLoading = ref(false)
const uploadRef = ref(null)

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
  } catch (error) {
    ElMessage.error(error.response?.data?.detail || '获取数据失败，请确认后端已启动')
  }
}

async function handleUpload() {
  // 从 el-upload 组件内部获取已选择的文件
  const uploadFiles = uploadRef.value?.uploadFiles ?? []
  const files = uploadFiles.map((item) => item.raw).filter(Boolean)
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
}

.upload-button {
  margin-top: 12px;
}
</style>
