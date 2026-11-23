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
      <div class="settings-label">布局</div>
      <div class="layout-options">
        <div
          v-for="opt in layoutOptions"
          :key="opt.value"
          :class="['theme-card', 'layout-card', { active: layoutMode === opt.value }]"
          @click="layoutMode = opt.value"
        >
          <div class="lay-wire" :class="'lay-' + opt.value">
            <span class="lw-side">
              <i class="lw-dot"></i>
              <i class="lw-dot"></i>
              <i class="lw-dot"></i>
            </span>
            <span class="lw-main">
              <i class="lw-bar"></i>
              <i class="lw-line"></i>
              <i class="lw-line short"></i>
            </span>
          </div>
          <div class="theme-meta">
            <span class="theme-name">{{ opt.label }}</span>
            <el-icon v-if="layoutMode === opt.value" class="theme-check">
              <CircleCheckFilled />
            </el-icon>
          </div>
        </div>
      </div>
      <div class="merged-row">
        <div class="merged-text">
          <span class="merged-title">会话列表与导航合并为一条侧栏</span>
          <span class="merged-desc">
            开启后导航收成图标条，会话列表紧贴其右侧、同底色无圆角，视觉上是一条栏
            （Claude / Hermes 桌面端的主流做法）。关闭则是两条独立栏。
          </span>
        </div>
        <el-switch v-model="mergedSidebar" />
      </div>
      <div class="merged-row">
        <div class="merged-text">
          <span class="merged-title">按工作区分类会话</span>
          <span class="merged-desc">
            会话按工作目录分组：置顶会话单独置顶，其余按工作区分组、组内再按时间分桶。
            工作区同时是文件与命令工具的工作根；关闭则回到纯时间分组。
          </span>
        </div>
        <el-switch v-model="groupSessionsByWorkspace" />
      </div>
      <div class="settings-tip">
        布局改骨架：导航位置、侧栏形态、图标体系。配色与字体各自独立，可自由组合。
      </div>

      <div class="settings-label" style="margin-top: 22px">主页样式</div>
      <div class="layout-options home-options">
        <div
          v-for="opt in homeOptions"
          :key="opt.value"
          :class="['theme-card', 'home-card', { active: homeStyle === opt.value }]"
          @click="homeStyle = opt.value"
        >
          <div class="home-wire" :class="'hw-' + opt.value">
            <span class="hw-mark"></span>
            <span class="hw-title"></span>
            <span class="hw-input"></span>
            <span class="hw-chip"></span>
            <span class="hw-chip"></span>
          </div>
          <div class="theme-meta">
            <span class="theme-name">{{ opt.label }}</span>
            <el-icon v-if="homeStyle === opt.value" class="theme-check">
              <CircleCheckFilled />
            </el-icon>
          </div>
        </div>
      </div>
      <div class="settings-tip">
        只影响空对话时的欢迎页：品牌块、问候语、推荐问法与快捷入口。四种样式都按聊天列宽自适应，
        窄窗口自动改为单列，不再横向滚动。
      </div>
    </div>

    <div class="settings-section">
      <div class="settings-label">字体</div>
      <div class="font-options">
        <div
          v-for="opt in fontOptions"
          :key="opt.value"
          :class="['theme-card', 'font-card', { active: fontMode === opt.value }]"
          @click="fontMode = opt.value"
        >
          <span class="font-sample" :style="opt.stack ? { fontFamily: opt.stack } : null">
            Aa 知识库
          </span>
          <span class="font-name">{{ opt.label }}</span>
        </div>
      </div>
      <div class="settings-tip">
        「跟随风格」时每种界面风格自带默认字体：书斋/炭火 → 宋体，森野 → 楷体，
        素墨/赛博 → 等宽，静雅 → 等线，其余 → 系统无衬线。正文会整站跟随。
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

const {
  theme,
  contentWidth,
  styleMode,
  layoutMode,
  fontMode,
  mergedSidebar,
  applyWidth,
  homeStyle,
  groupSessionsByWorkspace,
} = inject('settingsAppearance')

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
// 布局：图标体系与导航形态都不同，不是同一套骨架换皮
const homeOptions = [
  { value: 'classic', label: '经典问候' },
  { value: 'minimal', label: '极简启动器' },
  { value: 'hero', label: '巨幅标题' },
  { value: 'studio', label: '工作台' },
]

const layoutOptions = [
  { value: 'standard', label: '标准侧栏' },
  { value: 'rail', label: '极简导轨' },
  { value: 'island', label: '浮岛卡片' },
  { value: 'topbar', label: '顶部横栏' },
  { value: 'claude', label: 'Claude 桌面' },
  { value: 'hermes', label: 'Hermes 桌面' },
]
// 字体：栈全部走 theme.css 里的令牌，避免预览与实际不一致
const fontOptions = [
  { value: 'auto', label: '跟随风格', stack: null },
  { value: 'ui', label: '雅黑 / Segoe', stack: 'var(--font-ui)' },
  { value: 'sans', label: '苹方 / 雅黑', stack: 'var(--font-sans)' },
  { value: 'dengxian', label: '等线', stack: 'var(--font-dengxian)' },
  { value: 'serif', label: '宋体', stack: 'var(--font-serif)' },
  { value: 'kai', label: '楷体', stack: 'var(--font-kai)' },
  { value: 'fangsong', label: '仿宋', stack: 'var(--font-fangsong)' },
  { value: 'mono', label: '等宽', stack: 'var(--font-mono)' },
]
const styleOptions = [
  { value: 'default', label: '默认（品牌蓝）' },
  { value: 'quiet', label: '静奢雅致' },
  { value: 'salon', label: '书斋暖读' },
  { value: 'teal', label: '墨玉（Hermes Teal）' },
  { value: 'midnight', label: '子夜（Midnight）' },
  { value: 'mono', label: '素墨（Mono）' },
  { value: 'ember', label: '炭火（Ember）' },
  { value: 'cyber', label: '赛博（Cyberpunk）' },
  { value: 'catppuccin', label: '卡布（Catppuccin）' },
  { value: 'everforest', label: '森野（Everforest）' },
]

const widthLabel = computed(
  () => widthOptions.find((o) => o.value === contentWidth.value)?.label || contentWidth.value,
)
</script>
