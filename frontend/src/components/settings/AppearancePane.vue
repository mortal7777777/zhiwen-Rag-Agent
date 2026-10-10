<template>
  <div class="settings-pane">
    <div class="pane-head">
      <div class="pane-title">外观</div>
      <div class="pane-sub">主题与内容布局</div>
    </div>

    <div class="settings-section">
      <div class="settings-label">主题</div>
      <div class="theme-options">
        <div
          v-for="opt in themeOptions"
          :key="opt.value"
          :class="['theme-card', { active: theme === opt.value }]"
          @click="theme = opt.value"
        >
          <div class="theme-preview" :class="opt.value">
            <span class="preview-side"></span>
            <span class="preview-main">
              <i class="preview-line"></i>
              <i class="preview-line short"></i>
              <i class="preview-bubble"></i>
            </span>
          </div>
          <div class="theme-meta">
            <span class="theme-name">{{ opt.label }}</span>
            <el-icon v-if="theme === opt.value" class="theme-check">
              <CircleCheckFilled />
            </el-icon>
          </div>
        </div>
      </div>
    </div>

    <div class="settings-section">
      <div class="settings-label">界面风格</div>
      <div class="theme-options">
        <div
          v-for="opt in styleOptions"
          :key="opt.value"
          :class="['theme-card', { active: styleMode === opt.value }]"
          @click="styleMode = opt.value"
        >
          <div class="style-swatch" :class="opt.value">
            <span class="sw-side"></span>
            <span class="sw-body">
              <i class="sw-line"></i>
              <i class="sw-line short"></i>
            </span>
          </div>
          <div class="theme-meta">
            <span class="theme-name">{{ opt.label }}</span>
            <el-icon v-if="styleMode === opt.value" class="theme-check">
              <CircleCheckFilled />
            </el-icon>
          </div>
        </div>
      </div>
      <div class="settings-tip">
        风格与深浅主题独立生效：改变配色与排版气质，立即应用并记住选择。
      </div>
    </div>

    <div class="settings-section">
      <div class="settings-label">内容宽度</div>
      <el-segmented
        v-model="contentWidth"
        :options="widthOptions"
        class="width-segmented"
        @change="applyWidth"
      />
      <div class="width-feedback">
        <div class="width-preview-track">
          <div
            class="width-preview-bar"
            :style="{ width: contentWidth === '100%' ? '100%' : contentWidth }"
          ></div>
        </div>
        <span class="width-value">当前：{{ widthLabel }}</span>
      </div>
      <div class="settings-tip">
        按窗口比例控制聊天区宽度，各档位始终有明显区别，点击立即生效。
      </div>
    </div>
  </div>
</template>

<script setup>
/**
 * 设置页 · 外观面板（从 App.vue 抽出，2026-10-09 设置页拆分）。
 *
 * 状态由 useAppearance 持有（App.vue 创建并 provide），本组件只提供选项清单
 * 与渲染；标签切换的显隐（v-show）由父组件控制——组件根即原 .settings-pane。
 * 样式（.theme-card/.style-swatch/.width-* 等）在 App.vue 的**全局样式块**里，
 * 拆分后天然继续生效，无需随迁。
 */
import { computed, inject } from 'vue'
import { CircleCheckFilled } from '@element-plus/icons-vue'

const { theme, contentWidth, styleMode, applyWidth } = inject('settingsAppearance')

const themeOptions = [
  { value: 'light', label: '浅色' },
  { value: 'dark', label: '深色' },
  { value: 'system', label: '跟随系统' },
]
const widthOptions = [
  { label: '窄', value: '55%' },
  { label: '标准', value: '70%' },
  { label: '宽', value: '85%' },
  { label: '全宽', value: '100%' },
]
const styleOptions = [
  { value: 'default', label: '默认（品牌蓝）' },
  { value: 'quiet', label: '静奢雅致' },
  { value: 'salon', label: '书斋暖读' },
]

const widthLabel = computed(
  () => widthOptions.find((o) => o.value === contentWidth.value)?.label || contentWidth.value,
)
</script>
