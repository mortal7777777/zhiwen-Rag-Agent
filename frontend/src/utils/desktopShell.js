/** 桌面壳薄桥（Electron 主窗口 preload 注入 window.zhiwenShell；浏览器环境为 no-op）。
 *
 * 只包含出站能力：主进程侧还会校验 sender 与 origin。桌面端没运行时这些调用
 * 静默跳过，逻辑检查（Node）与普通浏览器也不受影响。
 */

const bridge = typeof window !== 'undefined' ? window.zhiwenShell : undefined

export const desktopShell = {
  available: !!bridge,

  /** 系统通知。主进程会在窗口已聚焦时自动忽略（避免打扰）。 */
  notify(payload) {
    try {
      bridge?.notify?.(payload)
    } catch {
      // 桌面桥不可用时忽略
    }
  },

  /** 任务栏闪烁提醒（待确认场景）。 */
  attention() {
    try {
      bridge?.attention?.()
    } catch {
      // 忽略
    }
  },

  /** 运行态：任务栏不确定进度 + 阻止显示器休眠 + 托盘提示。 */
  setBusy(active) {
    try {
      bridge?.setBusy?.(!!active)
    } catch {
      // 忽略
    }
  },
}
