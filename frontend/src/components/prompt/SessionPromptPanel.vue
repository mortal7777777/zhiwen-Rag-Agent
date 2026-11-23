<template>
  <el-dialog
    :model-value="modelValue"
    title="本会话的提示词版本"
    width="640px"
    append-to-body
    @update:model-value="$emit('update:modelValue', $event)"
    @open="load"
  >
    <div v-loading="loading" class="sp-body">
      <template v-if="status">
        <div class="sp-row">
          <span class="sp-label">当前锁定</span>
          <span v-if="status.locked" class="sp-value">
            {{ status.locked.template_name || status.locked.template_key || '模板' }}
            · v{{ status.locked.template_version_no }}
            <span class="sp-sub">bundle {{ shortHash(status.locked.bundle_hash) }}</span>
          </span>
          <span v-else class="sp-value sp-muted">尚未锁定（下次对话自动锁定最新版本）</span>
        </div>
        <div class="sp-row">
          <span class="sp-label">最新版本</span>
          <span v-if="status.latest" class="sp-value">
            {{ status.latest.template_name || status.latest.template_key || '模板' }}
            · v{{ status.latest.template_version_no }}
          </span>
          <span v-else class="sp-value sp-muted">无可用的最新版本</span>
        </div>
        <div v-if="outdated.length" class="sp-outdated">
          <div class="sp-label">可升级项</div>
          <div class="sp-tags">
            <el-tag
              v-for="item in outdated"
              :key="item.key"
              size="small"
              type="warning"
              effect="plain"
            >
              {{ item.title }}<template v-if="item.name"> · {{ item.name }}</template>
              <span
                v-if="item.locked_version_no != null || item.latest_version_no != null"
                class="sp-version"
              >
                v{{ item.locked_version_no ?? '—' }} → v{{ item.latest_version_no ?? '?' }}
              </span>
            </el-tag>
          </div>
        </div>
        <div class="sp-explain">
          锁定后，编辑模板/片段不会改变本会话的提示词，避免对话中途断掉前缀缓存；
          手动升级会改变系统提示词，<strong>下一轮请求的前缀缓存失效一次</strong>。
        </div>
      </template>
      <el-empty v-else description="暂无版本信息" />
    </div>
    <template #footer>
      <el-button @click="$emit('update:modelValue', false)">关闭</el-button>
      <el-button
        type="primary"
        :loading="applying"
        :disabled="!status?.upgradable"
        @click="applyLatest"
      >
        应用最新版本
      </el-button>
    </template>
  </el-dialog>
</template>

<script setup>
import { computed, ref } from 'vue'
import { ElMessage } from 'element-plus/es/components/message/index'
import { ElMessageBox } from 'element-plus/es/components/message-box/index'
import { applyLatestConversationPrompt, getConversationPrompt } from '../../api'

const props = defineProps({
  modelValue: { type: Boolean, default: false },
  conversationId: { type: Number, default: null },
})
const emit = defineEmits(['update:modelValue', 'applied'])

const loading = ref(false)
const applying = ref(false)
const status = ref(null)

const outdated = computed(() => {
  const details = status.value?.outdated_details
  if (details?.length) return details
  return (status.value?.outdated_units || []).map((key) => ({
    key,
    title: key === 'template' ? '角色模板' : key,
    name: '',
    locked_version_no: null,
    latest_version_no: null,
  }))
})

function shortHash(value) {
  return value ? String(value).slice(0, 10) : '—'
}

async function load() {
  if (!props.conversationId) {
    status.value = null
    return
  }
  loading.value = true
  try {
    status.value = await getConversationPrompt(props.conversationId)
  } catch (error) {
    ElMessage.error(error.response?.data?.detail || '读取会话提示词状态失败')
    status.value = null
  } finally {
    loading.value = false
  }
}

async function applyLatest() {
  if (!props.conversationId) return
  try {
    await ElMessageBox.confirm(
      '应用最新版本会改变本会话的系统提示词，下一轮请求的历史前缀缓存将失效一次；只影响后续回答，不会删除历史。继续？',
      '应用最新提示词',
      { type: 'warning', confirmButtonText: '应用', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  applying.value = true
  try {
    const result = await applyLatestConversationPrompt(props.conversationId)
    ElMessage.success(result.message || '已应用最新版本')
    await load()
    emit('applied', status.value)
  } catch (error) {
    ElMessage.error(error.response?.data?.detail || '应用失败')
  } finally {
    applying.value = false
  }
}

defineExpose({ load, status })
</script>

<style scoped>
.sp-body {
  min-height: 150px;
}
.sp-row {
  display: flex;
  gap: 12px;
  padding: 8px 0;
  border-bottom: 1px solid var(--el-border-color-lighter);
}
.sp-label {
  width: 76px;
  flex-shrink: 0;
  color: var(--el-text-color-secondary);
  font-size: 13px;
}
.sp-value {
  font-size: 13px;
  font-weight: 500;
}
.sp-sub {
  margin-left: 8px;
  font-weight: 400;
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
.sp-muted {
  font-weight: 400;
  color: var(--el-text-color-secondary);
}
.sp-outdated {
  padding: 10px 0;
  border-bottom: 1px solid var(--el-border-color-lighter);
}
.sp-tags {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-top: 6px;
}
.sp-version {
  margin-left: 4px;
  font-size: 11px;
  opacity: 0.8;
}
.sp-explain {
  margin-top: 14px;
  font-size: 12px;
  line-height: 1.7;
  color: var(--el-text-color-secondary);
}
</style>
