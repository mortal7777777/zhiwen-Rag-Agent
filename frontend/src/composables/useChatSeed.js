/**
 * 跨路由把"查看器划选"带到对话页。
 *
 * 模块级 reactive（同一 SPA 内跨路由共享）；ChatView 激活时 consume 一次，
 * 避免 keep-alive 每次切回都重复应用。
 */
import { reactive } from 'vue'

const seed = reactive({
  nonce: 0,
  quote: null,
  scope: null,
  question: '',
})

export function seedChat(payload = {}) {
  seed.quote = payload.quote || null
  seed.scope = payload.scope || null
  seed.question = payload.question || ''
  seed.nonce += 1
}

export function consumeChatSeed() {
  if (!seed.nonce) return null
  const out = {
    quote: seed.quote,
    scope: seed.scope,
    question: seed.question,
  }
  seed.nonce = 0
  seed.quote = null
  seed.scope = null
  seed.question = ''
  return out
}
