<template>
  <el-drawer
    v-model="visible"
    title="运行回放"
    size="780px"
    append-to-body
  >
    <div v-loading="loading" class="replay-body">
      <el-alert
        v-if="error"
        :title="error"
        type="error"
        :closable="false"
        show-icon
      />
      <template v-if="run">
        <!-- 头部：问题与运行元信息 -->
        <div class="replay-question">{{ run.question }}</div>
        <div class="replay-meta">
          <el-tag
            size="small"
            :type="run.status === 'ok' ? 'success' : run.status === 'stopped' ? 'warning' : 'danger'"
          >
            {{ run.status }}
          </el-tag>
          <span>#{{ run.id }}</span>
          <span v-if="conversationTitle">会话：{{ conversationTitle }}</span>
          <span>{{ run.created_at }}</span>
          <span>耗时 {{ formatMs(run.latency_ms) }}</span>
        </div>

        <!-- 核心指标 -->
        <div class="replay-grid">
          <div class="replay-cell">
            <div class="cell-value">{{ usage.llm_calls ?? '-' }}</div>
            <div class="cell-label">主循环调用</div>
          </div>
          <div class="replay-cell">
            <div class="cell-value">{{ fmtTokens(usage.prompt_tokens) }}</div>
            <div class="cell-label">输入 tokens</div>
          </div>
          <div class="replay-cell">
            <div class="cell-value">{{ fmtTokens(usage.completion_tokens) }}</div>
            <div class="cell-label">输出 tokens</div>
          </div>
          <div class="replay-cell">
            <div class="cell-value">{{ hitRateText }}</div>
            <div class="cell-label">主循环缓存命中</div>
          </div>
          <div class="replay-cell">
            <div class="cell-value">{{ fmtTokens(usage.total_with_aux) }}</div>
            <div class="cell-label">含辅助调用</div>
          </div>
          <div class="replay-cell">
            <div class="cell-value">{{ run.estimated_cost_yuan != null ? '¥' + run.estimated_cost_yuan.toFixed(3) : '-' }}</div>
            <div class="cell-label">估算成本</div>
          </div>
        </div>

        <!-- 分阶段耗时 -->
        <template v-if="timingItems.length">
          <div class="replay-title">分阶段耗时</div>
          <div class="timing-list">
            <div v-for="item in timingItems" :key="item.key" class="timing-row">
              <span class="timing-name">{{ item.label }}</span>
              <div class="timing-bar"><i :style="{ width: item.pct }" /></div>
              <span class="timing-value">{{ formatMs(item.value) }}</span>
            </div>
          </div>
        </template>

        <!-- 逐调用遥测：定位轮内断链 -->
        <template v-if="callRows.length">
          <div class="replay-title">
            逐调用遥测
            <span class="title-hint">命中率 0% 的调用是前缀断链点</span>
          </div>
          <el-table :data="callRows" size="small" class="replay-table">
            <el-table-column prop="index" label="#" width="44" />
            <el-table-column label="相对时刻" width="90">
              <template #default="{ row }">{{ formatMs(row.t_ms) }}</template>
            </el-table-column>
            <el-table-column label="输入" width="90">
              <template #default="{ row }">{{ fmtTokens(row.in) }}</template>
            </el-table-column>
            <el-table-column label="输出" width="90">
              <template #default="{ row }">{{ fmtTokens(row.out) }}</template>
            </el-table-column>
            <el-table-column label="缓存读" width="90">
              <template #default="{ row }">{{ fmtTokens(row.read) }}</template>
            </el-table-column>
            <el-table-column label="命中率" min-width="90">
              <template #default="{ row }">
                <span :class="['hit-rate', { cold: row.read === 0 && row.in > 0 }]">
                  {{ row.hitRate }}
                </span>
              </template>
            </el-table-column>
          </el-table>
        </template>

        <!-- 提示词版本 -->
        <template v-if="prompt">
          <div class="replay-title">提示词版本</div>
          <div class="kv-row">
            <span class="kv-key">模板</span>
            <span class="kv-value">
              {{ prompt.template_name || prompt.template_key || '默认助手' }}
              <template v-if="prompt.template_version_no"> · v{{ prompt.template_version_no }}</template>
            </span>
          </div>
          <div class="kv-row">
            <span class="kv-key">来源</span>
            <span class="kv-value">{{ promptSourceLabel }} · bundle {{ shortHash(prompt.bundle_hash) }}</span>
          </div>
          <div class="kv-row">
            <span class="kv-key">体积</span>
            <span class="kv-value">
              模板 {{ prompt.template_chars ?? '-' }} 字 · 片段 {{ prompt.fragment_chars ?? '-' }} 字
            </span>
          </div>
        </template>

        <!-- 本轮范围与划选引用（只存元数据） -->
        <template v-if="kbScopeMeta || quoteMeta">
          <div class="replay-title">范围与引用</div>
          <div v-if="kbScopeMeta" class="kv-row">
            <span class="kv-key">检索范围</span>
            <span class="kv-value">
              {{ kbScopeMeta.label || '指定范围' }}（{{ kbScopeMeta.count }} 个文件）
              <template v-if="kbScopeMeta.files?.length">
                · {{ kbScopeMeta.files.join('、') }}
              </template>
            </span>
          </div>
          <div v-if="quoteMeta" class="kv-row">
            <span class="kv-key">划选原文</span>
            <span class="kv-value">
              {{ quoteMeta.source || '文档' }}
              <template v-if="quoteMeta.page"> · 第 {{ quoteMeta.page }} 页</template>
              · {{ quoteMeta.chars }} 字
              <template v-if="quoteMeta.context_chars">
                （上下文 {{ quoteMeta.context_chars }} 字）
              </template>
              · {{ quoteMeta.sha1 }}
            </span>
          </div>
        </template>

        <!-- 工具面与缓存前缀 -->
        <template v-if="hasToolSurface || hasChain">
          <div class="replay-title">工具面与缓存前缀</div>
          <div v-if="hasToolSurface" class="kv-row">
            <span class="kv-key">工具面</span>
            <span class="kv-value">
              {{ fmtTokens(usage.tools_chars) }} 字符 · hash {{ shortHash(usage.tools_hash) }}
              <template v-if="usage.tools_breakdown">
                （核心 {{ usage.tools_breakdown.core_count }} 个 /
                MCP {{ usage.tools_breakdown.mcp_count }} 个）
              </template>
            </span>
          </div>
          <div v-if="hasChain" class="kv-row">
            <span class="kv-key">消息链</span>
            <span class="kv-value">
              {{ usage.chain_len ?? '-' }} 条 ·
              与上一轮首处分歧 {{ usage.chain_divergence == null ? '无' : `#${usage.chain_divergence}` }} ·
              {{ (usage.chain_hashes || []).length }} 个指纹
            </span>
          </div>
        </template>

        <!-- 工具轨迹（可展开完整输出） -->
        <div class="replay-title">工具轨迹（{{ toolTrace.length }}）</div>
        <div v-if="!toolTrace.length" class="empty-hint">无工具调用</div>
        <div v-else class="tool-list">
          <div v-for="(t, i) in toolTrace" :key="i" class="tool-item">
            <div class="tool-head" @click="toggleTool(i)">
              <span class="tool-step">{{ t.step ?? i + 1 }}</span>
              <span class="tool-name">{{ toolName(t.name) }}</span>
              <span class="tool-summary">{{ t.summary || JSON.stringify(t.arguments || {}) }}</span>
              <span class="tool-dur">{{ t.duration_ms != null ? t.duration_ms + 'ms' : '' }}</span>
              <span v-if="t.detail" class="tool-arrow">{{ openTools.has(i) ? '▾' : '▸' }}</span>
            </div>
            <pre v-if="t.detail && openTools.has(i)" class="tool-detail">{{ t.detail }}</pre>
          </div>
        </div>

        <!-- 写后回读 / 答复核对 -->
        <template v-if="mutationLedger.length">
          <div class="replay-title">写后回读</div>
          <div v-for="(m, i) in mutationLedger" :key="i" class="kv-row">
            <span class="kv-key">{{ m.name || m.tool || 'mutation' }}</span>
            <span class="kv-value">
              {{ m.path || m.target || '' }}
              <!-- verified 是 {ok, reason?, readback_ms?} 或 null，不是布尔值 -->
              <template v-if="m.verified && m.verified.ok === true"> · 已核实</template>
              <template v-else-if="m.verified">
                · 未核实{{ m.verified.reason ? `（${m.verified.reason}）` : '' }}
              </template>
              <template v-if="m.verified && m.verified.readback_ms != null">
                · 回读 {{ m.verified.readback_ms }}ms
              </template>
            </span>
          </div>
        </template>
        <div v-if="usage.answer_lint_missing != null" class="kv-row">
          <span class="kv-key">答复核对</span>
          <span class="kv-value">缺口事实 {{ usage.answer_lint_missing }} 条</span>
        </div>

        <!-- 原始 trace -->
        <el-collapse v-if="traceJson" class="raw-collapse">
          <el-collapse-item title="原始 trace（JSONL）" name="raw">
            <pre class="raw-json">{{ traceJson }}</pre>
          </el-collapse-item>
        </el-collapse>
      </template>
    </div>
  </el-drawer>
</template>

<script setup>
/**
 * 运行回放抽屉：把一次 run 的用量、分阶段耗时、逐调用缓存、工具轨迹、
 * 提示词版本与消息链指纹放在同一屏，用于定位「这一轮为什么慢/贵/断缓存」。
 *
 * 数据源：GET /runs/{id}（agent_runs.token_usage）+ GET /runs/{id}/trace
 * （mutation ledger 与原始 JSONL 只在 trace 里）。
 */
import { computed, reactive, ref, watch } from 'vue'
import { getRun, getRunTrace } from '../../api'
import { toolName } from '../../composables/useChatUtils'

const props = defineProps({
  modelValue: { type: Boolean, default: false },
  runId: { type: Number, default: null },
  conversationTitle: { type: String, default: '' },
})
const emit = defineEmits(['update:modelValue'])

const visible = computed({
  get: () => props.modelValue,
  set: (v) => emit('update:modelValue', v),
})

const loading = ref(false)
const error = ref('')
const run = ref(null)
const trace = ref({})
const openTools = reactive(new Set())

const usage = computed(() => {
  const u = run.value?.token_usage
  return u && typeof u === 'object' ? u : {}
})

const hitRateText = computed(() => {
  const hit = usage.value.cache_hit_tokens ?? 0
  const miss = usage.value.cache_miss_tokens ?? 0
  if (!hit && !miss) return '-'
  return `${Math.round((hit / (hit + miss)) * 100)}%`
})

const TIMING_LABELS = [
  ['first_token_ms', '首 Token（TTFT）'],
  ['prepare_ms', '准备'],
  ['dispatch_ms', '子任务分派'],
  ['subagent_ms', '子代理执行'],
  ['merge_ms', '结果合并'],
  ['agent_ms', '模型生成'],
  ['tools_ms', '工具执行'],
  ['finalize_ms', '收尾落库'],
]

const timingItems = computed(() => {
  const timings = usage.value.timings || {}
  const total = Math.max(1, run.value?.latency_ms || 0)
  return TIMING_LABELS.filter(([key]) => timings[key] != null).map(([key, label]) => ({
    key,
    label,
    value: timings[key],
    pct: `${Math.min(100, Math.round((timings[key] / total) * 100))}%`,
  }))
})

const callRows = computed(() =>
  (usage.value.calls || []).map((c, i) => {
    const input = Number(c.in) || 0
    const read = Number(c.read) || 0
    return {
      index: i + 1,
      in: input,
      out: Number(c.out) || 0,
      read,
      t_ms: c.t_ms,
      hitRate: input > 0 ? `${Math.round((read / input) * 100)}%` : '-',
    }
  }),
)

const prompt = computed(() => {
  const p = usage.value.prompt
  return p && typeof p === 'object' && Object.keys(p).length ? p : null
})

const promptSourceLabel = computed(() => {
  const labels = {
    locked: '会话锁定',
    new_lock: '首次锁定',
    override: '请求覆盖',
    disabled: '版本化关闭',
    fallback: '回退默认',
    latest: '最新版本',
  }
  return labels[prompt.value?.source] || prompt.value?.source || '—'
})

const hasToolSurface = computed(
  () => usage.value.tools_hash || usage.value.tools_chars || usage.value.tools_breakdown,
)
const hasChain = computed(
  () => usage.value.chain_len != null || usage.value.chain_divergence != null,
)
// 后端无范围/引用时落库的是空对象 {}（真值），必须判空键，
// 否则抽屉会凭空渲染「指定范围（undefined 个文件）」。
const kbScopeMeta = computed(() => {
  const v = usage.value.kb_scope
  return v && typeof v === 'object' && Object.keys(v).length ? v : null
})
const quoteMeta = computed(() => {
  const v = usage.value.quote
  return v && typeof v === 'object' && Object.keys(v).length ? v : null
})
const toolTrace = computed(() => run.value?.tool_trace || trace.value?.tool_trace || [])
const mutationLedger = computed(() => trace.value?.mutation_ledger || [])
const traceJson = computed(() =>
  Object.keys(trace.value || {}).length
    ? JSON.stringify(trace.value, null, 2)
    : '',
)

function fmtTokens(n) {
  if (n == null) return '-'
  if (n >= 1000000) return `${(n / 1000000).toFixed(1)}M`
  if (n >= 1000) return `${(n / 1000).toFixed(1)}k`
  return String(n)
}

function formatMs(ms) {
  if (ms == null) return '-'
  if (ms < 1000) return `${ms}ms`
  return `${(ms / 1000).toFixed(1)}s`
}

function shortHash(value) {
  return value ? String(value).slice(0, 10) : '—'
}

function toggleTool(index) {
  if (openTools.has(index)) openTools.delete(index)
  else openTools.add(index)
}

async function load(runId) {
  loading.value = true
  error.value = ''
  run.value = null
  trace.value = {}
  openTools.clear()
  try {
    const [detail, raw] = await Promise.all([
      getRun(runId),
      getRunTrace(runId).catch(() => ({})),
    ])
    run.value = detail
    trace.value = raw || {}
  } catch (e) {
    error.value = e.response?.data?.detail || e.message || '加载运行记录失败'
  } finally {
    loading.value = false
  }
}

watch(
  () => [props.modelValue, props.runId],
  ([open, id]) => {
    if (open && id != null) load(id)
  },
  { immediate: true },
)
</script>

<style scoped>
.replay-body {
  min-height: 200px;
}

.replay-question {
  font-size: var(--font-lg);
  font-weight: 600;
  color: var(--text-1);
  line-height: 1.5;
  margin-bottom: 8px;
}

.replay-meta {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 10px;
  font-size: var(--font-xs);
  color: var(--text-3);
  margin-bottom: 14px;
}

.replay-grid {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 8px;
  margin-bottom: 16px;
}

.replay-cell {
  padding: 8px 10px;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  background: var(--bg-card-2);
}

.cell-value {
  font-size: var(--font-lg);
  font-weight: 600;
  color: var(--text-1);
}

.cell-label {
  margin-top: 2px;
  font-size: var(--font-2xs);
  color: var(--text-3);
}

.replay-title {
  display: flex;
  align-items: baseline;
  gap: 8px;
  margin: 16px 0 8px;
  font-size: var(--font-sm);
  font-weight: 600;
  color: var(--text-2);
}

.title-hint {
  font-size: var(--font-2xs);
  font-weight: 400;
  color: var(--text-3);
}

.timing-list {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.timing-row {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: var(--font-xs);
  color: var(--text-2);
}

.timing-name {
  width: 110px;
  flex-shrink: 0;
}

.timing-bar {
  flex: 1;
  height: 6px;
  border-radius: var(--radius-pill);
  background: var(--bg-hover);
  overflow: hidden;
}

.timing-bar i {
  display: block;
  height: 100%;
  background: var(--primary);
}

.timing-value {
  width: 64px;
  text-align: right;
  color: var(--text-3);
}

.replay-table {
  margin-bottom: 4px;
}

.hit-rate {
  color: var(--success-text);
}

.hit-rate.cold {
  color: var(--warning-text);
  font-weight: 600;
}

.kv-row {
  display: flex;
  gap: 10px;
  padding: 4px 0;
  font-size: var(--font-xs);
  line-height: 1.5;
}

.kv-key {
  width: 72px;
  flex-shrink: 0;
  color: var(--text-3);
}

.kv-value {
  color: var(--text-2);
  word-break: break-all;
}

.tool-list {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.tool-item {
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  overflow: hidden;
}

.tool-head {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 10px;
  font-size: var(--font-xs);
  cursor: pointer;
}

.tool-step {
  color: var(--text-3);
  width: 20px;
}

.tool-name {
  color: var(--text-1);
  font-weight: 600;
  flex-shrink: 0;
}

.tool-summary {
  flex: 1;
  color: var(--text-3);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.tool-dur {
  color: var(--text-3);
  flex-shrink: 0;
}

.tool-arrow {
  color: var(--text-3);
}

.tool-detail {
  margin: 0;
  padding: 8px 10px;
  border-top: 1px dashed var(--border);
  background: var(--bg-card-3, rgba(128, 128, 128, 0.06));
  color: var(--text-2);
  font-family: var(--font-mono);
  font-size: var(--font-2xs);
  line-height: 1.5;
  white-space: pre-wrap;
  word-break: break-all;
  max-height: 240px;
  overflow: auto;
}

.empty-hint {
  font-size: var(--font-xs);
  color: var(--text-3);
}

.raw-collapse {
  margin-top: 16px;
}

.raw-json {
  margin: 0;
  font-family: var(--font-mono);
  font-size: var(--font-2xs);
  line-height: 1.5;
  color: var(--text-2);
  white-space: pre-wrap;
  word-break: break-all;
  max-height: 360px;
  overflow: auto;
}
</style>
