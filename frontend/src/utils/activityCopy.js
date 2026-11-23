/**
 * 运行状态文案：相位 → 一组同义短句，按已持续秒数轮换。
 *
 * 设计原则（对齐 Claude Code 的状态条体验）：
 * - 相位由真实事件驱动（prepare / thinking / tool / finalizing…），不猜；
 * - 相位内部只轮换措辞，制造"还在动"的反馈感，且措辞贴近该相位真实在做的事；
 * - "快好了"只在真实进入 finalizing 后出现，不在思考阶段假装快完成。
 */
export const ACTIVITY_COPY = {
  prepare: ['正在翻聊天记录和记忆…', '把资料摆上桌…', '整理工具与文档清单…'],
  thinking: ['琢磨一下…', '推演下一步…', '核对相关依据…', '在脑子里过一遍…'],
  reasoning: ['想得再深一点…', '拆解问题中…', '梳理推理链…'],
  vision: ['看看这张图…', '读一下图片内容…'],
  answering: ['开始动笔…', '组织语言中…', '把结论写清楚…'],
  finalizing: ['检查引用…', '快好了…', '收个尾…'],
  waiting: ['等你拍板…', '挂起中，等你确认…'],
  subagent: ['分身并行干活…', '子代理跑起来了…', '汇总子任务…'],
  resume: ['接着往下跑…', '续上上下文…'],
  retry: ['刚才没成，重试一下…', '换个法子再试…', '调整思路重来…'],
  plan: [],
  tool: [],
}

export function pickActivityLabel(phase, detail, elapsedSeconds = 0) {
  if (phase === 'tool' && detail) return detail
  const list = ACTIVITY_COPY[phase]
  if (!list || list.length === 0) return detail || ''
  const index = Math.floor(Math.max(0, Number(elapsedSeconds) || 0) / 8) % list.length
  return list[index]
}
