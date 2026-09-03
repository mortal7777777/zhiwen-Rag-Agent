<template>
  <div class="runs-page">
    <!-- 用量统计卡：时间筛选 + 指标卡 + 图表（类 DeepSeek platform 用量页） -->
    <el-card v-if="stats" shadow="never" class="stats-card">
      <template #header>
        <div class="header-row">
          <span class="card-title">用量统计{{ periodLabel }}</span>
          <div class="header-actions">
            <el-radio-group v-model="period" size="small" @change="loadStats">
              <el-radio-button value="today">今天</el-radio-button>
              <el-radio-button value="3d">近 3 天</el-radio-button>
              <el-radio-button value="7d">近 7 天</el-radio-button>
              <el-radio-button value="all">全部</el-radio-button>
            </el-radio-group>
            <el-button size="small" @click="loadStats()">刷新</el-button>
          </div>
        </div>
      </template>
      <div class="stats-grid">
        <div class="stat-cell stat-main">
          <div class="stat-value">{{ fmtTokens(stats.input_tokens + stats.output_tokens) }}</div>
          <div class="stat-label">消耗 tokens</div>
          <div class="stat-detail">
            输入 {{ fmtTokens(stats.input_tokens) }} · 输出 {{ fmtTokens(stats.output_tokens) }}
          </div>
        </div>
        <div class="stat-cell">
          <div class="stat-value">¥{{ stats.estimated_cost_yuan?.toFixed(2) ?? '0.00' }}</div>
          <div class="stat-label">估算成本（非账单）</div>
        </div>
        <div class="stat-cell">
          <div class="stat-value">
            {{ stats.cache_hit_rate != null ? (stats.cache_hit_rate * 100).toFixed(0) + '%' : '-' }}
          </div>
          <div class="stat-label">缓存命中率</div>
          <div class="stat-detail">
            命中 {{ fmtTokens(stats.cache_hit) }} · 未命中 {{ fmtTokens(stats.cache_miss) }}
          </div>
        </div>
        <div class="stat-cell">
          <div class="stat-value">{{ stats.ok_rate != null ? (stats.ok_rate * 100).toFixed(0) + '%' : '-' }}</div>
          <div class="stat-label">成功率（{{ stats.ok_runs }}/{{ stats.runs }}）</div>
        </div>
        <div class="stat-cell">
          <div class="stat-value">{{ formatMs(stats.avg_latency_ms) }}</div>
          <div class="stat-label">平均耗时</div>
        </div>
        <div class="stat-cell">
          <div class="stat-value">{{ stats.runs }}</div>
          <div class="stat-label">运行次数</div>
        </div>
      </div>
      <!-- 图表区：每日 token 趋势 / 成本与缓存命中率趋势 -->
      <div v-if="stats.daily && stats.daily.length" class="charts-row">
        <div ref="tokensChartEl" class="chart-box" />
        <div ref="costChartEl" class="chart-box" />
      </div>
    </el-card>

    <el-card shadow="never" class="runs-card">
      <template #header>
        <div class="header-row">
          <span class="card-title">Agent 运行记录</span>
          <el-button v-if="!stats" size="small" @click="load">刷新</el-button>
        </div>
      </template>
      <el-table v-loading="loading" :data="runs" stripe>
        <el-table-column prop="id" label="ID" width="70" />
        <el-table-column label="会话" width="160" show-overflow-tooltip>
          <template #default="{ row }">{{ convLabel(row) }}</template>
        </el-table-column>
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

        <!-- 分阶段耗时：prepare/子代理/模型/工具/TTFT（token_usage.timings） -->
        <template v-if="detailTimings">
          <div class="detail-label">分阶段耗时（总 {{ formatMs(detail.latency_ms) }}）</div>
          <div class="detail-timings">
            <div
              v-for="item in detailTimingItems"
              :key="item.key"
              class="timing-row"
            >
              <span class="timing-name">{{ item.label }}</span>
              <div class="timing-bar">
                <i :style="{ width: item.pct }" />
              </div>
              <span class="timing-value">{{ formatMs(item.value) }}</span>
              <span class="timing-pct">{{ item.pct }}</span>
            </div>
          </div>
        </template>

        <!-- Token 用量 + 缓存命中（token_usage：主循环 + 辅助调用分开统计） -->
        <template v-if="detailUsage">
          <div class="detail-label">Token 用量</div>
          <div class="usage-grid">
            <div class="usage-cell">
              <div class="usage-value">{{ detailUsage.llm_calls ?? '-' }}</div>
              <div class="usage-label">主循环 LLM 调用</div>
            </div>
            <div class="usage-cell">
              <div class="usage-value">{{ fmtTokens(detailUsage.prompt_tokens) }}</div>
              <div class="usage-label">输入 tokens</div>
            </div>
            <div class="usage-cell">
              <div class="usage-value">{{ fmtTokens(detailUsage.completion_tokens) }}</div>
              <div class="usage-label">输出 tokens</div>
            </div>
            <div class="usage-cell">
              <div class="usage-value">
                {{
                  detailUsage.cache_hit_rate != null
                    ? (detailUsage.cache_hit_rate * 100).toFixed(0) + '%'
                    : '-'
                }}
              </div>
              <div class="usage-label">主循环缓存命中率</div>
            </div>
          </div>
          <div class="usage-sub">
            <template v-if="detailUsage.aux_llm_calls">
              辅助调用（标题/计划/摘要/记忆等）：{{ detailUsage.aux_llm_calls }} 次 ·
              {{ fmtTokens(detailUsage.aux_prompt_tokens) }} 入 /
              {{ fmtTokens(detailUsage.aux_completion_tokens) }} 出
            </template>
            <template v-else>无辅助调用</template>
          </div>
          <div v-if="detailUsage.cache_hit_tokens != null" class="usage-sub">
            缓存明细：命中 {{ fmtTokens(detailUsage.cache_hit_tokens) }} ·
            未命中 {{ fmtTokens(detailUsage.cache_miss_tokens) }}
          </div>
          <div v-if="lastCallCache" class="usage-sub">
            末次调用缓存：{{ fmtTokens(lastCallCache.read) }} 读 /
            {{ fmtTokens(lastCallCache.total) }} 总
          </div>
        </template>

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
import { computed, nextTick, onBeforeUnmount, onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import * as echarts from 'echarts'
import { getRunTrace, getRunStats, listConversations, listRuns } from '../api'

defineOptions({ name: 'RunsView' })

const runs = ref([])
const loading = ref(false)
const stats = ref(null)
const period = ref('7d')
const detailVisible = ref(false)
const detail = ref(null)
const traceJson = ref('')
const convTitles = ref({})
const tokensChartEl = ref(null)
const costChartEl = ref(null)
let tokensChart = null
let costChart = null

const PERIOD_LABELS = { today: '（今日）', '3d': '（近 3 天）', '7d': '（近 7 天）', all: '（全部）' }
const periodLabel = computed(() => PERIOD_LABELS[period.value] || '')

function fmtTokens(n) {
  if (n == null) return '-'
  if (n >= 1000000) return `${(n / 1000000).toFixed(1)}M`
  if (n >= 1000) return `${(n / 1000).toFixed(1)}k`
  return String(n)
}

// ---------- 图表（echarts，DeepSeek platform 用量页风格） ----------

function renderCharts() {
  if (!stats.value?.daily?.length) return
  const daily = stats.value.daily
  const dates = daily.map((d) => d.date)
  const input = daily.map((d) => d.input_tokens)
  const output = daily.map((d) => d.output_tokens)
  const totalTok = daily.map((d) => d.input_tokens + d.output_tokens)
  const cost = daily.map((d) => Number((d.cost || 0).toFixed(3)))
  const hitRate = daily.map((d) => {
    const all = (d.cache_hit || 0) + (d.cache_miss || 0)
    return all > 0 ? Number(((d.cache_hit / all) * 100).toFixed(1)) : null
  })
  const runsPerDay = daily.map((d) => d.runs)
  const axis = { type: 'category', data: dates, axisLabel: { fontSize: 11 } }
  const tooltip = { trigger: 'axis' }

  if (tokensChartEl.value) {
    tokensChart = tokensChart || echarts.init(tokensChartEl.value)
    tokensChart.setOption(
      {
        tooltip: {
          ...tooltip,
          valueFormatter: (v) => (v == null ? '-' : `${fmtTokens(v)} tokens`),
        },
        legend: { top: 0, textStyle: { fontSize: 12 } },
        grid: { left: 8, right: 8, top: 32, bottom: 4, containLabel: true },
        xAxis: axis,
        yAxis: { type: 'value', axisLabel: { formatter: (v) => fmtTokens(v) } },
        series: [
          {
            name: '输入 tokens',
            type: 'bar',
            stack: 'total',
            data: input,
            itemStyle: { color: '#5470c6', opacity: 0.85 },
          },
          {
            name: '输出 tokens',
            type: 'bar',
            stack: 'total',
            data: output,
            itemStyle: { color: '#91cc75', opacity: 0.9 },
          },
          {
            name: '总消耗',
            type: 'line',
            smooth: true,
            data: totalTok,
            lineStyle: { width: 2, color: '#fac858' },
            itemStyle: { color: '#fac858' },
          },
        ],
      },
      true,
    )
  }
  if (costChartEl.value) {
    costChart = costChart || echarts.init(costChartEl.value)
    costChart.setOption(
      {
        tooltip: { trigger: 'axis' },
        legend: { top: 0, textStyle: { fontSize: 12 } },
        grid: { left: 8, right: 8, top: 32, bottom: 4, containLabel: true },
        xAxis: axis,
        yAxis: [
          { type: 'value', name: '成本 ¥', nameTextStyle: { fontSize: 11 } },
          {
            type: 'value',
            name: '命中率 %',
            max: 100,
            nameTextStyle: { fontSize: 11 },
          },
          { type: 'value', show: false },
        ],
        series: [
          {
            name: '估算成本（¥）',
            type: 'bar',
            data: cost,
            itemStyle: { color: '#ee6666', opacity: 0.8 },
            barMaxWidth: 22,
          },
          {
            name: '缓存命中率',
            type: 'line',
            smooth: true,
            yAxisIndex: 1,
            connectNulls: true,
            data: hitRate,
            lineStyle: { width: 2, color: '#3ba272' },
            itemStyle: { color: '#3ba272' },
          },
          {
            name: '运行次数',
            type: 'line',
            smooth: true,
            yAxisIndex: 2,
            showSymbol: false,
            data: runsPerDay,
            lineStyle: { width: 1.5, type: 'dashed', color: '#999' },
            itemStyle: { color: '#999' },
          },
        ],
      },
      true,
    )
  }
}

function onResize() {
  tokensChart?.resize()
  costChart?.resize()
}

// 分阶段耗时的展示顺序与中文名（token_usage.timings，旧数据无此字段则不显示）
const TIMING_LABELS = [
  ['first_token_ms', '首 Token（TTFT）'],
  ['prepare_ms', '准备（记忆/规划）'],
  ['dispatch_ms', '子任务分派'],
  ['subagent_ms', '子代理执行'],
  ['merge_ms', '结果合并'],
  ['agent_ms', '模型生成'],
  ['tools_ms', '工具执行'],
  ['finalize_ms', '收尾落库'],
]

const detailTimings = computed(() => detail.value?.token_usage?.timings || null)

// Token 用量 + 主循环缓存命中率（token_usage 顶层字段）
const detailUsage = computed(() => {
  const usage = detail.value?.token_usage
  if (!usage || typeof usage !== 'object') return null
  const hit = usage.cache_hit_tokens ?? 0
  const miss = usage.cache_miss_tokens ?? 0
  return {
    llm_calls: usage.llm_calls,
    prompt_tokens: usage.prompt_tokens,
    completion_tokens: usage.completion_tokens,
    cache_hit_tokens: usage.cache_hit_tokens,
    cache_miss_tokens: usage.cache_miss_tokens,
    aux_llm_calls: usage.aux_llm_calls,
    aux_prompt_tokens: usage.aux_prompt_tokens,
    aux_completion_tokens: usage.aux_completion_tokens,
    // 缓存命中率 = 命中/(命中+未命中)，无缓存数据时 null
    cache_hit_rate: hit + miss > 0 ? hit / (hit + miss) : null,
  }
})

// 末次 LLM 调用的缓存详情（last_call.input_token_details.cache_read）
const lastCallCache = computed(() => {
  const last = detail.value?.token_usage?.last_call
  if (!last || typeof last !== 'object') return null
  const details = last.input_token_details
  const read =
    details?.cache_read ??
    last.prompt_cache_hit_tokens ??
    last.prompt_tokens_details?.cached_tokens
  if (read == null) return null
  return {
    read,
    total: last.input_tokens ?? last.prompt_tokens ?? 0,
  }
})

const detailTimingItems = computed(() => {
  const timings = detailTimings.value
  if (!timings || !detail.value) return []
  const total = Math.max(1, detail.value.latency_ms || 0)
  const items = []
  for (const [key, label] of TIMING_LABELS) {
    const value = timings[key]
    if (value == null) continue
    items.push({
      key,
      label,
      value,
      pct: `${Math.min(100, Math.round((value / total) * 100))}%`,
    })
  }
  return items
})

function formatMs(ms) {
  if (ms == null) return '-'
  if (ms < 1000) return `${ms}ms`
  return `${(ms / 1000).toFixed(1)}s`
}

async function loadStats() {
  if (!stats.value) return // 首次加载由 load() 统一拉取（含运行列表）
  try {
    const runStats = await getRunStats(7, period.value)
    stats.value = runStats
    await nextTick()
    renderCharts()
  } catch (error) {
    ElMessage.error(error.response?.data?.detail || '统计加载失败')
  }
}

async function load() {
  loading.value = true
  try {
    const [runList, convList, runStats] = await Promise.all([
      listRuns({ limit: 200 }),
      listConversations().catch(() => []),
      getRunStats(7, period.value).catch(() => null),
    ])
    runs.value = runList
    stats.value = runStats
    const map = {}
    for (const c of convList || []) {
      map[c.id] = c.title || `会话 ${c.id}`
    }
    convTitles.value = map
    await nextTick()
    renderCharts()
  } catch (error) {
    ElMessage.error(error.response?.data?.detail || '加载失败')
  } finally {
    loading.value = false
  }
}

function convLabel(row) {
  const id = row.conversation_id
  if (id == null) return '-'
  return convTitles.value[id] || `会话 ${id}`
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

onMounted(() => {
  load()
  window.addEventListener('resize', onResize)
})

onBeforeUnmount(() => {
  window.removeEventListener('resize', onResize)
  tokensChart?.dispose()
  costChart?.dispose()
  tokensChart = null
  costChart = null
})
</script>

<style scoped>
.runs-page {
  height: calc(100vh - 32px);
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: 16px;
}

/* 卡片不被 flex 压缩（对齐 KnowledgeView）：统计卡 + 运行列表各自完整
   显示，内容高时页面整体滚动，而不是压缩卡片导致显示不全 */
.runs-page > :deep(.el-card) {
  flex-shrink: 0;
}

.runs-page :deep(.el-card) {
  border-radius: 14px;
  border: 1px solid var(--border);
}

/* 用量统计卡 */
.stats-card {
  margin-bottom: 0;
}

.stats-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
  gap: 12px;
}

.stat-cell {
  display: flex;
  flex-direction: column;
  gap: 3px;
  padding: 10px 14px;
  background: var(--bg-hover, rgba(128, 128, 128, 0.06));
  border-radius: 12px;
}

.stat-main {
  background: linear-gradient(
    135deg,
    rgba(64, 158, 255, 0.12),
    rgba(64, 158, 255, 0.03)
  );
}

.stat-value {
  font-size: 24px;
  font-weight: 700;
  color: var(--text-1);
  font-variant-numeric: tabular-nums;
}

.stat-label {
  font-size: 12px;
  color: var(--text-3);
}

.stat-detail {
  font-size: 11.5px;
  color: var(--text-3);
  margin-top: 2px;
}

.header-actions {
  display: flex;
  align-items: center;
  gap: 10px;
}

.charts-row {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));
  gap: 14px;
  margin-top: 16px;
}

.chart-box {
  height: 240px;
  width: 100%;
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

/* 分阶段耗时：名称 + 占比条 + 数值 */
.detail-timings {
  display: flex;
  flex-direction: column;
  gap: 5px;
  margin-bottom: 12px;
}

/* Token 用量卡片网格 */
.usage-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(130px, 1fr));
  gap: 10px;
  margin-bottom: 8px;
}

.usage-cell {
  display: flex;
  flex-direction: column;
  gap: 2px;
  background: var(--bg-hover, rgba(128, 128, 128, 0.06));
  border-radius: 10px;
  padding: 8px 12px;
}

.usage-value {
  font-size: 17px;
  font-weight: 600;
  color: var(--text-1);
  font-variant-numeric: tabular-nums;
}

.usage-label {
  font-size: 11.5px;
  color: var(--text-3);
}

.usage-sub {
  font-size: 12px;
  color: var(--text-3);
  margin-top: 4px;
}

.timing-row {
  display: grid;
  grid-template-columns: 130px 1fr 64px 40px;
  align-items: center;
  gap: 8px;
  font-size: 12px;
}

.timing-name {
  color: var(--text-3, #909399);
}

.timing-bar {
  height: 6px;
  border-radius: 4px;
  background: rgba(128, 128, 128, 0.12);
  overflow: hidden;
}

.timing-bar i {
  display: block;
  height: 100%;
  border-radius: 4px;
  background: linear-gradient(90deg, var(--el-color-primary, #409eff), #67c23a);
}

.timing-value {
  text-align: right;
  font-variant-numeric: tabular-nums;
  color: var(--text-2, #606266);
}

.timing-pct {
  text-align: right;
  color: var(--text-3, #909399);
  font-size: 11px;
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
