<template>
  <div class="settings-pane">
    <div class="pane-head">
      <div class="pane-title">模型与供应商</div>
      <div class="pane-sub">集中管理对话、视觉、检索与文档解析模型，切换立即生效</div>
    </div>

    <!-- 分组导航：单页不再从头堆到尾，一次只看一类设置 -->
    <SettingsSubnav
      v-model="activeModelTab"
      :tabs="modelTabs"
      storage-key="rag_model_settings_tab"
      label="模型设置分组"
    />

    <div class="settings-section" v-show="activeModelTab === 'chat'">
      <div class="section-head">
        <span class="settings-label">对话模型</span>
        <div class="section-actions">
          <el-dropdown trigger="click" :disabled="ioBusy" @command="onProviderMenu">
            <el-button size="small">
              更多
              <el-icon class="el-icon--right" :size="12"><ArrowDown /></el-icon>
            </el-button>
            <template #dropdown>
              <el-dropdown-menu>
                <el-dropdown-item command="import">导入供应商</el-dropdown-item>
                <el-dropdown-item command="export">导出</el-dropdown-item>
                <el-dropdown-item command="export-keys">导出（含密钥）</el-dropdown-item>
                <el-dropdown-item divided command="speedtest" :disabled="speedtesting">
                  测速全部
                </el-dropdown-item>
              </el-dropdown-menu>
            </template>
          </el-dropdown>
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
        :class="['provider-card', { inactive: !p.enabled, expanded: p._expanded, 'is-current': activeChatId === p.id }]"
      >
        <div
          class="provider-head"
          role="button"
          tabindex="0"
          :aria-expanded="!!p._expanded"
          @click="toggleProvider(p)"
          @keydown.enter.prevent="toggleProvider(p)"
        >
          <span class="provider-pick" @click.stop>
            <el-tooltip :show-after="600" content="设为当前供应商" placement="top" :hide-after="600">
              <el-radio
                v-model="activeChatId"
                :value="p.id"
                :disabled="!p.enabled"
                class="provider-radio"
                aria-label="设为当前供应商"
              />
            </el-tooltip>
          </span>
          <span class="provider-name">{{ p.name }}</span>
          <el-tag v-if="activeChatId === p.id" size="small" type="success" effect="plain">
            使用中
          </el-tag>
          <el-tag size="small" effect="plain" type="primary">对话</el-tag>
          <el-tag v-if="p.supports_vision" size="small" effect="plain" type="success">
            视觉
          </el-tag>
          <el-tag v-if="!p.enabled" size="small" effect="plain" type="info">已停用</el-tag>
          <span class="provider-flex"></span>
          <span class="provider-model" :title="p.model || '未设置默认模型'">
            {{ p.model || '未设模型' }}
          </span>
          <el-button size="small" :loading="p._testing" @click.stop="testProviderCard(p)">
            测试
          </el-button>
          <span class="provider-toggle" @click.stop>
            <el-switch v-model="p.enabled" size="small" @change="onProviderEnabledChange(p)" />
          </span>
          <el-tooltip :show-after="1000" content="删除供应商" placement="top" :hide-after="800">
            <el-button link type="danger" size="small" class="provider-del" @click.stop="removeProvider(p)">
              <el-icon :size="14"><Delete /></el-icon>
            </el-button>
          </el-tooltip>
          <el-button link size="small" @click.stop="toggleProvider(p)">
            {{ p._expanded ? '收起' : '编辑' }}
          </el-button>
          <el-icon class="provider-chev" :class="{ open: p._expanded }" :size="12">
            <ArrowDown />
          </el-icon>
        </div>
        <div class="provider-summary" @click="toggleProvider(p)">
          <span class="provider-summary-item">{{ p.base_url || '未设置接口地址' }}</span>
          <span class="provider-sep">·</span>
          <span>Key {{ p.api_key ? '已设置' : '未设置' }}</span>
          <span class="provider-sep">·</span>
          <span>{{ apiFormatLabel(p) }}</span>
          <template v-if="p.thinking_enabled">
            <span class="provider-sep">·</span>
            <span>思考 {{ thinkingLabel(p.thinking_effort) }}</span>
          </template>
          <template v-if="p.max_tokens">
            <span class="provider-sep">·</span>
            <span>max_tokens {{ p.max_tokens }}</span>
          </template>
          <span class="provider-flex"></span>
          <el-tooltip
            v-if="p._testResult"
            :content="p._testResult.ok
              ? `连接正常；模型列表 ${p._testResult.models_count ?? '不可用'} 个`
              : (p._testResult.error || '测试失败')"
            placement="top"
            :show-after="600"
            :hide-after="600"
          >
            <span :class="['probe-badge', p._testResult.ok ? 'ok' : 'fail']">
              {{ p._testResult.ok ? `✓ ${p._testResult.latency_ms}ms` : '✗ 失败' }}
            </span>
          </el-tooltip>
          <el-tooltip
            v-if="p._speedResult"
            :content="p._speedResult.ok ? '最近一次测速结果' : (p._speedResult.error || '测速失败')"
            placement="top"
            :show-after="600"
            :hide-after="600"
          >
            <span :class="['probe-badge', p._speedResult.ok ? 'ok' : 'fail']">
              ⚡ {{ p._speedResult.ok ? `${p._speedResult.latency_ms}ms` : '失败' }}
            </span>
          </el-tooltip>
        </div>
        <div v-show="p._expanded" class="provider-grid">
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
            <label>接口支持图像输入</label>
            <el-switch v-model="p.supports_vision" />
          </div>
          <div class="field wide">
            <label>支持视觉的模型（逗号分隔，支持通配符）</label>
            <el-input
              v-model="p._visionModelsText"
              placeholder="deepseek-v4.1-flash, *-vl-*"
            />
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

    <div class="settings-section" v-show="activeModelTab === 'vision'">
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
        :class="['provider-card', { inactive: !p.enabled, expanded: p._expanded, 'is-current': activeVisionId === p.id }]"
      >
        <div
          class="provider-head"
          role="button"
          tabindex="0"
          :aria-expanded="!!p._expanded"
          @click="toggleProvider(p)"
          @keydown.enter.prevent="toggleProvider(p)"
        >
          <span class="provider-pick" @click.stop>
            <el-tooltip :show-after="600" content="设为当前供应商" placement="top" :hide-after="600">
              <el-radio
                v-model="activeVisionId"
                :value="p.id"
                :disabled="!p.enabled"
                class="provider-radio"
                aria-label="设为当前供应商"
              />
            </el-tooltip>
          </span>
          <span class="provider-name">{{ p.name }}</span>
          <el-tag v-if="activeVisionId === p.id" size="small" type="success" effect="plain">
            使用中
          </el-tag>
          <el-tag size="small" effect="plain" type="success">视觉</el-tag>
          <el-tag v-if="!p.enabled" size="small" effect="plain" type="info">已停用</el-tag>
          <span class="provider-flex"></span>
          <span class="provider-toggle" @click.stop>
            <el-switch v-model="p.enabled" size="small" />
          </span>
          <el-tooltip :show-after="1000" content="删除供应商" placement="top" :hide-after="800">
            <el-button link type="danger" size="small" class="provider-del" @click.stop="removeProvider(p)">
              <el-icon :size="14"><Delete /></el-icon>
            </el-button>
          </el-tooltip>
          <el-button link size="small" @click.stop="toggleProvider(p)">
            {{ p._expanded ? '收起' : '编辑' }}
          </el-button>
          <el-icon class="provider-chev" :class="{ open: p._expanded }" :size="12">
            <ArrowDown />
          </el-icon>
        </div>
        <div class="provider-summary" @click="toggleProvider(p)">
          <span class="provider-summary-item">{{ p.base_url || '未设置接口地址' }}</span>
          <span class="provider-sep">·</span>
          <span>Key {{ p.api_key ? '已设置' : '未设置' }}</span>
          <span class="provider-sep">·</span>
          <span>{{ p.model || '未设模型' }}</span>
        </div>
        <div v-show="p._expanded" class="provider-grid">
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

    <div class="settings-section" v-show="activeModelTab === 'agents'">
      <div class="section-head">
        <span class="settings-label">子代理类型（提示词 / 工具 / 模型 / 预算）</span>
        <span class="provider-flex"></span>
        <el-button size="small" @click="openAgentTypeDialog()">
          <el-icon><Plus /></el-icon>
          新建类型
        </el-button>
      </div>
      <div class="settings-tip">
        每种类型可指定供应商与模型（允许跨供应商）；留空继承主模型与全局预算。
        点「编辑配置」可修改提示词追加段与工具子集（只能收窄），计划模式仍强制只读。
      </div>
      <div v-for="t in agentTypes" :key="t.key" class="subagent-type">
        <div class="subagent-head">
          <el-tag size="small" effect="plain">{{ t.name }}</el-tag>
          <el-tag
            v-if="modeLabel(t.mode) !== t.name"
            size="small"
            type="info"
            effect="plain"
          >
            {{ modeLabel(t.mode) }}
          </el-tag>
          <span class="subagent-key">{{ t.key }}</span>
          <span class="provider-flex"></span>
          <el-tag
            size="small"
            :type="t.system_prompt ? 'warning' : 'info'"
            effect="plain"
          >
            {{ t.system_prompt ? '自定义提示词' : '提示词默认' }}
          </el-tag>
          <el-tag size="small" effect="plain">
            工具 {{ (t.tools || []).length ? `${t.tools.length} 项` : '默认' }}
          </el-tag>
          <el-button link size="small" @click="openAgentTypeDialog(t)">编辑配置</el-button>
          <el-button v-if="t.is_builtin" link size="small" @click="restoreAgentType(t)">
            恢复默认
          </el-button>
        </div>
        <div class="generic-form">
          <div class="generic-row">
            <span class="generic-label">供应商</span>
            <el-select
              v-model="t.model_provider_id"
              size="small"
              clearable
              placeholder="继承主模型"
              class="generic-select"
              @change="onAgentProviderChange(t)"
            >
              <el-option v-for="p in chatProviders" :key="p.id" :label="p.name" :value="p.id" />
            </el-select>
            <span class="generic-label">模型</span>
            <el-select
              v-model="t.model"
              size="small"
              clearable
              filterable
              allow-create
              placeholder="供应商默认"
              class="generic-select"
            >
              <el-option v-for="m in agentProviderModels(t)" :key="m" :label="m" :value="m" />
            </el-select>
          </div>
          <div class="generic-row">
            <span class="generic-label">思考</span>
            <el-select v-model="t.thinking" size="small" class="generic-select">
              <el-option label="继承供应商" value="inherit" />
              <el-option label="强制开启" value="on" />
              <el-option label="关闭" value="off" />
            </el-select>
            <span class="generic-label">最大轮数</span>
            <el-input v-model.number="t.max_rounds" size="small" placeholder="继承" class="generic-select" />
            <span class="generic-label">调用预算</span>
            <el-input v-model.number="t.llm_budget" size="small" placeholder="继承" class="generic-select" />
          </div>
          <div class="generic-row">
            <span class="provider-flex"></span>
            <el-button
              size="small"
              :loading="agentSavingKey === t.key"
              @click="saveAgentType(t)"
            >
              保存
            </el-button>
          </div>
        </div>
      </div>
      <div v-if="!agentTypes.length" class="settings-tip">类型注册表为空（后端未就绪）。</div>
    </div>

    <el-dialog
      v-model="agentDialogVisible"
      :title="agentEditingKey ? `编辑子代理类型：${agentEditingKey}` : '新建子代理类型'"
      width="680px"
      append-to-body
    >
      <el-form :model="agentForm" label-position="top">
        <el-form-item label="标识 key">
          <el-input
            v-model="agentForm.key"
            :disabled="Boolean(agentEditingKey)"
            placeholder="例如 frontend-review（小写字母/数字/-/_）"
          />
        </el-form-item>
        <el-form-item label="名称">
          <el-input v-model="agentForm.name" maxlength="100" />
        </el-form-item>
        <el-form-item label="描述">
          <el-input v-model="agentForm.description" maxlength="255" />
        </el-form-item>
        <el-form-item label="模式">
          <el-select v-model="agentForm.mode" @change="onAgentFormModeChange">
            <el-option v-for="m in agentModeOptions" :key="m" :label="m" :value="m" />
          </el-select>
          <div class="settings-tip">
            research/explore/review 强制只读；execute 可写文件/执行命令。
          </div>
        </el-form-item>
        <el-form-item label="工具子集">
          <el-select
            v-model="agentForm.tools"
            multiple
            filterable
            placeholder="留空=使用模式默认工具集"
            style="width: 100%"
          >
            <el-option
              v-for="tool in agentToolOptions"
              :key="tool"
              :label="toolLabel(tool)"
              :value="tool"
            />
          </el-select>
          <div class="settings-tip">
            当前生效：{{ agentFormEffectiveTools.join('、') }}
          </div>
          <div class="settings-tip">
            只能收窄，不能扩权；不属于当前模式的工具会被后端拒绝。
            <el-button
              v-if="(agentForm.tools || []).length"
              link
              size="small"
              @click="agentForm.tools = []"
            >
              使用模式默认
            </el-button>
          </div>
        </el-form-item>
        <el-form-item label="提示词（追加段）">
          <el-input
            v-model="agentForm.system_prompt"
            type="textarea"
            :rows="6"
            maxlength="20000"
            show-word-limit
            placeholder="留空=使用模式默认提示词；内容追加在子代理基础安全提示之后。"
          />
          <div v-if="agentForm.system_prompt" class="settings-tip">
            <el-button link size="small" @click="agentForm.system_prompt = ''">
              恢复模式默认提示词
            </el-button>
          </div>
        </el-form-item>
        <el-form-item label="供应商 / 模型">
          <div style="display: flex; gap: 8px; width: 100%">
            <el-select
              v-model="agentForm.model_provider_id"
              clearable
              placeholder="继承主模型"
              style="flex: 1"
              @change="onAgentFormProviderChange"
            >
              <el-option v-for="p in chatProviders" :key="p.id" :label="p.name" :value="p.id" />
            </el-select>
            <el-select
              v-model="agentForm.model"
              clearable
              filterable
              allow-create
              placeholder="供应商默认"
              style="flex: 1"
            >
              <el-option
                v-for="m in agentFormProviderModels"
                :key="m"
                :label="m"
                :value="m"
              />
            </el-select>
          </div>
        </el-form-item>
        <el-form-item label="思考 / 最大轮数 / 调用预算">
          <div style="display: flex; gap: 8px; width: 100%">
            <el-select v-model="agentForm.thinking" style="width: 150px">
              <el-option label="继承供应商" value="inherit" />
              <el-option label="强制开启" value="on" />
              <el-option label="关闭" value="off" />
            </el-select>
            <el-input v-model.number="agentForm.max_rounds" placeholder="轮数（继承）" />
            <el-input v-model.number="agentForm.llm_budget" placeholder="预算（继承）" />
          </div>
        </el-form-item>
        <el-form-item label="启用">
          <el-switch v-model="agentForm.enabled" />
        </el-form-item>
        <el-form-item v-if="agentForm.mode === 'execute'" label="只读执行">
          <el-switch v-model="agentForm.read_only" />
          <span class="settings-tip">开启后该类型降级为 research 工具面（安全收窄）。</span>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button
          v-if="agentEditingKey && !agentForm.is_builtin"
          type="danger"
          plain
          @click="removeAgentTypeDialog"
        >
          删除类型
        </el-button>
        <el-button @click="agentDialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="agentDialogSaving" @click="submitAgentType">
          保存
        </el-button>
      </template>
    </el-dialog>

    <div class="settings-section" v-show="activeModelTab === 'retrieval'">
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

    <div class="settings-section" v-show="activeModelTab === 'retrieval'">
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

    <div class="settings-section" v-show="activeModelTab === 'docs'">
      <div class="section-head">
        <span class="settings-label">文档解析</span>
      </div>
      <div class="generic-form">
        <div class="generic-row">
          <span class="generic-label">PDF 解析模式</span>
          <el-select v-model="pdfParseBackend" size="small" class="generic-select">
            <el-option label="自动（按文件画像）" value="auto" />
            <el-option label="仅原生文本解析" value="pymupdf" />
            <el-option label="复杂文档走 PP-StructureV3" value="paddle" />
          </el-select>
        </div>
        <div class="generic-row">
          <span class="generic-label">OCR Worker 懒启动</span>
          <el-switch v-model="paddleLazyStart" />
        </div>
        <div class="generic-row">
          <span class="generic-label">Worker 空闲退出（秒，0=不退出）</span>
          <el-input-number
            v-model="paddleIdleSeconds"
            :min="0"
            :max="7200"
            :step="60"
            size="small"
          />
        </div>
        <div class="generic-row">
          <span class="generic-label">Worker 单进程页数上限（0=不限）</span>
          <el-input-number
            v-model="paddlePagesPerProcess"
            :min="0"
            :max="5000"
            :step="50"
            size="small"
          />
        </div>
        <div class="generic-row">
          <span class="generic-label">低置信/空页 VLM 兜底</span>
          <el-switch v-model="vlmFallback" />
        </div>
        <div class="generic-row">
          <span class="generic-label">知识库图片问答（视觉辅助）</span>
          <el-switch v-model="kbVisionEnabled" />
        </div>
        <div v-if="kbVisionEnabled" class="generic-row">
          <span class="generic-label">单次最多送图数</span>
          <el-input-number v-model="kbVisionMaxImages" :min="1" :max="6" size="small" />
        </div>
        <div class="settings-tip">
          Worker 默认 CPU；懒启动后首次复杂解析需 20–40 秒预热，空闲到点自动退出释放内存。
          VLM 兜底需要配置视觉模型，仅对 OCR 置信度低或近空页调用。
          知识库图片问答仅对图形/图表类问题触发，最多送设定张数给视觉模型。
        </div>
      </div>
    </div>

    <div class="settings-section" v-show="activeModelTab === 'advanced'">
      <div class="section-head">
        <span class="settings-label">GPU 模型生命周期</span>
      </div>
      <div class="generic-form">
        <div class="generic-row">
          <span class="generic-label">空闲释放本地 BGE / reranker</span>
          <el-switch v-model="gpuIdleEnabled" />
        </div>
        <div v-if="gpuIdleEnabled" class="generic-row">
          <span class="generic-label">空闲分钟数</span>
          <el-input-number
            v-model="gpuIdleMinutes"
            :min="1"
            :max="1440"
            :step="5"
            size="small"
          />
        </div>
        <div class="settings-tip">
          释放后下次检索/记忆召回自动重载（首次可能增加 1–10 秒）；记忆与检索共用 BGE，建议不少于 30 分钟。
        </div>
      </div>
    </div>

    <div class="settings-section" v-show="activeModelTab === 'advanced'">
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
          <el-form-item label="接口支持图像输入">
            <el-switch v-model="providerForm.supportsVision" />
          </el-form-item>
          <el-form-item
            v-if="providerForm.supportsVision"
            label="支持视觉的模型（逗号分隔，支持通配符）"
          >
            <el-input
              v-model="providerForm.visionModelsText"
              placeholder="deepseek-v4.1-flash, *-vl-*"
            />
          </el-form-item>
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
import { computed, inject, onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  ArrowDown,
  ChatDotRound,
  Connection,
  Delete,
  Document,
  Picture,
  Plus,
  Search,
  Setting,
} from '@element-plus/icons-vue'
import {
  createAgentType,
  deleteAgentType,
  getAgentTypeMeta,
  listAgentTypes,
  resetAgentType,
  updateAgentType,
} from '../../api'
import ModelListDialog from './ModelListDialog.vue'
import SettingsSubnav from './SettingsSubnav.vue'

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
  pdfParseBackend,
  paddleLazyStart,
  paddleIdleSeconds,
  paddlePagesPerProcess,
  vlmFallback,
  kbVisionEnabled,
  kbVisionMaxImages,
  gpuIdleEnabled,
  gpuIdleMinutes,
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

// ---------------- 分组导航 / 供应商卡片 ----------------

/** 当前展示的设置分组（记住上次选择由 SettingsSubnav 负责） */
const activeModelTab = ref('chat')

/** 供应商卡片：默认收起，只看摘要；点卡片/编辑展开全部字段 */
function toggleProvider(p) {
  if (!p) return
  p._expanded = !p._expanded
}

function onProviderMenu(command) {
  if (command === 'import') providerImportInput.value?.click()
  else if (command === 'export') exportProvidersToFile(false)
  else if (command === 'export-keys') exportProvidersToFile(true)
  else if (command === 'speedtest') runSpeedtest()
}

function apiFormatLabel(p) {
  const raw = String(p?.api_format || '')
  if (raw === 'anthropic') return 'Anthropic 兼容'
  if (raw === 'openai') return 'OpenAI 兼容'
  return '自动识别格式'
}

function thinkingLabel(effort) {
  return { low: '低', medium: '中', high: '高', max: '最高' }[effort] || effort || '默认'
}

// ---------------- 子代理类型（6.1/6.2） ----------------

const agentTypes = ref([])
const agentSavingKey = ref('')

/** 分组导航：徽标展示该类当前条目数，便于一眼看到有没有配置 */
const modelTabs = computed(() => [
  {
    key: 'chat',
    label: '对话模型',
    icon: ChatDotRound,
    badge: chatProviders.value.length || '',
  },
  {
    key: 'vision',
    label: '视觉',
    icon: Picture,
    badge: visionProviders.value.length || '',
  },
  {
    key: 'agents',
    label: '子代理',
    icon: Connection,
    badge: agentTypes.value.length || '',
  },
  { key: 'retrieval', label: '检索', icon: Search, badge: '' },
  { key: 'docs', label: '文档解析', icon: Document, badge: '' },
  { key: 'advanced', label: '高级', icon: Setting, badge: '' },
])
const agentMeta = ref({
  modes: {},
  thinking: ['inherit', 'on', 'off'],
  tool_labels: {},
  mode_labels: {},
})
const agentDialogVisible = ref(false)
const agentDialogSaving = ref(false)
const agentEditingKey = ref('')
const agentForm = ref(_emptyAgentForm())

const agentModeOptions = computed(() => Object.keys(agentMeta.value.modes || {}))
const agentToolOptions = computed(
  () => agentMeta.value.modes?.[agentForm.value.mode] || [],
)
const agentFormEffectiveTools = computed(() => {
  const picked = agentForm.value.tools || []
  const effective = picked.length ? picked : agentToolOptions.value
  return effective.map((tool) => toolLabel(tool))
})

function toolLabel(tool) {
  return agentMeta.value.tool_labels?.[tool] || tool
}

function modeLabel(mode) {
  return agentMeta.value.mode_labels?.[mode] || mode
}
const agentFormProviderModels = computed(() => {
  const provider = chatProviders.value.find(
    (p) => p.id === agentForm.value.model_provider_id,
  )
  return provider?.models || []
})

function _emptyAgentForm() {
  return {
    key: '',
    name: '',
    description: '',
    mode: 'research',
    tools: [],
    system_prompt: '',
    model_provider_id: '',
    model: '',
    thinking: 'inherit',
    max_rounds: null,
    llm_budget: null,
    read_only: true,
    enabled: true,
    is_builtin: false,
  }
}

async function loadAgentTypes() {
  try {
    agentTypes.value = await listAgentTypes()
  } catch (err) {
    ElMessage.error(`加载子代理类型失败：${err?.response?.data?.detail || err.message}`)
  }
}

async function loadAgentMeta() {
  try {
    agentMeta.value = await getAgentTypeMeta()
  } catch (err) {
    ElMessage.error(`加载类型元数据失败：${err?.response?.data?.detail || err.message}`)
  }
}

onMounted(() => {
  loadAgentTypes()
  loadAgentMeta()
})

function agentProviderModels(t) {
  const provider = chatProviders.value.find((p) => p.id === t.model_provider_id)
  return provider?.models || []
}

function onAgentProviderChange(t) {
  if (!agentProviderModels(t).includes(t.model)) t.model = ''
}

function _numberOrNull(value) {
  if (value === '' || value === null || value === undefined) return null
  const number = Number(value)
  return Number.isFinite(number) ? number : null
}

async function saveAgentType(t) {
  agentSavingKey.value = t.key
  try {
    const updated = await updateAgentType(t.key, {
      model_provider_id: t.model_provider_id || '',
      model: t.model || '',
      thinking: t.thinking || 'inherit',
      max_rounds: _numberOrNull(t.max_rounds),
      llm_budget: _numberOrNull(t.llm_budget),
    })
    Object.assign(t, updated)
    ElMessage.success(`已保存 ${t.name}`)
  } catch (err) {
    ElMessage.error(`保存失败：${err?.response?.data?.detail || err.message}`)
  } finally {
    agentSavingKey.value = ''
  }
}

async function restoreAgentType(t) {
  try {
    const updated = await resetAgentType(t.key)
    Object.assign(t, updated)
    ElMessage.success(`已恢复 ${t.name} 的默认配置`)
  } catch (err) {
    ElMessage.error(`恢复失败：${err?.response?.data?.detail || err.message}`)
  }
}

function openAgentTypeDialog(t) {
  agentEditingKey.value = t?.key || ''
  agentForm.value = t
    ? {
        key: t.key,
        name: t.name || '',
        description: t.description || '',
        mode: t.mode || 'research',
        tools: [...(t.tools || [])],
        system_prompt: t.system_prompt || '',
        model_provider_id: t.model_provider_id || '',
        model: t.model || '',
        thinking: t.thinking || 'inherit',
        max_rounds: t.max_rounds,
        llm_budget: t.llm_budget,
        read_only: t.read_only !== false,
        enabled: t.enabled !== false,
        is_builtin: Boolean(t.is_builtin),
      }
    : _emptyAgentForm()
  agentDialogVisible.value = true
}

function onAgentFormModeChange() {
  const allowed = new Set(agentToolOptions.value)
  agentForm.value.tools = (agentForm.value.tools || []).filter((tool) =>
    allowed.has(tool),
  )
  if (agentForm.value.mode !== 'execute') {
    agentForm.value.read_only = true
  }
}

function onAgentFormProviderChange() {
  if (!agentFormProviderModels.value.includes(agentForm.value.model)) {
    agentForm.value.model = ''
  }
}

async function submitAgentType() {
  const form = agentForm.value
  const key = String(form.key || '').trim().toLowerCase()
  if (!agentEditingKey.value && !/^[a-z0-9][a-z0-9_-]*$/.test(key)) {
    ElMessage.error('key 只能用小写字母/数字/-/_，且以字母或数字开头')
    return
  }
  if (!String(form.name || '').trim()) {
    ElMessage.error('名称不能为空')
    return
  }
  const payload = {
    name: String(form.name).trim(),
    description: String(form.description || '').trim(),
    mode: form.mode,
    system_prompt: form.system_prompt || '',
    tools: form.tools || [],
    max_rounds: _numberOrNull(form.max_rounds),
    model_provider_id: form.model_provider_id || '',
    model: form.model || '',
    thinking: form.thinking || 'inherit',
    llm_budget: _numberOrNull(form.llm_budget),
    read_only: form.mode === 'execute' ? Boolean(form.read_only) : true,
    enabled: Boolean(form.enabled),
  }
  agentDialogSaving.value = true
  try {
    if (agentEditingKey.value) {
      await updateAgentType(agentEditingKey.value, payload)
    } else {
      await createAgentType({ key, ...payload })
    }
    await loadAgentTypes()
    agentDialogVisible.value = false
    ElMessage.success('子代理类型已保存')
  } catch (err) {
    ElMessage.error(`保存失败：${err?.response?.data?.detail || err.message}`)
  } finally {
    agentDialogSaving.value = false
  }
}

async function removeAgentTypeDialog() {
  const key = agentEditingKey.value
  if (!key) return
  try {
    await ElMessageBox.confirm(
      `删除子代理类型「${agentForm.value.name || key}」？此操作不可撤销。`,
      '确认删除',
      { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  try {
    await deleteAgentType(key)
    await loadAgentTypes()
    agentDialogVisible.value = false
    ElMessage.success('类型已删除')
  } catch (err) {
    ElMessage.error(`删除失败：${err?.response?.data?.detail || err.message}`)
  }
}
</script>
