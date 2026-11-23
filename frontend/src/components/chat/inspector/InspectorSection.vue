<template>
  <section class="isec" :class="{ open }">
    <button
      type="button"
      class="isec-head"
      :aria-expanded="open ? 'true' : 'false'"
      @click="emit('toggle')"
    >
      <el-icon class="isec-chev" :size="12"><ArrowRight /></el-icon>
      <el-icon v-if="icon" class="isec-icon" :size="14">
        <component :is="icon" />
      </el-icon>
      <span class="isec-title">{{ title }}</span>
      <span v-if="live" class="isec-live"><i />运行中</span>
      <span v-if="count" class="inspector-badge">{{ count > 99 ? '99+' : count }}</span>
    </button>
    <div v-show="open" class="isec-body">
      <slot />
    </div>
  </section>
</template>

<script setup>
/** 会话面板的分区头：点击展开/收起，body 用 v-show 保留子组件状态。 */
import { ArrowRight } from '@element-plus/icons-vue'

defineProps({
  title: { type: String, required: true },
  icon: { type: [Object, Function], default: null },
  count: { type: Number, default: 0 },
  live: { type: Boolean, default: false },
  open: { type: Boolean, default: false },
})
const emit = defineEmits(['toggle'])
</script>
