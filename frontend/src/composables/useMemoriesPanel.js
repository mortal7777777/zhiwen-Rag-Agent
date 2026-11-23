/** 长期记忆抽屉：列表 / 新建 / 编辑 / 归档 / 删除 / 整理。 */
import { ref } from 'vue'
import { ElMessage } from 'element-plus/es/components/message/index'
import { ElMessageBox } from 'element-plus/es/components/message-box/index'
import {
  addMemory,
  consolidateMemories,
  deleteMemory,
  listMemories,
  updateMemory,
} from '../api'
import {
  MEMORY_CATEGORIES,
  categoryLabel,
  categoryTagType,
  formatMemoryDate,
} from './useChatUtils'

export function useMemoriesPanel() {
  const memoriesVisible = ref(false)
  const memories = ref([])
  const memoryEditVisible = ref(false)
  const consolidating = ref(false)
  const memoryForm = ref({ id: null, content: '', category: 'profile' })
  // 长记忆折叠：默认两行截断，点击展开
  const expandedMemoryIds = ref(new Set())
  const showArchived = ref(false)
  const categoryMap = MEMORY_CATEGORIES

  function toggleMemoryExpand(id) {
    const next = new Set(expandedMemoryIds.value)
    next.has(id) ? next.delete(id) : next.add(id)
    expandedMemoryIds.value = next
  }

  async function openMemories() {
    memoriesVisible.value = true
    await loadMemories()
  }

  async function loadMemories() {
    try {
      memories.value = await listMemories(showArchived.value ? 'all' : 'active')
    } catch (error) {
      ElMessage.error(error.response?.data?.detail || '加载记忆失败')
    }
  }

  async function handleArchiveMemory(memory) {
    const archived = memory.status !== 'archived'
    try {
      await updateMemory(memory.id, { status: archived ? 'archived' : 'active' })
      await loadMemories()
      ElMessage.success(archived ? '已归档（不再参与召回，可随时恢复）' : '已恢复')
    } catch (error) {
      ElMessage.error(
        error.response?.data?.detail || (archived ? '归档失败' : '恢复失败'),
      )
    }
  }

  function openMemoryEdit(memory) {
    memoryForm.value = memory
      ? { id: memory.id, content: memory.content, category: memory.category }
      : { id: null, content: '', category: 'profile' }
    memoryEditVisible.value = true
  }

  async function saveMemory() {
    const form = memoryForm.value
    if (!form.content.trim()) {
      ElMessage.warning('内容不能为空')
      return
    }
    try {
      if (form.id) {
        await updateMemory(form.id, {
          content: form.content.trim(),
          category: form.category,
        })
      } else {
        await addMemory({ content: form.content.trim(), category: form.category })
      }
      memoryEditVisible.value = false
      await loadMemories()
      ElMessage.success(form.id ? '记忆已更新' : '记忆已添加')
    } catch (error) {
      ElMessage.error(error.response?.data?.detail || '保存失败')
    }
  }

  async function handleConsolidate() {
    consolidating.value = true
    try {
      const result = await consolidateMemories()
      await loadMemories()
      ElMessage.success(
        `整理完成：合并 ${result.merged || 0} 条，归档 ${result.archived || 0} 条`,
      )
    } catch (error) {
      ElMessage.error(error.response?.data?.detail || '整理失败')
    } finally {
      consolidating.value = false
    }
  }

  async function handleDeleteMemory(memory) {
    try {
      await ElMessageBox.confirm('确定删除这条记忆吗？', '提示', {
        type: 'warning',
        confirmButtonText: '删除',
        cancelButtonText: '取消',
      })
    } catch {
      return
    }
    try {
      await deleteMemory(memory.id)
      memories.value = memories.value.filter((m) => m.id !== memory.id)
      ElMessage.success('已删除')
    } catch (error) {
      ElMessage.error(error.response?.data?.detail || '删除失败')
    }
  }

  return {
    memoriesVisible,
    memories,
    memoryEditVisible,
    consolidating,
    memoryForm,
    expandedMemoryIds,
    showArchived,
    categoryMap,
    categoryLabel,
    categoryTagType,
    formatMemoryDate,
    toggleMemoryExpand,
    openMemories,
    loadMemories,
    handleArchiveMemory,
    openMemoryEdit,
    saveMemory,
    handleConsolidate,
    handleDeleteMemory,
  }
}
