/**
 * API 层出口（barrel）：按域拆分见同目录模块，这里只做 re-export。
 *
 * 拆分历史（2026-10-09）：原来是单文件 425 行、十几个域混在一起，churn 高
 * （近 80 次提交改动 12 次）。现在：
 *   client.js     axios 实例
 *   sse.js        SSE 帧解析 + 流式请求封装
 *   agent.js      会话流/运行控制/快照/任务清单
 *   agentTypes.js 子代理类型注册表
 *   sessions.js   会话、模板、长期记忆
 *   documents.js  知识库文档、版本、索引、统计
 *   prompts.js    提示词模板/片段版本、会话锁定
 *   settings.js   设置、模型、技能、MCP、项目记忆、受控执行
 *
 * 导出面有测试守着（frontend/scripts/check_api_sse.mjs）：新增接口请在本文件
 * 之外改对应域模块，别把逻辑又堆回这里。
 */
export * from './agent.js'
export * from './agentTypes.js'
export * from './documents.js'
export * from './prompts.js'
export * from './sessions.js'
export * from './settings.js'
