/**
 * 模板与系统提示词管理：模板列表/CRUD、版本/片段/预览对话框、
 * 会话锁定版本标签。模板选择跟随会话（切换时写入 conversation）。
 */
import { computed, nextTick, ref, watch } from 'vue'
import { ElMessage } from 'element-plus/es/components/message/index'
import { ElMessageBox } from 'element-plus/es/components/message-box/index'
import {
  archiveTemplate,
  auditPrompts,
  cloneTemplate,
  createTemplate,
  deleteTemplate,
  getConversationPrompt,
  getDefaultTemplate,
  lintTemplate,
  listTemplates,
  normalizePrompt,
  renameConversation,
  unarchiveTemplate,
  updateTemplate,
} from '../api'

export function usePromptAdmin({ activeId, currentTitle, loading, templateId }) {
  const templates = ref([])
  const availableTemplates = computed(() =>
    templates.value.filter((item) => !item.archived),
  )

  const templateManagerVisible = ref(false)
  const templateEditVisible = ref(false)
  const templateForm = ref({
    id: null,
    name: '',
    category: 'general',
    description: '',
    content: '',
    versionNote: '',
  })
  const promptFragmentsVisible = ref(false)
  const templateVersionsVisible = ref(false)
  const templateVersionTarget = ref({ id: null, title: '', readonly: false })
  const templatePreviewVisible = ref(false)
  const templatePreviewRequest = ref({})
  const defaultTemplateVisible = ref(false)
  const defaultTemplate = ref(null)
  const sessionPromptVisible = ref(false)
  const conversationPrompt = ref(null)
  const conversationPromptLoading = ref(false)
  let restoringTemplate = false // 切换会话时回填模板，不触发持久化写入

  const promptLockTag = computed(() => {
    if (!activeId.value || !conversationPrompt.value) return null
    const state = conversationPrompt.value
    if (!state.locked) {
      return { text: '提示词未锁定', type: 'info', upgradable: true }
    }
    const pending =
      state.outdated_details?.length || state.outdated_units?.length || 0
    return {
      text: `提示词 v${state.locked.template_version_no ?? '?'}${
        pending ? ` · ${pending} 项待更新` : ''
      }`,
      type: state.upgradable ? 'warning' : 'success',
      upgradable: !!state.upgradable,
    }
  })

  // 模板跟随会话：切换模板时立即写入当前会话，下次切回来自动恢复
  watch(templateId, (value) => {
    if (restoringTemplate || loading.value || !activeId.value) return
    const payload = {
      title: currentTitle.value || '新对话',
      template_id: value || 0,
    }
    renameConversation(activeId.value, payload)
      .then(() => loadConversationPrompt(activeId.value))
      .catch(() => {
        // 保存失败不打断对话，下次发送消息时后端会再同步
      })
  })

  /** 切换会话时回填模板：不触发上面的持久化写入。
   *  watch 默认在 pre 队列异步执行，必须等下一个 tick 再复位标记。 */
  function setTemplateFromSession(value) {
    restoringTemplate = true
    templateId.value = value || 0
    nextTick(() => {
      restoringTemplate = false
    })
  }

  function clearConversationPrompt() {
    conversationPrompt.value = null
  }

  async function loadTemplates() {
    try {
      templates.value = await listTemplates()
    } catch (error) {
      ElMessage.error(error.response?.data?.detail || '加载模板失败')
    }
  }

  function openTemplateManager() {
    templateManagerVisible.value = true
    loadTemplates()
  }

  async function loadConversationPrompt(id = activeId.value) {
    conversationPrompt.value = null
    if (!id) return
    conversationPromptLoading.value = true
    try {
      conversationPrompt.value = await getConversationPrompt(id)
    } catch {
      // 状态读取失败不影响对话
    } finally {
      conversationPromptLoading.value = false
    }
  }

  function openSessionPrompt() {
    if (activeId.value) sessionPromptVisible.value = true
  }

  function openTemplateVersions(tpl) {
    templateVersionTarget.value = {
      id: tpl.id,
      title: tpl.name,
      readonly: !!tpl.is_system,
    }
    templateVersionsVisible.value = true
  }

  function openPromptFragments() {
    promptFragmentsVisible.value = true
  }

  async function openDefaultTemplate() {
    try {
      defaultTemplate.value = await getDefaultTemplate()
      defaultTemplateVisible.value = true
    } catch (error) {
      ElMessage.error(error.response?.data?.detail || '读取默认模板失败')
    }
  }

  async function handleCloneTemplate(tpl) {
    try {
      await cloneTemplate(tpl.id)
      await loadTemplates()
      ElMessage.success('已克隆为自定义模板，可编辑')
    } catch (error) {
      ElMessage.error(error.response?.data?.detail || '克隆模板失败')
    }
  }

  async function handleArchiveTemplate(tpl) {
    try {
      if (tpl.archived) {
        await unarchiveTemplate(tpl.id)
      } else {
        await archiveTemplate(tpl.id)
      }
      await loadTemplates()
      ElMessage.success(tpl.archived ? '已恢复模板' : '已归档模板')
    } catch (error) {
      ElMessage.error(error.response?.data?.detail || '操作失败')
    }
  }

  async function onPromptArtifactsChanged() {
    await loadTemplates()
    await loadConversationPrompt()
  }

  function onSessionPromptApplied(status) {
    conversationPrompt.value = status
  }

  function openTemplateEdit(tpl) {
    if (tpl && tpl.is_system) {
      ElMessage.warning('内置模板不可编辑，可新建自定义模板')
      return
    }
    templateForm.value = tpl
      ? {
          id: tpl.id,
          name: tpl.name,
          category: tpl.category,
          description: tpl.description,
          content: tpl.content,
          versionNote: '',
        }
      : {
          id: null,
          name: '',
          category: 'general',
          description: '',
          content: '',
          versionNote: '',
        }
    templateEditVisible.value = true
  }

  function openTemplatePreview() {
    if (!templateForm.value.content.trim()) {
      ElMessage.warning('模板内容为空，无法预览')
      return
    }
    templatePreviewRequest.value = {
      template_id: templateForm.value.id || null,
      template_content: templateForm.value.content,
    }
    templatePreviewVisible.value = true
  }

  async function normalizeTemplateBreaks() {
    if (!templateForm.value.content.trim()) {
      ElMessage.warning('模板内容为空')
      return
    }
    try {
      const result = await normalizePrompt({ content: templateForm.value.content })
      if (!result.changed) {
        ElMessage.info('没有检测到句中硬换行')
        return
      }
      templateForm.value.content = result.content
      ElMessage.success('已合并句中硬换行，请预览后保存')
    } catch (error) {
      ElMessage.error(error.response?.data?.detail || '合并换行失败')
    }
  }

  async function auditPromptHealth() {
    try {
      const result = await auditPrompts()
      if (result.ok) {
        ElMessage.success('提示词体检通过：模板与片段均未发现问题')
        return
      }
      const lines = []
      for (const item of result.templates || []) {
        lines.push(
          `模板「${item.name}」v${item.version_no ?? '?'}：${item.warnings.join('；')}`,
        )
      }
      for (const item of result.fragments || []) {
        lines.push(
          `片段「${item.title}」v${item.version_no ?? '?'}：${item.warnings.join('；')}`,
        )
      }
      await ElMessageBox.alert(lines.join('；'), `提示词体检：${result.issue_count} 项`, {
        confirmButtonText: '知道了',
      })
    } catch (error) {
      ElMessage.error(error.response?.data?.detail || '提示词体检失败')
    }
  }

  async function saveTemplate() {
    const form = templateForm.value
    if (!form.name.trim() || !form.content.trim()) {
      ElMessage.warning('名称和内容不能为空')
      return
    }
    try {
      const check = await lintTemplate({ content: form.content })
      if (check.warnings?.length) {
        try {
          await ElMessageBox.confirm(
            check.warnings.join('；'),
            '发布前提示（仍要保存？）',
            {
              type: 'warning',
              confirmButtonText: '仍然保存',
              cancelButtonText: '返回修改',
            },
          )
        } catch {
          return
        }
      }
      const payload = {
        name: form.name.trim(),
        category: form.category,
        description: form.description.trim(),
        content: form.content,
        version_note: form.versionNote.trim(),
      }
      if (form.id) {
        await updateTemplate(form.id, payload)
      } else {
        await createTemplate(payload)
      }
      templateEditVisible.value = false
      await loadTemplates()
      await loadConversationPrompt()
      ElMessage.success(
        form.id
          ? '模板已更新（新版本）；已有会话保持锁定版本，新会话使用最新'
          : '模板已创建',
      )
    } catch (error) {
      ElMessage.error(error.response?.data?.detail || '保存失败')
    }
  }

  async function handleDeleteTemplate(tpl) {
    const used = Number(tpl.conversation_count || 0)
    const message = used
      ? `模板「${tpl.name}」正被 ${used} 个会话使用。删除后这些会话会解绑模板并回退默认提示词，锁定版本也会清空。确定删除吗？`
      : `确定删除模板「${tpl.name}」吗？`
    try {
      await ElMessageBox.confirm(message, '删除模板', {
        type: 'warning',
        confirmButtonText: '删除',
        cancelButtonText: '取消',
      })
    } catch {
      return
    }
    try {
      await deleteTemplate(tpl.id)
      if (templateId.value === tpl.id) templateId.value = 0
      await loadTemplates()
      await loadConversationPrompt()
      ElMessage.success('模板已删除')
    } catch (error) {
      ElMessage.error(error.response?.data?.detail || '删除失败')
    }
  }

  return {
    templates,
    availableTemplates,
    templateManagerVisible,
    templateEditVisible,
    templateForm,
    promptFragmentsVisible,
    templateVersionsVisible,
    templateVersionTarget,
    templatePreviewVisible,
    templatePreviewRequest,
    defaultTemplateVisible,
    defaultTemplate,
    sessionPromptVisible,
    conversationPrompt,
    conversationPromptLoading,
    promptLockTag,
    loadTemplates,
    openTemplateManager,
    loadConversationPrompt,
    openSessionPrompt,
    openTemplateVersions,
    openPromptFragments,
    openDefaultTemplate,
    handleCloneTemplate,
    handleArchiveTemplate,
    onPromptArtifactsChanged,
    onSessionPromptApplied,
    openTemplateEdit,
    openTemplatePreview,
    normalizeTemplateBreaks,
    auditPromptHealth,
    saveTemplate,
    handleDeleteTemplate,
    setTemplateFromSession,
    clearConversationPrompt,
  }
}
