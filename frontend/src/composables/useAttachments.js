/**
 * 输入区附件：图片走 data URL（视觉链路），文档先上传换 attachment id。
 * 支持选择文件、拖拽、Ctrl+V 粘贴。
 */
import { ref } from 'vue'
import { ElMessage } from 'element-plus/es/components/message/index'
import { deleteAgentAttachment, uploadAgentAttachments } from '../api'
import { formatCount, formatFileSize } from './useChatUtils'

const MAX_ATTACH_FILES = 5
const MAX_ATTACH_MB = 20
const MAX_IMAGES = 6

export function useAttachments() {
  let imageSeq = 0
  const attachedImages = ref([])
  const attachedFiles = ref([])
  const attachInputRef = ref(null)
  const uploadingAttachments = ref(false)
  const dragOver = ref(false)

  function fileToDataUrl(file) {
    return new Promise((resolve, reject) => {
      const reader = new FileReader()
      reader.onload = () => resolve(reader.result)
      reader.onerror = () => reject(new Error('读取图片失败'))
      reader.readAsDataURL(file)
    })
  }

  async function addImageFiles(files) {
    for (const file of files) {
      if (!file.type || !file.type.startsWith('image/')) continue
      if (attachedImages.value.length >= MAX_IMAGES) {
        ElMessage.warning(`最多同时上传 ${MAX_IMAGES} 张图片`)
        break
      }
      try {
        const dataUrl = await fileToDataUrl(file)
        attachedImages.value.push({ id: ++imageSeq, dataUrl })
      } catch (error) {
        ElMessage.error(error.message || '读取图片失败')
      }
    }
  }

  /** 图片走既有 data URL 视觉链路；文档上传到后端换 attachment id */
  function routeAttachedFiles(files) {
    const images = []
    const docs = []
    for (const file of files) {
      if (file.type && file.type.startsWith('image/')) images.push(file)
      else docs.push(file)
    }
    if (images.length) addImageFiles(images)
    if (docs.length) uploadAttachments(docs)
  }

  async function uploadAttachments(files) {
    const accepted = []
    for (const file of files) {
      if (file.size > MAX_ATTACH_MB * 1024 * 1024) {
        ElMessage.warning(`${file.name} 超过 ${MAX_ATTACH_MB}MB，已跳过`)
        continue
      }
      if (attachedFiles.value.length + accepted.length >= MAX_ATTACH_FILES) {
        ElMessage.warning(`最多同时上传 ${MAX_ATTACH_FILES} 个文档`)
        break
      }
      accepted.push(file)
    }
    if (!accepted.length) return
    uploadingAttachments.value = true
    try {
      const data = await uploadAgentAttachments(accepted)
      for (const item of data.attachments || []) {
        attachedFiles.value.push({ ...item })
      }
    } catch (error) {
      ElMessage.error(
        error.response?.data?.detail || error.message || '附件上传失败',
      )
    } finally {
      uploadingAttachments.value = false
    }
  }

  function onAttachFiles(event) {
    routeAttachedFiles(Array.from(event.target.files || []))
    event.target.value = ''
  }

  function onDragOver() {
    dragOver.value = true
  }

  function onDragLeave(event) {
    if (event.currentTarget?.contains?.(event.relatedTarget)) return
    dragOver.value = false
  }

  function onDrop(event) {
    dragOver.value = false
    const files = Array.from(event.dataTransfer?.files || [])
    if (files.length) routeAttachedFiles(files)
  }

  /** 支持在输入框直接 Ctrl+V 粘贴截图/图片，或从资源管理器复制文件 */
  function onPaste(event) {
    const items = event.clipboardData?.items || []
    const files = []
    for (const item of items) {
      if (item.kind !== 'file') continue
      const file = item.getAsFile()
      if (file) files.push(file)
    }
    for (const file of event.clipboardData?.files || []) {
      if (!files.includes(file)) files.push(file)
    }
    if (files.length) {
      event.preventDefault()
      routeAttachedFiles(files)
    }
  }

  async function removeAttachment(file) {
    attachedFiles.value = attachedFiles.value.filter((item) => item.id !== file.id)
    try {
      await deleteAgentAttachment(file.id)
    } catch {
      // 临时附件过期后会被后端自动清理，删除失败不打扰用户
    }
  }

  function removeImage(id) {
    attachedImages.value = attachedImages.value.filter((img) => img.id !== id)
  }

  function clearAttachments() {
    attachedImages.value = []
    attachedFiles.value = []
  }

  return {
    attachedImages,
    attachedFiles,
    attachInputRef,
    uploadingAttachments,
    dragOver,
    onAttachFiles,
    onDragOver,
    onDragLeave,
    onDrop,
    onPaste,
    removeAttachment,
    removeImage,
    clearAttachments,
    formatFileSize,
    formatCount,
  }
}
