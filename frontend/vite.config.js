import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// 开发服务器配置：/api 请求代理到 FastAPI 后端（127.0.0.1:8000）
export default defineConfig({
  plugins: [vue()],
  build: {
    // 依赖拆成独立 chunk：应用代码改动不会让用户重下 vue/element-plus/echarts
    // （此前 index 1.2MB + RunsView 1.14MB 单块，缓存利用率低）
    // element-plus 整包 ~950KB 是已知的最大块（按需引入组件是后续项）；
    // 阈值定 1000 让其它块超限时仍能报警
    chunkSizeWarningLimit: 1000,
    rollupOptions: {
      output: {
        manualChunks(id) {
          if (!id.includes('node_modules')) return undefined
          if (id.includes('echarts') || id.includes('zrender')) return 'vendor-echarts'
          if (id.includes('element-plus') || id.includes('@element-plus')) {
            return 'vendor-element'
          }
          if (id.includes('vue-router') || id.includes('@vue/') || id.includes('/vue/')) {
            return 'vendor-vue'
          }
          // 其余依赖（pdfjs-dist / epubjs / docx-preview 等）保持"谁按需引入就在谁的
          // chunk 里"——统一塞进 vendor 会让查看器的重型库变成首屏必下载
          return undefined
        },
      },
    },
  },
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
      },
    },
  },
})
