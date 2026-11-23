<template>
  <el-dialog
    :model-value="modelValue"
    :title="title || '提示词预览'"
    width="880px"
    append-to-body
    @update:model-value="$emit('update:modelValue', $event)"
    @open="load"
  >
    <div v-loading="loading" class="pp-body">
      <el-alert
        v-if="warnings.length"
        type="warning"
        :closable="false"
        class="pp-alert"
      >
        <template #title>发布前提示</template>
        <ul class="pp-warnings">
          <li v-for="(item, index) in warnings" :key="index">{{ item }}</li>
        </ul>
      </el-alert>

      <template v-if="result">
        <div class="pp-meta">
          <span>
            来源：{{ sourceLabel }}
            <template v-if="result.template?.name">
              · {{ result.template.name }}
            </template>
            <template v-if="result.template?.version_no">
              · v{{ result.template.version_no }}
            </template>
          </span>
          <span>
            静态核心 {{ result.chars?.static_core ?? 0 }} 字 ·
            历史前 {{ result.chars?.pre_history ?? 0 }} 字 ·
            历史后 {{ result.chars?.post_history ?? 0 }} 字
          </span>
        </div>
        <div class="pp-sub">
          技能目录 {{ result.skills_index?.chars ?? 0 }} 字
          <template v-if="result.skills_index?.names_only">（仅名称）</template>
          · 知识库 {{ result.kb_listed ?? 0 }}/{{ result.kb_total ?? 0 }} 个文档
          <template v-if="result.todos_present"> · 含当前任务清单</template>
        </div>
        <el-tabs v-model="tab" class="pp-tabs">
          <el-tab-pane label="静态核心" name="static">
            <pre class="pp-content">{{ result.static_core }}</pre>
          </el-tab-pane>
          <el-tab-pane label="历史前动态块" name="pre">
            <pre class="pp-content">{{ result.pre_history || '（本轮为空）' }}</pre>
          </el-tab-pane>
          <el-tab-pane label="历史后动态块" name="post">
            <pre class="pp-content">{{ result.post_history || '（本轮为空）' }}</pre>
          </el-tab-pane>
        </el-tabs>
        <div class="pp-note">{{ result.note }}</div>
      </template>
      <el-empty v-else-if="!loading" description="暂无预览" />
    </div>
    <template #footer>
      <el-button @click="$emit('update:modelValue', false)">关闭</el-button>
    </template>
  </el-dialog>
</template>

<script setup>
import { computed, ref } from 'vue'
import { ElMessage } from 'element-plus/es/components/message/index'
import { previewPrompt } from '../../api'

const props = defineProps({
  modelValue: { type: Boolean, default: false },
  request: { type: Object, default: () => ({}) },
  title: { type: String, default: '' },
})
defineEmits(['update:modelValue'])

const loading = ref(false)
const result = ref(null)
const tab = ref('static')

const warnings = computed(() => result.value?.warnings || [])
const sourceLabel = computed(() => {
  const source = result.value?.source
  if (source === 'locked') return '本会话锁定版本'
  if (source === 'override') return '未保存内容覆盖'
  if (source === 'fallback') return '代码默认回退'
  return '当前最新版本'
})

async function load() {
  loading.value = true
  result.value = null
  tab.value = 'static'
  try {
    result.value = await previewPrompt(props.request || {})
  } catch (error) {
    ElMessage.error(error.response?.data?.detail || '生成预览失败')
  } finally {
    loading.value = false
  }
}
</script>

<style scoped>
.pp-body {
  min-height: 260px;
}
.pp-alert {
  margin-bottom: 12px;
}
.pp-warnings {
  margin: 4px 0 0;
  padding-left: 18px;
  line-height: 1.7;
}
.pp-meta {
  display: flex;
  flex-wrap: wrap;
  gap: 6px 18px;
  font-size: 13px;
  color: var(--el-text-color-regular);
}
.pp-sub {
  margin-top: 4px;
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
.pp-tabs {
  margin-top: 10px;
}
.pp-content {
  margin: 0;
  max-height: 420px;
  overflow: auto;
  padding: 12px;
  border-radius: 8px;
  background: var(--el-fill-color-lighter);
  white-space: pre-wrap;
  word-break: break-word;
  font-family: var(--font-mono);
  font-size: 12px;
  line-height: 1.65;
}
.pp-note {
  margin-top: 10px;
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
</style>
