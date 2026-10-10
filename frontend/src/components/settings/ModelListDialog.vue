<template>
  <!-- 模型管理对话框：拉取候选 + 勾选 + 手工追加；存储仍是逗号串（list[str]） -->
  <el-dialog
    v-model="modelDialogVisible"
    :title="`管理模型 · ${modelDialogOwnerName}`"
    width="520px"
    append-to-body
  >
    <div class="model-dialog">
      <div class="model-dialog-bar">
        <el-input
          v-model="modelDialogFilter"
          size="small"
          placeholder="搜索模型 ID"
          clearable
          class="model-dialog-search"
        />
        <el-button size="small" :loading="modelDialogLoading" @click="refreshModelOptions()">
          重新拉取
        </el-button>
      </div>
      <div class="model-dialog-list">
        <el-checkbox
          v-for="id in filteredModels"
          :key="id"
          :model-value="modelDialogSelected.includes(id)"
          class="model-dialog-item"
          @change="(checked) => toggleModelOption(id, checked)"
        >
          {{ id }}
        </el-checkbox>
        <div v-if="!filteredModels.length" class="model-dialog-empty">
          暂无候选模型：点「重新拉取」，或在下方手动添加
        </div>
      </div>
      <div class="model-dialog-add">
        <el-input
          v-model="modelDialogNew"
          size="small"
          placeholder="手动添加模型 ID（回车加入）"
          @keydown.enter.prevent="addModelOption"
        />
        <el-button size="small" @click="addModelOption">添加</el-button>
      </div>
      <div class="model-dialog-hint">
        已选 {{ modelDialogSelected.length }} 个；取消勾选默认模型时会自动改用第一个候选。
      </div>
    </div>
    <template #footer>
      <el-button @click="modelDialogVisible = false">取消</el-button>
      <el-button type="primary" @click="modelDialogCommit">确定</el-button>
    </template>
  </el-dialog>
</template>

<script setup>
/**
 * 模型管理对话框（从 ModelSettingsPane 抽出，2026-10-10 供应商页升级）。
 * 状态与动作全部来自 useModelSettings（App.vue provide('settingsModel')）。
 */
import { computed, inject } from 'vue'

const {
  modelDialogVisible,
  modelDialogAll,
  modelDialogSelected,
  modelDialogFilter,
  modelDialogLoading,
  modelDialogNew,
  modelDialogOwnerName,
  refreshModelOptions,
  toggleModelOption,
  addModelOption,
  modelDialogCommit,
} = inject('settingsModel')

const filteredModels = computed(() => {
  const q = String(modelDialogFilter.value || '').trim().toLowerCase()
  if (!q) return modelDialogAll.value
  return modelDialogAll.value.filter((m) => m.toLowerCase().includes(q))
})
</script>

<style scoped>
.model-dialog {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.model-dialog-bar {
  display: flex;
  gap: 8px;
}

.model-dialog-search {
  flex: 1;
}

.model-dialog-list {
  display: flex;
  flex-direction: column;
  gap: 2px;
  max-height: 320px;
  overflow: auto;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  padding: 8px 10px;
}

.model-dialog-item {
  height: 26px;
}

.model-dialog-empty {
  color: var(--text-2);
  font-size: var(--font-xs);
  padding: 8px 0;
}

.model-dialog-add {
  display: flex;
  gap: 8px;
}

.model-dialog-hint {
  color: var(--text-2);
  font-size: var(--font-xs);
}
</style>
