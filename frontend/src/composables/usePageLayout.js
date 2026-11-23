/**
 * 页面级布局变体（知识库 / 运行记录 / 提示词各自一套）。
 *
 * 只存一个字符串到 localStorage，页面根节点上拼成 `view-<variant>` 类，
 * 具体差异全部写在 styles/view-variants.css 里——组件逻辑不因布局分叉。
 */
import { computed, ref, watch } from 'vue'

export function usePageLayout(storageKey, options, fallback) {
  const variant = ref(fallback)

  try {
    const saved = localStorage.getItem(storageKey)
    if (saved && options.some((o) => o.value === saved)) variant.value = saved
  } catch {
    // ignore
  }

  watch(variant, (value) => {
    try {
      localStorage.setItem(storageKey, value)
    } catch {
      // ignore
    }
  })

  return { variant, viewClass: computed(() => `view-${variant.value}`) }
}
