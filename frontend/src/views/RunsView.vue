<template>
  <div :class="['runs-page', viewClass]">
    <!-- 用量统计卡：时间筛选 + 指标卡 + 图表（类 DeepSeek platform 用量页） -->
    <el-card v-if="stats" shadow="never" class="stats-card">
      <template #header>
        <div class="header-row">
          <span class="card-title">用量统计{{ periodLabel }}</span>
          <div class="header-actions">
            <ViewLayoutSwitch v-model="runsView" :options="runsViewOptions" />
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
            <el-button size="small" link type="primary" @click="openReplay(row)">回放</el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <RunReplayDrawer
      v-model="detailVisible"
      :run-id="detailId"
      :conversation-title="detailConvTitle"
    />
  </div>
</template>

<script setup>
import { computed, nextTick, onActivated, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { ElMessage } from 'element-plus/es/components/message/index'
// echarts 按需引入（只用到 bar/line + 坐标轴/图例/提示框/Canvas 渲染器）：
// 全量引入会把 RunsView 的 chunk 推到 1.1MB+，按需后约 300KB
import * as echarts from 'echarts/core'
import { BarChart, LineChart } from 'echarts/charts'
import {
  GridComponent,
  LegendComponent,
  TooltipComponent,
} from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'

echarts.use([
  BarChart,
  LineChart,
  GridComponent,
  LegendComponent,
  TooltipComponent,
  CanvasRenderer,
])
import { getRunStats, listConversations, listRuns } from '../api'
import RunReplayDrawer from '../components/runs/RunReplayDrawer.vue'
import { Grid, List, TrendCharts } from '@element-plus/icons-vue'
import ViewLayoutSwitch from '../components/ViewLayoutSwitch.vue'
import { usePageLayout } from '../composables/usePageLayout'

defineOptions({ name: 'RunsView' })

const runs = ref([])
// 上次成功加载时间（初始视为刚加载，避免首次 onActivated 重复拉取）
let lastLoadedAt = Date.now()

const loading = ref(false)
const stats = ref(null)

// 页面布局变体（卡片 / 紧凑 / 图表优先），差异见 styles/view-variants.css
const runsViewOptions = [
  { value: 'cards', label: '卡片', icon: Grid, hint: '卡片：指标块 + 图表 + 明细表' },
  { value: 'dense', label: '紧凑', icon: List, hint: '紧凑：6 个指标压成一行，双图并排，表格行高更小' },
  { value: 'focus', label: '图表优先', icon: TrendCharts, hint: '图表优先：图表加高到 320px，指标区收紧' },
]
const { variant: runsView, viewClass } = usePageLayout('rag_runs_view', runsViewOptions, 'cards')

// 变体改了栅格 → 图表容器尺寸变了，派发一次 resize 让 ECharts 重新量
watch(runsView, () => {
  nextTick(() => window.dispatchEvent(new Event('resize')))
})
const period = ref('7d')
// 快速切换时间筛选时的请求序号：只有最后一次请求的响应能落到 stats/图表上
let statsSeq = 0
const detailVisible = ref(false)
const detailId = ref(null)
const convTitles = ref({})
const tokensChartEl = ref(null)
const costChartEl = ref(null)
let tokensChart = null
let costChart = null
let themeObserver = null
let chartObserver = null

const PERIOD_LABELS = { today: '（今日）', '3d': '（近 3 天）', '7d': '（近 7 天）', all: '（全部）' }
const periodLabel = computed(() => PERIOD_LABELS[period.value] || '')

function fmtTokens(n) {
  if (n == null) return '-'
  if (n >= 1000000) return `${(n / 1000000).toFixed(1)}M`
  if (n >= 1000) return `${(n / 1000).toFixed(1)}k`
  return String(n)
}

// ---------- 图表（echarts，DeepSeek platform 用量页风格） ----------

/** 图表配色取自设计令牌（主题切换时经 MutationObserver 重绘）。 */
function chartPalette() {
  const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim()
  return {
    primary: css('--primary') || '#1677ff',
    success: css('--success') || '#2eb872',
    warning: css('--warning') || '#e8912d',
    danger: css('--danger') || '#f56c6c',
    muted: css('--text-3') || '#646b78',
    // 图表 chrome（2026-11-19 V4）：ECharts 默认是浅色皮（轴标签 #54555a
    // 压深底仅 2.27:1、网格线 #dbdee4 在暗底上反而刺眼），这里全部改取令牌。
    text: css('--text-2') || '#4c515b',
    axis: css('--border-strong') || '#dde1ea',
    grid: css('--border') || '#e8ebf1',
    tooltipBg: css('--bg-panel') || '#ffffff',
    tooltipBorder: css('--border-strong') || '#dde1ea',
    tooltipText: css('--text-1') || '#23262d',
    font: css('--font-display') || 'inherit',
  }
}

/** 容器尺寸变化（侧栏折叠/窗口缩放）时重绘，避免画布保持旧宽度。 */
function ensureChartObserver() {
  if (chartObserver || !tokensChartEl.value || !costChartEl.value) return
  chartObserver = new ResizeObserver(() => {
    tokensChart?.resize()
    costChart?.resize()
  })
  chartObserver.observe(tokensChartEl.value)
  chartObserver.observe(costChartEl.value)
}

/**
 * 容器存在性 + 实例与容器一致性检查。
 * 卡片重渲染（v-if / keep-alive）后 el 会换人，但缓存的实例还绑在旧节点上，
 * 表现就是"图表突然不见了"。这里发现不一致就 dispose 重建。
 */
function ensureChartInstances() {
  if (!tokensChartEl.value || !costChartEl.value) return false
  if (tokensChart && tokensChart.getDom() !== tokensChartEl.value) {
    tokensChart.dispose()
    tokensChart = null
  }
  if (costChart && costChart.getDom() !== costChartEl.value) {
    costChart.dispose()
    costChart = null
  }
  return true
}

function renderCharts(retry = 0) {
  // 容器还没挂上（刚切回 keep-alive / 统计卡刚渲染）：等一拍再试，最多 5 次
  if (!ensureChartInstances()) {
    if (retry < 5) nextTick(() => renderCharts(retry + 1))
    return
  }
  ensureChartObserver()
  // 空区间也要画出坐标轴（以前直接 return，快速切换后会留下上一区间的旧图）
  const daily = stats.value?.daily || []
  if (!daily.length) {
    const empty = { title: { text: '该区间暂无数据', left: 'center', top: 'middle',
      textStyle: { color: chartPalette().muted, fontSize: 12, fontWeight: 400 } } }
    tokensChart = tokensChart || echarts.init(tokensChartEl.value)
    costChart = costChart || echarts.init(costChartEl.value)
    tokensChart.clear()
    costChart.clear()
    tokensChart.setOption(empty, true)
    costChart.setOption(empty, true)
    return
  }
  // daily 已在函数开头解析（含空区间兜底）
  const dates = daily.map((d) => d.label || d.date)
  const input = daily.map((d) => d.input_tokens)
  const output = daily.map((d) => d.output_tokens)
  const totalTok = daily.map((d) => d.input_tokens + d.output_tokens)
  const cost = daily.map((d) => Number((d.cost || 0).toFixed(3)))
  const hitRate = daily.map((d) => {
    const all = (d.cache_hit || 0) + (d.cache_miss || 0)
    return all > 0 ? Number(((d.cache_hit / all) * 100).toFixed(1)) : null
  })
  const runsPerDay = daily.map((d) => d.runs)
  const C = chartPalette()
  const axisStyle = {
    axisLabel: { fontSize: 11, color: C.muted },
    axisLine: { lineStyle: { color: C.axis } },
    axisTick: { lineStyle: { color: C.axis } },
  }
  const valueAxis = {
    type: 'value',
    axisLabel: { fontSize: 11, color: C.muted },
    splitLine: { lineStyle: { color: C.grid } },
  }
  const axis = { type: 'category', data: dates, ...axisStyle }
  const tooltip = {
    trigger: 'axis',
    backgroundColor: C.tooltipBg,
    borderColor: C.tooltipBorder,
    borderWidth: 1,
    padding: [6, 10],
    textStyle: { color: C.tooltipText, fontSize: 12, fontFamily: C.font },
    extraCssText: 'box-shadow: var(--shadow-pop); border-radius: var(--radius-sm);',
  }
  const legend = { top: 0, textStyle: { fontSize: 12, color: C.text, fontFamily: C.font } }
  const base = { textStyle: { color: C.text, fontFamily: C.font } }

  if (tokensChartEl.value) {
    tokensChart = tokensChart || echarts.init(tokensChartEl.value)
    tokensChart.setOption(
      {
        tooltip: {
          ...tooltip,
          valueFormatter: (v) => (v == null ? '-' : `${fmtTokens(v)} tokens`),
        },
        ...base,
        legend,
        grid: { left: 8, right: 8, top: 32, bottom: 4, containLabel: true },
        xAxis: axis,
        yAxis: { ...valueAxis, axisLabel: { ...valueAxis.axisLabel, formatter: (v) => fmtTokens(v) } },
        series: [
          {
            name: '输入 tokens',
            type: 'bar',
            stack: 'total',
            data: input,
            itemStyle: { color: C.primary, opacity: 0.85 },
          },
          {
            name: '输出 tokens',
            type: 'bar',
            stack: 'total',
            data: output,
            itemStyle: { color: C.success, opacity: 0.9 },
          },
          {
            name: '总消耗',
            type: 'line',
            smooth: true,
            data: totalTok,
            lineStyle: { width: 2, color: C.warning },
            itemStyle: { color: C.warning },
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
        ...base,
        tooltip,
        legend,
        grid: { left: 8, right: 8, top: 32, bottom: 4, containLabel: true },
        xAxis: axis,
        yAxis: [
          { ...valueAxis, name: '成本 ¥', nameTextStyle: { fontSize: 11, color: C.muted } },
          {
            ...valueAxis,
            name: '命中率 %',
            max: 100,
            nameTextStyle: { fontSize: 11, color: C.muted },
          },
          { type: 'value', show: false },
        ],
        series: [
          {
            name: '估算成本（¥）',
            type: 'bar',
            data: cost,
            itemStyle: { color: C.danger, opacity: 0.8 },
            barMaxWidth: 22,
          },
          {
            name: '缓存命中率',
            type: 'line',
            smooth: true,
            yAxisIndex: 1,
            connectNulls: true,
            data: hitRate,
            lineStyle: { width: 2, color: C.success },
            itemStyle: { color: C.success },
          },
          {
            name: '运行次数',
            type: 'line',
            smooth: true,
            yAxisIndex: 2,
            showSymbol: false,
            data: runsPerDay,
            lineStyle: { width: 1.5, type: 'dashed', color: C.muted },
            itemStyle: { color: C.muted },
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

// 布局变体/字体切换后容器可能已变尺寸：setOption 完成再量一次
watch(
  () => [runsView.value, document.documentElement.dataset.font],
  () => nextTick(() => onResize()),
)


function formatMs(ms) {
  if (ms == null) return '-'
  if (ms < 1000) return `${ms}ms`
  return `${(ms / 1000).toFixed(1)}s`
}

async function loadStats() {
  // 不因 stats 为空而跳过：首次拉取失败后统计卡不渲染，再也不能从
  // 时间筛选里重试（只有整页刷新才能恢复）。
  const seq = ++statsSeq
  try {
    const runStats = await getRunStats(7, period.value)
    // 连点筛选时先发的请求可能后到：晚到的旧响应既不能覆盖 stats，
    // 也不能再触发一次 setOption（那会用旧区间把新图表盖掉）
    if (seq !== statsSeq) return
    stats.value = runStats
    lastLoadedAt = Date.now()
    await nextTick()
    renderCharts()
  } catch (error) {
    if (seq !== statsSeq) return
    ElMessage.error(error.response?.data?.detail || '统计加载失败')
  }
}

async function fetchAll({ silent = false } = {}) {
  if (!silent) loading.value = true
  try {
    const [runList, convList, runStats] = await Promise.all([
      listRuns({ limit: 200 }),
      listConversations().catch(() => []),
      getRunStats(7, period.value).catch(() => null),
    ])
    runs.value = runList
    // 统计拉取失败（runStats=null）保留上次数据，别把统计卡整块撤掉
    if (runStats) stats.value = runStats
    const map = {}
    for (const c of convList || []) {
      map[c.id] = c.title || `会话 ${c.id}`
    }
    convTitles.value = map
    lastLoadedAt = Date.now()
    await nextTick()
    renderCharts()
  } catch (error) {
    if (!silent) ElMessage.error(error.response?.data?.detail || '加载失败')
  } finally {
    if (!silent) loading.value = false
  }
}

function load() {
  return fetchAll()
}

/** keep-alive 复用时的静默刷新：失败保留旧数据，不闪 loading。 */
function refreshSilently() {
  return fetchAll({ silent: true })
}

function convLabel(row) {
  const id = row.conversation_id
  if (id == null) return '-'
  return convTitles.value[id] || `会话 ${id}`
}

const detailConvTitle = computed(() => {
  const row = runs.value.find((r) => r.id === detailId.value)
  return row ? convLabel(row) : ''
})

function openReplay(row) {
  detailId.value = row.id
  detailVisible.value = true
}

onMounted(() => {
  load()
  window.addEventListener('resize', onResize)
  // 主题切换（html.dark 改变）时按新令牌重绘图表
  themeObserver = new MutationObserver(() => renderCharts())
  themeObserver.observe(document.documentElement, { attributes: true, attributeFilter: ['class'] })
})

onActivated(() => {
  // keep-alive 复用：先把图表尺寸恢复到当前容器（隐藏期间可能被后台 resize 过）
  nextTick(() => {
    tokensChart?.resize()
    costChart?.resize()
  })
  // 数据超过 TTL 才静默刷新，避免每次切回都重新加载
  if (Date.now() - lastLoadedAt > 60_000) refreshSilently()
})

onBeforeUnmount(() => {
  window.removeEventListener('resize', onResize)
  themeObserver?.disconnect()
  chartObserver?.disconnect()
  chartObserver = null
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
  border-radius: var(--radius-md);
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
  background: var(--bg-hover);
  border-radius: var(--radius-md);
}

.stat-main {
  /* 跟主色走：以前写死品牌蓝 rgba(64,158,255)，换风格后这块高亮会留在蓝调上 */
  background: linear-gradient(
    135deg,
    color-mix(in srgb, var(--primary) 12%, transparent),
    color-mix(in srgb, var(--primary) 3%, transparent)
  );
}

.stat-value {
  font-size: var(--font-2xl);
  font-weight: 700;
  color: var(--text-1);
  font-variant-numeric: tabular-nums;
}

.stat-label {
  font-size: var(--font-xs);
  color: var(--text-3);
}

.stat-detail {
  font-size: var(--font-xs);
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
  border-radius: var(--radius-md);
  border: 1px solid var(--border);
}

.header-row {
  display: flex;
  justify-content: space-between;
  align-items: center;
}

.card-title {
  font-size: var(--font-lg);
  font-weight: 600;
  color: var(--text-1);
}


/* 入场动效由路由过渡统一提供（App.vue 的 page 过渡），避免双重动画 */
</style>
