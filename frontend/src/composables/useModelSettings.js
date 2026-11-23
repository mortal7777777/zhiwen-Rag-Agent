/**
 * 设置页 · 模型与供应商域（从 App.vue 抽出，2026-10-09 设置页拆分）。
 *
 * 覆盖：供应商列表与 CRUD、活跃供应商选择、温度/联网搜索/嵌入重排提供方式。
 * App.vue 创建唯一实例 provide('settingsModel')，模型面板与检索面板经 inject 读写；
 * `reload` 为注入的完整刷新回调（保存后与服务端归一化对齐）。
 */
import { computed, ref } from 'vue'
import { ElMessage } from 'element-plus/es/components/message/index'
import { ElMessageBox } from 'element-plus/es/components/message-box/index'
import {
  exportProviders,
  getProviderPresets,
  importProviders,
  saveSettings,
} from '../api'
import { headersToText, parseHeadersText, providerToPayload } from './providerPayload'
import { useProviderModels } from './useProviderModels'

export function useModelSettings(reload) {
  const settingsModel = ref(null)
  const providers = ref([])

  const activeChatId = ref('')
  const activeVisionId = ref('')
  const temperature = ref(0.5)
  const webProvider = ref('duckduckgo')
  const tavilyKey = ref('')
  const tavilyMasked = ref('')
  const webMaxResults = ref(6)
  const searxngBaseUrl = ref('http://localhost:8888')
  const searxngEngines = ref('')
  const modelSaving = ref(false)

  // 嵌入 / 重排序模型提供方式（local=本地 BGE / api=OpenAI 兼容接口）
  const embeddingProvider = ref('local')
  const embeddingApi = ref({ base_url: '', api_key: '', model: '' })
  const rerankerProvider = ref('local')
  const rerankerApi = ref({ base_url: '', api_key: '', model: '' })

  // 文档解析与 GPU 模型生命周期（2026-11-18）
  const pdfParseBackend = ref('auto')
  const paddleLazyStart = ref(true)
  const paddleIdleSeconds = ref(600)
  const paddlePagesPerProcess = ref(300)
  const vlmFallback = ref(false)
  const kbVisionEnabled = ref(true)
  const kbVisionMaxImages = ref(3)
  const gpuIdleEnabled = ref(true)
  const gpuIdleMinutes = ref(30)

  const providerDialogVisible = ref(false)
  const providerDialogType = ref('chat')
  const providerSaving = ref(false)
  const providerForm = ref({
    name: '',
    base_url: '',
    model: '',
    modelsText: '',
    apiKey: '',
    thinkingEnabled: true,
    thinkingEffort: 'high',
    supportsVision: false,
    visionModelsText: '',
    presetKey: '',
    apiFormat: '',
    maxTokens: null,
    temperature: null,
    headersText: '',
  })
  const presets = ref([])
  const speedtesting = ref(false)

  const chatProviders = computed(() => providers.value.filter((p) => p.type === 'chat'))
  const visionProviders = computed(() => providers.value.filter((p) => p.type === 'vision'))

  // 模型探测 / 模型管理对话框（独立 composable，状态与函数整体注入到返回对象）
  const providerModels = useProviderModels({
    providers,
    providerForm,
    providerSaving,
    speedtesting,
  })

  /** 把后端快照映射进模型域（原先内联在 loadSettings 里）。 */
  function applySnapshot(snapshot) {
    settingsModel.value = snapshot
      const raw = settingsModel.value.providers || []
      providers.value = raw.map((p) => ({
        ...p,
        api_format: p.api_format || '',
        temperature: p.temperature ?? null,
        max_tokens: p.max_tokens ?? null,
        _keyInput: '',
        _modelsText: (p.models || []).join(', '),
        _visionModelsText: (p.vision_models || []).join(', '),
        supports_vision: p.supports_vision === true,
        _headersText: headersToText(p.headers),
        _testing: false,
        _testResult: null,
        _speedResult: null,
        _expanded: false,
        thinking_enabled: p.thinking_enabled !== false,
        thinking_effort: p.thinking_effort || 'high',
      }))
      activeChatId.value =
        settingsModel.value.active_chat_provider || chatProviders.value[0]?.id || ''
      activeVisionId.value =
        settingsModel.value.active_vision_provider || visionProviders.value[0]?.id || ''
      temperature.value = settingsModel.value.editable.chat_temperature ?? 0.5
      webProvider.value = settingsModel.value.editable.web_search_provider || 'duckduckgo'
      webMaxResults.value = settingsModel.value.editable.web_search_max_results || 6
      tavilyMasked.value = settingsModel.value.editable.tavily_api_key || ''
      tavilyKey.value = ''
      searxngBaseUrl.value =
        settingsModel.value.editable.searxng_base_url || 'http://localhost:8888'
      searxngEngines.value = settingsModel.value.editable.searxng_engines || ''
      const editable = settingsModel.value.editable || {}
      // 嵌入 / 重排序模型提供方式
      embeddingProvider.value = editable.embedding_provider || 'local'
      embeddingApi.value = {
        base_url: editable.embedding_api_base_url || '',
        api_key: '',
        model: editable.embedding_api_model || '',
        _masked: editable.embedding_api_key || '',
      }
      rerankerProvider.value = editable.reranker_provider || 'local'
      rerankerApi.value = {
        base_url: editable.reranker_api_base_url || '',
        api_key: '',
        model: editable.reranker_api_model || '',
        _masked: editable.reranker_api_key || '',
      }
      // 解析与 GPU 模型生命周期
      pdfParseBackend.value = editable.pdf_parse_backend || 'auto'
      paddleLazyStart.value = editable.paddle_ocr_lazy_start !== false
      paddleIdleSeconds.value = Number(editable.paddle_ocr_idle_timeout_s ?? 600)
      paddlePagesPerProcess.value = Number(
        editable.paddle_ocr_pages_per_process ?? 300,
      )
      vlmFallback.value = editable.paddle_ocr_vlm_fallback === true
      kbVisionEnabled.value = editable.kb_vision_image_answer !== false
      kbVisionMaxImages.value = Number(editable.kb_vision_max_images ?? 3)
      const idleMinutes = Number(editable.gpu_model_idle_unload_minutes ?? 30)
      gpuIdleEnabled.value = idleMinutes > 0
      gpuIdleMinutes.value = idleMinutes > 0 ? idleMinutes : 30
  }

  function openProviderDialog(type) {
    providerDialogType.value = type
    providerForm.value = {
      name: '',
      base_url: type === 'vision' ? 'https://token.sensenova.cn/v1' : '',
      model: '',
      modelsText: '',
      apiKey: '',
      thinkingEnabled: true,
      thinkingEffort: 'high',
      supportsVision: false,
      visionModelsText: '',
      presetKey: '',
      apiFormat: '',
      maxTokens: null,
      temperature: null,
      headersText: '',
    }
    providerDialogVisible.value = true
    ensurePresets()
  }

  /** 预设表懒加载（首次打开对话框时拉一次；失败静默，不影响手填） */
  async function ensurePresets() {
    if (presets.value.length) return
    try {
      const res = await getProviderPresets()
      presets.value = res.presets || []
    } catch {
      /* 预设只是便捷入口，失败不打扰用户 */
    }
  }

  /** 应用预设：填充表单（名称/base_url/格式/推荐模型/思考开关），Key 仍需手填 */
  function applyPreset(key) {
    const preset = presets.value.find((x) => x.key === key)
    if (!preset) return
    const form = providerForm.value
    form.presetKey = key
    form.name = preset.name
    form.base_url = preset.base_url
    form.apiFormat = preset.api_format || ''
    form.modelsText = (preset.models || []).join(', ')
    form.model = (preset.models || [])[0] || ''
    form.thinkingEnabled = preset.thinking_enabled !== false
    form.thinkingEffort = 'high'
  }

  async function confirmAddProvider() {
    const form = providerForm.value
    if (!form.name.trim() || !form.base_url.trim() || !form.model.trim()) {
      ElMessage.warning('名称、接口地址和默认模型不能为空')
      return
    }
    providerSaving.value = true
    try {
      const models = form.modelsText
        ? form.modelsText.split(/[,，]/).map((m) => m.trim()).filter(Boolean)
        : [form.model.trim()]
      if (!models.includes(form.model.trim())) models.unshift(form.model.trim())
      const isChat = providerDialogType.value === 'chat'
      const headers = parseHeadersText(form.headersText)
      providers.value.push({
        id: `p_${Date.now()}`,
        name: form.name.trim(),
        type: providerDialogType.value,
        base_url: form.base_url.trim(),
        api_key: form.apiKey.trim() || '',
        model: form.model.trim(),
        models,
        enabled: true,
        note: '',
        ...(isChat && form.apiFormat ? { api_format: form.apiFormat } : {}),
        ...(isChat && form.temperature !== null && form.temperature !== ''
          ? { temperature: Number(form.temperature) }
          : {}),
        ...(isChat && form.maxTokens ? { max_tokens: Number(form.maxTokens) } : {}),
        ...(isChat && Object.keys(headers).length ? { headers } : {}),
        supports_vision: isChat ? !!form.supportsVision : false,
        vision_models: isChat
          ? String(form.visionModelsText || '')
              .split(/[,，]/)
              .map((m) => m.trim())
              .filter(Boolean)
          : [],
        _keyInput: '',
        _modelsText: models.join(', '),
        _visionModelsText: isChat ? (form.visionModelsText || '') : '',
        _headersText: isChat ? form.headersText : '',
        _testing: false,
        _testResult: null,
        _speedResult: null,
        _expanded: true, // 新加供应商立即展开，方便继续补配置
        thinking_enabled: !isChat || form.thinkingEnabled,
        thinking_effort: isChat ? form.thinkingEffort : 'high',
      })
      const added = providers.value[providers.value.length - 1]
      if (providerDialogType.value === 'chat') activeChatId.value = added.id
      if (providerDialogType.value === 'vision') activeVisionId.value = added.id
      providerDialogVisible.value = false
      await saveModelSettings(true)
      ElMessage.success('供应商已添加')
    } catch (error) {
      ElMessage.error(error.response?.data?.detail || '添加失败')
    } finally {
      providerSaving.value = false
    }
  }

  // ---------------- 导入 / 导出 ----------------

  const ioBusy = ref(false)

  async function exportProvidersToFile(includeKeys = false) {
    if (includeKeys) {
      try {
        await ElMessageBox.confirm(
          '导出文件将包含明文 API Key，请只保存在本机可信位置。继续？',
          '导出（含密钥）',
          { type: 'warning', confirmButtonText: '导出', cancelButtonText: '取消' },
        )
      } catch {
        return
      }
    }
    ioBusy.value = true
    try {
      const data = await exportProviders(includeKeys)
      const stamp = new Date().toISOString().slice(0, 16).replace(/[-:T]/g, '')
      const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' })
      const url = URL.createObjectURL(blob)
      const link = document.createElement('a')
      link.href = url
      link.download = `zhiwen-providers-${stamp}.json`
      link.click()
      URL.revokeObjectURL(url)
      ElMessage.success(`已导出 ${data.providers?.length ?? 0} 个供应商配置`)
    } catch (error) {
      ElMessage.error(error.response?.data?.detail || '导出失败')
    } finally {
      ioBusy.value = false
    }
  }

  async function importProvidersFromFile(file) {
    if (!file) return
    let payload
    try {
      payload = JSON.parse(await file.text())
    } catch {
      ElMessage.error('文件不是有效的 JSON')
      return
    }
    const count = Array.isArray(payload?.providers) ? payload.providers.length : 0
    if (!count) {
      ElMessage.warning('文件中没有供应商数据')
      return
    }
    try {
      await ElMessageBox.confirm(
        `将从文件合并导入 ${count} 个供应商（同 id 覆盖；空 Key 保留旧值）。继续？`,
        '导入供应商',
        { type: 'warning', confirmButtonText: '导入', cancelButtonText: '取消' },
      )
    } catch {
      return
    }
    ioBusy.value = true
    try {
      const res = await importProviders(payload)
      if (!res.ok) {
        ElMessage.error(res.error || '导入失败')
        return
      }
      const skipped = (res.skipped || []).length
      ElMessage.success(
        `导入完成：新增 ${res.imported}、更新 ${res.updated}${skipped ? `、跳过 ${skipped}` : ''}（已即时生效）`,
      )
      await reload()
    } catch (error) {
      ElMessage.error(error.response?.data?.detail || '导入失败')
    } finally {
      ioBusy.value = false
    }
  }

  function onProviderEnabledChange(p) {
    if (!p.enabled) {
      if (p.type === 'chat' && activeChatId.value === p.id) {
        activeChatId.value = chatProviders.value.find((x) => x.id !== p.id && x.enabled)?.id || ''
      }
      if (p.type === 'vision' && activeVisionId.value === p.id) {
        activeVisionId.value = visionProviders.value.find((x) => x.id !== p.id && x.enabled)?.id || ''
      }
    }
  }

  async function removeProvider(p) {
    try {
      await ElMessageBox.confirm(
        `确定删除供应商「${p.name}」吗？`,
        '提示',
        { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消' },
      )
    } catch {
      return
    }
    providers.value = providers.value.filter((x) => x.id !== p.id)
    if (activeChatId.value === p.id) activeChatId.value = chatProviders.value[0]?.id || ''
    if (activeVisionId.value === p.id) activeVisionId.value = visionProviders.value[0]?.id || ''
    await saveModelSettings(true)
    ElMessage.success('供应商已删除')
  }

  async function saveModelSettings(silent = false) {
    modelSaving.value = true
    try {
      const updates = {
        // 映射规则集中在 providerPayload.js（有 check_provider_payload.mjs 守门）
        providers: providers.value.map((p) => providerToPayload(p)),
        active_chat_provider: activeChatId.value,
        active_vision_provider: activeVisionId.value,
        chat_temperature: temperature.value,
        web_search_provider: webProvider.value,
        web_search_max_results: webMaxResults.value,
        searxng_base_url: searxngBaseUrl.value.trim(),
        searxng_engines: searxngEngines.value.trim(),
        // 嵌入 / 重排序模型提供方式
        embedding_provider: embeddingProvider.value,
        embedding_api_base_url: embeddingApi.value.base_url.trim(),
        embedding_api_model: embeddingApi.value.model.trim(),
        reranker_provider: rerankerProvider.value,
        reranker_api_base_url: rerankerApi.value.base_url.trim(),
        reranker_api_model: rerankerApi.value.model.trim(),
        // 文档解析与 GPU 模型生命周期
        pdf_parse_backend: pdfParseBackend.value,
        paddle_ocr_lazy_start: paddleLazyStart.value,
        paddle_ocr_idle_timeout_s: Number(paddleIdleSeconds.value) || 0,
        paddle_ocr_pages_per_process:
          Number(paddlePagesPerProcess.value) || 0,
        paddle_ocr_vlm_fallback: vlmFallback.value,
        kb_vision_image_answer: kbVisionEnabled.value,
        kb_vision_max_images: Number(kbVisionMaxImages.value) || 3,
        gpu_model_idle_unload_minutes: gpuIdleEnabled.value
          ? Math.max(1, Number(gpuIdleMinutes.value) || 30)
          : 0,
      }
      if (tavilyKey.value.trim()) updates.tavily_api_key = tavilyKey.value.trim()
      if (embeddingApi.value.api_key.trim())
        updates.embedding_api_key = embeddingApi.value.api_key.trim()
      if (rerankerApi.value.api_key.trim())
        updates.reranker_api_key = rerankerApi.value.api_key.trim()
      await saveSettings(updates)
      await reload()
      if (!silent) ElMessage.success('设置已保存并生效')
    } catch (error) {
      ElMessage.error(error.response?.data?.detail || '保存设置失败')
    } finally {
      modelSaving.value = false
    }
  }

  return {
    ...providerModels,
    settingsModel,
    providers,
    activeChatId,
    activeVisionId,
    temperature,
    webProvider,
    tavilyKey,
    tavilyMasked,
    webMaxResults,
    searxngBaseUrl,
    searxngEngines,
    modelSaving,
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
    providerDialogVisible,
    providerDialogType,
    providerSaving,
    providerForm,
    presets,
    speedtesting,
    ioBusy,
    exportProvidersToFile,
    importProvidersFromFile,
    chatProviders,
    visionProviders,
    applySnapshot,
    openProviderDialog,
    applyPreset,
    confirmAddProvider,
    onProviderEnabledChange,
    removeProvider,
    saveModelSettings,
  }
}
