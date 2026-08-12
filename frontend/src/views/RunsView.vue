<template>
  <div class="runs-page">
    <el-card shadow="never" class="runs-card">
      <template #header>
        <div class="header-row">
          <span class="card-title">Agent 运行记录</span>
          <el-button size="small" @click="load">刷新</el-button>
        </div>
      </template>
      <el-table v-loading="loading" :data="runs" stripe>
        <el-table-column prop="id" label="ID" width="70" />
        <el-table-column prop="question" label="问题" min-width="220" show-overflow-tooltip />
        <el-table-column label="状态" width="90">
          <template #default="{ row }">
            <el-tag
              size="small"
              :type="row.status === 'ok' ? 'success' : row.status === 'stopped' ? 'warning' : 'danger'"
            >
              {{ row.status }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="工具" width="70">
          <template #default="{ row }">{{ (row.tool_trace || []).length }}</template>
        </el-table-column>
        <el-table-column label="耗时" width="100">
          <template #default="{ row }">{{ formatMs(row.latency_ms) }}</template>
        </el-table-column>
        <el-table-column label="tokens" width="110">
          <template #default="{ row }">
            {{ (row.token_usage && row.token_usage.total_tokens) ?? '-' }}
          </template>
        </el-table-column>
        <el-table-column prop="created_at" label="时间" width="170" />
        <el-table-column label="操作" width="90" align="center">
          <template #default="{ row }">
            <el-button size="small" link type="primary" @click="showDetail(row)">详情</el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <el-dialog v-model="detailVisible" title="运行详情" width="760px" append-to-body>
      <div v-if="detail" class="detail-body">
        <div class="detail-label">问题</div>
        <div class="detail-text">{{ detail.question }}</div>

        <div v-if="detail.plan && detail.plan.length" class="detail-label">执行计划</div>
        <ol v-if="detail.plan && detail.plan.length" class="detail-steps">
          <li v-for="(s, i) in detail.plan" :key="i">{{ s }}</li>
        </ol>

        <div v-if="detail.error" class="detail-label">错误</div>
        <div v-if="detail.error" class="detail-error">{{ detail.error }}</div>

        <div class="detail-label">工具轨迹</div>
        <div v-if="!detail.tool_trace || !detail.tool_trace.length" class="detail-empty">
          无工具调用
        </div>
        <div v-for="(t, i) in detail.tool_trace" :key="i" class="detail-tool">
          <span class="dt-step">{{ t.step }}</span>
          <span class="dt-name">{{ t.name }}</span>
          <span class="dt-summary">{{ t.summary || JSON.stringify(t.arguments || {}) }}</span>
          <span class="dt-dur">{{ t.duration_ms != null ? t.duration_ms + 'ms' : '' }}</span>
        </div>

        <div class="detail-label">Trace（JSONL）</div>
        <el-input
          :model-value="traceJson"
          type="textarea"
          :rows="8"
          readonly
          class="trace-textarea"
        />
      </div>
    </el-dialog>
  </div>
</template>

<script setup>
import { onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { getRunTrace, listRuns } from '../api'

defineOptions({ name: 'RunsView' })

const runs = ref([])
const loading = ref(false)
const detailVisible = ref(false)
const detail = ref(null)
const traceJson = ref('')

function formatMs(ms) {
  if (ms == null) return '-'
  if (ms < 1000) return `${ms}ms`
  return `${(ms / 1000).toFixed(1)}s`
}

async function load() {
  loading.value = true
  try {
    runs.value = await listRuns({ limit: 100 })
  } catch (error) {
    ElMessage.error(error.response?.data?.detail || '加载失败')
  } finally {
    loading.value = false
  }
}

async function showDetail(row) {
  detail.value = row
  detailVisible.value = true
  traceJson.value = ''
  try {
    const trace = await getRunTrace(row.id)
    traceJson.value = JSON.stringify(trace, null, 2)
  } catch {
    traceJson.value = '（trace 不可用）'
  }
}

onMounted(load)
</script>

<style scoped>
.runs-page {
  height: calc(100vh - 32px);
  overflow-y: auto;
}

.runs-card :deep(.el-card) {
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
  color: var(--text-1);
}

.detail-body {
  max-height: 60vh;
  overflow-y: auto;
}

.detail-label {
  font-size: 12.5px;
  font-weight: 600;
  color: var(--text-3);
  margin: 12px 0 6px;
}

.detail-text {
  font-size: 14px;
  color: var(--text-1);
  line-height: 1.7;
}

.detail-steps {
  margin: 0;
  padding-left: 20px;
}

.detail-steps li {
  font-size: 13px;
  color: var(--text-2);
  line-height: 1.8;
}

.detail-error {
  font-size: 13px;
  color: var(--danger);
  background: var(--danger-bg);
  border: 1px solid var(--danger-border);
  border-radius: 8px;
  padding: 8px 12px;
}

.detail-empty {
  font-size: 12.5px;
  color: var(--text-3);
  padding: 6px 0;
}

.detail-tool {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 7px 10px;
  border: 1px solid var(--border);
  border-radius: 8px;
  margin-bottom: 6px;
  font-size: 12.5px;
}

.dt-step {
  width: 20px;
  height: 20px;
  flex-shrink: 0;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border-radius: 6px;
  background: var(--bg-active);
  color: var(--primary);
  font-weight: 600;
  font-size: 11px;
}

.dt-name {
  font-weight: 600;
  color: var(--text-1);
  flex-shrink: 0;
}

.dt-summary {
  flex: 1;
  min-width: 0;
  color: var(--text-2);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.dt-dur {
  flex-shrink: 0;
  color: var(--text-3);
  font-size: 11.5px;
}

.trace-textarea :deep(.el-textarea__inner) {
  font-family: Consolas, Monaco, monospace;
  font-size: 12px;
  line-height: 1.6;
}
</style>
