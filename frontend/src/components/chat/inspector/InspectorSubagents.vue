<template>
  <div class="inspector-stack">
    <div v-if="!subs.length" class="inspector-empty">
      本会话还没有子代理运行记录（派发子任务后出现在这里）
    </div>

    <article v-for="sub in subs" :key="sub.key" class="isub-card">
      <header
        class="isub-head"
        role="button"
        tabindex="0"
        @click="toggle(sub.key)"
        @keydown.enter.prevent="toggle(sub.key)"
      >
        <span :class="['itool-dot', sub.status]"></span>
        <span class="isub-task">{{ sub.task }}</span>
        <el-tag v-if="sub.profile || sub.mode" size="small" effect="plain">
          {{ sub.profile || modeLabel(sub.mode) }}
        </el-tag>
        <el-tag v-if="sub.cancelled" size="small" type="info" effect="plain">
          已取消
        </el-tag>
        <span class="isub-turn">第 {{ sub.turn }} 轮</span>
        <el-button
          v-if="canCancel(sub)"
          class="isub-cancel"
          size="small"
          text
          type="danger"
          :loading="cancelling.has(sub.subtaskId)"
          @click.stop="cancelSub(sub)"
        >
          取消
        </el-button>
        <el-icon class="itool-chev" :class="{ open: expanded.has(sub.key) }" :size="12">
          <ArrowDown />
        </el-icon>
      </header>
      <p class="isub-summary">{{ sub.summary || (sub.status === 'running' ? (sub.currentHint || '执行中…') : '（无摘要）') }}</p>
      <div class="isub-meta">
        <span v-if="sub.status === 'running' && sub.currentTool">正在调用 {{ sub.currentTool }}</span>
        <span v-if="sub.maxRounds">轮次 {{ sub.rounds }}/{{ sub.maxRounds }}</span>
        <span v-else-if="sub.rounds">执行 {{ sub.rounds }} 轮</span>
        <span v-if="sub.durationMs != null">{{ fmtDuration(sub.durationMs) }}</span>
        <span v-if="sub.tokens && sub.tokens.total_tokens">tokens {{ sub.tokens.total_tokens }}</span>
        <span v-if="sub.artifacts.length">产物 {{ sub.artifacts.length }}</span>
        <span v-if="sub.commands.length">命令 {{ sub.commands.length }}</span>
        <span v-if="sub.filesRead.length">读取 {{ sub.filesRead.length }}</span>
        <span v-if="sub.verify">已验证</span>
        <span v-if="sub.findings.length">发现 {{ sub.findings.length }}</span>
      </div>

      <div v-if="expanded.has(sub.key)" class="isub-detail">
        <div v-if="sub.findings.length" class="isub-section">
          <div class="itool-detail-title">结构化发现（{{ sub.findings.length }}）</div>
          <div v-for="(f, i) in sub.findings" :key="`f-${i}`" class="isub-finding">
            <el-tag size="small" :type="severityType(f.severity)" effect="plain">
              {{ f.severity }}
            </el-tag>
            <span v-if="f.path" class="isub-path">
              {{ f.path }}<template v-if="f.line">:{{ f.line }}</template>
            </span>
            <span class="isub-claim">{{ f.claim }}</span>
            <span v-if="f.evidence" class="isub-evidence">证据：{{ f.evidence }}</span>
            <span v-if="f.trigger" class="isub-evidence">触发：{{ f.trigger }}</span>
          </div>
        </div>
        <div v-if="sub.openQuestions.length" class="isub-section">
          <div class="itool-detail-title">未决问题</div>
          <div v-for="(q, i) in sub.openQuestions" :key="`q-${i}`" class="isub-read">{{ q }}</div>
        </div>
        <div v-if="sub.artifacts.length" class="isub-section">
          <div class="itool-detail-title">产物</div>
          <div v-for="(a, i) in sub.artifacts" :key="`${a.path}-${i}`" class="isub-artifact">
            <span :class="['isub-action', a.action === 'modified' ? 'modified' : 'created']">
              {{ a.action === 'modified' ? '改动' : '新建' }}
            </span>
            <span class="isub-path" :title="a.path">{{ a.path }}</span>
            <span v-if="a.bytes" class="isub-bytes">{{ a.bytes }}B</span>
          </div>
        </div>
        <div v-if="sub.commands.length" class="isub-section">
          <div class="itool-detail-title">执行的命令</div>
          <pre>{{ sub.commands.map((c) => c.command).join('\n') }}</pre>
        </div>
        <div v-if="sub.filesRead.length" class="isub-section">
          <div class="itool-detail-title">读取的文件</div>
          <div v-for="path in sub.filesRead" :key="path" class="isub-read">{{ path }}</div>
        </div>
        <div v-if="sub.verify" class="isub-section">
          <div class="itool-detail-title">写后验证</div>
          <pre>{{ formatVerify(sub.verify) }}</pre>
        </div>
      </div>
    </article>
  </div>
</template>

<script setup>
/** 子代理页签：任务、摘要、产物、命令、读取与验证；运行中可单独取消。 */
import { computed, ref } from 'vue'
import { ArrowDown } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus/es/components/message/index'
import { cancelAgentSubtask } from '../../../api'
import { deriveSubagents } from '../../../composables/useInspectorModel'

const props = defineProps({
  messages: { type: Array, default: () => [] },
  // 当前运行 id（session 事件下发）与运行态：决定是否显示取消按钮
  runId: { type: String, default: '' },
  running: { type: Boolean, default: false },
})

const subs = computed(() => deriveSubagents(props.messages))
const expanded = ref(new Set())
const cancelling = ref(new Set())

function canCancel(sub) {
  return Boolean(
    props.running &&
      props.runId &&
      sub.subtaskId &&
      sub.status === 'running' &&
      !cancelling.value.has(sub.subtaskId),
  )
}

async function cancelSub(sub) {
  if (!canCancel(sub)) return
  const next = new Set(cancelling.value)
  next.add(sub.subtaskId)
  cancelling.value = next
  try {
    const res = await cancelAgentSubtask(props.runId, sub.subtaskId)
    if (res?.cancelled) {
      ElMessage.success('已请求取消该子任务（当前调用结束后生效）')
    } else {
      ElMessage.warning(
        res?.reason === 'subtask not found' ? '该子任务已结束' : '取消失败',
      )
    }
  } catch (error) {
    ElMessage.error(error.response?.data?.detail || '取消失败')
  } finally {
    const rest = new Set(cancelling.value)
    rest.delete(sub.subtaskId)
    cancelling.value = rest
  }
}

const MODE_LABELS = {
  research: '检索',
  explore: '侦察',
  review: '审查',
  execute: '执行',
}

function modeLabel(mode) {
  return MODE_LABELS[mode] || mode
}

function severityType(severity) {
  const level = String(severity || '').toUpperCase()
  if (level === 'CRITICAL' || level === 'HIGH') return 'danger'
  if (level === 'MEDIUM') return 'warning'
  return 'info'
}

function fmtDuration(ms) {
  return ms < 1000 ? `${ms}ms` : `${(ms / 1000).toFixed(1)}s`
}

function toggle(key) {
  const next = new Set(expanded.value)
  if (next.has(key)) next.delete(key)
  else next.add(key)
  expanded.value = next
}

function formatVerify(verify) {
  if (Array.isArray(verify)) {
    return verify
      .map((v) => `${v.command || 'verify'} → ${v.exit_code === 0 ? '通过' : `退出码 ${v.exit_code}`}`)
      .join('\n')
  }
  try {
    return JSON.stringify(verify, null, 2)
  } catch {
    return String(verify)
  }
}
</script>
