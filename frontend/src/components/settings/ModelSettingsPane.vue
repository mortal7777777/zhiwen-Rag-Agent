<template>
  <div class="settings-pane">
    <div class="pane-head">
      <div class="pane-title">模型与供应商</div>
      <div class="pane-sub">参考 cc-switch：多供应商管理，切换立即生效</div>
    </div>

    <div class="settings-section">
      <div class="section-head">
        <span class="settings-label">对话模型</span>
        <div class="section-actions">
          <el-button size="small" :disabled="ioBusy" @click="providerImportInput?.click()">
            导入
          </el-button>
          <el-button size="small" :disabled="ioBusy" @click="exportProvidersToFile(false)">
            导出
          </el-button>
          <el-button size="small" :disabled="ioBusy" @click="exportProvidersToFile(true)">
            导出（含密钥）
          </el-button>
          <el-button size="small" :loading="speedtesting" @click="runSpeedtest">
            测速全部
          </el-button>
          <el-button size="small" type="primary" plain @click="openProviderDialog('chat')">
            <el-icon :size="13"><Plus /></el-icon>
            添加供应商
          </el-button>
          <input
            ref="providerImportInput"
            type="file"
            accept=".json,application/json"
            style="display: none"
            @change="onProviderImportFile"
          />
        </div>
      </div>
      <div v-if="!chatProviders.length" class="provider-empty">
        还没有对话模型供应商，点击"添加供应商"配置一个
      </div>
      <div
        v-for="p in chatProviders"
        :key="p.id"
        :class="['provider-card', { inactive: !p.enabled }]"
      >
        <div class="provider-head">
          <el-radio
            v-model="activeChatId"
            :value="p.id"
            :disabled="!p.enabled"
            class="provider-radio"
          >
            当前
          </el-radio>
          <span class="provider-name">{{ p.name }}</span>
          <el-tag size="small" effect="plain" type="primary">对话</el-tag>
          <span class="provider-flex"></span>
          <el-tooltip
            v-if="p._testResult"
            :content="p._testResult.ok
              ? `连接正常；模型列表 ${p._testResult.models_count ?? '不可用'} 个`
              : (p._testResult.error || '测试失败')"
            placement="top"
          >
            <span :class="['probe-badge', p._testResult.ok ? 'ok' : 'fail']">
              {{ p._testResult.ok ? `✓ ${p._testResult.latency_ms}ms` : '✗ 失败' }}
            </span>
          </el-tooltip>
          <el-tooltip
            v-if="p._speedResult"
            :content="p._speedResult.ok ? '最近一次测速结果' : (p._speedResult.error || '测速失败')"
            placement="top"
          >
            <span :class="['probe-badge', p._speedResult.ok ? 'ok' : 'fail']">
              ⚡ {{ p._speedResult.ok ? `${p._speedResult.latency_ms}ms` : '失败' }}
            </span>
          </el-tooltip>
          <el-button size="small" :loading="p._testing" @click="testProviderCard(p)">
            测试
          </el-button>
          <el-switch v-model="p.enabled" size="small" @change="onProviderEnabledChange(p)" />
          <el-tooltip content="删除供应商" placement="top">
            <el-button link type="danger" size="small" class="provider-del" @click="removeProvider(p)">
              <el-icon :size="14"><Delete /></el-icon>
            </el-button>
          </el-tooltip>
        </div>
        <div class="provider-grid">
          <div class="field">
            <label>接口地址 Base URL</label>
            <el-input v-model="p.base_url" placeholder="https://api.example.com" />
          </div>
          <div class="field">
            <label>默认模型</label>
            <el-select
              v-model="p.model"
              filterable
              allow-create
              default-first-option
              placeholder="选择或输入模型 ID"
            >
              <el-option v-for="m in p.models" :key="m" :label="m" :value="m" />
            </el-select>
          </div>
          <div class="field wide">
            <label>模型列表（逗号分隔）</label>
            <div style="display: flex; gap: 8px">
              <el-input v-model="p._modelsText" placeholder="model-a, model-b" />
              <el-button
                size="small"
                :loading="p._modelsLoading"
                @click="fetchChatModels(p)"
              >
                拉取模型
              </el-button>
              <el-button size="small" @click="openModelDialog(p)">管理模型</el-button>
            </div>
          </div>
          <div class="field">
            <label>API Key</label>
            <el-input
              v-model="p._keyInput"
              type="password"
              show-password
              :placeholder="p.api_key ? `已设置 ${p.api_key}（留空保持不变）` : '未设置'"
            />
          </div>
          <div class="field">
            <label>思考模式（DeepSeek V4 等支持）</label>
            <el-switch v-model="p.thinking_enabled" />
          </div>
          <div class="field">
            <label>思考强度</label>
            <el-select v-model="p.thinking_effort" :disabled="!p.thinking_enabled" style="width: 100%">
              <el-option label="低 low" value="low" />
              <el-option label="中 medium" value="medium" />
              <el-option label="高 high（默认）" value="high" />
              <el-option label="最高 max" value="max" />
            </el-select>
          </div>
          <div class="field">
            <label>接口格式</label>
            <el-select v-model="p.api_format" style="width: 100%">
              <el-option label="自动（按地址推断）" value="" />
              <el-option label="OpenAI 兼容" value="openai" />
              <el-option label="Anthropic 兼容" value="anthropic" />
            </el-select>
          </div>
          <div class="field">
            <label>输出上限 max_tokens（留空=默认）</label>
            <el-input-number
              v-model="p.max_tokens"
              :min="256"
              :max="200000"
              :step="1024"
              :value-on-clear="null"
              controls-position="right"
              style="width: 100%"
            />
          </div>
          <div class="field">
            <label>温度（留空=全局；Anthropic 上界 1）</label>
            <el-input-number
              v-model="p.temperature"
              :min="0"
              :max="2"
              :step="0.1"
              :precision="1"
              :value-on-clear="null"
              controls-position="right"
              style="width: 100%"
            />
          </div>
          <div class="field wide">
            <label>自定义请求头（每行 Key: Value；敏感名自动脱敏）</label>
            <el-input
              v-model="p._headersText"
              type="textarea"
              :rows="2"
              placeholder="例如：X-Custom-Header: value"
            />
          </div>
        </div>
      </div>
    </div>

    <div class="settings-section">
      <div class="section-head">
        <span class="settings-label">视觉模型（识图 / OCR）</span>
        <el-button size="small" type="primary" plain @click="openProviderDialog('vision')">
          <el-icon :size="13"><Plus /></el-icon>
          添加供应商
        </el-button>
      </div>
      <div v-if="!visionProviders.length" class="provider-empty">
        还没有视觉模型供应商（可选，未配置时图片功能不可用）
      </div>
      <div
        v-for="p in visionProviders"
        :key="p.id"
        :class="['provider-card', { inactive: !p.enabled }]"
      >
        <div class="provider-head">
          <el-radio
            v-model="activeVisionId"
            :value="p.id"
            :disabled="!p.enabled"
            class="provider-radio"
          >
            当前
          </el-radio>
          <span class="provider-name">{{ p.name }}</span>
          <el-tag size="small" effect="plain" type="success">视觉</el-tag>
          <span class="provider-flex"></span>
          <el-switch v-model="p.enabled" size="small" />
          <el-tooltip content="删除供应商" placement="top">
            <el-button link type="danger" size="small" class="provider-del" @click="removeProvider(p)">
              <el-icon :size="14"><Delete /></el-icon>
            </el-button>
          </el-tooltip>
        </div>
        <div class="provider-grid">
          <div class="field">
            <label>接口地址 Base URL</label>
            <el-input v-model="p.base_url" placeholder="https://token.sensenova.cn/v1" />
          </div>
          <div class="field">
            <label>默认模型</label>
            <el-select
              v-model="p.model"
              filterable
              allow-create
              default-first-option
              placeholder="选择或输入模型 ID"
            >
              <el-option v-for="m in p.models" :key="m" :label="m" :value="m" />
            </el-select>
          </div>
          <div class="field wide">
            <label>模型列表（逗号分隔）</label>
            <el-input v-model="p._modelsText" placeholder="sensenova-6.8-flash-lite" />
          </div>
          <div class="field">
            <label>API Key</label>
            <el-input
              v-model="p._keyInput"
              type="password"
              show-password
              :placeholder="p.api_key ? `已设置 ${p.api_key}（留空保持不变）` : '未设置'"
            />
          </div>
        </div>
      </div>
    </div>

    <div class="settings-section">
      <div class="section-head">
        <span class="settings-label">嵌入模型（Embedding）</span>
      </div>
      <div class="generic-form">
        <div class="generic-row">
          <span class="generic-label">提供方式</span>
          <el-radio-group v-model="embeddingProvider" size="small">
            <el-radio-button value="local">本地 BGE</el-radio-button>
            <el-radio-button value="api">API</el-radio-button>
          </el-radio-group>
        </div>
        <template v-if="embeddingProvider === 'api'">
          <div class="generic-row">
            <span class="generic-label">Base URL</span>
            <el-input
              v-model="embeddingApi.base_url"
              size="small"
              placeholder="https://api.siliconflow.cn/v1"
              class="generic-select"
            />
          </div>
          <div class="generic-row">
            <span class="generic-label">模型</span>
            <el-input
              v-model="embeddingApi.model"
              size="small"
              placeholder="BAAI/bge-m3（留空用供应商默认）"
              class="generic-select"
            />
          </div>
          <div class="generic-row">
            <span class="generic-label">API Key</span>
            <el-input
              v-model="embeddingApi.api_key"
              type="password"
              show-password
              size="small"
              :placeholder="embeddingApi._masked ? `已设置 ${embeddingApi._masked}（留空保持不变）` : '未设置'"
              class="generic-select"
            />
          </div>
        </template>
        <div v-else class="settings-tip">
          使用本地 BGE 模型（bge-base-zh-v1.5，768 维），无需网络与密钥；
          切换为 API 后需点击下方「保存」并重建索引。
        </div>
      </div>
    </div>

    <div class="settings-section">
      <div class="section-head">
        <span class="settings-label">重排序模型（Reranker）</span>
      </div>
      <div class="generic-form">
        <div class="generic-row">
          <span class="generic-label">提供方式</span>
          <el-radio-group v-model="rerankerProvider" size="small">
            <el-radio-button value="local">本地 BGE</el-radio-button>
            <el-radio-button value="api">API</el-radio-button>
          </el-radio-group>
        </div>
        <template v-if="rerankerProvider === 'api'">
          <div class="generic-row">
            <span class="generic-label">Base URL</span>
            <el-input
              v-model="rerankerApi.base_url"
              size="small"
              placeholder="https://api.siliconflow.cn/v1"
              class="generic-select"
            />
          </div>
          <div class="generic-row">
            <span class="generic-label">模型</span>
            <el-input
              v-model="rerankerApi.model"
              size="small"
              placeholder="BAAI/bge-reranker-v2-m3（留空用供应商默认）"
              class="generic-select"
            />
          </div>
          <div class="generic-row">
            <span class="generic-label">API Key</span>
            <el-input
              v-model="rerankerApi.api_key"
              type="password"
              show-password
              size="small"
              :placeholder="rerankerApi._masked ? `已设置 ${rerankerApi._masked}（留空保持不变）` : '未设置'"
              class="generic-select"
            />
          </div>
        </template>
        <div v-else class="settings-tip">
          使用本地 BGE 重排序模型（bge-reranker-v2-m3），无需网络与密钥。
        </div>
      </div>
    </div>

    <div class="settings-section">
      <div class="settings-label">通用</div>
      <div class="generic-form">
        <div class="generic-row">
          <span class="generic-label">回答温度</span>
          <el-input-number v-model="temperature" :min="0" :max="2" :step="0.1" size="small" />
        </div>
      </div>
    </div>

    <div class="settings-save-row">
      <el-button type="primary" :loading="modelSaving" @click="saveModelSettings">
        保存并立即生效
      </el-button>
    </div>

    <!-- 新增供应商对话框：append-to-body 已传送至 body，不受面板 v-show 影响 -->
    <!-- 新增供应商对话框 -->
    <el-dialog
      v-model="providerDialogVisible"
      :title="providerDialogType === 'chat' ? '添加对话模型供应商' : '添加视觉模型供应商'"
      width="460px"
      append-to-body
    >
      <el-form :model="providerForm" label-position="top">
        <el-form-item v-if="providerDialogType === 'chat'" label="从预设开始（可选）">
          <el-select
            v-model="providerForm.presetKey"
            clearable
            placeholder="选择常见供应商自动填充"
            style="width: 100%"
            @change="applyPreset"
          >
            <el-option v-for="preset in presets" :key="preset.key" :label="preset.name" :value="preset.key" />
          </el-select>
        </el-form-item>
        <el-form-item label="供应商名称">
          <el-input v-model="providerForm.name" placeholder="例如：DeepSeek / 自定义 OpenAI 兼容" />
        </el-form-item>
        <el-form-item label="接口地址 Base URL">
          <el-input v-model="providerForm.base_url" placeholder="https://api.deepseek.com" />
        </el-form-item>
        <el-form-item label="默认模型">
          <el-input v-model="providerForm.model" placeholder="模型 ID，例如 deepseek-v4-flash" />
        </el-form-item>
        <el-form-item label="模型列表（逗号分隔，可选）">
          <div style="display: flex; gap: 8px; width: 100%">
            <el-input v-model="providerForm.modelsText" placeholder="model-a, model-b" />
            <el-button
              v-if="providerDialogType === 'chat'"
              size="small"
              :loading="providerSaving"
              @click="fetchDraftModels"
            >
              拉取模型
            </el-button>
          </div>
        </el-form-item>
        <el-form-item label="API Key">
          <el-input v-model="providerForm.apiKey" type="password" show-password placeholder="sk-..." />
        </el-form-item>
        <template v-if="providerDialogType === 'chat'">
          <el-form-item label="思考模式（DeepSeek V4 等支持）">
            <el-switch v-model="providerForm.thinkingEnabled" />
          </el-form-item>
          <el-form-item v-if="providerForm.thinkingEnabled" label="思考强度">
            <el-select v-model="providerForm.thinkingEffort" style="width: 100%">
              <el-option label="低 low" value="low" />
              <el-option label="中 medium" value="medium" />
              <el-option label="高 high（默认）" value="high" />
              <el-option label="最高 max" value="max" />
            </el-select>
          </el-form-item>
          <el-form-item label="接口格式">
            <el-select v-model="providerForm.apiFormat" style="width: 100%">
              <el-option label="自动（按地址推断）" value="" />
              <el-option label="OpenAI 兼容" value="openai" />
              <el-option label="Anthropic 兼容（如 /anthropic、MiniMax on Go）" value="anthropic" />
            </el-select>
          </el-form-item>
          <el-form-item label="输出上限 max_tokens（留空=默认）">
            <el-input-number
              v-model="providerForm.maxTokens"
              :min="256"
              :max="200000"
              :step="1024"
              :value-on-clear="null"
              controls-position="right"
              style="width: 100%"
            />
          </el-form-item>
          <el-form-item label="温度（留空=全局）">
            <el-input-number
              v-model="providerForm.temperature"
              :min="0"
              :max="2"
              :step="0.1"
              :precision="1"
              :value-on-clear="null"
              controls-position="right"
              style="width: 100%"
            />
          </el-form-item>
          <el-form-item label="自定义请求头（每行 Key: Value，可选）">
            <el-input
              v-model="providerForm.headersText"
              type="textarea"
              :rows="2"
              placeholder="例如：X-Custom-Header: value"
            />
          </el-form-item>
        </template>
      </el-form>
      <template #footer>
        <el-button @click="providerDialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="providerSaving" @click="confirmAddProvider">
          添加
        </el-button>
      </template>
    </el-dialog>

    <!-- 模型管理对话框（勾选式编辑，状态在 useModelSettings） -->
    <ModelListDialog />
  </div>
</template>

<script setup>
/**
 * 设置页 · 模型与供应商面板（从 App.vue 抽出，2026-10-09 设置页拆分）。
 * 全部状态与操作来自 useModelSettings（App.vue provide）；
 * 供应商新增对话框随本面板走（append-to-body 已传送，不受 v-show 影响）。
 */
import { inject, ref } from 'vue'
import { Delete, Plus } from '@element-plus/icons-vue'
import ModelListDialog from './ModelListDialog.vue'

const providerImportInput = ref(null)

const {
  activeChatId,
  activeVisionId,
  chatProviders,
  visionProviders,
  temperature,
  embeddingProvider,
  embeddingApi,
  rerankerProvider,
  rerankerApi,
  modelSaving,
  providerDialogVisible,
  providerDialogType,
  providerForm,
  providerSaving,
  presets,
  speedtesting,
  ioBusy,
  exportProvidersToFile,
  importProvidersFromFile,
  openProviderDialog,
  applyPreset,
  fetchDraftModels,
  confirmAddProvider,
  fetchChatModels,
  testProviderCard,
  runSpeedtest,
  openModelDialog,
  onProviderEnabledChange,
  removeProvider,
  saveModelSettings,
} = inject('settingsModel')

function onProviderImportFile(event) {
  const file = event.target.files?.[0]
  importProvidersFromFile(file)
  event.target.value = '' // 允许重复选择同一文件
}
</script>
