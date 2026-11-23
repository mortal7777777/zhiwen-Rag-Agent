<template>
  <div
    ref="cardEl"
    class="question-card"
    :data-question-state="request.state"
    tabindex="0"
    @keydown="onKeydown"
  >
    <div class="question-head">
      <el-icon :size="14"><ChatDotRound /></el-icon>
      <span class="question-title">
        {{ request.state === 'answered' ? '回答已提交' : '需要你的选择' }}
      </span>
      <span class="question-count">{{ totalQuestions }} 个问题</span>
    </div>
    <div class="question-body">
      <div
        v-for="(req, ri) in request.requests"
        :key="req.call_id || ri"
        class="question-group"
      >
        <div
          v-for="(q, qi) in req.questions || []"
          :key="qi"
          :data-qkey="qkey(ri, qi)"
          :class="['q-block', { focused: focusKey === qkey(ri, qi), skipped: skipped[qkey(ri, qi)] }]"
          @click="focusQuestion(ri, qi)"
        >
          <div class="q-head">
            <span v-if="q.header" class="q-tag">{{ q.header }}</span>
            <span :class="['q-text', { skipped: skipped[qkey(ri, qi)] }]">{{ q.question }}</span>
            <span class="q-mode">{{ q.multi_select ? '可多选' : '单选' }}</span>
          </div>
          <div class="q-options" role="listbox" :aria-label="q.question">
            <button
              v-for="(opt, oi) in q.options || []"
              :key="oi"
              type="button"
              role="option"
              :aria-selected="isSelected(ri, qi, opt.label)"
              :class="['q-opt', { active: isSelected(ri, qi, opt.label), cursor: isCursor(ri, qi, oi) }]"
              :disabled="locked(ri, qi)"
              @mouseenter="setCursor(ri, qi, oi)"
              @click.stop="pickOption(ri, qi, oi)"
            >
              <span class="q-opt-key">{{ oi + 1 }}</span>
              <span class="q-opt-main">
                <span class="q-opt-label">{{ opt.label }}</span>
                <span v-if="opt.description" class="q-opt-desc">{{ opt.description }}</span>
              </span>
              <span v-if="isSelected(ri, qi, opt.label)" class="q-opt-check">✓</span>
            </button>

            <!-- 其他：选中后展开输入框，把自己的想法写进来 -->
            <div
              :class="[
                'q-opt',
                'q-opt-other',
                { active: isOtherActive(ri, qi), cursor: isCursor(ri, qi, otherIndex(q)) },
              ]"
              role="option"
              :aria-selected="isOtherActive(ri, qi)"
              :aria-disabled="locked(ri, qi)"
              @mouseenter="setCursor(ri, qi, otherIndex(q))"
              @click.stop="openOther(ri, qi)"
            >
              <span class="q-opt-key q-other-key">
                <el-icon :size="10"><EditPen /></el-icon>
              </span>
              <span class="q-opt-main">
                <span class="q-opt-label">其他（自己输入）</span>
                <span v-if="!isOtherActive(ri, qi)" class="q-opt-desc">写出你自己的方案或补充</span>
                <span
                  v-else-if="!otherFocus[qkey(ri, qi)] && String(other[qkey(ri, qi)] || '').trim()"
                  class="q-opt-desc q-other-preview"
                >{{ other[qkey(ri, qi)] }}</span>
              </span>
              <span v-if="isOtherActive(ri, qi)" class="q-opt-check">✓</span>
            </div>
            <div v-if="otherFocus[qkey(ri, qi)]" class="q-other-wrap" @click.stop>
              <el-input
                :ref="(el) => setOtherInput(ri, qi, el)"
                v-model="other[qkey(ri, qi)]"
                placeholder="输入你的想法，Enter 确认，Esc 返回"
                size="small"
                :disabled="locked(ri, qi)"
                @input="onOtherInput(ri, qi)"
                @focus="setCursor(ri, qi, otherIndex(q))"
                @keydown.enter.stop.prevent="confirmOther(ri, qi)"
                @keydown.esc.stop.prevent="closeOther(ri, qi)"
              />
            </div>

            <button
              type="button"
              class="q-opt q-skip"
              :class="{ active: skipped[qkey(ri, qi)], cursor: isCursor(ri, qi, skipIndex(q)) }"
              :disabled="request.state !== 'pending'"
              @mouseenter="setCursor(ri, qi, skipIndex(q))"
              @click.stop="toggleSkip(ri, qi)"
            >
              <span class="q-opt-key q-skip-key">—</span>
              <span class="q-opt-main">
                <span class="q-opt-label">跳过此题</span>
              </span>
            </button>
          </div>
        </div>
      </div>
    </div>
    <div class="question-actions">
      <el-button
        size="small"
        type="primary"
        :disabled="loading || request.state !== 'pending'"
        @click="submit"
      >
        {{ request.state === 'answered' ? '已提交' : '提交回答' }}
      </el-button>
      <span class="q-key-hint">↑↓ 移动 · ←→ 切题 · 空格多选 · Enter 确认 · 数字键直选</span>
    </div>
  </div>
</template>

<script setup>
/**
 * 向用户提问卡（原生挂起）。模型调用 ask_user_question 后渲染，
 * 答案经 /agent/resume 的 answers 字段回填为 ToolMessage。
 *
 * 键盘（类 Claude Code 审批）：数字键直选；↑↓ 在选项间移动游标；
 * ←→ 切换题目；空格多选切换；Enter 确认当前项（单选即选中并进入下一题）。
 * 「其他（自己输入）」是选项列表中的一项，选中后展开输入框。
 * 全部题目作答/跳过后，Enter 自动提交；也可随时点「提交回答」。
 */
import { computed, nextTick, onMounted, reactive, ref } from 'vue'
import { ChatDotRound, EditPen } from '@element-plus/icons-vue'

const props = defineProps({
  request: { type: Object, required: true },
  loading: { type: Boolean, default: false },
})
const emit = defineEmits(['submit'])

const cardEl = ref(null)
const selections = reactive({})
const other = reactive({})
const skipped = reactive({})
const otherFocus = reactive({})
const cursor = reactive({})
const focusKey = ref('')
const otherInputs = {}

const qkey = (ri, qi) => `${ri}:${qi}`

const flatQuestions = computed(() => {
  const out = []
  ;(props.request.requests || []).forEach((req, ri) => {
    (req.questions || []).forEach((q, qi) => out.push({ ri, qi, q }))
  })
  return out
})

const totalQuestions = computed(() => flatQuestions.value.length)

function questionOf(ri, qi) {
  const req = (props.request.requests || [])[ri]
  return req && (req.questions || [])[qi]
}

function otherIndex(q) {
  return (q?.options || []).length
}

function skipIndex(q) {
  return (q?.options || []).length + 1
}

function locked(ri, qi) {
  return props.request.state !== 'pending' || !!skipped[qkey(ri, qi)]
}

function currentEntry() {
  const flat = flatQuestions.value
  if (!flat.length) return null
  return flat.find((x) => qkey(x.ri, x.qi) === focusKey.value) || flat[0]
}

function focusQuestion(ri, qi) {
  focusKey.value = qkey(ri, qi)
  cardEl.value?.focus?.()
}

function focusEntry(entry) {
  if (!entry) return
  focusKey.value = qkey(entry.ri, entry.qi)
  nextTick(() => {
    cardEl.value
      ?.querySelector(`[data-qkey="${focusKey.value}"]`)
      ?.scrollIntoView({ block: 'nearest' })
    cardEl.value?.focus?.()
  })
}

function setCursor(ri, qi, index) {
  focusKey.value = qkey(ri, qi)
  cursor[qkey(ri, qi)] = index
}

function isCursor(ri, qi, index) {
  const key = qkey(ri, qi)
  if (focusKey.value !== key) return false
  return (cursor[key] ?? 0) === index
}

function isSelected(ri, qi, label) {
  return (selections[qkey(ri, qi)] || []).includes(label)
}

function isOtherActive(ri, qi) {
  const key = qkey(ri, qi)
  return !!otherFocus[key] || !!String(other[key] || '').trim()
}

function hasAnswer(ri, qi) {
  const key = qkey(ri, qi)
  return !!skipped[key] || (selections[key] || []).length > 0 || !!String(other[key] || '').trim()
}

function selectOption(ri, qi, oi) {
  if (props.request.state !== 'pending') return
  const key = qkey(ri, qi)
  const q = questionOf(ri, qi)
  const opt = q && (q.options || [])[oi]
  if (!opt) return
  skipped[key] = false
  setCursor(ri, qi, oi)
  if (q.multi_select) {
    const current = selections[key] || []
    selections[key] = current.includes(opt.label)
      ? current.filter((x) => x !== opt.label)
      : [...current, opt.label]
  } else {
    selections[key] = [opt.label]
  }
  other[key] = ''
  otherFocus[key] = false
}

function pickOption(ri, qi, oi) {
  selectOption(ri, qi, oi)
  cardEl.value?.focus?.()
}

function openOther(ri, qi) {
  if (props.request.state !== 'pending' || skipped[qkey(ri, qi)]) return
  const q = questionOf(ri, qi)
  if (!q) return
  otherFocus[qkey(ri, qi)] = true
  setCursor(ri, qi, otherIndex(q))
  nextTick(() => {
    otherInputs[qkey(ri, qi)]?.focus?.()
  })
}

function onOtherInput(ri, qi) {
  const key = qkey(ri, qi)
  if (String(other[key] || '').trim()) {
    selections[key] = []
    skipped[key] = false
  }
}

function closeOther(ri, qi) {
  otherFocus[qkey(ri, qi)] = false
  nextTick(() => cardEl.value?.focus?.())
}

function confirmOther(ri, qi) {
  if (!String(other[qkey(ri, qi)] || '').trim()) return
  otherFocus[qkey(ri, qi)] = false
  advanceFrom(ri, qi)
}

function toggleSkip(ri, qi) {
  if (props.request.state !== 'pending') return
  const key = qkey(ri, qi)
  skipped[key] = !skipped[key]
  if (skipped[key]) {
    selections[key] = []
    other[key] = ''
    otherFocus[key] = false
  }
  setCursor(ri, qi, skipIndex(questionOf(ri, qi)))
  cardEl.value?.focus?.()
}

function setOtherInput(ri, qi, el) {
  const key = qkey(ri, qi)
  if (el) otherInputs[key] = el
  else delete otherInputs[key]
}

function advanceFrom(ri, qi) {
  const rest = flatQuestions.value.filter((x) => !(x.ri === ri && x.qi === qi))
  const next = rest.find((x) => !hasAnswer(x.ri, x.qi))
  if (next) {
    cursor[qkey(next.ri, next.qi)] = 0
    focusEntry(next)
    return
  }
  submit()
}

function moveCursor(delta) {
  const entry = currentEntry()
  if (!entry) return
  const key = qkey(entry.ri, entry.qi)
  const total = (entry.q.options || []).length + 2
  cursor[key] = ((cursor[key] ?? 0) + delta + total) % total
}

function moveQuestion(delta) {
  const flat = flatQuestions.value
  if (!flat.length) return
  const index = flat.findIndex((x) => qkey(x.ri, x.qi) === focusKey.value)
  const next = flat[(index + delta + flat.length) % flat.length]
  focusEntry(next)
}

function toggleCurrent() {
  const entry = currentEntry()
  if (!entry) return
  const { ri, qi, q } = entry
  const row = cursor[qkey(ri, qi)] ?? 0
  if (row < (q.options || []).length) selectOption(ri, qi, row)
  else if (row === otherIndex(q)) openOther(ri, qi)
  else toggleSkip(ri, qi)
}

function confirmCurrent() {
  const entry = currentEntry()
  if (!entry) return
  const { ri, qi, q } = entry
  const key = qkey(ri, qi)
  const row = cursor[key] ?? 0
  if (row < (q.options || []).length) {
    if (!q.multi_select) selectOption(ri, qi, row)
    advanceFrom(ri, qi)
  } else if (row === otherIndex(q)) {
    openOther(ri, qi)
  } else {
    toggleSkip(ri, qi)
    advanceFrom(ri, qi)
  }
}

function onKeydown(event) {
  const tag = event.target?.tagName
  if (tag === 'INPUT' || tag === 'TEXTAREA') return
  // 按钮上的 Enter/空格保留原生激活语义（click 处理里会聚焦回卡片）
  const onButton = !!event.target?.closest?.('.q-opt, .q-skip')
  if (onButton && (event.key === 'Enter' || event.key === ' ')) return
  if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
    event.preventDefault()
    moveCursor(event.key === 'ArrowDown' ? 1 : -1)
    return
  }
  if (event.key === 'ArrowLeft' || event.key === 'ArrowRight') {
    event.preventDefault()
    moveQuestion(event.key === 'ArrowLeft' ? -1 : 1)
    return
  }
  if (event.key === 'Enter') {
    event.preventDefault()
    confirmCurrent()
    return
  }
  if (event.key === ' ') {
    event.preventDefault()
    toggleCurrent()
    return
  }
  if (!/^[1-9]$/.test(event.key)) return
  const entry = currentEntry()
  if (!entry) return
  const index = Number(event.key) - 1
  if (index >= (entry.q.options || []).length) return
  event.preventDefault()
  selectOption(entry.ri, entry.qi, index)
}

function buildAnswers() {
  return (props.request.requests || []).map((req, ri) => ({
    call_id: req.call_id,
    items: (req.questions || []).map((q, qi) => {
      const key = qkey(ri, qi)
      const selected = selections[key] || []
      const text = String(other[key] || '').trim()
      return {
        index: qi,
        selected: [...selected],
        other: text,
        skipped: !!skipped[key] || (!selected.length && !text),
      }
    }),
  }))
}

function submit() {
  if (props.request.state !== 'pending' || props.loading) return
  emit('submit', buildAnswers())
}

onMounted(() => {
  const first = flatQuestions.value[0]
  if (first) focusKey.value = qkey(first.ri, first.qi)
})
</script>

<style scoped>
.question-card {
  margin-top: 8px;
  border-radius: var(--radius-md);
  border: 1px solid var(--warning);
  background: var(--bg-card-2);
  box-shadow: 0 0 0 1px var(--warning-bg) inset;
  outline: none;
}

.question-card:focus-visible {
  box-shadow: 0 0 0 2px var(--bg-active), 0 0 0 1px var(--warning) inset;
}

.question-head {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 8px 12px;
  font-size: var(--font-sm);
  font-weight: 600;
  color: var(--text-2);
  background: var(--bg-card-3, rgba(128, 128, 128, 0.08));
  border-bottom: 1px solid var(--border);
}

.question-head .el-icon {
  color: var(--warning-text);
}

.question-title {
  color: var(--text-1);
}

.question-count {
  margin-left: auto;
  padding: 1px 8px;
  border-radius: var(--radius-pill);
  background: var(--bg-card-2);
  border: 1px solid var(--border);
  color: var(--text-3);
  font-size: var(--font-2xs);
  font-weight: 400;
}

.question-body {
  display: flex;
  flex-direction: column;
  gap: 10px;
  padding: 10px 12px 4px;
}

.question-group {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.q-block {
  padding: 8px 10px;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  background: var(--bg-card-3, rgba(128, 128, 128, 0.05));
}

.q-block.focused {
  border-color: var(--primary);
}

.q-head {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
}

.q-tag {
  padding: 1px 7px;
  border-radius: var(--radius-pill);
  background: var(--bg-hover);
  color: var(--text-3);
  font-size: var(--font-2xs);
}

.q-text {
  font-size: var(--font-sm);
  font-weight: 600;
  color: var(--text-1);
  line-height: 1.5;
}

.q-text.skipped {
  color: var(--text-3);
  text-decoration: line-through;
}

.q-mode {
  margin-left: auto;
  color: var(--text-3);
  font-size: var(--font-2xs);
}

.q-options {
  display: flex;
  flex-direction: column;
  gap: 6px;
  margin-top: 8px;
}

.q-opt {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  width: 100%;
  padding: 6px 10px;
  border-radius: var(--radius-sm);
  border: 1px solid var(--border);
  background: var(--bg-card-2);
  color: var(--text-2);
  font-size: var(--font-sm);
  text-align: left;
  cursor: pointer;
  transition: border-color 0.15s, background 0.15s, box-shadow 0.15s;
}

/* 键盘游标：只表示「当前指向」，与已选中态区分 */
.q-opt.cursor {
  border-color: var(--primary);
  box-shadow: 0 0 0 2px var(--bg-active);
}

/* 已选中：底色 + 实心序号 */
.q-opt.active {
  border-color: var(--primary);
  background: var(--bg-active);
}

.q-opt:hover:not(:disabled) {
  border-color: var(--primary);
}

.q-opt:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.q-opt-key {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 16px;
  height: 16px;
  margin-top: 1px;
  border-radius: var(--radius-xs);
  background: var(--bg-hover);
  font-size: var(--font-2xs);
  font-weight: 700;
  color: var(--text-3);
  flex-shrink: 0;
}

.q-opt.active .q-opt-key {
  background: var(--primary);
  color: var(--text-on-brand);
}

.q-opt-main {
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
}

.q-opt-label {
  font-weight: 600;
  color: var(--text-1);
}

.q-opt-desc {
  color: var(--text-3);
  font-size: var(--font-xs);
  line-height: 1.4;
}

.q-other-preview {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.q-opt-check {
  margin-left: auto;
  align-self: center;
  color: var(--primary);
  font-weight: 700;
  flex-shrink: 0;
}

.q-opt-other {
  border-style: dashed;
}

.q-opt-other.active {
  border-style: solid;
}

.q-other-wrap {
  margin-top: -2px;
  padding: 0 2px;
}

.q-skip {
  border-style: dashed;
  background: transparent;
}

.q-skip .q-opt-label {
  color: var(--text-3);
  font-weight: 400;
}

.q-skip.active {
  border-color: var(--warning);
  background: var(--warning-bg, transparent);
}

.q-skip.active .q-opt-label {
  color: var(--warning-text);
}

.question-actions {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 8px 12px 10px;
}

.q-key-hint {
  font-size: var(--font-2xs);
  color: var(--text-3);
}
</style>
