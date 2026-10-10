/**
 * 组件预览入口（开发用，不参与构建产物）。
 *
 * 用途：把从 ChatView 抽出的卡片组件单独挂起来做**视觉验证**——不需要后端，
 * 也不需要模型：`npm run dev` 后访问 /dev/preview.html?theme=dark 切换主题，
 * 由 dev/shot_cards.mjs 用 Playwright 截图（写到 scratch/check_outputs/）。
 *
 * 视觉验证的必要性：卡片样式是 `scoped`，从 4000 行的视图里搬出来后必须确认
 * 样式跟着走了（父组件的 scoped 规则不会作用到子组件内部元素）。
 */
import { createApp, h, reactive, ref } from 'vue'
import ElementPlus from 'element-plus'
import zhCn from 'element-plus/es/locale/lang/zh-cn'
import 'element-plus/dist/index.css'
import 'element-plus/theme-chalk/dark/css-vars.css'
import '../src/styles/theme.css'
import PlanApprovalCard from '../src/components/chat/PlanApprovalCard.vue'
import PlanSteps from '../src/components/chat/PlanSteps.vue'
import PermissionCard from '../src/components/chat/PermissionCard.vue'
import ToolTraceList from '../src/components/chat/ToolTraceList.vue'
import SourceList from '../src/components/chat/SourceList.vue'
import TodoPanel from '../src/components/chat/TodoPanel.vue'
import SessionList from '../src/components/chat/SessionList.vue'

// 主题：?theme=dark 时挂 html.dark（与 App.vue 的主题机制一致）
const dark = new URLSearchParams(location.search).get('theme') === 'dark'
document.documentElement.classList.toggle('dark', dark)
document.documentElement.dataset.theme = dark ? 'dark' : 'light'

const fixtures = [
  {
    label: 'pending（带 summary）',
    approval: {
      state: 'pending',
      summary: '给知识库补一份季度复盘，先只读探查再落地',
      text: '读取 data/ 下最近三个月的文档\n汇总关键指标到 scratch/quarterly.md\n校验 Markdown 语法并给出引用来源',
      steps: [],
    },
  },
  {
    label: 'pending（无 summary，按钮禁用态）',
    approval: { state: 'pending', summary: '', text: '只列一步', steps: [] },
    loading: true,
  },
  {
    label: 'confirmed（已完成态，走共享 .plan-step 样式）',
    approval: {
      state: 'confirmed',
      summary: '给知识库补一份季度复盘',
      text: '',
      steps: ['读取 data/ 下最近三个月的文档', '汇总关键指标到 scratch/quarterly.md', '校验 Markdown 语法'],
    },
  },
]

// toolName 在正式应用里由 ChatView 注入（与 useAgentStream 同一函数）；
// 预览页用最小映射替代（镜像 mcp_ 前缀规则），保证卡片上展示的是中文工具名即可。
const previewToolName = (name) => {
  if (name && name.startsWith('mcp_')) {
    return `MCP · ${name.slice(4).replace(/_/g, ' ')}`
  }
  return (
    {
      edit_file: '编辑文件',
      write_file: '写入文件',
      bash: '执行命令',
      delete_file: '删除文件',
      knowledge_base_search: '知识库检索',
      web_search: '联网搜索',
      image_to_text: '图片识别',
      skill_lookup: '查找技能',
    }[name] || name
  )
}

// 审批卡夹具：覆盖三种待决定形态（diff_lines / old-new / args_text）、
// 两种已决 chip 与挂起条；reactive 包装贴近真实会话（hover 切换 active 态）。
const permFixture = reactive([
  {
    id: 'perm-diff',
    status: 'pending',
    tool: 'edit_file',
    summary: '编辑文件 docs/notes.md（替换引言段落）',
    reason: '',
    rememberable: true,
    args: {
      diff_lines: [
        { op: ' ', text: '## 背景' },
        { op: '-', text: '本项目的简介。' },
        { op: '+', text: '本项目的简介（含目标与范围）。' },
        { op: '+', text: '维护者：知问团队。' },
        { op: ' ', text: '' },
      ],
    },
    args_text: '',
    _focusIndex: 1,
    _suspended: true,
  },
  {
    id: 'perm-oldnew',
    status: 'pending',
    tool: 'edit_file',
    summary: '编辑文件 backend/app/config.py（单处替换）',
    reason: '',
    rememberable: false,
    args: {
      old_string: 'AGENT_MAX_ITERATIONS = 30',
      new_string: 'AGENT_MAX_ITERATIONS = 50',
    },
    args_text: '',
    _focusIndex: 0,
  },
  {
    id: 'perm-args',
    status: 'pending',
    tool: 'bash',
    summary: '执行命令：python -m pytest tests -q',
    reason: '',
    rememberable: false,
    args: null,
    args_text: 'cwd: backend\ncommand: python -m pytest tests -q\n说明: 全量测试约 30s',
    _focusIndex: 0,
  },
  {
    id: 'perm-ok',
    status: 'approved',
    tool: 'write_file',
    summary: '写入文件 scratch/quarterly.md',
    reason: '已记住，下次不再询问',
  },
  {
    id: 'perm-no',
    status: 'denied',
    tool: 'delete_file',
    summary: '删除文件 data/old_notes.md',
    reason: '这份还要留着',
  },
])

// 工具轨迹夹具：两种思考链形态（已完成展开 / 进行中）+ 多种工具 chip
//（含展开的 detail、「执行中…」回退、mcp_ 图标分支）；reactive 便于点击折叠/展开
const traceFixture = reactive([
  {
    kind: 'thinking',
    text: '用户想要一份季度复盘：先看知识库里最近三个月的文档，再决定汇总口径……',
    done: true,
    duration: 3,
    _open: true,
  },
  { kind: 'thinking', text: '正在梳理可用的数据源……', done: false },
  {
    name: 'knowledge_base_search',
    summary: '检索「季度复盘」相关段落',
    arguments: { query: '季度复盘' },
  },
  {
    name: 'bash',
    summary: '执行命令：pytest tests -q',
    detail: 'collected 434 items\n\n434 passed in 27.06s',
    _open: true,
  },
  { name: 'mcp_playwright_browser_click', summary: '' },
  {
    name: 'web_search',
    summary: '「DeepSeek 前缀缓存」最新进展',
    arguments: { query: 'prefix cache' },
  },
])

// 引用来源夹具：知识库 ×2（带/不带页码）+ 联网 ×1；展开态由 ref 控制（预览里可点击折叠）
const sourceFixture = [
  {
    index: 1,
    source: '技术方案.md',
    score: 0.92,
    page: 12,
    content: '在检索增强阶段采用 Parent-Child 切分：子块（320/64）用于召回，父块（~600）用于生成……',
  },
  {
    index: 2,
    source: 'RAG-优化笔记.md',
    score: 0.81,
    content: 'RRF 融合多路召回后，接本地 bge 重排模型精排，最后做 parent 聚合……',
  },
  {
    index: 3,
    type: 'web',
    title: 'DeepSeek 前缀缓存文档',
    url: 'https://api-docs.deepseek.com/guides/kv_cache',
    content: '自动前缀缓存命中条件：请求前缀与上一轮逐字节一致……',
  },
]

// 执行计划 / 任务清单两种形态：plan（走 PlanSteps）与 todos（勾选清单，2/4 完成）
const todoPlanFixture = {
  plan: ['读取 data/ 下最近三个月的文档', '汇总关键指标到 scratch/quarterly.md', '校验 Markdown 语法'],
  todos: [],
  planDone: 1,
  planTotal: 3,
  showRunning: true,
}

const todoListFixture = {
  plan: [],
  todos: [
    { id: 't1', text: '读取 data/ 下最近三个月的文档', done: true },
    { id: 't2', text: '汇总关键指标到 scratch/quarterly.md', done: true },
    { id: 't3', text: '校验 Markdown 语法并给出引用来源', done: false },
    { id: 't4', text: '输出季度复盘草稿', done: false },
  ],
  planDone: 2,
  planTotal: 4,
  showRunning: false,
}

// 会话列表夹具：4 条（含 active 与消息数角标）；另挂一个折叠竖条实例
const sessionFixture = [
  { id: 350, title: '《梦、进化与价值》阅读笔记整理', message_count: 12 },
  { id: 351, title: '群体实相与共识现实', message_count: 8 },
  { id: 352, title: '前端重构：卡片组件拆分方案', message_count: 21 },
  { id: 353, title: '狂喜艺术的核心思想', message_count: 6 },
]

const App = {
  setup() {
    const confirmed = ref(null)
    const permEvent = ref(null)
    const sourceOpen = ref(true)
    const sourceEvent = ref(null)
    const sessionActive = ref(352)
    const sessionCollapsed = ref(false)
    const sessionCollapsedRail = ref(true)
    const sessionEvent = ref(null)
    return () =>
      h(
        'div',
        {
          class: 'preview-wrap',
          style: {
            background: 'var(--bg-app)',
            minHeight: '100vh',
            padding: '24px',
            fontFamily: 'var(--font-sans, system-ui, sans-serif)',
          },
        },
        fixtures.map((f, i) =>
          h('section', { key: i, style: { maxWidth: '720px', marginBottom: '24px' } }, [
            h(
              'div',
              {
                style: {
                  fontSize: '12px',
                  color: 'var(--text-3)',
                  marginBottom: '6px',
                },
              },
              f.label,
            ),
            h(
              'div',
              {
                style: {
                  background: 'var(--bg-chat)',
                  borderRadius: 'var(--radius-lg)',
                  padding: '14px 16px',
                },
              },
              [
                h(PlanApprovalCard, {
                  approval: f.approval,
                  loading: f.loading || false,
                  onConfirm: () => {
                    confirmed.value = f.label
                  },
                  onCancel: () => {
                    confirmed.value = `cancel:${f.label}`
                  },
                  onKeydown: () => {},
                }),
              ],
            ),
          ]),
        ),
        // 共享步骤组件的两种形态（progress 带完成/进行中状态；plain 只有序号）
        h('section', { style: { maxWidth: '720px' } }, [
          h(
            'div',
            { style: { fontSize: '12px', color: 'var(--text-3)', marginBottom: '6px' } },
            'PlanSteps：progress（1 完成 / 1 进行中）与 plain',
          ),
          h(
            'div',
            {
              style: {
                background: 'var(--bg-chat)',
                borderRadius: 'var(--radius-lg)',
                padding: '14px 16px',
                display: 'flex',
                flexDirection: 'column',
                gap: '16px',
              },
            },
            [
              h(PlanSteps, {
                steps: ['读取 data/ 下最近三个月的文档', '汇总关键指标', '输出季度复盘'],
                doneCount: 1,
                showRunning: true,
              }),
              h(PlanSteps, {
                steps: ['只带序号的简单清单', '第二行'],
                variant: 'plain',
              }),
            ],
          ),
        ]),

        // 人工确认卡（HITL）：待决定 ×3（diff_lines / old-new / args_text 三种形态）
        // + 已决 chip ×2 + 挂起条；点击选项会写下面的事件行（验证事件接线）
        h('section', { style: { maxWidth: '720px', marginTop: '24px' } }, [
          h(
            'div',
            { style: { fontSize: '12px', color: 'var(--text-3)', marginBottom: '6px' } },
            'PermissionCard：待决定 ×3 + 已决 ×2 + 挂起条',
          ),
          h(
            'div',
            {
              style: {
                background: 'var(--bg-chat)',
                borderRadius: 'var(--radius-lg)',
                padding: '14px 16px',
              },
            },
            [
              h(PermissionCard, {
                permissions: permFixture,
                loading: false,
                toolName: previewToolName,
                onDecide: (p, optionIndex) => {
                  permEvent.value = `decide: ${p.id} → ${optionIndex}`
                },
                onStop: () => {
                  permEvent.value = 'stop'
                },
              }),
            ],
          ),
          h(
            'div',
            { style: { fontSize: '11px', color: 'var(--text-3)', marginTop: '6px' } },
            `事件：${permEvent.value || '（未触发）'}`,
          ),
        ]),

        // 工具轨迹 + 思考链：已完成（展开）/ 进行中 / 不可展开 chip / 展开的 detail
        h('section', { style: { maxWidth: '720px', marginTop: '24px' } }, [
          h(
            'div',
            { style: { fontSize: '12px', color: 'var(--text-3)', marginBottom: '6px' } },
            'ToolTraceList：思考链 ×2 + 工具 chip ×4（含展开 detail 与「执行中…」）',
          ),
          h(
            'div',
            {
              style: {
                background: 'var(--bg-chat)',
                borderRadius: 'var(--radius-lg)',
                padding: '14px 16px',
              },
            },
            [
              h(ToolTraceList, {
                items: traceFixture,
                toolName: previewToolName,
              }),
            ],
          ),
        ]),

        // 引用来源：知识库 ×2（带/不带页码）+ 联网 ×1；open 受控（点头部折叠）
        h('section', { style: { maxWidth: '720px', marginTop: '24px' } }, [
          h(
            'div',
            { style: { fontSize: '12px', color: 'var(--text-3)', marginBottom: '6px' } },
            'SourceList：知识库 ×2 + 联网 ×1（open=true；点头部可折叠）',
          ),
          h(
            'div',
            {
              style: {
                background: 'var(--bg-chat)',
                borderRadius: 'var(--radius-lg)',
                padding: '14px 16px',
              },
            },
            [
              h(SourceList, {
                sources: sourceFixture,
                open: sourceOpen.value,
                'onUpdate:open': (v) => {
                  sourceOpen.value = v
                },
                onOpenDoc: (src) => {
                  sourceEvent.value = `open-doc: ${src.source}`
                },
              }),
            ],
          ),
          h(
            'div',
            { style: { fontSize: '11px', color: 'var(--text-3)', marginTop: '6px' } },
            `事件：${sourceEvent.value || '（未触发）'}`,
          ),
        ]),

        // 执行计划（走 PlanSteps，含恢复的 .plan-head 主色标题）与任务清单（勾选清单）
        h('section', { class: 'shot-todo-panel', style: { maxWidth: '720px', marginTop: '24px' } }, [
          h(
            'div',
            { style: { fontSize: '12px', color: 'var(--text-3)', marginBottom: '6px' } },
            'TodoPanel：执行计划（plan 1/3，进行中）/ 任务清单（todos 2/4）',
          ),
          h(
            'div',
            {
              style: {
                background: 'var(--bg-chat)',
                borderRadius: 'var(--radius-lg)',
                padding: '14px 16px',
                display: 'flex',
                flexDirection: 'column',
                gap: '16px',
              },
            },
            [h(TodoPanel, { ...todoPlanFixture }), h(TodoPanel, { ...todoListFixture })],
          ),
        ]),

        // 左侧会话列表：展开（active=352）与折叠竖条两个实例；点条目/按钮写事件行
        h('section', { class: 'shot-session-list', style: { maxWidth: '720px', marginTop: '24px' } }, [
          h(
            'div',
            { style: { fontSize: '12px', color: 'var(--text-3)', marginBottom: '6px' } },
            'SessionList：展开（active + 消息数/悬停操作）+ 折叠竖条',
          ),
          h('div', { style: { display: 'flex', gap: '16px', alignItems: 'stretch', height: '360px' } }, [
            h(
              'div',
              { style: { display: 'flex', background: 'var(--bg-app)', borderRadius: 'var(--radius-lg)', padding: '10px' } },
              [
                h(SessionList, {
                  sessions: sessionFixture,
                  activeId: sessionActive.value,
                  collapsed: sessionCollapsed.value,
                  loading: false,
                  'onUpdate:collapsed': (v) => {
                    sessionCollapsed.value = v
                  },
                  onNewChat: () => {
                    sessionEvent.value = 'new-chat'
                  },
                  onSwitch: (s) => {
                    sessionActive.value = s.id
                    sessionEvent.value = `switch: ${s.id}`
                  },
                  onRename: (s) => {
                    sessionEvent.value = `rename: ${s.id}`
                  },
                  onDelete: (s) => {
                    sessionEvent.value = `delete: ${s.id}`
                  },
                }),
              ],
            ),
            h(
              'div',
              { style: { display: 'flex', background: 'var(--bg-app)', borderRadius: 'var(--radius-lg)', padding: '10px' } },
              [
                h(SessionList, {
                  sessions: sessionFixture,
                  activeId: sessionActive.value,
                  collapsed: sessionCollapsedRail.value,
                  loading: false,
                  'onUpdate:collapsed': (v) => {
                    sessionCollapsedRail.value = v
                  },
                  onNewChat: () => {
                    sessionEvent.value = 'new-chat'
                  },
                  onSwitch: () => {},
                  onRename: () => {},
                  onDelete: () => {},
                }),
              ],
            ),
          ]),
          h(
            'div',
            { style: { fontSize: '11px', color: 'var(--text-3)', marginTop: '6px' } },
            `事件：${sessionEvent.value || '（未触发）'}`,
          ),
        ]),
      )
  },
}

createApp(App).use(ElementPlus, { locale: zhCn }).mount('#preview')
