# Vue 3 学习指南：从概念到本项目实战

> 适用对象：学过基础前端（HTML/JS）但对 Vue 不熟，希望**通过本项目代码**快速达到
> "熟悉 Vue、能独立改前端"水平的读者。
> 学习方式：每个概念 = ① 一句话本质 → ② 本项目真实代码对照 → ③ 自己动手改一处。
> 配合 [DATA_FLOW_GUIDE.md](DATA_FLOW_GUIDE.md) 一起读（那篇讲前后端怎么通信）。

---

## 0. 这个前端项目的地图

```
frontend/
├── index.html              # 唯一的 HTML 入口（Vue 挂载点）
├── vite.config.js          # 开发服务器配置 + /api 代理到后端
├── package.json            # 依赖清单（vue3 / vue-router4 / element-plus / axios）
└── src/
    ├── main.js             # 程序入口：创建应用、装插件、挂载
    ├── App.vue             # 根组件：全局导航 + 设置弹窗 + <router-view>
    ├── router/index.js     # 路由表：/ 、/knowledge 、/runs
    ├── api/index.js        # ★ 所有后端请求的封装（前后端解耦的关键）
    ├── styles/theme.css    # 全局样式
    ├── views/              # 页面级组件（路由直接加载它们）
    │   ├── ChatView.vue        # 聊天主界面（3473 行，本项目最大的学习样本）
    │   ├── KnowledgeView.vue   # 知识库管理（上传/删除/预览/重建索引）
    │   └── RunsView.vue        # Agent 运行记录
    └── components/         # 复用子组件
        ├── MarkdownContent.vue    # 把模型回答渲染成 Markdown
        ├── DocumentViewer.vue     # 文档预览总入口（pdf/epub/docx/txt 分发）
        └── viewer/…              # pdf.js / epub.js / docx-preview 封装
```

技术栈结论（重要）：

- **Vue 3 + `<script setup>` 组合式 API**（不是选项式 API，看教程要认准版本）；
- 组件库 **Element Plus**（el- 开头的都是现成组件）；
- 路由 **vue-router 4**；HTTP 用 **axios** + 原生 `fetch` 各司其职；
- **没有 Pinia/Vuex**——项目靠"页面内部 state + localStorage 持久化 + keep-alive 缓存"管理状态，
  这是刻意保持简单的取舍，后面 §8 会讲。

启动（两个终端）：

```bash
# 后端（backend 目录）
python -m uvicorn app.main:app --reload --port 8000
# 前端（frontend 目录）
npm run dev        # 打开 http://localhost:5173
```

`npm run dev` 是 `vite`（package.json scripts），Vite 是开发服务器+打包器：改代码热更新，
`vite.config.js` 里把 `/api` 开头的请求代理到 `http://127.0.0.1:8000`（后端）。这就是
"前端里所有请求都写 `/api/...` 而不是 `http://localhost:8000/api/...`"的原因。

---

## 1. 入口：从 index.html 到第一个页面

Vue 是"单页应用"：整个项目只有一个 HTML，后续页面切换全部在 JS 里完成，不刷新浏览器。

`src/main.js` 的全部逻辑（14 行）：

```js
import { createApp } from 'vue'
import ElementPlus from 'element-plus'
import zhCn from 'element-plus/es/locale/lang/zh-cn'
import 'element-plus/dist/index.css'          // 组件库样式（全局引入）
import './styles/theme.css'                    // 项目自定义样式

import App from './App.vue'                    // 根组件
import router from './router'                  // 路由

const app = createApp(App)                     // ① 以 App.vue 为根创建应用
app.use(ElementPlus, { locale: zhCn })         // ② 注册组件库（模板里就能用 <el-button> 等）
app.use(router)                                // ③ 注册路由（模板里就能用 <router-view>）
app.mount('#app')                              // ④ 挂载到 index.html 的 <div id="app">
```

对应 Vue 概念：**应用实例 + 插件机制**。`app.use(X)` 就是把一个插件装进应用，
Element Plus、vue-router 都是插件。`mount('#app')` 把 Vue 接管 index.html 里 `id="app"`
的节点，之后该节点内部完全由 Vue 渲染。

## 2. 单文件组件（SFC）：一个 .vue 文件 = 一个组件

ChatView.vue / App.vue 都是"单文件组件"：`<template>`（长得像 HTML 的模板）、
`<script setup>`（逻辑）、`<style>`（样式）写在一个文件里。

组件分两类，两种写法：

| 概念 | 本例 | 说明 |
|---|---|---|
| 页面组件 | `views/ChatView.vue` 等 | 被路由懒加载，一个页面一个文件 |
| 复用组件 | `components/MarkdownContent.vue` 等 | 在别的组件里 import 后当标签用 |

路由表（`router/index.js`）决定了 URL 与页面的关系：

```js
const routes = [
  { path: '/', name: 'chat', component: () => import('../views/ChatView.vue') },
  { path: '/knowledge', component: () => import('../views/KnowledgeView.vue') },
  { path: '/runs', component: () => import('../views/RunsView.vue') },
]
```

注意 `() => import(...)` 是**懒加载**：访问到该路由才下载这个文件。根组件 App.vue 里
用 `<router-view>` 占位，路由匹配到的页面会渲染到这个位置；外面包一层
`<keep-alive include="ChatView">`（App.vue:53-57），作用是切走再切回时**保留聊天页的
内存状态**（消息不丢）。

> 小实验①：给路由表加一个 `/about` 路由指向新写的 `views/AboutView.vue`（先复制
> ChatView 改名即可），App.vue 的 `<el-menu>` 里加一项跳过去，体会"路由→页面"。

## 3. 模板语法：在 HTML 里写逻辑

模板 = HTML + Vue 指令。从 ChatView.vue 会话列表摘一段真实代码（:26-42）：

```html
<div v-for="session in sessions" :key="session.id"
     :class="['session-item', { active: session.id === activeId }]"
     @click="switchSession(session)">
  <div class="session-info">
    <span class="session-title">{{ session.title }}</span>
    <span v-if="session.message_count" class="session-count">
      {{ session.message_count }}
    </span>
  </div>
  <div class="session-actions" @click.stop>          <!-- .stop = 阻止冒泡 -->
    <el-icon @click="handleRename(session)"><EditPen /></el-icon>
    <el-icon @click="handleDelete(session)"><Delete /></el-icon>
  </div>
</div>
```

对照学习表（会了这张表就懂 80% 的模板）：

| 语法 | 作用 | 本项目的例子 |
|---|---|---|
| `{{ expr }}` | 文本插值，输出变量值 | `{{ session.title }}` |
| `v-for="x in list" :key="唯一id"` | 循环渲染列表 | `sessions`、`messages`、`msg.plan` |
| `v-if` / `v-else` / `v-show` | 条件渲染（v-show 只切 display） | `v-if="!sessions.length"` 空态 |
| `:prop="值"`（v-bind 简写） | 给组件/元素传动态值 | `:class`、`:disabled="loading"`、`:src` |
| `@事件="函数"`（v-on 简写） | 绑定事件 | `@click`、`@scroll`、`@keydown` |
| `v-model` | 双向绑定（输入框↔变量） | 聊天输入框 `question` |
| `:class` 带对象 | 条件加类名 | `{ active: session.id === activeId }` |

关于 `:key`：v-for 里必须给每一项一个稳定 id，Vue 靠它做**列表复用与精确更新**
（没有 key，插入/删除时 Vue 会"错位复用"产生 bug——本项目的消息流靠它正确渲染）。

`:class` 的对象语法 `{ active: 布尔 }` 是高频写法：布尔为真就加 `active` 类。
ChatView 的消息区（:128-133）连用三个绑定：

```html
<div v-for="(msg, index) in messages"
     :key="index"
     :class="['msg', msg.role]"
     :data-msg-index="index">
```

注意消息列表的 `:key` 用的是 `index` 而不是 id——对"只从尾部追加、可截断尾部"的
消息流来说这是安全且省事的（流式生成时还靠它保证顺序）；但**任何需要中间插入/重排
的列表都必须用稳定 id**，比如会话列表用的是 `:key="session.id"`（:27-28）。

> 小实验②：把聊天气泡背景色与角色绑定——找到 `.msg.user` / `.msg.assistant` 的
> CSS（ChatView.vue `<style>` 区），各改一种颜色，刷新看效果。

## 4. 响应式状态：ref / reactive / computed / watch

Vue 3 的核心是**响应式**：变量一变，用到它的界面自动变。你不需要手动操作 DOM。

### ref —— 包装一个"被追踪"的值

ChatView.vue 顶部（:845-849）：

```js
const question = ref('')        // 输入框内容
const loading = ref(false)      // 是否正在生成
const messages = ref([])        // 当前会话的消息数组
```

规律（背下来，新手 90% 的错误在这里）：

- **JS 里读写必须 `.value`**：`question.value = ''`、`messages.value.push(x)`；
- **模板里不需要 `.value`**：直接 `{{ question }}`、`:disabled="loading"`（Vue 自动解包）；
- `ref` 可以是任意类型：`ref([])` 数组照样是响应式的，`push`/`splice`/直接下标改值都会被追踪；
- 用 `let streamMsg = null`（:891）保存"响应式数组里某一项"的引用再直接改它的字段
  （`streamMsg.content += ...`）也是响应式的——因为那一项本身就是被代理过的对象。

### 项目里一个典型的"输入 → 响应式更新"闭环

聊天输入框与发送（ChatView.vue 模板 :496-528 一带）：

```html
<el-input v-model="question" :rows="2" @keydown.enter.exact.prevent="send" ... />
<el-button @click="send" ...>发送</el-button>
```

`v-model` = `:value="question"` + `@input="question = $event"` 的语法糖：
用户打字 → `question` 变 → 输入框显示。发送后（`send()` 里 :1679）`question.value = ''`，
输入框自动清空——**全程没有一行 DOM 操作**，这就是响应式的意义。

注意修饰符 `.enter.exact.prevent`：只有"单独按 Enter"才触发发送（`.exact` 排除按了
Shift/Ctrl 等组合键的情况），`.prevent` 阻止默认行为（否则换行会插进输入框）。
模板管理对话框里的 `el-input v-model="templateForm.name"`（:576）是更简单的 v-model 例子。

### computed —— 由别的状态推导的值

App.vue（:985-987）：

```js
const widthLabel = computed(
  () => widthOptions.find((o) => o.value === contentWidth.value)?.label || contentWidth.value,
)
```

`widthOptions` 是定义好的选项数组，`computed` 从中找出当前内容的显示名。`computed`
返回**只读**的派生值：依赖变了它自动重算，模板里当普通变量用。
规则：能推导的不要存成独立 state（否则两处不同步）。

### watch —— 状态变化时的副作用

App.vue（:1008-1011）：

```js
watch(theme, () => {
  applyTheme()                               // 主题切换 → 重新应用
  localStorage.setItem(THEME_KEY, theme.value)  // 同时持久化
})
```

ChatView（:977）也用它做联动：`toolMode` 变 'auto' 时把联网/知识库两个开关同时打开。
另一个常见用法是 watch 对话框显隐做初始化（App.vue:1665 `watch(settingsVisible, …)`）。

### 模板引用 ref 与 nextTick

模板里给 DOM 打 `ref="名字"`，JS 里同名 ref 变量就是那个 DOM 元素：

```html
<div ref="messagesRef" class="messages" ...>    <!-- ChatView.vue:99-100 -->
```

```js
const messagesRef = ref(null)
```

但注意：**状态更新后 DOM 是异步刷新的**，紧接着读 DOM 会拿到旧值，要
`await nextTick()`。本项目滚动定位大量用这个模式（`:1826 nextTick(() => scrollToBottom(true))`），
"生成完后滚动到最新消息"如果不在 nextTick 里做，高度还是旧的，会滚不到位。

## 5. 综合实例剖析：聊天"打字机"效果

这是理解"响应式 + 定时器 + 流式更新"的绝佳样本（ChatView.vue:887-923）：

```js
const pendingText = ref('')        // 后端 token 先进缓冲区（不直接改正文）
let typewriterTimer = null
let streamMsg = null               // 正在生成的那条 assistant 消息的引用

function flushTypewriter() {       // 停止定时器并把积压内容一次性刷入
  if (typewriterTimer) { clearInterval(typewriterTimer); typewriterTimer = null }
  if (streamMsg && pendingText.value) {
    streamMsg.content += pendingText.value
    pendingText.value = ''
    scrollToBottom()
  }
}

function startTypewriter() {
  if (typewriterTimer || !streamMsg) return
  typewriterTimer = setInterval(() => {
    if (!pendingText.value) { flushTypewriter(); return }
    const n = Math.max(1, Math.min(10, Math.round(pendingText.value.length / 10)))
    streamMsg.content += pendingText.value.slice(0, n)   // 单帧最多写 10 字符
    pendingText.value = pendingText.value.slice(n)       // 剩下的留着下一帧
    ...
  }, 24)
}
```

它解决什么问题：后端 token 是**流式**到达的（每秒可能几百字），如果来一个就渲染一次，
内容会"整段蹦出"没有打字感。于是 token 先进 `pendingText` 缓冲区，定时器每 24ms
从缓冲区取一小段追加进 `streamMsg.content`，**content 变了模板自动重渲染**。

两个值得注意的设计：

- 缓冲为空但定时器还在 → 调 `flushTypewriter()` 自我清理，避免空转；
- 积压大时 `n` 自适应变大（最快约 400 字/秒"追赶"），不会越积越慢。

> 小实验③：把 24ms 改成 100ms、10 改成 3，感受"慢速打字"效果——理解定时器与
> 流式渲染的配合。改完记得改回来。

## 6. 组件通信：props 进、事件出

Vue 组件通信的黄金法则：**父传子用 props（属性），子传父用事件（$emit）**。
数据"单向向下流"，界面事件"向上抛"。

本项目例子（ChatView.vue 引用两个子组件，:816-817）：

```js
import MarkdownContent from '../components/MarkdownContent.vue'
import DocumentViewer from '../components/DocumentViewer.vue'
```

模板里这样用（示意）：

```html
<MarkdownContent v-if="…" :content="msg.content" />
```

`MarkdownContent` 负责把模型回答的 Markdown 渲染成富文本（内部用 `marked` 库 +
DOMPurify 消毒），ChatView 只传一个 `content` prop 进去，**渲染细节完全封装在子组件里**。
DocumentViewer 同理：父组件传文档路径，它内部再决定用 PdfViewer / EpubViewer /
DocxViewer / TextViewer 中的哪一个。

为什么要拆组件：ChatView 已经 3473 行了，把"纯展示"的部分抽出去，主文件的逻辑
（会话/流式/审批/模板管理）才看得清。这是项目里最重要的组件化动机：**为可读性而拆，
不为炫技而拆**。

## 7. 页面状态怎么管理（这个项目为什么不用 Pinia）

多页面应用常见的状态管理需求：聊天页的消息、知识库页的列表、设置……本项目刻意
选择了"轻方案"：

1. **状态留在自己页面**：每个 view 内部 `ref` 自持，切走页面组件被销毁（除了
   keep-alive 的 ChatView）；
2. **需要跨页面/跨刷新保留的数据 → localStorage**：ChatView 用
   `PREFS_KEY='rag_chat_prefs'`（:931-975）存开关与折叠状态，`loadPrefs()` 在
   `onMounted` 里恢复；
3. **真正的共享数据（会话、设置）在后端**：前端任何页面需要时通过 api 层拉取，
   不在前端复制一份全局 store。

一句话：**状态离使用它的地方越近越好；会被别的页面或下次刷新使用的，要么持久化到
localStorage，要么问后端要。** 你的新功能如果不需要跨页面共享状态，就不用引入 Pinia。

## 8. 生命周期钩子

`<script setup>` 里直接用（不需要写在对象里）：

```js
onMounted(() => { loadSessions(); loadPrefs(); })        // 组件挂载后：拉数据、恢复偏好
onBeforeUnmount(() => {                                   // 组件销毁前：清理
  window.removeEventListener('keydown', onPermissionKeydown, true)
})
```

常见对应关系（本项目实例）：

| 钩子 | 时机 | 用途 |
|---|---|---|
| `onMounted` | 首次渲染完 | 发初始请求、注册全局监听、恢复缓存 |
| `onBeforeUnmount` | 销毁前 | 移除全局监听、清定时器（防泄漏） |
| `watch(x, fn)` | x 变化 | 联动副作用（项目里大量用于开关联动） |

注意 ChatView 里审批键盘监听用的是 `window.addEventListener` + `removeEventListener`
（:1937-1941），不是模板 `@keydown`——因为审批卡可能在任何焦点位置出现，要的是
**全局**按键捕获。全局监听一定要在 onBeforeUnmount 里移除，否则组件销毁后回调还在跑
（改 state 一个"组件已卸载"警告算轻的，重的是内存泄漏）。

## 9. 状态与视图的"三件套"写法（本项目的编码惯例）

看任何页面，代码都按这个顺序组织（ChatView.vue 就是模板）：

1. **状态声明区**：一堆 `ref`/`computed` 集中放 `<script setup>` 开头；
2. **纯函数区**：不改状态的辅助函数（格式化、判断）；
3. **异步动作区**：`async function loadSessions()` 等，内部 try/catch + `ElMessage.error`
   提示错误——每个请求函数都要考虑"失败了用户看到什么"；
4. **watch/onMounted 收尾**。

模板里则遵守：**列表用 v-for + :key；空态要单独 v-if；危险操作要确认
（ElMessageBox.confirm）**。浏览一下 KnowledgeView.vue（493 行，最小的完整页面）把
这套惯例看一遍，你写新页面的骨架就有了。

## 10. 从"会看"到"会改"：练习清单（按顺序）

按这个顺序做，每步都在本地 `npm run dev` 里验证：

1. **改文案**：ChatView 欢迎页标题"有什么可以帮你？"改成你自己的话 → 热更新立刻生效，
   体会"改代码→保存→浏览器自动变"的开发循环。
2. **改样式**：给 `.welcome-title` 换个颜色字号（在 ChatView.vue `<style>` 里找）。
3. **加状态**：页头加一个 `const myCount = ref(0)`，放个按钮 `@click="myCount++"` 显示次数。
4. **条件渲染**：`v-if="myCount > 3"` 显示一句"点到四次了"。
5. **调真实接口（读）**：在页面 onMounted 里调 api 层现成函数，如 `listTemplates()`，
   把模板名渲染成一个列表（模板已存在，只需展示——学会"消费一个接口"）。
6. **调真实接口（写）**：KnowledgeView 抄一段上传逻辑，加个"上传后自动重建索引"的开关
   （提示：`rebuildIndex()` 现成）。
7. **自己封装一个接口**：在 `api/index.js` 按现有函数样式加一个
   `export const getHealth = () => client.get('/health').then(r => r.data)`，
   后端 `api/health.py` 已有该端点——体会"加一个功能 = 后端加路由 + 前端加 api 函数 + 页面调用"。
8. **做一个小页面**：新建 `views/AboutView.vue`（组件/版本号/一句自我介绍），加路由 +
   左侧菜单项（对照 §2 实验①）。
9. **手写一个"流式输出"**：不用 streamAgentChat，在 /about 页面写 `fetch('/api/chat/stream')`
   拿 reader 逐段读，把返回的 token 拼到 ref 里——后端接口现成（backend `api/chat.py`），
   这一步做完你就真正理解 §7 打字机与 DATA_FLOW 的 SSE 章节。
10. **理解一个 Bug**：把 Api 层一个请求故意改错 URL，在浏览器 Network 面板看请求与
    报错形状；再把 `:key` 删掉跑一个长会话，观察列表错乱——自己"制造"一次再修好，
    比读十遍文档都牢。

自测题（能不看代码答出再看答案）：

- Q1：为什么 JS 里要 `messages.value.push` 而模板里写 `{{ messages.length }}` 不用 .value？
- Q2：v-for 不写 :key 会怎样？
- Q3：改一个 ref 的值后立刻读 DOM 高度为什么是旧的？怎么解决？
- Q4：组件销毁时忘记 removeEventListener 会怎样？
- Q5：为什么本项目没有 Pinia？哪些数据其实需要跨页面共享？

## 11. 常见坑清单（本项目踩过的真实注释）

1. **script setup 与选项式混淆**：看网上教程先确认是 Vue3 组合式 API 的（`ref` 导入自
   'vue'），Vue2 时代"data(){return{}}"的教程不适用本项目。
2. **忘写 .value**：模板外读写 ref 变量不加 .value，最常见的报错与静默 bug 来源。
3. **列表 :key 用下标代替 id**：插入/删除/重排后渲染错位。例外：消息流这种
   "只尾插+可截尾"的列表刻意用 index（见 §3），但一般业务列表（会话、文档）必须 id。
4. **v-model 用在组件上的自定义语义**：Element Plus 的 el-input 直接可用；自定义组件要
   自己 defineModel/emit('update:modelValue')。
5. **在 nextTick 前操作依赖新高度的 DOM**：滚动定位必须 `await nextTick()`。
6. **Element Plus 组件样式不生效**：检查 main.js 是否 import 了
   `element-plus/dist/index.css`（本项目已全局引）。
7. **@click.stop vs 冒泡**：行内操作按钮（如会话删除）要 `.stop`，否则会连带触发
   父级 `@click="switchSession"`——这正是 ChatView.vue:38 `@click.stop` 存在的原因。
8. **encodeURIComponent 分段编码**：文档路径含中文/斜杠，api/index.js:191-195 对每一段
   单独编码再拼接（整体编码会把 `/` 也转掉，路由对不上）。
9. **类型是字符串的数字**：`:key="session.id"` 若后端返回字符串会引发意外；本项目
   后端 id 是 int，保持一致。
10. **不要在大页面里堆所有子 UI**：超过 ~500 行就考虑抽组件（MarkdownContent 就是先例）。

## 附：快速导航表（照着读源码）

| 想学什么 | 打开哪个文件 | 看哪段 |
|---|---|---|
| 应用启动 | `src/main.js` | 全文 14 行 |
| 路由 | `src/router/index.js` | 全文 |
| 全局布局+导航+设置弹窗 | `src/App.vue` | 模板 1-58 行；script 915-955 |
| 会话列表/消息渲染 | `views/ChatView.vue` | 模板 23-58、130-260 |
| 输入→发送→流式接收 | `views/ChatView.vue` | script 1651-1852（send 函数） |
| 打字机效果 | `views/ChatView.vue` | script 887-923 |
| 页面最小完整样本 | `views/KnowledgeView.vue` | 全文 493 行 |
| 所有后端请求封装 | `src/api/index.js` | 全文 327 行 |
| 组件封装范例 | `components/MarkdownContent.vue` | 全文 |
| Markdown 渲染 | `components/MarkdownContent.vue` | props 与内部 marked 用法 |

下一步：读完本篇去读 [DATA_FLOW_GUIDE.md](DATA_FLOW_GUIDE.md)（前端发的每个请求在
后端怎么被接收和处理），再读 [FASTAPI_GUIDE.md](FASTAPI_GUIDE.md) 把后端补齐，
你就拥有了"全栈改这个项目"的能力。
