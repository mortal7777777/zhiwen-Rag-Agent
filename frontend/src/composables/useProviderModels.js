/**
 * 设置页 · 供应商模型探测（从 useModelSettings 拆出，2026-11-13）。
 *
 * 覆盖：拉取模型（草稿/已存卡片）、测试连接、全部测速、模型管理对话框。
 * 不持有供应商列表本身；providers / providerForm / 保存态由调用方注入。
 */
import { ref } from 'vue'
import { ElMessage } from 'element-plus/es/components/message/index'
import { listProviderModels, speedtestProviders, testProvider } from '../api'
import { providerToPayload } from './providerPayload'

export function useProviderModels({
  providers,
  providerForm,
  providerSaving,
  speedtesting,
}) {
  // 模型管理对话框（勾选式编辑，存储仍是 list[str]）
  const modelDialogVisible = ref(false)
  const modelDialogProvider = ref(null)
  const modelDialogOwnerName = ref('')
  const modelDialogAll = ref([])
  const modelDialogSelected = ref([])
  const modelDialogFilter = ref('')
  const modelDialogLoading = ref(false)
  const modelDialogNew = ref('')

  /** 对话框内拉取模型（草稿用明文 Key；不落库） */
  async function fetchDraftModels() {
    const form = providerForm.value
    if (!form.base_url.trim()) {
      ElMessage.warning('请先填写接口地址')
      return
    }
    providerSaving.value = true
    try {
      const res = await listProviderModels({
        base_url: form.base_url.trim(),
        api_key: form.apiKey.trim(),
        model: form.model.trim(),
        api_format: form.apiFormat,
      })
      if (res.ok && res.models.length) {
        const ids = res.models.map((m) => m.id)
        form.modelsText = ids.join(', ')
        if (!ids.includes(form.model)) form.model = ids[0]
        ElMessage.success(`拉取到 ${ids.length} 个模型`)
      } else {
        ElMessage.warning(res.error || '未拉取到模型')
      }
    } catch (error) {
      ElMessage.error(error.response?.data?.detail || '拉取模型失败')
    } finally {
      providerSaving.value = false
    }
  }

  async function fetchChatModels(p) {
    p._modelsLoading = true
    try {
      // 按卡片自身的配置拉取（修复"永远拉激活供应商"的既有 bug）；Key 未改
      // 时传掩码 + id，由后端按"同 id 同 host"规则还原
      const res = await listProviderModels(providerToPayload(p))
      if (res.ok && res.models.length) {
        const ids = res.models.map((m) => m.id)
        p._modelsText = ids.join(', ')
        if (!ids.includes(p.model)) p.model = ids[0]
        ElMessage.success(`拉取到 ${ids.length} 个模型`)
      } else {
        ElMessage.warning(res.error || '未拉取到模型')
      }
    } catch (error) {
      ElMessage.error(error.response?.data?.detail || '拉取模型失败')
    } finally {
      p._modelsLoading = false
    }
  }

  /** 测试连接：恒 200，结果进 p._testResult（模板里显示徽标与错误 tooltip） */
  async function testProviderCard(p) {
    p._testing = true
    try {
      p._testResult = await testProvider(providerToPayload(p))
    } catch (error) {
      p._testResult = {
        ok: false,
        error: error.response?.data?.detail || String(error),
      }
    } finally {
      p._testing = false
    }
  }

  /** 全部测速：服务端并发跑已存配置，结果按 id 回填到各卡片 */
  async function runSpeedtest() {
    speedtesting.value = true
    try {
      const res = await speedtestProviders()
      const byId = Object.fromEntries((res.results || []).map((r) => [r.id, r]))
      for (const p of providers.value) {
        if (byId[p.id]) p._speedResult = byId[p.id]
      }
      ElMessage.success('测速完成')
    } catch (error) {
      ElMessage.error(error.response?.data?.detail || '测速失败')
    } finally {
      speedtesting.value = false
    }
  }

  function openModelDialog(p) {
    modelDialogProvider.value = p
    modelDialogOwnerName.value = p.name
    modelDialogSelected.value = (p._modelsText || '')
      .split(/[,，]/)
      .map((m) => m.trim())
      .filter(Boolean)
    modelDialogAll.value = [...modelDialogSelected.value]
    modelDialogFilter.value = ''
    modelDialogNew.value = ''
    modelDialogVisible.value = true
    refreshModelOptions()
  }

  async function refreshModelOptions() {
    const p = modelDialogProvider.value
    if (!p) return
    modelDialogLoading.value = true
    try {
      const res = await listProviderModels(providerToPayload(p))
      if (res.ok && res.models.length) {
        const ids = res.models.map((m) => m.id)
        modelDialogAll.value = [...new Set([...modelDialogSelected.value, ...ids])]
      } else if (res.error) {
        ElMessage.warning(`在线列表拉取失败（${res.error}），可手动添加`)
      }
    } catch (error) {
      ElMessage.warning(error.response?.data?.detail || '在线列表拉取失败，可手动添加')
    } finally {
      modelDialogLoading.value = false
    }
  }

  function toggleModelOption(id, checked) {
    const selected = modelDialogSelected.value
    const idx = selected.indexOf(id)
    if (checked && idx === -1) selected.push(id)
    if (!checked && idx !== -1) selected.splice(idx, 1)
  }

  function addModelOption() {
    const name = modelDialogNew.value.trim()
    if (!name) return
    if (!modelDialogAll.value.includes(name)) modelDialogAll.value.push(name)
    if (!modelDialogSelected.value.includes(name)) modelDialogSelected.value.push(name)
    modelDialogNew.value = ''
  }

  function modelDialogCommit() {
    const p = modelDialogProvider.value
    if (!p) return
    const selected = modelDialogSelected.value
    p._modelsText = selected.join(', ')
    if (p.model && !selected.includes(p.model)) p.model = selected[0] || ''
    if (!p.model && selected.length) p.model = selected[0]
    modelDialogVisible.value = false
    ElMessage.success(`已更新模型列表（${selected.length} 个），保存后生效`)
  }

  return {
    modelDialogVisible,
    modelDialogProvider,
    modelDialogOwnerName,
    modelDialogAll,
    modelDialogSelected,
    modelDialogFilter,
    modelDialogLoading,
    modelDialogNew,
    fetchDraftModels,
    fetchChatModels,
    testProviderCard,
    runSpeedtest,
    openModelDialog,
    refreshModelOptions,
    toggleModelOption,
    addModelOption,
    modelDialogCommit,
  }
}
