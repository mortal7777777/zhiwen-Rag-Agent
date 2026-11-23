import { createApp } from 'vue'
// element-plus 按需引入（2026-11-19 P2·C6）：
// - 模板组件由 vite 的 unplugin-vue-components + ElementPlusResolver 精确注入，
//   不再整包 import ElementPlus（此前 vendor-element ~956KB）；
// - 程序式组件（ElMessage / ElMessageBox）与 v-loading 指令不走模板扫描，
//   样式必须手动引一次，否则暗色主题下无样式/白条；
// - 中文 locale 从全局 app.use 移到 App.vue 的 ElConfigProvider。
import 'element-plus/theme-chalk/dark/css-vars.css'
import 'element-plus/es/components/message/style/css'
import 'element-plus/es/components/message-box/style/css'
import 'element-plus/es/components/loading/style/css'
import { ElLoadingDirective } from 'element-plus/es/components/loading/index'
import './styles/theme.css'

import App from './App.vue'
import router from './router'

const app = createApp(App)
app.directive('loading', ElLoadingDirective)
app.use(router)
app.mount('#app')
