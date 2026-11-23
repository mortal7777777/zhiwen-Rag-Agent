<template>
  <div class="inspector-stack">
    <div class="ictx-actions">
      <el-button size="small" text @click="emit('refresh')">刷新</el-button>
      <el-button
        size="small"
        type="primary"
        plain
        :loading="compacting"
        :disabled="!stats"
        @click="emit('compact')"
      >
        立即压缩
      </el-button>
    </div>

    <template v-if="stats">
      <section v-if="categories.length" class="ictx-section">
        <div class="ictx-title">
          构成估算
          <span class="ictx-est">估算</span>
          <span v-if="stats.compaction_would_trigger" class="ictx-warn">超过压缩水位</span>
        </div>
        <div class="ictx-stack">
          <i
            v-for="cat in categories.filter((c) => c.tokens > 0)"
            :key="cat.key"
            :class="['ictx-seg', cat.key]"
            :style="{ width: pct(cat.tokens) }"
            :title="`${cat.label}：${fmt(cat.tokens)}`"
          />
        </div>
        <div class="ictx-legend">
          <div v-for="cat in categories" :key="cat.key" class="ictx-legend-row">
            <span :class="['ictx-dot', cat.key]"></span>
            <span class="ictx-legend-label">{{ cat.label }}</span>
            <span class="ictx-legend-val">{{ fmt(cat.tokens) }} · {{ pct(cat.tokens) }}</span>
          </div>
        </div>
        <div class="ictx-budget">
          估算合计 {{ fmt(total) }}
          <template v-if="stats.token_budget"> / 预算 {{ fmt(stats.token_budget) }}（{{ budgetPct }}%）</template>
        </div>
      </section>

      <section class="ictx-section">
        <div class="ictx-row">
          <span class="ictx-label">历史消息</span>
          <span class="ictx-value">
            {{ stats.effective_history_messages ?? stats.message_count ?? 0 }}
            / {{ stats.history_max_messages ?? '—' }}
          </span>
        </div>
        <div class="ictx-bar">
          <i :style="{ width: barPct(effectiveMessages, highWater) }" />
        </div>
        <div class="ictx-row">
          <span class="ictx-label">发送 token</span>
          <span class="ictx-value">
            {{ fmt(sentTokens) }} / {{ fmt(stats.token_budget) }}
          </span>
        </div>
        <div class="ictx-bar">
          <i :style="{ width: barPct(sentTokens, stats.token_budget) }" />
        </div>
        <div class="ictx-row">
          <span class="ictx-label">缓存命中率</span>
          <span class="ictx-value">
            {{ stats.recent_cache_hit_rate != null ? Math.round(stats.recent_cache_hit_rate * 100) + '%' : '—' }}
            <span v-if="stats.recent_runs" class="ictx-sub">（近 {{ stats.recent_runs }} 次运行）</span>
          </span>
        </div>
        <div class="ictx-row">
          <span class="ictx-label">滚动摘要</span>
          <span class="ictx-value">
            {{ stats.summary_chars ? `${stats.summary_chars} 字` : '未生成' }}
          </span>
        </div>
      </section>
    </template>

    <div v-else class="inspector-empty">暂无统计数据</div>
  </div>
</template>

<script setup>
/** 上下文页签：分项估算构成 + 历史/预算/缓存/摘要。 */
import { computed } from 'vue'

const props = defineProps({
  stats: { type: Object, default: null },
  compacting: { type: Boolean, default: false },
})
const emit = defineEmits(['refresh', 'compact'])

const CATEGORY_META = [
  { key: 'system_prompt', label: '系统提示词' },
  { key: 'tools', label: '工具定义' },
  { key: 'context_injections', label: '上下文注入' },
  { key: 'history', label: '历史消息' },
  { key: 'question', label: '当前问题' },
]

const breakdown = computed(() => props.stats?.context_breakdown || null)
const categories = computed(() => {
  const b = breakdown.value
  if (!b) return []
  return CATEGORY_META.map((meta) => ({
    ...meta,
    tokens: Number(b[meta.key]?.tokens || 0),
  }))
})
const total = computed(
  () => Number(breakdown.value?.total || categories.value.reduce((sum, c) => sum + c.tokens, 0)),
)
const budgetPct = computed(() => {
  const budget = Number(props.stats?.token_budget || 0)
  if (!budget) return 0
  return Math.min(100, Math.round((total.value / budget) * 100))
})
const effectiveMessages = computed(
  () => props.stats?.effective_history_messages ?? props.stats?.message_count ?? 0,
)
const sentTokens = computed(
  () => props.stats?.sent_estimate_tokens ?? props.stats?.estimated_tokens ?? 0,
)
const highWater = computed(
  () => props.stats?.history_high_water_messages ?? props.stats?.history_max_messages ?? 1,
)

function pct(tokens) {
  if (!total.value || !tokens) return '0%'
  return `${Math.max(1, Math.round((tokens / total.value) * 100))}%`
}

function barPct(cur, cap) {
  if (!cap) return '0%'
  return `${Math.min(100, Math.round((cur / cap) * 100))}%`
}

function fmt(n) {
  if (n == null) return '—'
  if (n >= 1000000) return `${(n / 1000000).toFixed(1)}M`
  if (n >= 1000) return `${(n / 1000).toFixed(1)}k`
  return String(n)
}
</script>
