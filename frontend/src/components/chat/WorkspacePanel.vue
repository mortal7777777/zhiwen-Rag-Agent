<template>
  <aside class="chat-inspector" aria-label="会话面板">
    <header class="inspector-head">
      <span class="inspector-title">{{ detailRun ? '工具详情' : '会话面板' }}</span>
      <span v-if="!detailRun" class="inspector-meta">{{ messageCount }} 条消息</span>
    </header>

    <!-- 详情模式：点击聊天或工具轨迹里的条目后，整块面板让给完整内容 -->
    <div v-if="detailRun" class="inspector-body">
      <div class="inspector-scroll">
        <InspectorToolDetail :run="detailRun" @back="emit('close-detail')" />
      </div>
    </div>

    <template v-else>
      <section class="inspector-summary">
        <button type="button" class="inspector-ws" @click="emit('change-workspace')">
          <el-icon class="inspector-ws-icon" :size="13"><FolderOpened /></el-icon>
          <span class="inspector-ws-name">{{ workspaceName }}</span>
          <span v-if="workspace" class="inspector-ws-path" :title="workspace">{{ workspace }}</span>
        </button>
        <div class="inspector-tags">
          <el-tag size="small" effect="plain">{{ templateName }}</el-tag>
          <el-tag v-if="promptTag" size="small" :type="promptTag.type">
            {{ promptTag.text }}
          </el-tag>
        </div>
      </section>

      <!-- 分区折叠：空分区不出现；运行中出现数据时自动展开 -->
      <div class="inspector-body">
        <div class="inspector-scroll">
          <InspectorSection
            title="上下文"
            :icon="Histogram"
            :open="open.context"
            @toggle="toggleSection('context')"
          >
            <InspectorContext
              :stats="stats"
              :compacting="compacting"
              @refresh="emit('refresh-context')"
              @compact="emit('compact')"
            />
          </InspectorSection>

          <InspectorSection
            v-if="planCount"
            title="计划与任务"
            :icon="List"
            :count="planCount"
            :live="running && (plan.length > 0 || todos.length > 0)"
            :open="open.plan"
            @toggle="toggleSection('plan')"
          >
            <InspectorPlan
              :plan="plan"
              :todos="todos"
              :plan-done="planDone"
              :plan-total="planTotal"
              :running="running"
              :history-plan="historyPlan"
            />
          </InspectorSection>

          <InspectorSection
            v-if="toolRuns.length"
            title="工具轨迹"
            :icon="Tools"
            :count="toolRuns.length"
            :live="running"
            :open="open.tools"
            @toggle="toggleSection('tools')"
          >
            <InspectorTools
              :messages="messages"
              :running="running"
              @open-detail="emit('open-detail', $event)"
            />
          </InspectorSection>

          <InspectorSection
            v-if="subagents.length"
            title="子代理运行"
            :icon="Connection"
            :count="subagents.length"
            :live="running"
            :open="open.subagents"
            @toggle="toggleSection('subagents')"
          >
            <InspectorSubagents :messages="messages" :run-id="runId" :running="running" />
          </InspectorSection>

          <InspectorSection
            v-if="artifacts.length"
            title="输出产物"
            :icon="Document"
            :count="artifacts.length"
            :open="open.outputs"
            @toggle="toggleSection('outputs')"
          >
            <InspectorOutputs :messages="messages" />
          </InspectorSection>

          <InspectorSection
            v-if="changes.length"
            title="文件变更"
            :icon="EditPen"
            :count="changes.length"
            :open="open.changes"
            @toggle="toggleSection('changes')"
          >
            <InspectorChanges :messages="messages" />
          </InspectorSection>
        </div>
      </div>
    </template>
  </aside>
</template>

<script setup>
/**
 * 右侧会话面板：Claude 桌面端式分区折叠。
 * 上下文常驻；计划/工具/子代理/产出/变更按数据是否出现动态显示，
 * 运行中出现新数据时自动展开；点击分区头展开/收起并记忆状态。
 * 详情模式展示单个工具调用的完整内容（由 ChatView 选中条目进入）。
 */
import { computed, reactive, ref, watch } from 'vue'
import {
  Connection,
  Document,
  EditPen,
  FolderOpened,
  Histogram,
  List,
  Tools,
} from '@element-plus/icons-vue'
import InspectorChanges from './inspector/InspectorChanges.vue'
import InspectorContext from './inspector/InspectorContext.vue'
import InspectorOutputs from './inspector/InspectorOutputs.vue'
import InspectorPlan from './inspector/InspectorPlan.vue'
import InspectorSection from './inspector/InspectorSection.vue'
import InspectorSubagents from './inspector/InspectorSubagents.vue'
import InspectorToolDetail from './inspector/InspectorToolDetail.vue'
import InspectorTools from './inspector/InspectorTools.vue'
import {
  deriveArtifacts,
  deriveChanges,
  deriveSubagents,
  flattenToolRuns,
  normalizeRunEntry,
} from '../../composables/useInspectorModel'
import { listRuns } from '../../api'

const props = defineProps({
  session: { type: Object, default: null },
  workspace: { type: String, default: '' },
  workspaceName: { type: String, default: '默认工作区' },
  templateName: { type: String, default: '默认模板' },
  promptTag: { type: Object, default: null },
  messageCount: { type: Number, default: 0 },
  messages: { type: Array, default: () => [] },
  activeId: { type: [Number, String], default: null },
  loading: { type: Boolean, default: false },
  plan: { type: Array, default: () => [] },
  todos: { type: Array, default: () => [] },
  planDone: { type: Number, default: null },
  planTotal: { type: Number, default: null },
  running: { type: Boolean, default: false },
  // 当前流式运行 id（子代理卡片取消按钮用）
  runId: { type: String, default: '' },
  stats: { type: Object, default: null },
  compacting: { type: Boolean, default: false },
  detail: { type: Object, default: null },
})
const emit = defineEmits([
  'change-workspace',
  'refresh-context',
  'compact',
  'open-detail',
  'close-detail',
])

const SECTIONS_KEY = 'rag_inspector_sections'
const SECTION_KEYS = ['context', 'plan', 'tools', 'subagents', 'outputs', 'changes']
const SECTION_DEFAULTS = {
  context: true,
  plan: true,
  tools: true,
  subagents: false,
  outputs: false,
  changes: true,
}

function readSections() {
  try {
    const saved = JSON.parse(localStorage.getItem(SECTIONS_KEY) || '{}')
    const out = {}
    for (const key of SECTION_KEYS) {
      if (typeof saved[key] === 'boolean') out[key] = saved[key]
    }
    return out
  } catch {
    return {}
  }
}

const open = reactive({ ...SECTION_DEFAULTS, ...readSections() })

function persistSections() {
  try {
    localStorage.setItem(SECTIONS_KEY, JSON.stringify({ ...open }))
  } catch {
    // ignore
  }
}

function toggleSection(key) {
  if (!(key in open)) return
  open[key] = !open[key]
  persistSections()
}

/** 供 ChatView 打开指定分区（例如点击聊天里的计划）。 */
function openSection(key) {
  if (!(key in open)) return
  open[key] = true
  persistSections()
}

const detailRun = computed(() => (props.detail ? normalizeRunEntry(props.detail) : null))

const toolRuns = computed(() => flattenToolRuns(props.messages))
const subagents = computed(() => deriveSubagents(props.messages))
const artifacts = computed(() => deriveArtifacts(props.messages))
const changes = computed(() => deriveChanges(props.messages))

// 历史计划从最近一次运行记录读取（流式期间优先用实时 plan）
const historyPlan = ref([])
watch(
  () => props.activeId,
  async (id) => {
    historyPlan.value = []
    if (id == null) return
    try {
      const runs = await listRuns({ conversation_id: id, limit: 1 })
      // 拉取期间可能已切走会话：A 的计划不能落到 B 上
      if (props.activeId !== id) return
      const plan = Array.isArray(runs) && Array.isArray(runs[0]?.plan) ? runs[0].plan : []
      historyPlan.value = plan
    } catch {
      if (props.activeId === id) historyPlan.value = []
    }
  },
  { immediate: true },
)

const planCount = computed(() => {
  const steps = props.plan.length ? props.plan : historyPlan.value
  return props.todos.length || steps.length
})

const available = computed(() => ({
  context: true,
  plan: planCount.value > 0,
  tools: toolRuns.value.length > 0,
  subagents: subagents.value.length > 0,
  outputs: artifacts.value.length > 0,
  changes: changes.value.length > 0,
}))
let prevAvailable = { ...available.value }

// 只在运行中出现新数据时自动展开：加载历史/切会话不覆盖用户的折叠状态
watch(
  available,
  (now) => {
    if (props.running) {
      for (const key of SECTION_KEYS) {
        if (key !== 'context' && now[key] && !prevAvailable[key]) open[key] = true
      }
    }
    prevAvailable = { ...now }
  },
  { deep: true },
)

watch(
  () => props.activeId,
  () => {
    prevAvailable = { ...available.value }
  },
)

defineExpose({ openSection })
</script>
