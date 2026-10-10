/** axios 实例（统一 baseURL / 超时）。所有域的模块都从这里取 client。 */

import axios from 'axios'

// 统一的后端请求封装：前端只管调用函数，不关心 URL 细节
const client = axios.create({
  baseURL: '/api',
  timeout: 600000, // 首次建索引可能较慢，给足超时时间
})

export default client
