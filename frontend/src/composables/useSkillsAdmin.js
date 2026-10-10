/**
 * 设置页 · 技能管理域（从 App.vue 抽出，2026-10-09 设置页拆分）。
 *
 * 覆盖：技能列表/搜索/筛选、启用开关、「自创」技能的共享/删除/预览、
 * 隐藏与恢复、重置偏好，以及技能卡片展示用的纯函数（图标/来源标签/分组）。
 * App.vue 创建唯一实例 provide('settingsSkills')，技能面板经 inject 读写；
 * 打开设置页时由 App.vue 的 watch(settingsVisible) 调 refreshSkills() 刷新。
 */
import { computed, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  Brush,
  Calendar,
  Cpu,
  Document,
  EditPen,
  Link,
  MagicStick,
  Search,
} from '@element-plus/icons-vue'
import {
  deleteAgentSkill as deleteAgentSkillApi,
  getAgentSkillContent as getAgentSkillContentApi,
  listSkills,
  resetSkillPrefs,
  saveSettings,
  saveSkillPrefs,
  searchSkills,
  shareAgentSkill as shareAgentSkillApi,
  unshareAgentSkill as unshareAgentSkillApi,
} from '../api'

export function useSkillsAdmin() {
  const skills = ref([])
  const hiddenSkills = ref([])
  const skillsEnabled = ref(true)
  const skillQuery = ref('')
  const skillSourceFilter = ref('all')
  const skillGroupFilter = ref('all')
  const skillsLoading = ref(false)

  const groupOptions = {
    documents: '文档处理',
    writing: '写作内容',
    research: '研究资料',
    productivity: '效率办公',
    design: '设计创意',
    coding: '编程调试',
    github: 'GitHub 协作',
    'data-ml': '数据与 AI',
    media: '媒体处理',
    email: '邮件沟通',
  }

  const sourceFilters = [
    { value: 'all', label: '全部' },
    { value: 'agent', label: '自创' },
    { value: 'codex', label: 'Codex' },
    { value: 'hermes', label: 'Hermes' },
    { value: 'claude', label: 'Claude' },
  ]

  const enabledCount = computed(() => skills.value.filter((s) => s.enabled).length)
  const filteredSkills = computed(() => {
    const q = skillQuery.value.trim().toLowerCase()
    return skills.value.filter((s) => {
      const primary = (s.source || '').split(' + ')[0]
      if (skillSourceFilter.value !== 'all' && primary !== skillSourceFilter.value) return false
      if (skillGroupFilter.value !== 'all' && (s.group || 'other') !== skillGroupFilter.value) {
        return false
      }
      if (!q) return true
      return (
        s.name.toLowerCase().includes(q) ||
        s.description.toLowerCase().includes(q) ||
        (s.tags || []).some((t) => t.toLowerCase().includes(q))
      )
    })
  })

  async function refreshSkills() {
    skillsLoading.value = true
    try {
      const data = await listSkills(true)
      skills.value = (data.items || []).filter((s) => !s.hidden)
      hiddenSkills.value = (data.items || []).filter((s) => s.hidden)
    } catch {
      skills.value = []
      hiddenSkills.value = []
    } finally {
      skillsLoading.value = false
    }
  }

  async function onSkillSearch() {
    const q = skillQuery.value.trim()
    if (!q) {
      await refreshSkills()
      return
    }
    try {
      const data = await searchSkills(q)
      skills.value = (data.items || []).filter((s) => !s.hidden)
    } catch {
      // ignore
    }
  }

  function switchSourceFilter(value) {
    skillSourceFilter.value = value
    if (skillQuery.value.trim()) {
      onSkillSearch()
    } else {
      refreshSkills()
    }
  }
  const agentPreviewVisible = ref(false)
  const agentPreviewContent = ref('')

  function isAgentSkill(s) {
    return (s.source || '').split(' + ')[0] === 'agent'
  }

  const agentPendingCount = computed(
    () => skills.value.filter((s) => isAgentSkill(s) && !s.enabled && !s.hidden).length,
  )

  /** 自创技能：共享 / 取消共享（复制或移除 Hermes 个人层副本）。 */
  async function shareAgentSkill(s) {
    const action = s.shared ? '取消共享' : '共享'
    try {
      await ElMessageBox.confirm(
        s.shared
          ? `确定取消共享「${s.name}」吗？\n将删除 Hermes 技能池中的副本，自创技能库里的原技能保留。`
          : `把「${s.name}」共享给 Hermes？\n将复制到 Hermes 个人技能层，Hermes 也能检索使用它。`,
        '提示',
        { type: 'info', confirmButtonText: action, cancelButtonText: '取消' },
      )
    } catch {
      return
    }
    try {
      if (s.shared) await unshareAgentSkillApi(s.dir_name, s.category)
      else await shareAgentSkillApi(s.dir_name, s.category)
      await refreshSkills()
      ElMessage.success(s.shared ? '已取消共享' : '已共享给 Hermes')
    } catch (error) {
      ElMessage.error(error.response?.data?.detail || `${action}失败`)
    }
  }

  async function deleteAgentSkill(s) {
    try {
      await ElMessageBox.confirm(
        `确定删除自创技能「${s.name}」吗？删除后不可恢复。${
          s.shared ? '\n该技能已共享给 Hermes，共享副本将一并删除。' : ''
        }`,
        '删除确认',
        { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消' },
      )
    } catch {
      return
    }
    try {
      await deleteAgentSkillApi(s.dir_name, s.category)
      await refreshSkills()
      ElMessage.success('已删除自创技能')
    } catch (error) {
      ElMessage.error(error.response?.data?.detail || '删除失败')
    }
  }

  async function viewAgentSkill(s) {
    try {
      const data = await getAgentSkillContentApi(s.dir_name, s.category)
      agentPreviewContent.value = data.content || ''
      agentPreviewVisible.value = true
    } catch (error) {
      ElMessage.error(error.response?.data?.detail || '读取技能内容失败')
    }
  }

  async function persistSkills() {
    const enabled = skills.value.filter((s) => s.enabled).map((s) => s.name)
    const hidden = hiddenSkills.value.map((s) => s.name)
    try {
      await saveSkillPrefs(enabled, hidden)
    } catch (error) {
      ElMessage.error(error.response?.data?.detail || '保存技能偏好失败')
    }
  }

  async function hideSkill(s) {
    try {
      await ElMessageBox.confirm(
        `确定从本助手移除技能「${s.name}」吗？\n这只影响本助手（skill_lookup 不再使用它），不会修改其他 Agent 的技能文件。`,
        '提示',
        { type: 'info', confirmButtonText: '移除', cancelButtonText: '取消' },
      )
    } catch {
      return
    }
    s.enabled = false
    hiddenSkills.value.push(s)
    skills.value = skills.value.filter((x) => x.name !== s.name)
    await persistSkills()
  }

  async function restoreSkill(s) {
    hiddenSkills.value = hiddenSkills.value.filter((x) => x.name !== s.name)
    s.hidden = false
    s.enabled = true
    skills.value.push(s)
    skills.value.sort((a, b) => a.source.localeCompare(b.source) || a.name.localeCompare(b.name))
    await persistSkills()
  }

  async function handleResetSkills() {
    try {
      await ElMessageBox.confirm(
        '将恢复为默认精选手集（启用最实用的技能，隐藏项清空）。确定继续吗？',
        '提示',
        { type: 'warning' },
      )
    } catch {
      return
    }
    try {
      await resetSkillPrefs()
      await refreshSkills()
      ElMessage.success('已恢复默认精选手集')
    } catch (error) {
      ElMessage.error(error.response?.data?.detail || '恢复失败')
    }
  }

  async function saveSkillsEnabled() {
    try {
      await saveSettings({ skills_enabled: skillsEnabled.value })
      ElMessage.success(skillsEnabled.value ? '技能工具已开启' : '技能工具已关闭')
    } catch (error) {
      ElMessage.error(error.response?.data?.detail || '保存失败')
    }
  }


  function skillIcon(s) {
    const name = s.name.toLowerCase()
    const tags = (s.tags || []).map((t) => t.toLowerCase()).join(' ')
    const hay = `${name} ${s.description.toLowerCase()} ${tags}`
    if (/pdf|docx|xlsx|powerpoint|ocr|document|nano-pdf/.test(hay)) return Document
    if (/write|draft|polish|humanize|resume|email|meeting|blog/.test(hay)) return EditPen
    if (/arxiv|research|paper|llm-wiki|citation|youtube/.test(hay)) return Search
    if (/notion|obsidian|calendar|airtable|google|map|notes|teams/.test(hay)) return Calendar
    if (/design|canvas|diagram|infographic|frontend|ascii|web/.test(hay)) return Brush
    if (/debug|python|node|github|code|test|review|spike|plan/.test(hay)) return Cpu
    if (/link|cite|source/.test(hay)) return Link
    return MagicStick
  }

  function sourceLabel(source) {
    const primary = (source || '').split(' + ')[0]
    return (
      {
        codex: 'Codex',
        agent: '自创',
        hermes: 'Hermes',
        claude: 'Claude',
      }[primary] || primary
    )
  }

  function sourceTagType(source) {
    const primary = (source || '').split(' + ')[0]
    return (
      {
        codex: 'primary',
        agent: 'warning',
        hermes: 'success',
        claude: 'info',
      }[primary] || 'info'
    )
  }

  function groupLabel(group) {
    return groupOptions[group] || group || '其他'
  }

  return {
    skills,
    hiddenSkills,
    skillsEnabled,
    skillQuery,
    skillSourceFilter,
    skillGroupFilter,
    skillsLoading,
    groupOptions,
    sourceFilters,
    enabledCount,
    filteredSkills,
    agentPreviewVisible,
    agentPreviewContent,
    agentPendingCount,
    refreshSkills,
    onSkillSearch,
    switchSourceFilter,
    isAgentSkill,
    shareAgentSkill,
    deleteAgentSkill,
    viewAgentSkill,
    persistSkills,
    hideSkill,
    restoreSkill,
    handleResetSkills,
    saveSkillsEnabled,
    skillIcon,
    sourceLabel,
    sourceTagType,
    groupLabel,
  }
}
