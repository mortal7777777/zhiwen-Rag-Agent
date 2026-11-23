import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import Components from 'unplugin-vue-components/vite'
import fs from 'node:fs'
import path from 'node:path'

/**
 * element-plus 深路径 resolver（2026-11-19 P2·C6）。
 *
 * 为什么不用官方 ElementPlusResolver：它对全部组件统一返回 `element-plus/es`
 * 平铺入口。平铺入口在无 manualChunks 时可摇树，但本项目用 manualChunks 做
 * 依赖缓存分块，其「命中模块的整棵依赖子树进 chunk」的语义会把平铺入口引到的
 * 全部组件拖进 vendor-element（实测 953KB 整库）。深路径
 * `es/components/<name>/index` 是按组件的独立模块，摇树粒度正确。
 *
 * 归属不规则的组件（ElTableColumn 住在 table/、ElMain 住在 container/ 等）
 * 用显式映射表；未收录的名字回退平铺入口并打构建警告（提醒补映射）。
 */
const EL_IRREGULAR = {
  // container 家族：aside/header/footer/main 都住在 components/container/
  Aside: 'container',
  Header: 'container',
  Footer: 'container',
  Main: 'container',
  // *-item / *-group 等：与父组件同目录
  TableColumn: 'table',
  MenuItem: 'menu',
  MenuItemGroup: 'menu',
  SubMenu: 'menu',
  BreadcrumbItem: 'breadcrumb',
  DropdownItem: 'dropdown',
  DropdownMenu: 'dropdown',
  FormItem: 'form',
  CheckboxButton: 'checkbox',
  CheckboxGroup: 'checkbox',
  CollapseItem: 'collapse',
  TabPane: 'tabs',
  TimelineItem: 'timeline',
  DescriptionsItem: 'descriptions',
  Step: 'steps',
  SkeletonItem: 'skeleton',
  Option: 'select',
  OptionGroup: 'select',
  RadioButton: 'radio',
  RadioGroup: 'radio',
  CarouselItem: 'carousel',
  AnchorLink: 'anchor',
  TourStep: 'tour',
  InfiniteScroll: 'infinite-scroll',
}

function deepElementPlusResolver() {
  const epRoot = path.resolve(__dirname, 'node_modules/element-plus/es/components')
  const cache = new Map()
  return [
    (name) => {
      if (!/^El[A-Z]/.test(name)) return undefined
      if (/^ElIcon[A-Z]/.test(name)) {
        return { name: name.replace(/^ElIcon/, ''), from: '@element-plus/icons-vue' }
      }
      const bare = name.slice(2)
      const partial =
        bare in EL_IRREGULAR
          ? EL_IRREGULAR[bare]
          : bare
              .replace(/([a-z\d])([A-Z])/g, '$1-$2')
              .replace(/([A-Z]+)([A-Z][a-z])/g, '$1-$2')
              .toLowerCase()
      let from = cache.get(partial)
      if (from === undefined) {
        const folder = path.join(epRoot, partial)
        if (fs.existsSync(path.join(folder, 'index.mjs'))) {
          from = `element-plus/es/components/${partial}/index`
        } else {
          from = 'element-plus/es'
          console.warn(
            `[deepElementPlusResolver] ${name} 无深路径映射（components/${partial} 不存在），` +
              '回退平铺入口；请补 EL_IRREGULAR 映射表',
          )
        }
        cache.set(partial, from)
      }
      if (from === 'element-plus/es') return { name, from }
      return {
        name,
        from,
        sideEffects: `element-plus/es/components/${partial}/style/css`,
      }
    },
  ]
}

// 开发服务器配置：/api 请求代理到 FastAPI 后端（127.0.0.1:8000）
export default defineConfig({
  plugins: [
    vue(),
    // element-plus 按需引入：模板 el-* 精确注入深路径；程序式组件
    // （ElMessage/ElMessageBox）与 v-loading 指令在 main.js 手动引样式。
    // dts: false —— 纯 JS 项目不需要 components.d.ts
    Components({ resolvers: [deepElementPlusResolver()], dts: false }),
  ],
  build: {
    // 依赖拆成独立 chunk：应用代码改动不会让用户重下 vue/element-plus/echarts
    // （此前 index 1.2MB + RunsView 1.14MB 单块，缓存利用率低）
    // 阈值从 1000 降到 600：按需引入后 vendor-element 不应再回到整包量级
    chunkSizeWarningLimit: 600,
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
