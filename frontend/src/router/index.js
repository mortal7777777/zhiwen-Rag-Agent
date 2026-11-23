import { createRouter, createWebHistory } from 'vue-router'

const routes = [
  {
    path: '/',
    name: 'chat',
    component: () => import('../views/ChatView.vue'),
    meta: { title: '知问 ZhiWen' },
  },
  {
    path: '/knowledge',
    name: 'knowledge',
    component: () => import('../views/KnowledgeView.vue'),
    meta: { title: '知识库' },
  },
  {
    path: '/runs',
    name: 'runs',
    component: () => import('../views/RunsView.vue'),
    meta: { title: '运行记录' },
  },
  {
    path: '/prompts',
    name: 'prompts',
    component: () => import('../views/PromptWorkbenchView.vue'),
    meta: { title: '提示词工作台' },
  },
]

const router = createRouter({
  history: createWebHistory(),
  routes,
})

export default router
