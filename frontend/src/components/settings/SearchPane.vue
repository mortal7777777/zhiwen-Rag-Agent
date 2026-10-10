<template>
  <div class="settings-pane">
    <div class="pane-head">
      <div class="pane-title">检索与联网</div>
      <div class="pane-sub">联网搜索 · 知识库检索参数</div>
    </div>

    <div class="settings-section">
      <div class="settings-label">联网搜索</div>
      <div class="generic-form">
        <div class="generic-row">
          <span class="generic-label">搜索供应商</span>
          <el-select v-model="webProvider" size="small" class="generic-select">
            <el-option label="DuckDuckGo（免费）" value="duckduckgo" />
            <el-option label="SearXNG（自托管，无限量）" value="searxng" />
            <el-option label="Tavily" value="tavily" />
            <el-option label="关闭" value="off" />
          </el-select>
        </div>
        <div v-if="webProvider === 'searxng'" class="generic-row">
          <span class="generic-label">SearXNG 地址</span>
          <el-input
            v-model="searxngBaseUrl"
            size="small"
            placeholder="http://localhost:8889"
            class="generic-select"
          />
        </div>
        <div v-if="webProvider === 'searxng'" class="generic-row">
          <span class="generic-label">引擎（可选）</span>
          <el-input
            v-model="searxngEngines"
            size="small"
            placeholder="bing,baidu,sogou（逗号分隔，留空用实例默认）"
            class="generic-select"
          />
        </div>
        <div v-if="webProvider === 'tavily'" class="generic-row">
          <span class="generic-label">Tavily Key</span>
          <el-input
            v-model="tavilyKey"
            type="password"
            show-password
            size="small"
            :placeholder="tavilyMasked ? `已设置 ${tavilyMasked}（留空保持不变）` : '未设置'"
            class="generic-select"
          />
        </div>
        <div class="generic-row">
          <span class="generic-label">结果条数</span>
          <el-input-number v-model="webMaxResults" :min="1" :max="20" size="small" />
        </div>
      </div>
    </div>

    <div class="settings-section">
      <div class="settings-label">知识库检索</div>
      <div class="generic-form">
        <div class="generic-row">
          <span class="generic-label">相关度门槛（0=关闭）</span>
          <el-input-number
            v-model="toolsForm.kb_chat_min_score"
            :min="0"
            :max="1"
            :step="0.01"
            :precision="2"
            size="small"
          />
          <span class="generic-hint">top1 低于此值视为「库里没有相关内容」，直接作答不喂引用</span>
        </div>
        <div class="generic-row">
          <span class="generic-label">低相关时联网兜底（CRAG）</span>
          <el-switch v-model="toolsForm.crag_fallback_enabled" size="small" />
        </div>
        <div class="generic-row">
          <span class="generic-label">联网兜底阈值</span>
          <el-input-number
            v-model="toolsForm.crag_min_score"
            :min="0"
            :max="1"
            :step="0.01"
            :precision="2"
            size="small"
          />
        </div>
        <div class="generic-row">
          <span class="generic-label">单次识图张数上限</span>
          <el-input-number v-model="toolsForm.vision_max_images" :min="1" :max="10" size="small" />
        </div>
      </div>
      <div class="provider-hint">
        CRAG：知识库最佳相关度低于阈值时自动补一次联网检索后再作答。
        相关度门槛 0 = 不做门槛（任何 top1 都当依据）。
      </div>
    </div>

    <div class="settings-save-row">
      <el-button
        type="primary"
        :loading="modelSaving || toolsSaving"
        @click="saveSearchSettings"
      >
        保存检索设置
      </el-button>
    </div>
  </div>
</template>

<script setup>
/**
 * 设置页 · 检索与联网面板（从 App.vue 抽出，2026-10-09 设置页拆分）。
 * 联网搜索字段来自 useModelSettings、知识库检索参数来自 useSettingsForm
 * （App.vue provide 两个域，本面板经 inject 共用）；
 * 保存 = 两个表单的联合提交（模型表单静默 + 工具表单带提示）。
 */
import { inject } from 'vue'

const {
  webProvider,
  searxngBaseUrl,
  searxngEngines,
  tavilyKey,
  tavilyMasked,
  webMaxResults,
  modelSaving,
  saveModelSettings,
} = inject('settingsModel')
const { toolsForm, toolsSaving, saveToolsSettings } = inject('settingsForm')

/** 保存检索设置：联网搜索（模型表单）+ 知识库检索参数（工具表单） */
async function saveSearchSettings() {
  await saveModelSettings(true)
  await saveToolsSettings()
}
</script>
