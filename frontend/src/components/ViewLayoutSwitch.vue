<template>
  <div class="view-switch" role="group" aria-label="页面布局">
    <el-tooltip :show-after="1000"
      v-for="opt in options"
      :key="opt.value"
      :content="opt.hint || opt.label"
      placement="top"
      :offset="6"
     :hide-after="800">
      <button
        type="button"
        :class="['view-switch-btn', { active: modelValue === opt.value }]"
        :aria-pressed="modelValue === opt.value ? 'true' : 'false'"
        @click="$emit('update:modelValue', opt.value)"
      >
        <el-icon :size="14"><component :is="opt.icon" /></el-icon>
        <span v-if="showLabel">{{ opt.label }}</span>
      </button>
    </el-tooltip>
  </div>
</template>

<script setup>
/**
 * 轻量布局切换器（分段按钮）：只发出 v-model，页面自己决定把变体类拼到根节点。
 * 图标由页面传入（@element-plus/icons-vue 的组件），标签可关掉只留图标。
 */
defineProps({
  modelValue: { type: String, required: true },
  options: { type: Array, required: true },
  showLabel: { type: Boolean, default: true },
})
defineEmits(['update:modelValue'])
</script>
