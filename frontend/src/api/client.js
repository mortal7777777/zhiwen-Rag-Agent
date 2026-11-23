/** axios 实例（统一 baseURL / 超时）。所有域的模块都从这里取 client。 */

import axios from 'axios'

// 统一的后端请求封装：前端只管调用函数，不关心 URL 细节
const client = axios.create({
  baseURL: '/api',
  timeout: 600000, // 大文档解析/长工具轮可能较慢，给足超时时间
})

// ---- 全局 API Token（后端设置 ZHIWEN_API_TOKEN 时启用）----

const API_TOKEN_KEY = 'zhiwen_api_token'

/** 给请求加 Authorization 头（sse.js 的 fetch 与本实例共用；未存令牌则空）。 */
export function apiTokenHeaders() {
  try {
    const token = localStorage.getItem(API_TOKEN_KEY)
    if (token) return { Authorization: `Bearer ${token}` }
  } catch {
    // 非浏览器环境（Node 逻辑检查）忽略
  }
  return {}
}

/** 弹窗索取一次全局令牌并保存（并发 401 共用同一次弹窗；取消则抛错）。 */
let tokenPrompting = null
export function ensureApiToken() {
  if (!tokenPrompting) {
    tokenPrompting = (async () => {
      // 动态导入：client.js 会被 Node 侧逻辑检查加载，不能在顶层引入组件库。
      // 必须走深路径——`import('element-plus')` 平铺入口会把整库拖进依赖图
      // （实测 vendor-element +620KB）；message-box 本体与其样式已在 main.js 全局引入。
      const { ElMessageBox } = await import(
        'element-plus/es/components/message-box/index'
      )
      const { value } = await ElMessageBox.prompt(
        '后端已启用 API Token 认证，请输入访问令牌（由部署方在环境变量 ZHIWEN_API_TOKEN 中设置）',
        'API Token',
        { confirmButtonText: '保存', cancelButtonText: '取消', closeOnClickModal: false },
      )
      const token = String(value || '').trim()
      if (!token) throw new Error('未输入令牌')
      try {
        localStorage.setItem(API_TOKEN_KEY, token)
      } catch {
        // 存储不可用时只影响下次仍要输入，不阻断本次
      }
      return token
    })().finally(() => {
      tokenPrompting = null
    })
  }
  return tokenPrompting
}

client.interceptors.request.use((config) => {
  try {
    const token = localStorage.getItem('zhiwen_prompt_admin_token')
    if (token) config.headers['X-Prompt-Admin-Token'] = token
  } catch {
    // 非浏览器环境忽略
  }
  Object.assign(config.headers, apiTokenHeaders())
  return config
})

client.interceptors.response.use(undefined, async (error) => {
  const status = error.response?.status
  const config = error.config
  // 只响应"全局 token 401"（响应头标记区分提示词管理 401）
  const marked = error.response?.headers?.['x-zhiwen-unauthorized'] === 'api-token'
  if (status === 401 && marked && config && !config._zhiwenTokenRetry) {
    try {
      await ensureApiToken()
      config._zhiwenTokenRetry = true
      Object.assign(config.headers, apiTokenHeaders())
      return client.request(config)
    } catch {
      // 用户取消输入：按原 401 抛出
    }
  } else if (status === 401 && marked) {
    // 重试仍是 401：令牌不对，清掉避免后续请求被坏令牌卡死（下次会重新弹窗）
    try {
      localStorage.removeItem(API_TOKEN_KEY)
    } catch {
      // ignore
    }
  }
  return Promise.reject(error)
})

export default client
