<template>
  <el-dialog
    :model-value="modelValue"
    title="提示词影响面"
    width="900px"
    append-to-body
    @update:model-value="$emit('update:modelValue', $event)"
    @open="load"
  >
    <div v-loading="loading" class="pi-body">
      <div class="pi-hint">
        以下会话锁定的是旧提示词版本。升级会让该会话下一轮请求前缀缓存失效一次；
        运行中或有挂起任务的会话会自动跳过。
      </div>
      <el-empty v-if="!loading && !rows.length" description="没有需要升级的会话" />
      <el-table
        v-else
        ref="tableRef"
        :data="rows"
        row-key="id"
        max-height="430"
        @selection-change="onSelectionChange"
      >
        <el-table-column type="selection" width="44" />
        <el-table-column prop="title" label="会话" min-width="180" />
        <el-table-column prop="template_name" label="模板" min-width="140" />
        <el-table-column label="版本" width="120">
          <template #default="{ row }">
            v{{ row.locked_template_version_no ?? '—' }}
            → v{{ row.latest_template_version_no ?? '?' }}
          </template>
        </el-table-column>
        <el-table-column label="待更新项" min-width="220">
          <template #default="{ row }">
            <el-tag
              v-for="item in row.outdated_details || []"
              :key="item.key"
              size="small"
              type="warning"
              effect="plain"
              class="pi-tag"
            >
              {{ item.title }} v{{ item.locked_version_no ?? '—' }} → v{{ item.latest_version_no ?? '?' }}
            </el-tag>
            <span v-if="!row.locked" class="pi-muted">尚未锁定</span>
          </template>
        </el-table-column>
      </el-table>
    </div>
    <template #footer>
      <el-button @click="$emit('update:modelValue', false)">关闭</el-button>
      <el-button
        type="warning"
        plain
        :disabled="!selected.length || applying"
        :loading="applying"
        @click="applySelected(selected)"
      >
        升级选中（{{ selected.length }}）
      </el-button>
      <el-button
        type="primary"
        :disabled="!rows.length || applying"
        :loading="applying"
        @click="applySelected(rows)"
      >
        全部升级
      </el-button>
    </template>
  </el-dialog>
</template>

<script setup>
import { ref } from 'vue'
import { ElMessage } from 'element-plus/es/components/message/index'
import { ElMessageBox } from 'element-plus/es/components/message-box/index'
import { applyLatestBatch, getPromptImpact } from '../../api'

defineProps({
  modelValue: { type: Boolean, default: false },
})
const emit = defineEmits(['update:modelValue', 'changed'])

const loading = ref(false)
const applying = ref(false)
const rows = ref([])
const selected = ref([])
const tableRef = ref(null)

function onSelectionChange(items) {
  selected.value = items
}

async function load() {
  loading.value = true
  try {
    const data = await getPromptImpact(500)
    rows.value = data.conversations || []
  } catch (error) {
    ElMessage.error(error.response?.data?.detail || '读取影响面失败')
    rows.value = []
  } finally {
    loading.value = false
  }
}

async function applySelected(items) {
  const ids = (items || []).map((item) => item.id)
  if (!ids.length) return
  try {
    await ElMessageBox.confirm(
      `将升级 ${ids.length} 个会话；每个会话会断一次提示词前缀缓存。继续？`,
      '批量升级提示词',
      {
        type: 'warning',
        confirmButtonText: '升级',
        cancelButtonText: '取消',
      },
    )
  } catch {
    return
  }
  applying.value = true
  try {
    const result = await applyLatestBatch(ids)
    const skipped = result.skipped?.length || 0
    const failed = result.failed?.length || 0
    ElMessage.success(
      `已升级 ${result.applied_count ?? 0} 个` +
        (skipped ? `，跳过 ${skipped} 个` : '') +
        (failed ? `，失败 ${failed} 个` : ''),
    )
    emit('changed', result)
    await load()
  } catch (error) {
    ElMessage.error(error.response?.data?.detail || '批量升级失败')
  } finally {
    applying.value = false
  }
}
</script>

<style scoped>
.pi-body {
  min-height: 220px;
}
.pi-hint {
  margin-bottom: 10px;
  color: var(--el-text-color-secondary);
  font-size: 12px;
  line-height: 1.7;
}
.pi-tag {
  margin: 2px 6px 2px 0;
}
.pi-muted {
  color: var(--el-text-color-secondary);
  font-size: 12px;
}
</style>
