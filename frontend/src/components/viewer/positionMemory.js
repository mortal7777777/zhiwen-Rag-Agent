// 文档查看器位置记忆：localStorage 按相对路径存 {mtime, position}
// mtime 变化（文档更新/重建索引）即失效，避免定位到错位的位置。

const KEY_PREFIX = 'docview:pos:'

export function loadPosition(relativePath, mtime) {
  try {
    const raw = localStorage.getItem(KEY_PREFIX + relativePath)
    if (!raw) return null
    const data = JSON.parse(raw)
    if (!data || typeof data !== 'object') return null
    if (mtime && data.mtime && data.mtime !== mtime) return null
    return data.position || null
  } catch {
    return null
  }
}

export function savePosition(relativePath, mtime, position) {
  if (!position) return
  try {
    localStorage.setItem(
      KEY_PREFIX + relativePath,
      JSON.stringify({ mtime: mtime || '', position }),
    )
  } catch {
    // 存储满等异常静默忽略（位置记忆只是增强体验）
  }
}

// 滚动防抖写位置的通用封装
export function createPositionSaver(relativePath, mtime, delay = 500) {
  let timer = null
  let latest = null
  return {
    push(position) {
      latest = position
      if (timer) clearTimeout(timer)
      timer = setTimeout(() => {
        savePosition(relativePath, mtime, latest)
        timer = null
      }, delay)
    },
    flush() {
      if (timer) clearTimeout(timer)
      if (latest) savePosition(relativePath, mtime, latest)
    },
  }
}
