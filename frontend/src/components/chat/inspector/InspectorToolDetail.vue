<template>
  <div class="itd">
    <div class="itd-head">
      <button type="button" class="itd-back" @click="emit('back')">
        <el-icon :size="13"><ArrowLeft /></el-icon>
        返回
      </button>
      <span class="itd-title">{{ title }}</span>
      <span :class="['itd-status', status]">{{ statusLabel }}</span>
      <span v-if="duration" class="itd-dur">{{ duration }}</span>
    </div>

    <div v-if="status === 'running'" class="itd-running">
      <i /> 工具执行中，输出会实时补全…
    </div>

    <section v-if="command" class="itd-section">
      <div class="itd-label itd-label-command">命令</div>
      <div v-if="cwd" class="itd-cwd">cwd: {{ cwd }}</div>
      <pre class="itd-code itd-command">{{ command }}</pre>
    </section>

    <section v-if="summary" class="itd-section">
      <div class="itd-label">摘要</div>
      <p class="itd-summary">{{ summary }}</p>
    </section>

    <section v-if="extraArgsJson" class="itd-section">
      <div class="itd-label">参数</div>
      <pre class="itd-code itd-json"><span
        v-for="(seg, i) in extraArgsTokens"
        :key="i"
        :class="`tok-${seg.cls}`"
      >{{ seg.text }}</span></pre>
    </section>

    <section v-if="editDiff && editDiff.lines.length" class="itd-section">
      <div class="itd-label">变更</div>
      <div class="itd-diff">
        <div
          v-for="(line, i) in editDiff.lines"
          :key="i"
          :class="['itd-line', line.type]"
        >
          <span class="itd-sign">{{ line.sign }}</span>
          <span class="itd-line-text">{{ line.text }}</span>
        </div>
      </div>
      <div v-if="editDiff.truncated" class="itd-hint">diff 已截断，仅显示前 {{ editDiff.lines.length }} 行</div>
    </section>
    <section v-else-if="writeContent" class="itd-section">
      <div class="itd-label">写入内容（{{ writeContent.length }} 字符）</div>
      <pre class="itd-code">{{ writeContent }}</pre>
    </section>

    <section v-if="outputText" class="itd-section">
      <div class="itd-label" :class="{ 'itd-label-fail': failed }">
        输出
        <span v-if="truncated" class="itd-hint">可能已截断</span>
      </div>
      <pre
        v-if="outputIsJson"
        class="itd-code itd-json"
        :class="{ fail: failed }"
      ><span
        v-for="(seg, i) in outputTokens"
        :key="i"
        :class="`tok-${seg.cls}`"
      >{{ seg.text }}</span></pre>
      <pre v-else class="itd-code" :class="{ fail: failed }">{{ outputText }}</pre>
    </section>

    <section v-if="artifacts.length" class="itd-section">
      <div class="itd-label">产物</div>
      <div v-for="(item, i) in artifacts" :key="i" class="itd-artifact">
        <span :class="['itd-action', item.action]">{{ actionLabel(item.action) }}</span>
        <span class="itd-path">{{ item.path }}</span>
        <span v-if="sizeLabel(item)" class="itd-bytes">{{ sizeLabel(item) }}</span>
      </div>
    </section>

    <div v-if="!command && !summary && !extraArgsJson && !outputText && !writeContent" class="inspector-empty">
      这条工具调用还没有可展示的内容。
    </div>
  </div>
</template>

<script setup>
/** 工具详情：命令/参数/输出分色渲染；数据来自 tool_trace 原始条目。 */
import { computed } from 'vue'
import { ArrowLeft } from '@element-plus/icons-vue'
import { formatFileSize, toolName } from '../../../composables/useChatUtils'
import { buildChangeDiff } from '../../../composables/useInspectorModel'
import { formatJson, tokenizeJson } from '../../../utils/jsonHighlight'

const props = defineProps({
  run: { type: Object, required: true },
})
const emit = defineEmits(['back'])

const args = computed(() => props.run.arguments || {})
const title = computed(() => toolName(props.run.name))
const status = computed(() => props.run.status || 'done')
const failed = computed(() => ['error', 'failed'].includes(status.value))
const statusLabel = computed(
  () =>
    ({
      running: '执行中',
      done: '已完成',
      error: '失败',
      failed: '失败',
    })[status.value] || status.value,
)
const duration = computed(() => {
  const ms = props.run.durationMs
  if (ms == null) return ''
  return ms < 1000 ? `${ms}ms` : `${(ms / 1000).toFixed(1)}s`
})

const command = computed(() => String(args.value.command || ''))
const cwd = computed(() => String(args.value.cwd || ''))
const summary = computed(() => String(props.run.summary || ''))

const SKIP_ARG_KEYS = new Set(['command', 'cwd', 'content', 'old_string', 'new_string'])
const extraArgsJson = computed(() => {
  const rest = {}
  for (const [key, value] of Object.entries(args.value)) {
    if (!SKIP_ARG_KEYS.has(key)) rest[key] = value
  }
  return Object.keys(rest).length ? formatJson(rest) : ''
})
const extraArgsTokens = computed(() =>
  extraArgsJson.value ? tokenizeJson(extraArgsJson.value) : [],
)

const writeContent = computed(() => {
  if (props.run.name !== 'write_file') return ''
  return String(args.value.content || '')
})

const editDiff = computed(() => {
  if (props.run.name !== 'edit_file') return null
  const oldText = String(args.value.old_string ?? '')
  const newText = String(args.value.new_string ?? '')
  if (!oldText && !newText) return null
  return buildChangeDiff(oldText, newText)
})

const detailText = computed(() => String(props.run.detail || ''))
const outputIsJson = computed(() => {
  const text = detailText.value.trim()
  if (!text || (text[0] !== '{' && text[0] !== '[')) return false
  try {
    JSON.parse(text)
    return true
  } catch {
    return false
  }
})
const outputText = computed(() =>
  outputIsJson.value ? formatJson(detailText.value) : detailText.value,
)
const outputTokens = computed(() =>
  outputIsJson.value ? tokenizeJson(outputText.value) : [],
)
// 后端把工具输出截断到约 2500 字符：接近上限时提示可能不完整
const truncated = computed(() => detailText.value.length >= 2400)

const artifacts = computed(() =>
  Array.isArray(props.run.artifacts) ? props.run.artifacts : [],
)

function actionLabel(action) {
  return { created: '新建', modified: '修改', deleted: '删除', added_document: '入库' }[action] || action || ''
}

function sizeLabel(item) {
  if (item.bytes) return formatFileSize(item.bytes)
  if (item.chars) return `${item.chars} 字符`
  return ''
}
</script>
