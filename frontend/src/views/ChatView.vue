wo<template>
  <div class="chat-page">
    <!-- 左侧：会话列表（可折叠） -->
    <aside :class="['session-panel', { collapsed: sessionCollapsed }]">
      <div class="session-head">
        <el-button
          v-if="!sessionCollapsed"
          type="primary"
          class="new-chat-btn"
          :disabled="loading"
          @click="startNewChat"
        >
          <el-icon><Plus /></el-icon>
          新建对话
        </el-button>
        <el-tooltip v-else content="新建对话" placement="right">
          <el-button circle type="primary" plain :disabled="loading" @click="startNewChat">
            <el-icon><Plus /></el-icon>
          </el-button>
        </el-tooltip>
      </div>

      <template v-if="!sessionCollapsed">
        <div v-if="!sessions.length" class="session-empty">暂无历史会话</div>
        <div class="session-list">
          <div
            v-for="session in sessions"
            :key="session.id"
            :class="['session-item', { active: session.id === activeId }]"
            @click="switchSession(session)"
          >
            <div class="session-info">
              <span class="session-title">{{ session.title }}</span>
              <span v-if="session.message_count" class="session-count">
                {{ session.message_count }}
              </span>
            </div>
            <div class="session-actions" @click.stop>
              <el-icon @click="handleRename(session)"><EditPen /></el-icon>
              <el-icon @click="handleDelete(session)"><Delete /></el-icon>
            </div>
          </div>
        </div>
      </template>

      <div class="session-foot">
        <el-tooltip
          :content="sessionCollapsed ? '展开会话列表' : '收起会话列表'"
          placement="right"
        >
          <div class="panel-toggle" @click="sessionCollapsed = !sessionCollapsed">
            <el-icon :size="17">
              <Expand v-if="sessionCollapsed" />
              <Fold v-else />
            </el-icon>
          </div>
        </el-tooltip>
      </div>
    </aside>

    <!-- 右侧：对话主区 -->
    <main class="chat-main">
      <header class="chat-header">
        <div class="header-left">
          <el-tooltip content="展开/收起会话列表" placement="bottom">
            <div class="header-toggle" @click="sessionCollapsed = !sessionCollapsed">
              <el-icon :size="16">
                <Expand v-if="sessionCollapsed" />
                <Fold v-else />
              </el-icon>
            </div>
          </el-tooltip>
          <span class="chat-title">{{ currentTitle }}</span>
        </div>
        <el-button text type="primary" @click="openTemplateManager">
          <el-icon><Operation /></el-icon>
          提示词模板
        </el-button>
        <el-button text type="primary" class="memory-btn" @click="openMemories">
          <el-icon><Memo /></el-icon>
          记忆
        </el-button>
        <el-button text type="primary" class="memory-btn" @click="$router.push('/runs')">
          <el-icon><DataAnalysis /></el-icon>
          运行记录
        </el-button>
      </header>

      <div
        ref="messagesRef"
        class="messages"
        @click="handleMessageClick"
        @scroll="onMessagesScroll"
      >
        <!-- 欢迎页 -->
        <div v-if="!messages.length && !loading" class="welcome">
          <div class="welcome-mark">
            <el-icon :size="26"><MagicStick /></el-icon>
          </div>
          <h2 class="welcome-title">有什么可以帮你？</h2>
          <p class="welcome-sub">支持知识库问答、联网搜索与多轮对话记忆</p>
          <div class="suggestions">
            <div
              v-for="s in suggestions"
              :key="s.text"
              class="suggestion"
              @click="ask(s)"
            >
              <span class="suggestion-icon">
                <el-icon :size="15"><component :is="s.icon" /></el-icon>
              </span>
              {{ s.text }}
            </div>
          </div>
        </div>

        <!-- 消息列表 -->
        <div
          v-for="(msg, index) in messages"
          :key="index"
          :class="['msg', msg.role]"
          :data-msg-index="index"
        >
          <div :class="['avatar', msg.role]">
            <el-icon :size="17">
              <User v-if="msg.role === 'user'" />
              <MagicStick v-else />
            </el-icon>
          </div>

          <div class="msg-body">
            <div v-if="msg.role === 'user'" class="user-bubble">
              <div v-if="msg.images && msg.images.length" class="user-images">
                <img
                  v-for="(img, i) in msg.images"
                  :key="i"
                  :src="img"
                  class="user-img"
                  alt="上传图片"
                />
              </div>
              <span v-if="msg.content">{{ msg.content }}</span>
            </div>

            <template v-else>
              <div
                v-if="(msg.plan && msg.plan.length) || (msg.todos && msg.todos.length)"
                class="plan-box"
              >
                <div class="plan-head">
                  <el-icon :size="14"><List /></el-icon>
                  {{ msg.plan && msg.plan.length ? '执行计划' : '任务清单' }}
                  <span v-if="msg.planDone != null" class="plan-progress">
                    {{ msg.planDone }}/{{
                      msg.planTotal != null ? msg.planTotal : msg.plan.length
                    }}
                  </span>
                </div>
                <div v-if="msg.todos && msg.todos.length" class="plan-todos">
                  <div
                    v-for="(t, i) in msg.todos"
                    :key="t.id"
                    :class="['plan-todo', { done: todoDone(msg, t, i) }]"
                  >
                    <el-icon v-if="todoDone(msg, t, i)" :size="13" class="plan-todo-check">
                      <CircleCheck />
                    </el-icon>
                    <span v-else class="plan-todo-box"></span>
                    <span class="plan-todo-text">{{ t.text }}</span>
                  </div>
                </div>
                <ol v-else class="plan-steps">
                  <li
                    v-for="(step, i) in msg.plan"
                    :key="i"
                    :class="[
                      'plan-step',
                      i < msg.planDone ? 'done' : '',
                      i === msg.planDone && currentTool ? 'running' : '',
                    ]"
                  >
                    <span class="plan-step-icon">
                      <el-icon v-if="i < msg.planDone" :size="12"><CircleCheck /></el-icon>
                      <span v-else-if="i === msg.planDone && currentTool" class="plan-step-dot"></span>
                      <span v-else class="plan-step-num">{{ i + 1 }}</span>
                    </span>
                    <span class="plan-step-text">{{ step }}</span>
                  </li>
                </ol>
              </div>
              <div v-if="msg.reasoning" class="reasoning-box">
                <div
                  class="reasoning-head"
                  @click="msg._reasoningOpen = !msg._reasoningOpen"
                >
                  <el-icon :size="13"><MagicStick /></el-icon>
                  <span>已深度思考</span>
                  <el-icon :size="12" class="arrow" :class="{ open: msg._reasoningOpen }">
                    <ArrowDown />
                  </el-icon>
                </div>
                <div v-if="msg._reasoningOpen" class="reasoning-body">
                  {{ msg.reasoning }}
                </div>
              </div>
              <div v-if="msg.content" class="assistant-content">
                <MarkdownContent :content="msg.content" :plain="!!msg._streaming" />
                <div class="msg-actions">
                  <el-tooltip
                    v-if="index === messages.length - 1"
                    content="重新生成"
                    placement="top"
                  >
                    <el-icon class="copy-btn" @click="regenerateLast">
                      <RefreshRight />
                    </el-icon>
                  </el-tooltip>
                  <el-tooltip content="复制回答" placement="top">
                    <el-icon class="copy-btn" @click="copyText(msg.content)">
                      <CopyDocument />
                    </el-icon>
                  </el-tooltip>
                </div>
              </div>
              <div v-else-if="loading" class="assistant-content">
                <span v-if="currentTool" class="status-chip">
                  <span class="status-spinner"></span>
                  {{ currentTool }}
                </span>
                <span v-else class="dots"><i /><i /><i /></span>
              </div>
              <div v-if="isFailed(msg.content)" class="failed-bar">
                <el-icon :size="14" class="failed-icon"><WarningFilled /></el-icon>
                <span>生成失败</span>
                <el-button size="small" type="primary" plain @click="regenerateLast">
                  重新生成
                </el-button>
              </div>
            </template>

            <!-- 工具调用轨迹 -->
            <div v-if="msg.tool_trace && msg.tool_trace.length" class="tool-trace">
              <div v-for="(t, i) in msg.tool_trace" :key="i" class="tool-chip">
                <el-icon :size="13"><component :is="toolIcon(t.name)" /></el-icon>
                <span class="tool-name">{{ toolName(t.name) }}</span>
                <span class="tool-summary">
                  {{ t.summary || (t.arguments && t.arguments.query) || '执行中…' }}
                </span>
              </div>
            </div>

            <!-- 人工确认（HITL）：等待用户批准/拒绝敏感操作 -->
            <div v-if="msg.permissions && msg.permissions.length" class="permission-box">
              <div
                v-for="p in msg.permissions"
                :key="p.id"
                :data-perm-id="p.id"
              >
                <div v-if="p.status === 'pending'" class="permission-card pending">
                  <div class="permission-head">
                    <el-icon :size="14"><Lock /></el-icon>
                    <span class="permission-title">等待人工确认</span>
                    <span class="permission-tool">{{ toolName(p.tool) }}</span>
                  </div>
                  <div class="permission-body">
                    <div class="permission-summary">{{ p.summary }}</div>
                    <div v-if="p.tool === 'edit_file' && p.args?.old_string != null" class="perm-diff">
                      <div class="perm-diff-head">变更预览（红删绿增）</div>
                      <div class="perm-diff-line del">
                        <span class="perm-diff-sign">−</span>
                        <span class="perm-diff-text">{{ p.args.old_string }}</span>
                      </div>
                      <div class="perm-diff-line add">
                        <span class="perm-diff-sign">+</span>
                        <span class="perm-diff-text">{{ p.args.new_string }}</span>
                      </div>
                    </div>
                    <pre v-else-if="p.args_text" class="permission-args">{{ p.args_text }}</pre>
                  </div>
                  <div class="permission-actions">
                    <el-input
                      v-model="p.reason"
                      placeholder="备注（可选）"
                      size="small"
                      class="permission-reason"
                      @keyup.enter="decidePermission(p, p._focusIndex ?? 0)"
                    />
                    <div class="permission-opts" tabindex="0">
                      <button
                        type="button"
                        class="perm-opt primary"
                        :class="{ active: p._focusIndex === 0 }"
                        @click="decidePermission(p, 0)"
                        @mouseenter="p._focusIndex = 0"
                      >
                        <span class="perm-num">1</span>批准
                      </button>
                      <button
                        type="button"
                        class="perm-opt danger"
                        :class="{ active: p._focusIndex === 1 }"
                        @click="decidePermission(p, 1)"
                        @mouseenter="p._focusIndex = 1"
                      >
                        <span class="perm-num">2</span>拒绝
                      </button>
                      <button
                        v-if="p.rememberable"
                        type="button"
                        class="perm-opt"
                        :class="{ active: p._focusIndex === 2 }"
                        @click="decidePermission(p, 2)"
                        @mouseenter="p._focusIndex = 2"
                      >
                        <span class="perm-num">3</span>批准并记住
                      </button>
                    </div>
                    <span class="perm-key-hint">↑↓ 切换 · Enter 确认 · Esc 返回输入</span>
                  </div>
                </div>
                <!-- 处理后折叠为一行状态条，不占空间 -->
                <div v-else :class="['permission-resolved-chip', p.status]">
                  <el-icon :size="13">
                    <CircleCheck v-if="p.status === 'approved'" />
                    <Close v-else />
                  </el-icon>
                  <span class="prc-label">{{ p.status === 'approved' ? '已批准' : '已拒绝' }}</span>
                  <span class="prc-tool">{{ toolName(p.tool) }}</span>
                  <span class="prc-summary">{{ p.summary }}</span>
                  <span v-if="p.reason" class="prc-reason">（{{ p.reason }}）</span>
                </div>
              </div>
            </div>

            <!-- 引用来源 -->
            <div v-if="msg.sources && msg.sources.length" class="sources">
              <div class="sources-head" @click="msg._sourcesOpen = !msg._sourcesOpen">
                <el-icon :size="13"><Link /></el-icon>
                引用来源（{{ msg.sources.length }}）
                <el-icon :size="12" class="arrow" :class="{ open: msg._sourcesOpen }">
                  <ArrowDown />
                </el-icon>
              </div>
              <div v-if="msg._sourcesOpen" class="sources-body">
                <div v-for="(src, i) in msg.sources" :key="i" class="source-item">
                  <template v-if="src.type === 'web'">
                    <div class="source-meta">
                      <span class="source-no">{{ src.index || i + 1 }}</span>
                      <span class="source-tag web">联网</span>
                      <span class="source-title">{{ src.title || src.url }}</span>
                    </div>
                    <div class="source-content">{{ src.content }}</div>
                    <a
                      v-if="src.url"
                      :href="src.url"
                      target="_blank"
                      rel="noopener noreferrer"
                      class="source-link"
                      @click.stop
                    >
                      打开链接 ↗
                    </a>
                  </template>
                  <template v-else>
                    <div class="source-meta">
                      <span class="source-no">{{ src.index || i + 1 }}</span>
                      <span class="source-tag">知识库</span>
                      <span v-if="src.score" class="source-score">相关度 {{ src.score }}</span>
                      <span v-if="src.source" class="source-name">{{ src.source }}</span>
                      <span v-if="src.page != null">｜第 {{ src.page }} 页</span>
                    </div>
                    <div class="source-content">{{ src.content }}</div>
                  </template>
                </div>
              </div>
            </div>
          </div>
        </div>
      </div>

      <!-- 输入区 -->
      <div class="input-area">
        <div class="toolbar">
          <div class="toolbar-group">
            <el-switch
              v-model="toolMode"
              active-value="auto"
              inactive-value="manual"
              active-text="自动"
              inactive-text="自动"
              class="auto-switch"
              :disabled="loading"
            />
            <el-switch
              v-model="useWebSearch"
              active-text="联网搜索"
              inactive-text="联网搜索"
              :disabled="loading || toolMode === 'auto'"
            />
            <el-switch
              v-model="useKnowledgeBase"
              active-text="知识库"
              inactive-text="知识库"
              :disabled="loading || toolMode === 'auto'"
            />
          </div>
          <el-select
            v-model="templateId"
            class="template-select"
            placeholder="提示词模板"
            :disabled="loading"
          >
            <el-option label="默认模板" :value="0" />
            <el-option
              v-for="t in templates"
              :key="t.id"
              :label="t.name"
              :value="t.id"
            />
          </el-select>
        </div>

        <!-- 已附加图片预览 -->
        <div v-if="attachedImages.length" class="image-preview-row">
          <div
            v-for="img in attachedImages"
            :key="img.id"
            class="image-preview-item"
          >
            <img :src="img.dataUrl" class="image-preview-img" alt="待发送图片" />
            <el-icon class="image-preview-remove" @click="removeImage(img.id)">
              <Close />
            </el-icon>
          </div>
        </div>

        <div class="input-box">
          <el-tooltip content="上传图片（支持 Ctrl+V 粘贴）" placement="top">
            <div class="attach-btn" @click="imageInputRef.click()">
              <el-icon :size="18"><Picture /></el-icon>
            </div>
          </el-tooltip>
          <el-input
            ref="questionInput"
            v-model="question"
            type="textarea"
            :rows="2"
            resize="none"
            placeholder="输入你的问题，Enter 发送，Shift+Enter 换行 · 支持粘贴/上传图片"
            @keydown.enter.exact.prevent="send"
            @paste="onPaste"
          />
          <input
            ref="imageInputRef"
            type="file"
            accept="image/*"
            multiple
            class="hidden-file-input"
            @change="onImageFiles"
          />
          <el-button
            v-if="loading"
            circle
            class="send-btn stop-btn"
            @click="stop"
          >
            <el-icon :size="17"><VideoPause /></el-icon>
          </el-button>
          <el-button
            v-else
            type="primary"
            circle
            class="send-btn"
            :disabled="!question.trim() && !attachedImages.length"
            @click="send"
          >
            <el-icon :size="17"><Promotion /></el-icon>
          </el-button>
        </div>
        <div class="input-foot">
          Enter 发送 · Shift+Enter 换行 · 支持 Ctrl+V 粘贴图片
        </div>
      </div>
    </main>

    <!-- 模板管理对话框 -->
    <el-dialog v-model="templateManagerVisible" title="提示词模板管理" width="760px">
      <div class="tpl-list">
        <div v-for="t in templates" :key="t.id" class="tpl-item">
          <div class="tpl-info">
            <div class="tpl-name-row">
              <span class="tpl-name">{{ t.name }}</span>
              <el-tag v-if="t.is_system" size="small" type="info">内置</el-tag>
              <el-tag v-else size="small" type="success">自定义</el-tag>
            </div>
            <div class="tpl-desc">{{ t.description || '暂无描述' }}</div>
          </div>
          <div class="tpl-actions">
            <el-button size="small" @click="openTemplateEdit(t)">编辑</el-button>
            <el-button
              v-if="!t.is_system"
              size="small"
              type="danger"
              @click="handleDeleteTemplate(t)"
            >
              删除
            </el-button>
          </div>
        </div>
      </div>
      <el-button type="primary" plain class="tpl-create" @click="openTemplateEdit(null)">
        新建模板
      </el-button>

      <el-dialog
        v-model="templateEditVisible"
        :title="templateForm.id ? '编辑模板' : '新建模板'"
        width="640px"
        append-to-body
      >
        <el-form :model="templateForm" label-width="60px">
          <el-form-item label="名称">
            <el-input v-model="templateForm.name" placeholder="模板名称" />
          </el-form-item>
          <el-form-item label="分类">
            <el-select v-model="templateForm.category">
              <el-option label="通用" value="general" />
              <el-option label="知识库" value="knowledge" />
              <el-option label="编程" value="coding" />
              <el-option label="写作" value="writing" />
              <el-option label="翻译" value="translate" />
            </el-select>
          </el-form-item>
          <el-form-item label="描述">
            <el-input v-model="templateForm.description" placeholder="一句话说明用途" />
          </el-form-item>
          <el-form-item label="内容">
            <el-input
              v-model="templateForm.content"
              type="textarea"
              :rows="12"
              placeholder="提示词内容"
            />
          </el-form-item>
        </el-form>
        <template #footer>
          <el-button @click="templateEditVisible = false">取消</el-button>
          <el-button type="primary" @click="saveTemplate">保存</el-button>
        </template>
      </el-dialog>
    </el-dialog>

    <!-- 长期记忆对话框 -->
    <el-dialog v-model="memoriesVisible" title="长期记忆" width="680px">
      <div class="memory-head">
        <div class="memory-hint">
          助手会从对话中自动提取你的偏好、背景与重要信息，提问时按相关性召回；
          系统会定期整合整理（合并重复、覆盖过时信息）。
        </div>
        <div class="memory-actions">
          <el-button size="small" @click="openMemoryEdit(null)">新建记忆</el-button>
          <el-button
            size="small"
            type="warning"
            plain
            :loading="consolidating"
            @click="handleConsolidate"
          >
            整理记忆
          </el-button>
        </div>
      </div>
      <div v-if="!memories.length" class="memory-empty">还没有提取到长期记忆</div>
      <div class="memory-list">
        <div v-for="m in memories" :key="m.id" class="memory-item">
          <div class="memory-main">
            <el-tag size="small" :type="categoryTagType(m.category)" effect="light">
              {{ categoryLabel(m.category) }}
            </el-tag>
            <div class="memory-content">{{ m.content }}</div>
          </div>
          <div class="memory-ops">
            <el-button size="small" link @click="openMemoryEdit(m)">编辑</el-button>
            <el-button size="small" type="danger" link @click="handleDeleteMemory(m)">
              删除
            </el-button>
          </div>
        </div>
      </div>

      <!-- 记忆编辑/新增子对话框 -->
      <el-dialog
        v-model="memoryEditVisible"
        :title="memoryForm.id ? '编辑记忆' : '新建记忆'"
        width="560px"
        append-to-body
      >
        <el-form :model="memoryForm" label-width="60px">
          <el-form-item label="分类">
            <el-select v-model="memoryForm.category">
              <el-option v-for="(label, key) in categoryMap" :key="key" :label="label" :value="key" />
            </el-select>
          </el-form-item>
          <el-form-item label="内容">
            <el-input
              v-model="memoryForm.content"
              type="textarea"
              :rows="4"
              placeholder="一条客观、可长期复用的事实"
            />
          </el-form-item>
        </el-form>
        <template #footer>
          <el-button @click="memoryEditVisible = false">取消</el-button>
          <el-button type="primary" @click="saveMemory">保存</el-button>
        </template>
      </el-dialog>
    </el-dialog>
  </div>
</template>

<script setup>
import { nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  ArrowDown,
  CircleCheck,
  Close,
  Collection,
  CopyDocument,
  DataAnalysis,
  Delete,
  EditPen,
  Expand,
  Fold,
  Lock,
  Link,
  List,
  MagicStick,
  Memo,
  Operation,
  Picture,
  Plus,
  Position,
  Promotion,
  RefreshRight,
  User,
  VideoPause,
  WarningFilled,
} from '@element-plus/icons-vue'
import MarkdownContent from '../components/MarkdownContent.vue'

// keep-alive 按组件名匹配，显式声明
defineOptions({ name: 'ChatView' })
import {
  addMemory,
  consolidateMemories,
  createTemplate,
  deleteConversation,
  deleteMemory,
  deleteTemplate,
  getConversationMessages,
  getSuggestions,
  getTodos,
  listConversations,
  listMemories,
  listTemplates,
  renameConversation,
  resolvePermission,
  streamAgentChat,
  updateMemory,
  updateTemplate,
} from '../api'

const question = ref('')
const loading = ref(false)
const messages = ref([])
const messagesRef = ref(null)
const questionInput = ref(null)
// 是否跟随最新内容自动滚动：用户上滑查看时暂停跟随，滚回底部后恢复
const scrollFollow = ref(true)

// 会话
const sessions = ref([])
const activeId = ref(null)
const currentTitle = ref('新对话')
const sessionCollapsed = ref(false)

// 输入区选项
const toolMode = ref('auto') // auto=自动（两者都启用）/ manual=手动
const useWebSearch = ref(false)
const useKnowledgeBase = ref(false)
const templateId = ref(0)
const templates = ref([])
const currentTool = ref('')
let abortController = null
let restoringTemplate = false // 切换会话时回填模板，不触发持久化写入

// 打字机效果：后端 token 先进缓冲区，按固定节奏刷入消息，
// 避免生成太快时"整段一下子跳出来"；积压多时自适应加速，不会拖慢
const pendingText = ref('')
let typewriterTimer = null
let streamMsg = null

function flushTypewriter() {
  if (typewriterTimer) {
    clearInterval(typewriterTimer)
    typewriterTimer = null
  }
  if (streamMsg && pendingText.value) {
    streamMsg.content += pendingText.value
    pendingText.value = ''
    scrollToBottom()
  }
}

function startTypewriter() {
  if (typewriterTimer || !streamMsg) return
  let scrollCounter = 0
  typewriterTimer = setInterval(() => {
    if (!pendingText.value) {
      flushTypewriter()
      return
    }
    // 24ms 一帧，单帧最多 10 字符：
    // - 稳态约 35~70 字符/秒，有清晰打字机观感；
    // - 大段一次性到达时以最高约 400 字符/秒快速追赶，观感流畅又不拖沓
    const n = Math.max(1, Math.min(10, Math.round(pendingText.value.length / 10)))
    streamMsg.content += pendingText.value.slice(0, n)
    pendingText.value = pendingText.value.slice(n)
    // 滚动节流：隔帧滚动一次，避免每 24ms 触发滚动导致底部抖动闪烁
    scrollCounter = (scrollCounter + 1) % 2
    if (scrollCounter === 0) scrollToBottom()
  }, 24)
}

// 附加图片（粘贴 / 上传，随消息发送给后端做视觉识别）
let imageSeq = 0
const attachedImages = ref([])
const imageInputRef = ref(null)

// 偏好持久化：开关/模板/侧栏折叠状态存 localStorage，组件重建后恢复
const PREFS_KEY = 'rag_chat_prefs'
const PANEL_KEY = 'rag_session_collapsed'

function loadPrefs() {
  try {
    const raw = localStorage.getItem(PREFS_KEY)
    if (raw) {
      const prefs = JSON.parse(raw)
      if (prefs.toolMode === 'auto' || prefs.toolMode === 'manual') {
        toolMode.value = prefs.toolMode
      }
      if (toolMode.value === 'auto') {
        // 自动模式下联网/知识库视为都开启
        useWebSearch.value = true
        useKnowledgeBase.value = true
      } else {
        useWebSearch.value = !!prefs.useWebSearch
        useKnowledgeBase.value = !!prefs.useKnowledgeBase
      }
      if (typeof prefs.templateId === 'number') {
        templateId.value = prefs.templateId
      }
    }
    sessionCollapsed.value = localStorage.getItem(PANEL_KEY) === '1'
  } catch {
    // 浏览器禁用 localStorage 时忽略，不影响使用
  }
}

function savePrefs() {
  try {
    localStorage.setItem(
      PREFS_KEY,
      JSON.stringify({
        toolMode: toolMode.value,
        useWebSearch: useWebSearch.value,
        useKnowledgeBase: useKnowledgeBase.value,
        templateId: templateId.value,
      }),
    )
    localStorage.setItem(PANEL_KEY, sessionCollapsed.value ? '1' : '0')
  } catch {
    // ignore
  }
}

watch(toolMode, (value) => {
  // 打开自动 = 联网/知识库都开启；关闭自动 = 都不开启（可再单独勾选）
  if (value === 'auto') {
    useWebSearch.value = true
    useKnowledgeBase.value = true
  } else {
    useWebSearch.value = false
    useKnowledgeBase.value = false
  }
})

// 模板跟随会话：切换模板时立即写入当前会话，下次切回来自动恢复
watch(templateId, (value) => {
  if (restoringTemplate || loading.value || !activeId.value) return
  const payload = {
    title: currentTitle.value || '新对话',
    template_id: value || 0,
  }
  renameConversation(activeId.value, payload).catch(() => {
    // 保存失败不打断对话，下次发送消息时后端会再同步
  })
})

watch(
  [toolMode, useWebSearch, useKnowledgeBase, templateId, sessionCollapsed],
  savePrefs,
)

// 模板管理
const templateManagerVisible = ref(false)
const templateEditVisible = ref(false)
const templateForm = ref({
  id: null,
  name: '',
  category: 'general',
  description: '',
  content: '',
})

// 长期记忆
const memoriesVisible = ref(false)
const memories = ref([])
const memoryEditVisible = ref(false)
const consolidating = ref(false)
const memoryForm = ref({ id: null, content: '', category: 'profile' })

const categoryMap = {
  profile: '用户画像',
  preference: '喜好偏好',
  project: '工作项目',
  decision: '重要决定',
  lesson: '教训与应对',
  other: '其他',
}

function categoryLabel(key) {
  return categoryMap[key] || key
}

function categoryTagType(key) {
  return (
    {
      profile: 'primary',
      preference: 'success',
      project: 'warning',
      decision: 'danger',
      lesson: 'info',
      other: 'info',
    }[key] || 'info'
  )
}

// 欢迎页建议问题：每天由后端生成（知识库 / 热点新闻 / 通用）
const suggestionIcons = { kb: Collection, news: Position, general: MagicStick }
const suggestions = ref([])

async function loadSuggestions() {
  try {
    const items = await getSuggestions()
    suggestions.value = items.map((item) => ({
      icon: suggestionIcons[item.type] || MagicStick,
      text: item.text,
    }))
  } catch {
    suggestions.value = [
      { icon: Collection, text: '《示例书》的主要观点是什么？' },
      { icon: MagicStick, text: '帮我写一封简洁的工作周报' },
      { icon: Position, text: '今天有什么值得关注的新闻？' },
    ]
  }
}

function toolName(name) {
  return {
    knowledge_base_search: '知识库检索',
    web_search: '联网搜索',
    image_to_text: '图片识别',
    read_file: '读取文件',
    list_dir: '查看目录',
    grep_search: '搜索代码',
    write_file: '写入文件',
    edit_file: '编辑文件',
    delete_file: '删除文件',
    bash: '执行命令',
    command_tool: '执行命令',
    file_tool: '文件操作',
    todo_update: '任务清单',
    subagent: '子代理',
    add_document: '添加知识库文档',
  }[name] || name
}

function toolIcon(name) {
  if (name === 'web_search') return Position
  if (name === 'image_to_text') return Picture
  return Collection
}

function onMessagesScroll() {
  const el = messagesRef.value
  if (!el) return
  const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 100
  scrollFollow.value = atBottom
}

let scrollRaf = null
function scrollToBottom(force = false) {
  // 把 24ms 打字机与各事件触发的滚动合并到每帧最多一次，
  // 避免高频改 scrollTop 造成底部抖动/文字跳动
  if (scrollRaf) return
  scrollRaf = requestAnimationFrame(() => {
    scrollRaf = null
    if (messagesRef.value && (force || scrollFollow.value)) {
      messagesRef.value.scrollTop = messagesRef.value.scrollHeight
    }
  })
}

async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text)
    ElMessage.success('已复制')
  } catch {
    ElMessage.warning('复制失败，请手动选择复制')
  }
}

/** 把审批参数格式化成多行展示文本（预览大段内容，避免刷屏） */
function formatArgs(args) {
  if (!args || !Object.keys(args).length) return ''
  const lines = []
  for (const [k, v] of Object.entries(args)) {
    if (v === '' || v == null) continue
    const text = typeof v === 'string' ? v : JSON.stringify(v)
    const preview = text.length > 160 ? `${text.slice(0, 160)}…（共 ${text.length} 字符）` : text
    lines.push(`${k}: ${preview}`)
  }
  return lines.join('\n')
}

/** 用户批准/拒绝敏感操作（选项卡：0=批准，1=拒绝，2=批准并永久记住） */
async function decidePermission(p, optionIndex) {
  if (!p || p.status !== 'pending' || p._deciding) return
  p._deciding = true
  const prevReason = p.reason
  const approve = optionIndex !== 1
  const remember = optionIndex === 2
  try {
    await resolvePermission(p.id, approve, prevReason || '', remember)
    // 后端会通过 permission_resolved 事件同步状态，这里先乐观更新
    p.status = approve ? 'approved' : 'denied'
    if (remember && approve) p.reason = (p.reason ? `${p.reason}；` : '') + '已记住，下次不再询问'
  } catch (error) {
    ElMessage.error(error.response?.data?.detail || '提交决定失败，请重试')
  } finally {
    p._deciding = false
  }
}

/** 待审批的请求：当前消息里第一个 pending 卡片 */
function pendingPermission() {
  return (streamMsg?.permissions || []).find((p) => p.status === 'pending') || null
}

/** 跳到审批选项卡：焦点移入卡片，方便用 ↑↓/数字/Enter 操作 */
function focusPermissionCard() {
  const p = pendingPermission()
  if (!p) return
  const active = document.activeElement
  // 用户正在输入问题时不强抢焦点（审批卡片仍在，点击即可进入选项卡）
  const typing = active && active.tagName === 'TEXTAREA' && active.value?.length > 0
  if (typing) return
  p._focusIndex = 0
  nextTick(() => {
    const card = document.querySelector(`.permission-card[data-perm-id="${p.id}"] .permission-opts`)
    card?.focus()
  })
}

/** 全局键盘：焦点在审批选项卡内时，数字/方向键/Enter/Esc 操作审批 */
function onPermissionKeydown(event) {
  const p = pendingPermission()
  if (!p) return
  // 在备注输入框/正文输入框里打字时，数字和 Enter 保持文本语义
  const tag = event.target?.tagName
  if (tag === 'INPUT' || tag === 'TEXTAREA') return
  const inCard = event.target.closest?.('.permission-card')
  if (!inCard) return
  const count = p.rememberable ? 3 : 2
  if (event.key >= '1' && event.key <= String(count)) {
    event.preventDefault()
    decidePermission(p, Number(event.key) - 1)
  } else if (event.key === 'ArrowDown' || event.key === 'ArrowRight') {
    event.preventDefault()
    p._focusIndex = (p._focusIndex + 1) % count
  } else if (event.key === 'ArrowUp' || event.key === 'ArrowLeft') {
    event.preventDefault()
    p._focusIndex = (p._focusIndex + count - 1) % count
  } else if (event.key === 'Enter') {
    event.preventDefault()
    decidePermission(p, p._focusIndex ?? 0)
  } else if (event.key === 'Escape') {
    event.preventDefault()
    questionInput.value?.focus()
  }
}

/** 任务项是否已完成：仅以模型/系统按进度标记的 done 为准，用户不可手改 */
function todoDone(msg, t) {
  return !!t.done
}

function isFailed(content) {
  return content && content.startsWith('请求失败')
}

// ---------------- 会话 ----------------

async function loadSessions() {
  try {
    sessions.value = await listConversations()
  } catch (error) {
    ElMessage.error(error.response?.data?.detail || '加载会话失败')
  }
}

function startNewChat() {
  if (loading.value) return
  activeId.value = null
  currentTitle.value = '新对话'
  messages.value = []
  question.value = ''
}

async function switchSession(session) {
  if (loading.value || session.id === activeId.value) return
  activeId.value = session.id
  currentTitle.value = session.title
  restoringTemplate = true
  templateId.value = session.template_id || 0
  restoringTemplate = false
  try {
    const history = await getConversationMessages(session.id)
    messages.value = history.map((m) => ({
      role: m.role,
      content: m.content,
      tool_trace: m.tool_trace || [],
      sources: m.sources || [],
      permissions: [],
      todos: [],
      plan: [],
      planTotal: 0,
      _sourcesOpen: false,
      _streaming: false,
    }))
    // 恢复 TodoWrite 任务清单，挂到最近一条助手消息（若有）
    try {
      const todoRes = await getTodos(session.id)
      const lastAssistant = [...messages.value].reverse().find((m) => m.role === 'assistant')
      if (lastAssistant && todoRes.todos?.length) lastAssistant.todos = todoRes.todos
    } catch {
      // 任务清单读取失败不阻塞会话加载
    }
    scrollToBottom(true)
  } catch (error) {
    ElMessage.error(error.response?.data?.detail || '加载历史消息失败')
  }
}

async function handleRename(session) {
  try {
    const { value } = await ElMessageBox.prompt(
      '输入新的会话标题',
      '重命名',
      { inputValue: session.title, confirmButtonText: '确定', cancelButtonText: '取消' },
    )
    const updated = await renameConversation(session.id, value.trim())
    session.title = updated.title
    if (session.id === activeId.value) {
      currentTitle.value = updated.title
    }
  } catch (error) {
    if (error !== 'cancel' && error !== 'close') {
      ElMessage.error(error.response?.data?.detail || '重命名失败')
    }
  }
}

async function handleDelete(session) {
  try {
    await ElMessageBox.confirm(
      `确定删除「${session.title}」吗？其全部对话记录将被删除。`,
      '提示',
      { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  try {
    await deleteConversation(session.id)
    if (session.id === activeId.value) {
      startNewChat()
    }
    await loadSessions()
    ElMessage.success('会话已删除')
  } catch (error) {
    ElMessage.error(error.response?.data?.detail || '删除失败')
  }
}

function upsertSession(data) {
  const index = sessions.value.findIndex((s) => s.id === data.conversation_id)
  const item = { id: data.conversation_id, title: data.title || '新对话', message_count: 0 }
  if (index >= 0) {
    if (data.title) sessions.value[index].title = data.title
  } else if (data.conversation_id) {
    sessions.value.unshift(item)
  }
}

// ---------------- 引用溯源 ----------------

function handleMessageClick(event) {
  const citeEl = event.target.closest('[data-cite]')
  if (!citeEl) return
  const msgEl = event.target.closest('.msg')
  if (!msgEl) return
  const index = Number(msgEl.dataset.msgIndex)
  const msg = messages.value[index]
  if (!msg) return
  const no = Number(citeEl.dataset.cite)
  if (!msg.sources || no < 1 || no > msg.sources.length) return

  // 展开来源面板并高亮对应条目
  msg._sourcesOpen = true
  nextTick(() => {
    const items = msgEl.querySelectorAll('.source-item')
    const target = items[no - 1]
    if (!target) return
    target.scrollIntoView({ behavior: 'smooth', block: 'center' })
    target.classList.add('flash')
    setTimeout(() => target.classList.remove('flash'), 1600)
  })
}

// ---------------- 长期记忆 ----------------

async function openMemories() {
  memoriesVisible.value = true
  await loadMemories()
}

async function loadMemories() {
  try {
    memories.value = await listMemories()
  } catch (error) {
    ElMessage.error(error.response?.data?.detail || '加载记忆失败')
  }
}

function openMemoryEdit(memory) {
  memoryForm.value = memory
    ? { id: memory.id, content: memory.content, category: memory.category }
    : { id: null, content: '', category: 'profile' }
  memoryEditVisible.value = true
}

async function saveMemory() {
  const form = memoryForm.value
  if (!form.content.trim()) {
    ElMessage.warning('内容不能为空')
    return
  }
  try {
    if (form.id) {
      await updateMemory(form.id, {
        content: form.content.trim(),
        category: form.category,
      })
    } else {
      await addMemory({ content: form.content.trim(), category: form.category })
    }
    memoryEditVisible.value = false
    await loadMemories()
    ElMessage.success(form.id ? '记忆已更新' : '记忆已添加')
  } catch (error) {
    ElMessage.error(error.response?.data?.detail || '保存失败')
  }
}

async function handleConsolidate() {
  consolidating.value = true
  try {
    const result = await consolidateMemories()
    await loadMemories()
    ElMessage.success(
      `整理完成：合并 ${result.merged || 0} 条，归档 ${result.archived || 0} 条`,
    )
  } catch (error) {
    ElMessage.error(error.response?.data?.detail || '整理失败')
  } finally {
    consolidating.value = false
  }
}

async function handleDeleteMemory(memory) {
  try {
    await ElMessageBox.confirm('确定删除这条记忆吗？', '提示', {
      type: 'warning',
      confirmButtonText: '删除',
      cancelButtonText: '取消',
    })
  } catch {
    return
  }
  try {
    await deleteMemory(memory.id)
    memories.value = memories.value.filter((m) => m.id !== memory.id)
    ElMessage.success('已删除')
  } catch (error) {
    ElMessage.error(error.response?.data?.detail || '删除失败')
  }
}

// ---------------- 发送 ----------------

function fileToDataUrl(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader()
    reader.onload = () => resolve(reader.result)
    reader.onerror = () => reject(new Error('读取图片失败'))
    reader.readAsDataURL(file)
  })
}

async function addImageFiles(files) {
  for (const file of files) {
    if (!file.type || !file.type.startsWith('image/')) continue
    if (attachedImages.value.length >= 6) {
      ElMessage.warning('最多同时上传 6 张图片')
      break
    }
    try {
      const dataUrl = await fileToDataUrl(file)
      attachedImages.value.push({ id: ++imageSeq, dataUrl })
    } catch (error) {
      ElMessage.error(error.message || '读取图片失败')
    }
  }
}

function onImageFiles(event) {
  addImageFiles(Array.from(event.target.files || []))
  event.target.value = ''
}

/** 支持在输入框直接 Ctrl+V 粘贴截图/图片 */
function onPaste(event) {
  const items = event.clipboardData?.items || []
  const files = []
  for (const item of items) {
    if (item.kind === 'file' && item.type.startsWith('image/')) {
      const file = item.getAsFile()
      if (file) files.push(file)
    }
  }
  if (files.length) {
    event.preventDefault()
    addImageFiles(files)
  }
}

function removeImage(id) {
  attachedImages.value = attachedImages.value.filter((img) => img.id !== id)
}

function stop() {
  if (abortController) {
    abortController.abort()
  }
}

/** 重新生成最后一条回答（复用其对应的用户问题） */
function regenerateLast() {
  if (loading.value) return
  const last = messages.value[messages.value.length - 1]
  if (!last || last.role !== 'assistant') return
  let userMsg = null
  for (let i = messages.value.length - 2; i >= 0; i--) {
    if (messages.value[i].role === 'user') {
      userMsg = messages.value[i]
      break
    }
  }
  if (!userMsg) return
  messages.value.pop()
  question.value = userMsg.content
  send({ skipUserPush: true })
}

function ask(suggestion) {
  question.value = suggestion.text
  send()
}

async function send(options = {}) {
  flushTypewriter() // 清理上一轮残留（如异常中断）
  const hasImages = attachedImages.value.length > 0
  const text = question.value.trim() || (hasImages ? '请识别并描述这张图片' : '')
  if (!text || loading.value) return
  const imageDataUrls = attachedImages.value.map((img) => img.dataUrl)

  if (!options.skipUserPush) {
    messages.value.push({ role: 'user', content: text, images: imageDataUrls })
  }
  const assistantMsg = {
    role: 'assistant',
    content: '',
    sources: [],
    tool_trace: [],
    permissions: [],
    todos: [],
    plan: [],
    planTotal: 0,
    planDone: 0,
    planCurrent: null,
    _sourcesOpen: false,
    _streaming: true,
  }
  messages.value.push(assistantMsg)
  // 用响应式代理引用消息：typewriter 每帧增量更新 content 时能触发渲染
  streamMsg = messages.value[messages.value.length - 1]
  pendingText.value = ''
  question.value = ''
  attachedImages.value = []
  loading.value = true
  currentTool.value = ''
  abortController = new AbortController()
  scrollFollow.value = true
  scrollToBottom(true)

  // 根据开关状态计算后端 tool_mode
  let effectiveMode = 'none'
  if (toolMode.value === 'auto' || (useWebSearch.value && useKnowledgeBase.value)) {
    effectiveMode = 'auto'
  } else if (useWebSearch.value) {
    effectiveMode = 'web'
  } else if (useKnowledgeBase.value) {
    effectiveMode = 'knowledge'
  }
  try {
    await streamAgentChat(
      {
        question: text,
        images: imageDataUrls,
        conversation_id: activeId.value,
        tool_mode: effectiveMode,
        template_id: templateId.value || null,
      },
      {
        onSession: (data) => {
          activeId.value = data.conversation_id
          if (data.title) currentTitle.value = data.title
          upsertSession(data)
        },
        onTitle: (data) => {
          // 新会话标题是后台生成的，生成完成后推送到前端更新
          if (data.title) {
            currentTitle.value = data.title
            upsertSession({ conversation_id: activeId.value, title: data.title })
          }
        },
        onReasoning: (data) => {
          // DeepSeek 网页端式"已深度思考"折叠区（摘要文本，非原始 CoT）
          if (data.summary) {
            streamMsg.reasoning = data.summary
            streamMsg._reasoningOpen = false
            scrollToBottom()
          }
        },
        onStatus: (data) => {
          // 动态运行状态：准备中…/思考中…（tool_start 会覆盖为具体工具名）
          if (data.text) {
            currentTool.value = data.text
            scrollToBottom()
          }
        },
        onPlanProgress: (data) => {
          // 计划按当前进度展示：已完成/进行中/未开始
          streamMsg.planDone = data.done || 0
          streamMsg.planTotal =
            data.total != null ? data.total : streamMsg.plan.length
          streamMsg.planCurrent = data.current || null
          scrollToBottom()
        },
        onToolStart: (data) => {
          currentTool.value = `${toolName(data.name)}…`
          streamMsg.tool_trace.push({
            name: data.name,
            arguments: data.arguments,
            summary: '',
          })
          scrollToBottom()
        },
        onPlan: (data) => {
          streamMsg.plan = data.steps || []
          streamMsg.planTotal = streamMsg.plan.length
          scrollToBottom()
        },
        onTodos: (data) => {
          // TodoWrite 任务清单：规划后随 SSE 推送，可勾选跟踪
          streamMsg.todos = data.todos || []
          scrollToBottom()
        },
        onVision: () => {
          currentTool.value = '识别图片中…'
        },
        onToolResult: (data) => {
          currentTool.value = ''
          const trace = streamMsg.tool_trace.find((t) => t.name === data.name && !t.summary)
          if (trace) trace.summary = data.summary
          if (data.sources && data.sources.length) {
            streamMsg.sources.push(...data.sources)
          }
          scrollToBottom()
        },
        onPermission: (data) => {
          // 敏感操作等待确认：渲染审批卡片，用户批准后后端继续执行
          currentTool.value = '等待人工确认…'
          streamMsg.permissions.push({
            id: data.id,
            tool: data.name,
            summary: data.summary || `${toolName(data.name)} 需要确认`,
            args_text: formatArgs(data.arguments),
            args: data.arguments || {},
            status: 'pending',
            reason: '',
            rememberable: data.name === 'bash' || data.name === 'command_tool',
            _focusIndex: 0,
          })
          scrollToBottom()
          // 跳到选项卡：未在输入框打字时自动把焦点移入审批卡片
          focusPermissionCard()
        },
        onPermissionResolved: (data) => {
          const p = streamMsg.permissions.find((x) => x.id === data.id)
          if (p) {
            p.status = data.approved ? 'approved' : 'denied'
            if (data.reason) p.reason = data.reason
          }
          if (data.approved) currentTool.value = ''
          const stillPending = (streamMsg.permissions || []).some((x) => x.status === 'pending')
          if (!stillPending) {
            // 全部处理完：焦点回到输入框，正文继续打字机效果
            nextTick(() => questionInput.value?.focus())
          }
          scrollToBottom()
        },
        onToken: (token) => {
          pendingText.value += token
          startTypewriter()
        },
        onDone: () => {
          currentTool.value = ''
          flushTypewriter()
          streamMsg._streaming = false // 流式结束：切换到完整 Markdown 渲染
          nextTick(() => scrollToBottom(true)) // 渲染切换后重新对齐底部，避免跳动
          if (!streamMsg.content) {
            streamMsg.content = '（未生成回答内容）'
          }
        },
      },
      { signal: abortController.signal },
    )
  } catch (error) {
    flushTypewriter()
    if (error.name === 'AbortError' || error.message === 'aborted') {
      // 用户主动停止生成
      streamMsg.content = streamMsg.content || '（已停止生成）'
    } else {
      const detail = error.message || '请求失败，请确认后端服务已启动'
      ElMessage.error(detail)
      streamMsg.content = streamMsg.content || `请求失败：${detail}`
    }
  } finally {
    abortController = null
    loading.value = false
    currentTool.value = ''
    if (streamMsg) streamMsg._streaming = false
    await loadSessions()
    scrollToBottom()
  }
}

// ---------------- 模板管理 ----------------

async function loadTemplates() {
  try {
    templates.value = await listTemplates()
  } catch (error) {
    ElMessage.error(error.response?.data?.detail || '加载模板失败')
  }
}

function openTemplateManager() {
  templateManagerVisible.value = true
  loadTemplates()
}

function openTemplateEdit(tpl) {
  if (tpl && tpl.is_system) {
    ElMessage.warning('内置模板不可编辑，可新建自定义模板')
    return
  }
  templateForm.value = tpl
    ? {
        id: tpl.id,
        name: tpl.name,
        category: tpl.category,
        description: tpl.description,
        content: tpl.content,
      }
    : { id: null, name: '', category: 'general', description: '', content: '' }
  templateEditVisible.value = true
}

async function saveTemplate() {
  const form = templateForm.value
  if (!form.name.trim() || !form.content.trim()) {
    ElMessage.warning('名称和内容不能为空')
    return
  }
  try {
    const payload = {
      name: form.name.trim(),
      category: form.category,
      description: form.description.trim(),
      content: form.content,
    }
    if (form.id) {
      await updateTemplate(form.id, payload)
    } else {
      await createTemplate(payload)
    }
    templateEditVisible.value = false
    await loadTemplates()
    ElMessage.success(form.id ? '模板已更新' : '模板已创建')
  } catch (error) {
    ElMessage.error(error.response?.data?.detail || '保存失败')
  }
}

async function handleDeleteTemplate(tpl) {
  try {
    await ElMessageBox.confirm(
      `确定删除模板「${tpl.name}」吗？`,
      '提示',
      { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  try {
    await deleteTemplate(tpl.id)
    if (templateId.value === tpl.id) templateId.value = 0
    await loadTemplates()
    ElMessage.success('模板已删除')
  } catch (error) {
    ElMessage.error(error.response?.data?.detail || '删除失败')
  }
}

onMounted(() => {
  loadPrefs()
  loadSessions()
  loadTemplates()
  loadSuggestions()
  window.addEventListener('keydown', onPermissionKeydown, true)
})

onBeforeUnmount(() => {
  window.removeEventListener('keydown', onPermissionKeydown, true)
})
</script>

<style scoped>
.chat-page {
  height: calc(100vh - 32px);
  display: flex;
  gap: 16px;
}

/* ============ 会话侧栏 ============ */
.session-panel {
  width: 248px;
  flex-shrink: 0;
  display: flex;
  flex-direction: column;
  background: var(--bg-panel);
  border-radius: 14px;
  border: 1px solid var(--border);
  overflow: hidden;
  transition: width 0.25s ease;
}

.session-panel.collapsed {
  width: 56px;
}

.session-head {
  padding: 14px;
  flex-shrink: 0;
}

.new-chat-btn {
  width: 100%;
  border-radius: 10px;
  background: linear-gradient(135deg, #1677ff 0%, #4f8dff 100%);
  border: none;
  box-shadow: 0 4px 12px rgba(22, 119, 255, 0.25);
}

.session-panel.collapsed .session-head {
  display: flex;
  justify-content: center;
  padding: 14px 0;
}

.session-empty {
  color: var(--text-3);
  font-size: 13px;
  text-align: center;
  padding: 32px 0;
}

.session-list {
  flex: 1;
  overflow-y: auto;
  padding: 0 10px 10px;
}

.session-item {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 10px 12px;
  border-radius: 10px;
  cursor: pointer;
  margin-bottom: 2px;
  transition: background 0.2s;
}

.session-item:hover {
  background: var(--bg-hover);
}

.session-item.active {
  background: var(--bg-active);
}

.session-info {
  display: flex;
  align-items: center;
  gap: 6px;
  min-width: 0;
  flex: 1;
}

.session-title {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 13.5px;
  color: var(--text-1);
}

.session-item.active .session-title {
  color: var(--primary);
  font-weight: 600;
}

.session-count {
  flex-shrink: 0;
  font-size: 11px;
  color: var(--text-3);
  background: var(--bg-hover);
  border-radius: 8px;
  padding: 1px 6px;
}

.session-actions {
  display: none;
  gap: 6px;
  color: var(--text-3);
}

.session-item:hover .session-actions {
  display: flex;
}

.session-actions .el-icon {
  cursor: pointer;
}

.session-actions .el-icon:hover {
  color: var(--primary);
}

.session-foot {
  display: flex;
  justify-content: center;
  padding: 10px 0;
  border-top: 1px solid var(--border);
  flex-shrink: 0;
}

.panel-toggle {
  width: 34px;
  height: 34px;
  display: flex;
  align-items: center;
  justify-content: center;
  border-radius: 10px;
  color: var(--text-3);
  cursor: pointer;
  transition: all 0.2s;
}

.panel-toggle:hover {
  background: var(--bg-hover);
  color: var(--primary);
}

/* ============ 对话主区 ============ */
.chat-main {
  flex: 0 1 auto;
  min-width: 0;
  display: flex;
  flex-direction: column;
  width: min(100%, var(--chat-content-width, 70%));
  margin: 0 auto;
  background: var(--bg-chat);
  border-radius: 14px;
  border: 1px solid var(--border);
  overflow: hidden;
}

.chat-header {
  height: 54px;
  flex-shrink: 0;
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0 16px;
  border-bottom: 1px solid var(--border);
}

.header-left {
  display: flex;
  align-items: center;
  gap: 10px;
  min-width: 0;
}

.header-toggle {
  width: 32px;
  height: 32px;
  display: flex;
  align-items: center;
  justify-content: center;
  border-radius: 8px;
  color: var(--text-3);
  cursor: pointer;
  transition: all 0.2s;
}

.header-toggle:hover {
  background: var(--bg-hover);
  color: var(--primary);
}

.chat-title {
  font-size: 15px;
  font-weight: 600;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  min-width: 0;
}

.messages {
  flex: 1;
  min-width: 0;
  width: 100%;
  overflow-y: auto;
  padding: 20px 24px;
  /* 防止浏览器滚动锚定与打字机滚动打架造成跳动 */
  overflow-anchor: none;
}

/* 欢迎页 */
.welcome {
  height: 100%;
  width: 100%;
  min-width: 0;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: flex-start;
  text-align: center;
  padding: 28px 12px;
  overflow-y: auto;
}

.welcome-mark {
  flex-shrink: 0;
  margin-top: auto;
}

.welcome-title,
.welcome-sub {
  flex-shrink: 0;
}

.welcome .suggestions {
  flex-shrink: 0;
  margin-bottom: auto;
}

.welcome-mark {
  width: 64px;
  height: 64px;
  display: flex;
  align-items: center;
  justify-content: center;
  border-radius: 20px;
  color: #fff;
  background: linear-gradient(135deg, #1677ff 0%, #6f5bff 100%);
  box-shadow: 0 8px 24px rgba(22, 119, 255, 0.3);
  margin-bottom: 20px;
}

.welcome-title {
  font-size: 24px;
  font-weight: 700;
  margin-bottom: 8px;
}

.welcome-sub {
  color: var(--text-3);
  font-size: 14px;
  margin-bottom: 32px;
  max-width: 100%;
  padding: 0 12px;
}

.suggestions {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(210px, 1fr));
  gap: 12px;
  width: 100%;
  max-width: 780px;
}

.suggestion {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 14px 16px;
  border: 1px solid var(--border);
  border-radius: 12px;
  background: var(--bg-card-2);
  color: var(--text-2);
  font-size: 13.5px;
  cursor: pointer;
  text-align: left;
  transition: all 0.2s;
}

.suggestion:hover {
  border-color: var(--primary);
  background: var(--bg-active);
  color: var(--primary);
  transform: translateY(-2px);
  box-shadow: 0 6px 16px rgba(22, 119, 255, 0.12);
}

.suggestion-icon {
  flex-shrink: 0;
  width: 28px;
  height: 28px;
  display: flex;
  align-items: center;
  justify-content: center;
  border-radius: 8px;
  background: var(--bg-active);
  color: var(--primary);
}

/* 消息 */
.msg {
  display: flex;
  gap: 12px;
  margin-bottom: 24px;
  max-width: 880px;
  margin-left: auto;
  margin-right: auto;
}

.msg.user {
  flex-direction: row-reverse;
}

.avatar {
  width: 36px;
  height: 36px;
  flex-shrink: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  border-radius: 12px;
}

.avatar.user {
  background: linear-gradient(135deg, #f7b955 0%, #ff9a5a 100%);
  color: #fff;
  box-shadow: 0 4px 10px rgba(255, 154, 90, 0.3);
}

.avatar.assistant {
  background: linear-gradient(135deg, #1677ff 0%, #6f5bff 100%);
  color: #fff;
  box-shadow: 0 4px 10px rgba(111, 91, 255, 0.3);
}

.msg-body {
  flex: 1;
  min-width: 0;
  max-width: calc(100% - 48px);
}

.msg.user .msg-body {
  display: flex;
  justify-content: flex-end;
}

.user-bubble {
  max-width: 70%;
  padding: 10px 16px;
  border-radius: 14px 14px 4px 14px;
  background: var(--user-bubble-bg);
  border: 1px solid var(--user-bubble-border);
  color: var(--user-bubble-text);
  line-height: 1.7;
  white-space: pre-wrap;
  word-break: break-word;
  font-size: 14px;
}

.user-images {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-bottom: 6px;
}

.user-img {
  max-width: 220px;
  max-height: 220px;
  border-radius: 10px;
  object-fit: cover;
  display: block;
  border: 1px solid rgba(22, 119, 255, 0.18);
}

.assistant-content {
  position: relative;
  padding: 14px 18px;
  border-radius: 14px 14px 14px 4px;
  background: var(--bg-card);
  border: 1px solid var(--border);
  box-shadow: var(--shadow-soft);
}

/* 执行计划 */
.plan-box {
  margin-bottom: 8px;
  padding: 10px 14px;
  border-radius: 10px;
  background: var(--plan-bg);
  border: 1px solid var(--plan-border);
}

.plan-head {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 12.5px;
  font-weight: 600;
  color: var(--primary);
  margin-bottom: 6px;
}

.plan-progress {
  margin-left: auto;
  font-size: 11px;
  font-weight: 500;
  color: var(--text-3);
  background: var(--bg-hover);
  border-radius: 999px;
  padding: 1px 8px;
}

.plan-steps {
  margin: 0;
  padding-left: 20px;
}

.plan-step {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 13px;
  color: var(--text-2);
  line-height: 1.8;
  list-style: none;
}

.plan-step-icon {
  width: 18px;
  height: 18px;
  flex-shrink: 0;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border-radius: 50%;
  font-size: 11px;
  color: var(--text-3);
  background: var(--bg-hover);
}

.plan-step.done .plan-step-icon {
  color: #fff;
  background: var(--success);
}

.plan-step.done .plan-step-text {
  color: var(--text-3);
  text-decoration: line-through;
}

.plan-step.running .plan-step-icon {
  background: var(--bg-active);
  color: var(--primary);
}

.plan-step.running .plan-step-text {
  color: var(--primary);
  font-weight: 600;
}

.plan-step-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--primary);
  animation: pulse-dot 1s infinite;
}

@keyframes pulse-dot {
  0%,
  100% {
    opacity: 0.35;
    transform: scale(0.8);
  }
  50% {
    opacity: 1;
    transform: scale(1.15);
  }
}

/* 已深度思考（折叠区） */
.reasoning-box {
  margin-bottom: 8px;
  border-radius: 10px;
  background: var(--plan-bg);
  border: 1px solid var(--plan-border);
  overflow: hidden;
}

.reasoning-head {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 8px 14px;
  font-size: 12.5px;
  font-weight: 600;
  color: var(--primary);
  cursor: pointer;
  user-select: none;
}

.reasoning-head .arrow {
  margin-left: auto;
  color: var(--text-3);
  transition: transform 0.2s;
}

.reasoning-head .arrow.open {
  transform: rotate(180deg);
}

.reasoning-body {
  padding: 2px 14px 12px;
  font-size: 13px;
  color: var(--text-2);
  line-height: 1.7;
  white-space: pre-wrap;
}

.msg-actions {
  position: absolute;
  top: 10px;
  right: 12px;
  opacity: 0;
  transition: opacity 0.2s;
}

.assistant-content:hover .msg-actions {
  opacity: 1;
}

.copy-btn {
  color: var(--text-3);
  cursor: pointer;
  padding: 4px;
  border-radius: 6px;
}

.copy-btn:hover {
  color: var(--primary);
  background: var(--bg-active);
}

.typing-tool {
  color: var(--primary);
  font-size: 13.5px;
}

.status-chip {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  padding: 5px 12px;
  border-radius: 999px;
  background: var(--bg-active);
  color: var(--primary);
  font-size: 13px;
  font-weight: 500;
}

.status-spinner {
  width: 12px;
  height: 12px;
  border-radius: 50%;
  border: 2px solid var(--primary-soft);
  border-top-color: var(--primary);
  animation: status-spin 0.8s linear infinite;
}

@keyframes status-spin {
  to {
    transform: rotate(360deg);
  }
}

.dots {
  display: inline-flex;
  gap: 4px;
  padding: 4px 0;
}

.dots i {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: var(--text-3);
  animation: bounce 1.2s infinite;
}

.dots i:nth-child(2) {
  animation-delay: 0.15s;
}

.dots i:nth-child(3) {
  animation-delay: 0.3s;
}

@keyframes bounce {
  0%,
  60%,
  100% {
    transform: translateY(0);
    opacity: 0.6;
  }
  30% {
    transform: translateY(-4px);
    opacity: 1;
  }
}

/* 工具轨迹 */
.tool-trace {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-top: 8px;
}

.tool-chip {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  padding: 4px 10px;
  border-radius: 999px;
  background: var(--bg-card-2);
  border: 1px solid var(--border);
  color: var(--text-2);
  font-size: 12px;
}

.tool-chip .el-icon {
  color: var(--primary);
}

.tool-name {
  font-weight: 600;
}

.tool-summary {
  color: var(--text-3);
  max-width: 320px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

/* 人工确认（HITL）审批卡片 */
.permission-box {
  display: flex;
  flex-direction: column;
  gap: 8px;
  margin-top: 8px;
}

.permission-card {
  border-radius: 10px;
  border: 1px solid var(--border);
  background: var(--bg-card-2);
  overflow: hidden;
}

.permission-card.pending {
  border-color: var(--el-color-warning, #e6a23c);
  box-shadow: 0 0 0 1px var(--el-color-warning-light-7, #fdf6ec) inset;
}

.permission-card.approved {
  border-color: var(--el-color-success-light-5, #b3e19d);
  opacity: 0.85;
}

.permission-card.denied {
  border-color: var(--el-color-danger-light-5, #fbc4c4);
  opacity: 0.85;
}

.permission-head {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 8px 12px;
  font-size: 13px;
  font-weight: 600;
  color: var(--text-2);
  background: var(--bg-card-3, rgba(128, 128, 128, 0.08));
  border-bottom: 1px solid var(--border);
}

.permission-head .el-icon {
  color: var(--el-color-warning, #e6a23c);
}

.permission-card.approved .permission-head .el-icon {
  color: var(--el-color-success, #67c23a);
}

.permission-card.denied .permission-head .el-icon {
  color: var(--el-color-danger, #f56c6c);
}

.permission-title {
  color: var(--text-1);
}

.permission-tool {
  margin-left: auto;
  padding: 1px 8px;
  border-radius: 999px;
  background: var(--bg-card-2);
  border: 1px solid var(--border);
  color: var(--text-3);
  font-size: 11px;
  font-weight: 400;
}

.permission-body {
  padding: 10px 12px 4px;
}

.permission-summary {
  font-size: 13px;
  color: var(--text-1);
  line-height: 1.5;
}

.permission-args {
  margin: 8px 0 0;
  padding: 8px 10px;
  border-radius: 8px;
  background: var(--bg-card-3, rgba(128, 128, 128, 0.07));
  border: 1px solid var(--border);
  font-family: 'Cascadia Code', Consolas, monospace;
  font-size: 12px;
  line-height: 1.5;
  color: var(--text-2);
  white-space: pre-wrap;
  word-break: break-all;
  max-height: 180px;
  overflow: auto;
}

/* 编辑类审批的变更预览（红删绿增） */
.perm-diff {
  margin-top: 8px;
  border: 1px solid var(--border);
  border-radius: 8px;
  overflow: hidden;
}

.perm-diff-head {
  padding: 4px 10px;
  font-size: 11px;
  color: var(--text-3);
  background: var(--bg-card-3, rgba(128, 128, 128, 0.07));
  border-bottom: 1px solid var(--border);
}

.perm-diff-line {
  display: flex;
  gap: 8px;
  padding: 6px 10px;
  font-family: 'Cascadia Code', Consolas, monospace;
  font-size: 12px;
  line-height: 1.5;
  white-space: pre-wrap;
  word-break: break-all;
}

.perm-diff-line.del {
  background: rgba(245, 108, 108, 0.1);
  color: #e88080;
}

.perm-diff-line.add {
  background: rgba(103, 194, 58, 0.1);
  color: #7ac25b;
}

.perm-diff-sign {
  flex-shrink: 0;
  font-weight: 700;
}

.permission-actions {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 12px 10px;
}

.permission-reason {
  flex: 1;
}

/* 审批选项卡：数字键选择 / 方向键移动 / Enter 确认 / Esc 返回输入 */
.permission-opts {
  display: flex;
  align-items: center;
  gap: 6px;
  outline: none;
}

.perm-opt {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  padding: 4px 10px;
  border-radius: 7px;
  border: 1px solid var(--border);
  background: var(--bg-card-2);
  color: var(--text-2);
  font-size: 12.5px;
  cursor: pointer;
  transition: all 0.15s;
}

.perm-opt .perm-num {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 16px;
  height: 16px;
  border-radius: 4px;
  background: var(--bg-hover);
  font-size: 11px;
  font-weight: 700;
  color: var(--text-3);
}

.perm-opt.primary {
  border-color: var(--el-color-primary-light-5, #b3d8ff);
  color: var(--primary);
}

.perm-opt.danger {
  border-color: var(--el-color-danger-light-5, #fbc4c4);
  color: var(--el-color-danger, #f56c6c);
}

.perm-opt:hover,
.perm-opt.active {
  border-color: var(--primary);
  background: var(--bg-active);
  box-shadow: 0 0 0 2px var(--bg-active);
}

.perm-opt.active .perm-num {
  background: var(--primary);
  color: #fff;
}

.perm-key-hint {
  font-size: 11px;
  color: var(--text-3);
  white-space: nowrap;
}

/* 审批处理后的紧凑状态条 */
.permission-resolved-chip {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-top: 8px;
  padding: 4px 10px;
  border-radius: 8px;
  border: 1px solid var(--border);
  background: var(--bg-card-2);
  font-size: 12px;
  color: var(--text-2);
}

.permission-resolved-chip.approved {
  border-color: var(--el-color-success-light-7, #e1f3d8);
}

.permission-resolved-chip.approved > .el-icon {
  color: var(--el-color-success, #67c23a);
}

.permission-resolved-chip.denied {
  border-color: var(--el-color-danger-light-7, #fef0f0);
}

.permission-resolved-chip.denied > .el-icon {
  color: var(--el-color-danger, #f56c6c);
}

.prc-label {
  font-weight: 600;
  flex-shrink: 0;
}

.prc-tool {
  padding: 0 7px;
  border-radius: 999px;
  background: var(--bg-hover);
  color: var(--text-3);
  font-size: 11px;
  flex-shrink: 0;
}

.prc-summary {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--text-3);
}

.prc-reason {
  color: var(--text-3);
  flex-shrink: 0;
}

/* TodoWrite 任务清单 */
.plan-todos {
  display: flex;
  flex-direction: column;
  gap: 2px;
  padding: 6px 10px 10px;
}

.plan-todo {
  display: flex;
  align-items: flex-start;
  gap: 7px;
  padding: 5px 8px;
  border-radius: 7px;
  font-size: 13px;
  color: var(--text-1);
}

.plan-todo.done {
  color: var(--text-3);
  text-decoration: line-through;
}

.plan-todo-check {
  color: var(--el-color-success, #67c23a);
  margin-top: 2px;
}

.plan-todo-box {
  width: 13px;
  height: 13px;
  margin-top: 2px;
  border-radius: 4px;
  border: 1.5px solid var(--border-strong);
  flex-shrink: 0;
}

.plan-todo-text {
  line-height: 1.5;
  word-break: break-word;
}

/* 引用来源 */
.sources {
  margin-top: 8px;
  border-radius: 10px;
  background: var(--bg-card-2);
  border: 1px solid var(--border);
  overflow: hidden;
}

.sources-head {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 9px 14px;
  font-size: 12.5px;
  color: var(--text-2);
  cursor: pointer;
  user-select: none;
}

.sources-head .el-icon:first-child {
  color: var(--primary);
}

.sources-head .arrow {
  margin-left: auto;
  color: var(--text-3);
  transition: transform 0.2s;
}

.sources-head .arrow.open {
  transform: rotate(180deg);
}

.sources-body {
  padding: 0 14px 10px;
}

.source-item {
  padding: 10px 0;
  border-top: 1px dashed var(--border);
}

.source-meta {
  display: flex;
  align-items: center;
  gap: 8px;
  color: var(--text-3);
  font-size: 12px;
  margin-bottom: 5px;
  flex-wrap: wrap;
}

.source-score {
  color: var(--primary);
  background: var(--bg-active);
  padding: 1px 8px;
  border-radius: 8px;
}

.source-tag {
  color: var(--text-2);
  background: var(--bg-hover);
  padding: 1px 8px;
  border-radius: 8px;
  font-size: 11px;
}

.source-tag.web {
  color: var(--web-tag-text);
  background: var(--web-tag-bg);
}

.source-title {
  font-weight: 600;
  color: var(--text-1);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  max-width: min(420px, 100%);
}

.source-link {
  display: inline-block;
  margin-top: 6px;
  font-size: 12.5px;
  color: var(--primary);
  text-decoration: none;
}

.source-link:hover {
  text-decoration: underline;
}

.source-no {
  width: 20px;
  height: 20px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border-radius: 6px;
  background: #1677ff;
  color: #fff;
  font-size: 11px;
  font-weight: 600;
}

.source-item.flash {
  animation: flash-bg 1.6s ease;
}

@keyframes flash-bg {
  0%,
  40% {
    background: var(--bg-active);
    border-radius: 8px;
  }
  100% {
    background: transparent;
  }
}

.source-content {
  font-size: 13px;
  color: var(--text-2);
  line-height: 1.7;
  max-height: 130px;
  overflow-y: auto;
}

/* ============ 输入区 ============ */
.input-area {
  flex-shrink: 0;
  min-width: 0;
  width: 100%;
  padding: 12px 24px 14px;
  border-top: 1px solid var(--border);
}

.toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 8px 16px;
  margin-bottom: 10px;
}

.toolbar-group {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 14px;
}

.toolbar-group .el-switch {
  --el-switch-on-color: #1677ff;
}

.auto-switch {
  --el-switch-on-color: #6f5bff !important;
}

.auto-switch :deep(.el-switch__label) {
  font-weight: 600;
  color: var(--text-1);
}

.toolbar-group .el-switch.is-disabled {
  opacity: 0.65;
}

.template-select {
  flex: 0 1 180px;
  min-width: 140px;
}

/* 附加图片预览 */
.image-preview-row {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-bottom: 8px;
}

.image-preview-item {
  position: relative;
  width: 64px;
  height: 64px;
  border-radius: 10px;
  overflow: hidden;
  border: 1px solid var(--border-strong);
  background: var(--bg-chat);
}

.image-preview-img {
  width: 100%;
  height: 100%;
  object-fit: cover;
  display: block;
}

.image-preview-remove {
  position: absolute;
  top: 3px;
  right: 3px;
  width: 18px;
  height: 18px;
  border-radius: 50%;
  background: rgba(0, 0, 0, 0.55);
  color: #fff;
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: center;
}

.image-preview-remove:hover {
  background: rgba(245, 108, 108, 0.9);
}

.input-box {
  position: relative;
  border: 1px solid var(--border-strong);
  border-radius: 14px;
  padding: 10px 56px 10px 44px;
  background: var(--bg-card-2);
  transition: all 0.2s;
}

.input-box:focus-within {
  border-color: var(--primary);
  background: var(--bg-chat);
  box-shadow: 0 0 0 3px var(--primary-soft);
}

.input-box :deep(.el-textarea__inner) {
  background: transparent;
  border: none;
  box-shadow: none;
  padding: 0;
  font-size: 14px;
  line-height: 1.6;
}

.attach-btn {
  position: absolute;
  left: 12px;
  top: 14px;
  color: var(--text-3);
  cursor: pointer;
  transition: color 0.2s;
}

.attach-btn:hover {
  color: var(--primary);
}

.hidden-file-input {
  display: none;
}

.send-btn {
  position: absolute;
  right: 10px;
  bottom: 10px;
  width: 38px;
  height: 38px;
  background: linear-gradient(135deg, #1677ff 0%, #4f8dff 100%);
  border: none;
  box-shadow: 0 4px 10px rgba(22, 119, 255, 0.3);
}

.send-btn.is-disabled {
  background: #c8cdd6;
  box-shadow: none;
}

.stop-btn {
  background: #f56c6c;
  box-shadow: 0 4px 10px rgba(245, 108, 108, 0.3);
}

.stop-btn:hover {
  background: #f78989;
}

.input-foot {
  margin-top: 8px;
  text-align: center;
  color: var(--text-3);
  font-size: 12px;
}

/* 失败提示 */
.failed-bar {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: 8px;
  padding: 8px 12px;
  border-radius: 10px;
  background: var(--danger-bg);
  border: 1px solid var(--danger-border);
  color: var(--danger);
  font-size: 13px;
}

.failed-icon {
  flex-shrink: 0;
}

/* ============ 模板管理 ============ */
.tpl-list {
  max-height: 380px;
  overflow-y: auto;
  margin-bottom: 12px;
}

.tpl-item {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 10px 12px;
  border: 1px solid var(--border);
  border-radius: 10px;
  margin-bottom: 8px;
}

.tpl-info {
  flex: 1;
  min-width: 0;
  margin-right: 12px;
}

.tpl-name-row {
  display: flex;
  align-items: center;
  gap: 8px;
}

.tpl-name {
  font-weight: 600;
  font-size: 14px;
}

.tpl-desc {
  color: var(--text-3);
  font-size: 12px;
  margin-top: 4px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.tpl-actions {
  flex-shrink: 0;
}

.tpl-create {
  width: 100%;
}

/* 长期记忆 */
.memory-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 12px;
}

.memory-hint {
  color: var(--text-3);
  font-size: 12.5px;
  line-height: 1.7;
}

.memory-actions {
  flex-shrink: 0;
  display: flex;
  gap: 8px;
}

.memory-empty {
  color: var(--text-3);
  text-align: center;
  padding: 32px 0;
  font-size: 13px;
}

.memory-list {
  max-height: 420px;
  overflow-y: auto;
}

.memory-item {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 10px 12px;
  border: 1px solid var(--border);
  border-radius: 10px;
  margin-bottom: 8px;
}

.memory-main {
  flex: 1;
  min-width: 0;
  display: flex;
  align-items: flex-start;
  gap: 10px;
}

.memory-content {
  flex: 1;
  font-size: 13.5px;
  color: var(--text-1);
  line-height: 1.6;
}

.memory-ops {
  flex-shrink: 0;
}

</style>
