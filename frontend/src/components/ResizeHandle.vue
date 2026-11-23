<template>
  <div
    class="resize-handle"
    :class="{ dragging }"
    role="separator"
    aria-orientation="vertical"
    :aria-label="label"
    :title="label"
    @pointerdown="onDown"
  ></div>
</template>

<script setup>
/**
 * 竖直分隔条：拖动时把像素宽度写进 CSS 变量（--nav-width / --session-width 等），
 * 同时持久化到 localStorage。变量挂在 documentElement 上，所有布局/页面都能吃到，
 * 具体生效位置写在 CSS 里（各布局有各自的 min/max 由页面传入）。
 */
import { ref, onBeforeUnmount, onMounted } from 'vue'

const props = defineProps({
  varName: { type: String, required: true },
  min: { type: Number, default: 160 },
  max: { type: Number, default: 420 },
  storeKey: { type: String, default: '' },
  /** true = 该元素在分隔条右侧（拖动方向取反） */
  invert: { type: Boolean, default: false },
  /** 'prev' = 以左侧兄弟为基准；'next' = 以右侧兄弟为基准（分隔条放在面板左侧时用） */
  target: { type: String, default: 'prev' },
  label: { type: String, default: '拖动调整宽度' },
})

const dragging = ref(false)
let startX = 0
let startWidth = 0

function currentWidth(el) {
  const raw = getComputedStyle(document.documentElement).getPropertyValue(props.varName)
  const parsed = parseFloat(raw)
  if (Number.isFinite(parsed) && parsed > 0) return parsed
  return el ? el.getBoundingClientRect().width : props.min
}

function apply(px) {
  const clamped = Math.round(Math.min(props.max, Math.max(props.min, px)))
  document.documentElement.style.setProperty(props.varName, `${clamped}px`)
  return clamped
}

// 启动时恢复上次拖动的宽度（--nav-width / --session-width 之前只写不读）
onMounted(() => {
  if (!props.storeKey) return
  try {
    const px = parseInt(localStorage.getItem(props.storeKey) || '', 10)
    if (Number.isFinite(px)) apply(px)
  } catch {
    // ignore
  }
})

function onMove(event) {
  const delta = (event.clientX - startX) * (props.invert ? -1 : 1)
  apply(startWidth + delta)
}

function onUp() {
  dragging.value = false
  window.removeEventListener('pointermove', onMove)
  window.removeEventListener('pointerup', onUp)
  document.body.classList.remove('is-resizing')
  if (props.storeKey) {
    try {
      localStorage.setItem(
        props.storeKey,
        getComputedStyle(document.documentElement).getPropertyValue(props.varName).trim(),
      )
    } catch {
      // ignore
    }
  }
}

function onDown(event) {
  event.preventDefault()
  dragging.value = true
  startX = event.clientX
  // 以相邻面板的当前宽度为基准：默认左侧兄弟，target='next' 时取右侧兄弟
  const el = event.currentTarget
  const sibling = props.target === 'next' ? el?.nextElementSibling : el?.previousElementSibling
  startWidth = currentWidth(sibling)
  document.body.classList.add('is-resizing')
  window.addEventListener('pointermove', onMove)
  window.addEventListener('pointerup', onUp)
}

onBeforeUnmount(() => {
  window.removeEventListener('pointermove', onMove)
  window.removeEventListener('pointerup', onUp)
  document.body.classList.remove('is-resizing')
})
</script>
