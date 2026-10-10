<template>
  <div class="chat-page">
    <!-- 左侧：会话列表（可折叠） -->
    <SessionList
      v-model:collapsed="sessionCollapsed"
      :sessions="sessions"
      :active-id="activeId"
      :loading="loading"
      @new-chat="startNewChat"
      @switch="switchSession"
      @rename="handleRename"
      @delete="handleDelete"
    />

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
        <!-- 供应商快捷切换（只切供应商；管理入口跳设置页模型面板） -->
        <el-dropdown
          trigger="click"
          :disabled="loading || quickBusy"
          @command="onQuickSwitch"
        >
          <span :class="['model-switch-chip', { busy: quickBusy }]">
            {{ quickActiveName || '未配置供应商' }}
            <span v-if="quickModel" class="model-switch-model">{{ quickModel }}</span>
          </span>
          <template #dropdown>
            <el-dropdown-menu>
              <el-dropdown-item
                v-for="p in quickProviders"
                :key="p.id"
                :command="p.id"
                :disabled="p.id === quickActiveId"
              >
                {{ p.name }} · {{ p.model || '未设模型' }}
              </el-dropdown-item>
              <el-dropdown-item divided command="__manage__">管理供应商…</el-dropdown-item>
            </el-dropdown-menu>
          </template>
        </el-dropdown>
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
        <el-button
          text
          type="primary"
          class="memory-btn"
          :disabled="!activeId"
          @click="openContextDialog"
        >
          <el-icon><Histogram /></el-icon>
          上下文
        </el-button>
        <el-button
          text
          type="primary"
          class="memory-btn"
          :disabled="!activeId"
          @click="openTimeline"
        >
          <el-icon><Clock /></el-icon>
          快照
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
            <span class="welcome-glyph">问</span>
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
            <div v-if="msg.role === 'user'" class="user-msg-group">
              <div class="user-bubble">
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
              <!-- 消息级回退：删除此消息及之后的对话，内容放回输入框重新编辑 -->
              <div v-if="msg.id && !loading" class="user-rewind">
                <el-tooltip content="回退到此消息（删除其后对话并重新编辑）" placement="top">
                  <span class="rewind-btn" @click="rewindTo(msg)">
                    <el-icon :size="12"><RefreshLeft /></el-icon>
                    回退
                  </span>
                </el-tooltip>
              </div>
            </div>

            <template v-else>
              <TodoPanel
                v-if="(msg.plan && msg.plan.length) || (msg.todos && msg.todos.length)"
                :plan="msg.plan"
                :todos="msg.todos"
                :plan-done="msg.planDone"
                :plan-total="msg.planTotal"
                :show-running="!!currentTool"
              />

              <!-- 计划审批卡：模型进入计划模式后提交的结构化计划 -->
              <PlanApprovalCard
                v-if="msg.planApproval && msg.planApproval.state !== 'cancelled'"
                :approval="msg.planApproval"
                :loading="loading"
                @confirm="confirmPlan(msg)"
                @cancel="cancelPlan(msg)"
                @keydown="onPlanCardKeydown(msg, $event)"
              />
              <!-- 中断未跑完：给「继续执行 / 丢弃这一轮」入口（刷新/切换会话恢复用） -->
              <div v-if="msg._unfinishedBar" class="suspension-bar interrupted-bar">
                <span class="suspension-text">上一轮执行被中断（后台退出或异常），可以继续跑完</span>
                <span class="suspension-actions">
                  <el-button size="small" type="primary" @click="continueInterrupted(msg)">
                    继续执行
                  </el-button>
                  <el-button size="small" :disabled="loading" @click="abandonInterrupted(msg)">
                    丢弃这一轮
                  </el-button>
                </span>
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
                  <span v-if="statusElapsed >= 3" class="status-time">· {{ statusElapsed }}s</span>
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

            <!-- 工具调用轨迹：有完整输出（detail）时可点击展开 -->
            <ToolTraceList
              v-if="msg.tool_trace && msg.tool_trace.length"
              :items="msg.tool_trace"
              :tool-name="toolName"
            />

            <!-- 人工确认（HITL）：等待用户批准/拒绝敏感操作 -->
            <PermissionCard
              v-if="msg.permissions && msg.permissions.length"
              :permissions="msg.permissions"
              :loading="loading"
              :tool-name="toolName"
              @decide="decidePermission"
              @stop="stopSuspended"
            />

            <!-- 引用来源 -->
            <SourceList
              v-if="msg.sources && msg.sources.length"
              v-model:open="msg._sourcesOpen"
              :sources="msg.sources"
              @open-doc="openSourceDoc"
            />
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
            :disabled="hasSuspendedPending"
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
            :disabled="hasSuspendedPending || (!question.trim() && !attachedImages.length)"
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
          <el-checkbox
            v-model="showArchived"
            size="small"
            class="memory-archived-toggle"
            @change="loadMemories"
          >
            显示归档
          </el-checkbox>
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
        <div
          v-for="m in memories"
          :key="m.id"
          class="memory-item"
          :class="{ 'is-archived': m.status === 'archived' }"
        >
          <div class="memory-main">
            <el-tag size="small" class="memory-tag" :type="categoryTagType(m.category)" effect="light">
              {{ categoryLabel(m.category) }}
            </el-tag>
            <div class="memory-body">
              <div
                class="memory-content"
                :class="{ expanded: expandedMemoryIds.has(m.id) }"
                @click="toggleMemoryExpand(m.id)"
              >
                {{ m.content }}
              </div>
              <div class="memory-meta">
                <span v-if="m.status === 'archived'" class="memory-archived-flag">已归档</span>
                <span>录入 {{ fmtMemoryDate(m.created_at) }}</span>
                <span v-if="m.hit_count">命中 {{ m.hit_count }} 次</span>
              </div>
            </div>
          </div>
          <div class="memory-ops">
            <el-button size="small" link @click="openMemoryEdit(m)">编辑</el-button>
            <el-button size="small" link @click="handleArchiveMemory(m)">
              {{ m.status === 'archived' ? '恢复' : '归档' }}
            </el-button>
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

    <!-- 上下文占用面板（/context + /compact 的 Web 入口） -->
    <!-- append-to-body：与其他 dialog 一致，避免渲染在父级 DOM 内导致
         层级错乱（内容看不见 / 关闭按钮失效）；接口 15s 超时自动报错关闭 -->
    <el-dialog
      v-model="contextVisible"
      title="上下文占用"
      width="480px"
      append-to-body
      :close-on-click-modal="true"
      :close-on-press-escape="true"
      @close="onContextClose"
    >
      <div v-if="contextStats" class="ctx-stats">
        <div class="ctx-row">
          <span class="ctx-label">历史消息</span>
          <span class="ctx-value">
            {{ ctxEffectiveMessages }} 条发给模型
            <span class="ctx-sub">
              （上限 {{ contextStats.history_max_messages }}，超
              {{ ctxHighWater }} 自动压缩<template
                v-if="contextStats.message_count !== ctxEffectiveMessages"
              >；会话累计 {{ contextStats.message_count }} 条</template>）
            </span>
          </span>
        </div>
        <div class="ctx-bar">
          <i :style="{ width: barPct(ctxEffectiveMessages, ctxHighWater) }" />
        </div>
        <div class="ctx-row">
          <span class="ctx-label">估算 token</span>
          <span class="ctx-value">
            {{ contextStats.estimated_tokens }} / {{ contextStats.token_budget }}
          </span>
        </div>
        <div class="ctx-bar">
          <i
            :style="{ width: barPct(contextStats.estimated_tokens, contextStats.token_budget) }"
          />
        </div>
        <div class="ctx-row">
          <span class="ctx-label">滚动摘要</span>
          <span class="ctx-value">
            {{
              contextStats.summary_chars
                ? `${contextStats.summary_chars} 字 · 覆盖 ${contextStats.summary_covered_messages} 条早期消息`
                : '未生成'
            }}
          </span>
        </div>

        <!-- 最近运行的缓存命中率与 LLM 调用量（主循环口径） -->
        <template v-if="contextStats.recent_runs">
          <div class="ctx-row">
            <span class="ctx-label">缓存命中率</span>
            <span class="ctx-value">
              {{
                contextStats.recent_cache_hit_rate != null
                  ? (contextStats.recent_cache_hit_rate * 100).toFixed(0) + '%'
                  : '-'
              }}
              <span class="ctx-sub">（近 {{ contextStats.recent_runs }} 次运行）</span>
            </span>
          </div>
          <div class="ctx-bar">
            <i
              :style="{
                width:
                  contextStats.recent_cache_hit_rate != null
                    ? (contextStats.recent_cache_hit_rate * 100).toFixed(1) + '%'
                    : '0%',
              }"
            />
          </div>
          <div class="ctx-row">
            <span class="ctx-label">主循环 LLM 调用</span>
            <span class="ctx-value">{{ contextStats.recent_llm_calls }} 次</span>
          </div>
          <div v-if="contextStats.recent_cache_hit_tokens != null" class="ctx-row">
            <span class="ctx-label">缓存明细</span>
            <span class="ctx-value">
              命中 {{ fmtCtxTokens(contextStats.recent_cache_hit_tokens) }} ·
              未命中 {{ fmtCtxTokens(contextStats.recent_cache_miss_tokens) }}
            </span>
          </div>
        </template>
        <div v-else class="ctx-row">
          <span class="ctx-label">缓存命中率</span>
          <span class="ctx-value">（本会话暂无运行记录）</span>
        </div>

        <div class="ctx-note">
          超出软窗口（条数或 token 预算）时下一轮自动压缩；也可以手动压缩——
          保留近期消息原文，更早的对话并入滚动摘要。
        </div>
      </div>
      <div v-else class="ctx-stats">加载中…</div>
      <template #footer>
        <el-button @click="contextVisible = false">关闭</el-button>
        <el-button
          type="primary"
          :loading="compacting"
          :disabled="!contextStats"
          @click="handleCompact"
        >
          立即压缩
        </el-button>
      </template>
    </el-dialog>

    <!-- 会话快照时间线（原生检查点）：可回滚消息与任务清单到任意最近快照 -->
    <el-drawer v-model="timelineVisible" title="会话快照" size="440px">
      <div v-if="timelineLoading" class="timeline-hint">加载中…</div>
      <div v-else-if="!timelineItems.length" class="timeline-hint">
        暂无快照（每轮收尾后按「高级参数 → 每会话保留快照数」裁剪保留最近若干条）
      </div>
      <div v-else class="timeline-list">
        <div v-for="(cp, i) in timelineItems" :key="cp.checkpoint_id" class="timeline-item">
          <div class="timeline-head">
            <span class="timeline-time">{{ formatCheckpointTime(cp.created_at) }}</span>
            <el-tag v-if="i === 0" size="small" type="success">最新</el-tag>
            <el-button size="small" text type="primary" @click="rollbackTo(cp)">
              回滚到此
            </el-button>
          </div>
          <div class="timeline-meta">
            消息 {{ cp.messages }} · 计划 {{ (cp.plan || []).length }} 步（完成
            {{ cp.plan_done_count || 0 }}） · 来源 {{ cp.sources_count || 0 }}
          </div>
          <div v-if="cp.plan && cp.plan.length" class="timeline-plan">
            {{ cp.plan.slice(0, 3).join('；') }}{{ cp.plan.length > 3 ? ' …' : '' }}
          </div>
        </div>
      </div>
    </el-drawer>

    <!-- 引用「查看原文」文档查看器 -->
    <DocumentViewer ref="docViewerRef" />
  </div>
</template>

<script setup>
import { computed, inject, nextTick, onActivated, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { createStreamHandlers } from '../composables/useAgentStream'
import { useTypewriter } from '../composables/useTypewriter'
import {
  Clock,
  Close,
  Collection,
  CopyDocument,
  DataAnalysis,
  Expand,
  Fold,
  Histogram,
  MagicStick,
  Memo,
  Operation,
  Picture,
  Position,
  Promotion,
  RefreshLeft,
  RefreshRight,
  User,
  VideoPause,
  WarningFilled,
} from '@element-plus/icons-vue'
import MarkdownContent from '../components/MarkdownContent.vue'
import DocumentViewer from '../components/DocumentViewer.vue'
import PermissionCard from '../components/chat/PermissionCard.vue'
import PlanApprovalCard from '../components/chat/PlanApprovalCard.vue'
import PlanSteps from '../components/chat/PlanSteps.vue'
import ToolTraceList from '../components/chat/ToolTraceList.vue'
import SourceList from '../components/chat/SourceList.vue'
import TodoPanel from '../components/chat/TodoPanel.vue'
import SessionList from '../components/chat/SessionList.vue'

// keep-alive 按组件名匹配，显式声明
defineOptions({ name: 'ChatView' })
import {
  abandonRun,
  addMemory,
  compactConversation,
  consolidateMemories,
  continueAgentRun,
  createTemplate,
  deleteConversation,
  deleteMemory,
  deleteTemplate,
  getAgentContext,
  getConversationMessages,
  getConversationTimeline,
  getPendingApproval,
  getSettings,
  getSuggestions,
  getTodos,
  listConversations,
  listDocuments,
  listMemories,
  listTemplates,
  renameConversation,
  resumeAgentChat,
  rewindConversation,
  rollbackConversation,
  streamAgentChat,
  switchChatProvider,
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

// 会话草稿：每个会话（含新对话 key='new'）独立保留输入框内容
const drafts = ref({})
// 会话消息内存缓存：切换时保存当前内存态（含生成中的消息），
// 切回时优先用缓存，避免正在生成的内容丢失
const sessionMessagesCache = {}

// ---------------- 供应商快捷切换（头部下拉） ----------------
// 读 /api/settings 快照（独立于设置页草稿）；切换走专用轻量端点：
// 只清对话缓存，不触发嵌入/重排重载；本会话流式中禁用。
// 已知边界：切换只影响后续 LLM 迭代，不中断进行中的 run（与设置页保存同语义）。
const quickProviders = ref([])
const quickActiveId = ref('')
const quickModel = ref('')
const quickBusy = ref(false)
const openSettingsTab = inject('openSettingsTab', null)

const quickActiveName = computed(
  () => quickProviders.value.find((p) => p.id === quickActiveId.value)?.name || '',
)

async function loadQuickProviders() {
  try {
    const snapshot = await getSettings()
    quickProviders.value = (snapshot.providers || []).filter(
      (p) => p.type === 'chat' && p.enabled,
    )
    quickActiveId.value =
      snapshot.active_chat_provider || quickProviders.value[0]?.id || ''
    syncQuickModel()
  } catch {
    /* 后端未就绪时静默，不影响对话主流程 */
  }
}

function syncQuickModel() {
  quickModel.value =
    quickProviders.value.find((p) => p.id === quickActiveId.value)?.model || ''
}

async function onQuickSwitch(command) {
  if (!command) return
  if (command === '__manage__') {
    openSettingsTab?.('models')
    return
  }
  if (command === quickActiveId.value) return
  quickBusy.value = true
  try {
    const res = await switchChatProvider(command)
    quickActiveId.value = res.active_chat_provider || command
    syncQuickModel()
    ElMessage.success(`已切换到 ${quickActiveName.value || command}（对后续消息生效）`)
  } catch (error) {
    ElMessage.error(error.response?.data?.detail || '切换失败')
    await loadQuickProviders()
  } finally {
    quickBusy.value = false
  }
}

function currentDraftKey() {
  return activeId.value ?? 'new'
}

function saveDraft() {
  drafts.value[currentDraftKey()] = question.value
}

function restoreDraft() {
  question.value = drafts.value[currentDraftKey()] || ''
}

// 输入区选项
const toolMode = ref('auto') // auto=自动（两者都启用）/ manual=手动
const useWebSearch = ref(false)
const useKnowledgeBase = ref(false)
const templateId = ref(0)
const templates = ref([])
const currentTool = ref('')
const statusElapsed = ref(0)
let statusTickId = null
watch(currentTool, (val) => {
  if (val) {
    statusElapsed.value = 0
    if (!statusTickId) {
      statusTickId = window.setInterval(() => {
        statusElapsed.value += 1
      }, 1000)
    }
  } else if (statusTickId) {
    window.clearInterval(statusTickId)
    statusTickId = null
  }
})
onBeforeUnmount(() => {
  if (statusTickId) window.clearInterval(statusTickId)
})

/** 结束当前打开的思考段（文本开始输出 / 工具开始 / 收尾时调用） */
function closeThinkingSegment() {
  if (!streamMsg || !streamMsg.tool_trace) return
  const trace = streamMsg.tool_trace
  const seg = trace[trace.length - 1]
  if (seg && seg.kind === 'thinking' && !seg.done) {
    seg.done = true
    seg.duration = Math.max(
      1,
      Math.round((Date.now() - (seg._startedAt || Date.now())) / 1000),
    )
  }
}
let abortController = null
let restoringTemplate = false // 切换会话时回填模板，不触发持久化写入

// 打字机/增量流状态（缓冲区、节奏、撤回、跳过标记）已抽到
// composables/useTypewriter.js（可单测：node frontend/scripts/check_typewriter.mjs）；
// 本组件只持有"目标消息"并注入滚动副作用。
let streamMsg = null
const typewriter = useTypewriter({
  getStreamMsg: () => streamMsg,
  scrollToBottom,
})
// 兼容别名：其余逻辑沿用拆分前的名字（状态本体在 composable 里，单一属主）
const pendingText = typewriter.pendingText
const flushTypewriter = typewriter.flush
const dropStreamedRound = typewriter.drop
const startTypewriter = typewriter.start
// 原生挂起（interrupt）的审批批次：{ conversationId, msg, submitted }
let suspended = null

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
// 长记忆折叠：默认两行截断，点击展开
const expandedMemoryIds = ref(new Set())
function toggleMemoryExpand(id) {
  const next = new Set(expandedMemoryIds.value)
  next.has(id) ? next.delete(id) : next.add(id)
  expandedMemoryIds.value = next
}
function fmtMemoryDate(v) {
  if (!v) return '未知'
  const d = new Date(v)
  if (Number.isNaN(d.getTime())) return '未知'
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`
}
// 是否连归档记忆一起显示（归档 = 停用但保留，不参与召回/导出）
const showArchived = ref(false)

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
      { icon: Collection, text: '知识库里的文档主要讲了什么？' },
      { icon: MagicStick, text: '帮我写一封简洁的工作周报' },
      { icon: Position, text: '今天有什么值得关注的新闻？' },
    ]
  }
}

function toolName(name) {
  if (name && name.startsWith('mcp_')) {
    return `MCP · ${name.slice(4).replace(/_/g, ' ')}`
  }
  return (
    {
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
      skill_lookup: '查找技能',
      write_skill: '沉淀技能',
      dispatch_subtasks: '并行子任务',
      enter_plan_mode: '进入计划模式',
      exit_plan_mode: '提交计划',
      verify: '验证结果',
    }[name] || name
  )
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
  if (!p || p.status !== 'pending') return
  const approve = optionIndex !== 1
  const remember = optionIndex === 2
  // 记录决定；整批决定齐了由 maybeSubmitSuspension 一次性提交（/agent/resume）
  p._decision = {
    call_id: p.id,
    approve,
    reason: p.reason || '',
    remember_session: false,
    remember_forever: !!remember && approve,
  }
  p.status = approve ? 'approved' : 'denied'
  if (remember && approve) {
    p.reason = (p.reason ? `${p.reason}；` : '') + '已记住，下次不再询问'
  }
  await maybeSubmitSuspension()
}

/** 待审批的请求：当前查看会话的消息里第一个 pending 卡片。
 * 不能用 streamMsg（它是"最近一次发送"的流）：并行会话时用户在 A 会话
 * 看审批卡，键盘快捷会落到 B 会话的流上甚至失效；按会话扫描才对。 */
function pendingPermission() {
  if (!messages.value) return null
  for (const msg of messages.value) {
    const p = (msg.permissions || []).find((x) => x.status === 'pending')
    if (p) return p
  }
  return null
}

/** 按 id 找待审批项：键盘焦点在哪个卡片上，就应该操作哪个卡片 */
function findPendingPermission(id) {
  if (!id || !messages.value) return null
  for (const msg of messages.value) {
    const p = (msg.permissions || []).find(
      (x) => x.id === id && x.status === 'pending',
    )
    if (p) return p
  }
  return null
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
    // 注意：data-perm-id 在外层包装 div 上（.permission-card 是其子元素），
    // 选择器必须用后代写法，否则查不到元素、focus() 静默失败，
    // 导致键盘（数字/Enter）全部无效
    const card = document.querySelector(`[data-perm-id="${p.id}"] .permission-opts`)
    card?.focus()
  })
}

/** 全局键盘：数字键在任何位置都可用（不会抢走输入框焦点，也避免"键盘按不动"）；
 * 方向键/Enter/Esc 需要焦点在审批卡片内，防止误触批准。 */
function onPermissionKeydown(event) {
  // 在输入框里打字时，数字和 Enter 保持文本语义
  const tag = event.target?.tagName
  if (tag === 'INPUT' || tag === 'TEXTAREA') return
  // 焦点在哪个卡片上就操作哪个（多张待审批卡片时不能都落到第一张）
  const cardEl = event.target.closest?.('[data-perm-id]')
  const p = cardEl
    ? findPendingPermission(cardEl.getAttribute('data-perm-id'))
    : pendingPermission()
  if (!p) return
  const count = p.rememberable ? 3 : 2
  if (event.key >= '1' && event.key <= String(count)) {
    event.preventDefault()
    decidePermission(p, Number(event.key) - 1)
    return
  }
  // 其余按键（含 Enter 批准）只在卡片内生效：焦点不在卡片里时按 Enter
  // 多半是在做别的事，误批准写文件/执行命令的代价太高
  if (!cardEl) return
  if (event.key === 'ArrowDown' || event.key === 'ArrowRight') {
    event.preventDefault()
    p._focusIndex = (p._focusIndex + 1) % count
  } else if (event.key === 'ArrowUp' || event.key === 'ArrowLeft') {
    event.preventDefault()
    p._focusIndex = (p._focusIndex + count - 1) % count
  } else if (event.key === 'Enter') {
    // 焦点在某个选项按钮上时交给原生激活（焦点在"拒绝"上按 Enter 就该拒绝）
    if (event.target.closest?.('.perm-opt')) return
    event.preventDefault()
    decidePermission(p, p._focusIndex ?? 0)
  } else if (event.key === 'Escape') {
    event.preventDefault()
    questionInput.value?.focus()
  }
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
  // 生成中也允许新建：流式消息对象独立于会话数组，切走不影响后台生成
  saveDraft()
  if (activeId.value != null) sessionMessagesCache[activeId.value] = messages.value
  activeId.value = null
  currentTitle.value = '新对话'
  messages.value = []
  restoreDraft()
}

async function switchSession(session) {
  if (session.id === activeId.value) return
  // 保存当前会话的草稿与内存消息（含生成中的内容），再切换
  saveDraft()
  if (activeId.value != null) sessionMessagesCache[activeId.value] = messages.value
  activeId.value = session.id
  currentTitle.value = session.title
  // 窄屏（<1024）会话栏为抽屉模式：选中会话后自动收起（媒体查询见样式末尾）
  if (typeof window !== 'undefined' && window.innerWidth < 1024) sessionCollapsed.value = true
  restoringTemplate = true
  templateId.value = session.template_id || 0
  restoringTemplate = false
  if (sessionMessagesCache[session.id]) {
    // 本会话有内存缓存（正在生成 / 刚切走）：直接用，不重新拉取，
    // streamMsg 继续写入同一个消息对象，切回即看到完整内容
    messages.value = sessionMessagesCache[session.id]
    scrollToBottom(true)
  } else {
    try {
      const history = await getConversationMessages(session.id)
      messages.value = history.map(mapHistoryMessage)
      // 恢复 TodoWrite 任务清单，挂到最近一条助手消息（若有）
      try {
        const todoRes = await getTodos(session.id)
        const lastAssistant = [...messages.value].reverse().find((m) => m.role === 'assistant')
        if (lastAssistant && todoRes.todos?.length) lastAssistant.todos = todoRes.todos
      } catch {
        // 任务清单读取失败不阻塞会话加载
      }
      scrollToBottom(true)
      // 挂起中的审批不在 MySQL 里（finalize 才落库）：刷新/切换后按后端状态恢复审批卡
      restorePendingApproval(session.id)
    } catch (error) {
      ElMessage.error(error.response?.data?.detail || '加载历史消息失败')
    }
  }
  restoreDraft()
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
    // 清理该会话的内存缓存与草稿
    delete sessionMessagesCache[session.id]
    delete drafts.value[session.id]
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

/** 后端历史消息 → 前端消息对象（含 id：消息级回退要用） */
function mapHistoryMessage(m) {
  return {
    id: m.id,
    role: m.role,
    content: m.content,
    tool_trace: (m.tool_trace || []).map((t) => ({ ...t, _open: false })),
    sources: m.sources || [],
    permissions: [],
    todos: [],
    plan: [],
    planTotal: 0,
    _sourcesOpen: false,
    _streaming: false,
  }
}

/** 消息级回退：删除该消息及其之后的所有对话，内容放回输入框重新编辑 */
async function rewindTo(msg) {
  if (loading.value || !activeId.value || !msg.id) return
  try {
    await ElMessageBox.confirm(
      '将删除这条消息及其之后的所有对话记录，并把内容放回输入框，确定回退？',
      '回退确认',
      { type: 'warning', confirmButtonText: '回退', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  try {
    const result = await rewindConversation(activeId.value, msg.id)
    const history = await getConversationMessages(activeId.value)
    messages.value = history.map(mapHistoryMessage)
    question.value = msg.content || ''
    const extra = result.summary_reset ? '，滚动摘要已重置' : ''
    ElMessage.success(`已回退（删除 ${result.removed} 条消息${extra}），内容已放回输入框`)
    nextTick(() => questionInput.value?.focus())
  } catch (error) {
    ElMessage.error(error.response?.data?.detail || '回退失败')
  }
}

// ---------------- 上下文占用面板（/context · /compact） ----------------

const contextVisible = ref(false)
const contextStats = ref(null)
const compacting = ref(false)

// 下一轮实际发给模型的历史条数（后端 effective_history_messages；
// 会话累计 message_count 含已并入滚动摘要的部分，两者通常不同）
const ctxEffectiveMessages = computed(
  () =>
    contextStats.value?.effective_history_messages ??
    contextStats.value?.message_count ??
    0,
)
// 压缩触发水位（1.5×上限）：窗口超过它后压缩到上限条数，
// 画进度条按水位对比，避免"70/60"这类超 100% 的显示
const ctxHighWater = computed(
  () =>
    contextStats.value?.history_high_water_messages ??
    contextStats.value?.history_max_messages ??
    1,
)

function barPct(cur, cap) {
  if (!cap) return '0%'
  return `${Math.min(100, Math.round((cur / cap) * 100))}%`
}

function fmtCtxTokens(n) {
  if (n == null) return '-'
  if (n >= 1000000) return `${(n / 1000000).toFixed(1)}M`
  if (n >= 1000) return `${(n / 1000).toFixed(1)}k`
  return String(n)
}

// 请求序号：关闭/重开对话框时使进行中的请求失效，防止旧请求
// 超时后误关新打开的对话框（竞态）
let contextReqId = 0

async function openContextDialog() {
  if (!activeId.value) return
  const reqId = ++contextReqId
  contextVisible.value = true
  contextStats.value = null
  try {
    const stats = await getAgentContext(activeId.value)
    if (reqId === contextReqId) contextStats.value = stats
  } catch (error) {
    if (reqId !== contextReqId) return // 过期请求（用户已关闭/重开），忽略
    ElMessage.error(error.response?.data?.detail || '读取上下文统计失败')
    contextVisible.value = false
  }
}

function onContextClose() {
  // 关闭时作废进行中的请求：15s 超时回调不会再触碰对话框状态
  contextReqId++
}

// ---------------- 会话快照时间线（原生检查点，时间旅行） ----------------

const timelineVisible = ref(false)
const timelineLoading = ref(false)
const timelineItems = ref([])

async function openTimeline() {
  if (!activeId.value) return
  timelineVisible.value = true
  timelineLoading.value = true
  try {
    const res = await getConversationTimeline(activeId.value)
    timelineItems.value = res.items || []
  } catch (error) {
    ElMessage.error(error.response?.data?.detail || '读取快照失败')
    timelineItems.value = []
  } finally {
    timelineLoading.value = false
  }
}

function formatCheckpointTime(ts) {
  if (!ts) return '—'
  try {
    return new Date(ts).toLocaleString('zh-CN', { hour12: false })
  } catch {
    return String(ts)
  }
}

async function rollbackTo(cp) {
  if (!activeId.value || loading.value) return
  try {
    await ElMessageBox.confirm(
      `回滚把消息历史与任务清单恢复到该快照（${formatCheckpointTime(cp.created_at)}，` +
        `消息 ${cp.messages} 条）；比它新的消息会从会话里删除，仍可从更新的快照回滚回来。确定继续？`,
      '回滚到快照',
      { type: 'warning', confirmButtonText: '回滚', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  try {
    const res = await rollbackConversation(activeId.value, cp.checkpoint_id)
    const history = await getConversationMessages(activeId.value)
    messages.value = history.map(mapHistoryMessage)
    try {
      const todoRes = await getTodos(activeId.value)
      const lastAssistant = [...messages.value]
        .reverse()
        .find((m) => m.role === 'assistant')
      if (lastAssistant && todoRes.todos?.length) lastAssistant.todos = todoRes.todos
    } catch {
      // 任务清单刷新失败不影响回滚结果
    }
    timelineVisible.value = false
    ElMessage.success(`已回滚（恢复 ${res.restored_messages} 条消息）`)
    scrollToBottom(true)
  } catch (error) {
    ElMessage.error(error.response?.data?.detail || '回滚失败')
  }
}

async function handleCompact() {
  if (!activeId.value || compacting.value) return
  compacting.value = true
  try {
    const result = await compactConversation(activeId.value)
    if (result.compacted) {
      ElMessage.success(
        `已压缩：保留最近 ${result.kept_messages} 条，${result.summarized_messages} 条并入摘要`,
      )
    } else {
      ElMessage.info(result.reason || '无需压缩')
    }
    contextStats.value = await getAgentContext(activeId.value)
  } catch (error) {
    ElMessage.error(error.response?.data?.detail || '压缩失败')
  } finally {
    compacting.value = false
  }
}

// ---------------- 引用溯源 ----------------

// 知识库文档列表缓存（旧索引来源无 relative_path 时按文件名反查）
let kbDocCache = { at: 0, list: [] }

const docViewerRef = ref(null)

async function openSourceDoc(src) {
  let path = src.relative_path
  if (!path) {
    try {
      if (Date.now() - kbDocCache.at > 60_000) {
        kbDocCache.list = await listDocuments()
        kbDocCache.at = Date.now()
      }
      const match = kbDocCache.list.find((d) => d.name === src.source)
      if (!match) {
        ElMessage.warning('未在知识库找到该文档（可能已删除；重建索引后可带精确路径）')
        return
      }
      path = match.relative_path
    } catch {
      ElMessage.error('获取知识库文档列表失败')
      return
    }
  }
  docViewerRef.value?.open(path, {
    page: src.page ?? null,
    anchorText: (src.content || '').slice(0, 120),
  })
}

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
    memories.value = await listMemories(showArchived.value ? 'all' : 'active')
  } catch (error) {
    ElMessage.error(error.response?.data?.detail || '加载记忆失败')
  }
}

async function handleArchiveMemory(memory) {
  const archived = memory.status !== 'archived'
  try {
    await updateMemory(memory.id, { status: archived ? 'archived' : 'active' })
    await loadMemories()
    ElMessage.success(archived ? '已归档（不再参与召回，可随时恢复）' : '已恢复')
  } catch (error) {
    ElMessage.error(error.response?.data?.detail || (archived ? '归档失败' : '恢复失败'))
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

/** 计划确认：把（可编辑的）计划作为决定经 /agent/resume 提交，同一 run 继续执行 */
function confirmPlan(msg) {
  const approval = msg.planApproval
  if (!approval || loading.value) return
  const steps = String(approval.text || '')
    .split('\n')
    .map((s) => s.trim())
    .filter(Boolean)
  if (!steps.length) {
    ElMessage.warning('计划不能为空')
    return
  }
  approval.steps = steps
  approval.state = 'confirmed'
  if (!suspended || suspended.submitted) return
  suspended.planPending = false
  suspended.submitted = true
  runResumeStream(
    suspended.conversationId,
    suspendedCards()
      .map((c) => c._decision)
      .filter(Boolean),
    false,
    { approved: true, steps, reason: '' },
  )
}

function cancelPlan(msg) {
  const approval = msg.planApproval
  if (!approval) return
  if (approval._suspended && suspended && !suspended.submitted) {
    // 原生挂起：拒绝计划 → 同一 run 继续（模型可调整后重新提交）
    approval.state = 'cancelled'
    suspended.planPending = false
    suspended.submitted = true
    runResumeStream(
      suspended.conversationId,
      suspendedCards()
        .map((c) => c._decision)
        .filter(Boolean),
      false,
      { approved: false, steps: [], reason: '用户取消了该计划' },
    )
    return
  }
  approval.state = 'cancelled'
}

/** 计划卡键盘：Enter / Ctrl+Enter 确认，Esc 取消（输入框内 Enter 保留换行语义） */
function onPlanCardKeydown(msg, event) {
  const approval = msg?.planApproval
  if (!approval || approval.state !== 'pending') return
  const inTextarea = event.target?.tagName === 'TEXTAREA'
  if ((event.ctrlKey || event.metaKey) && event.key === 'Enter') {
    event.preventDefault()
    confirmPlan(msg)
  } else if (event.key === 'Escape') {
    event.preventDefault()
    cancelPlan(msg)
  } else if (event.key === 'Enter' && !inTextarea) {
    event.preventDefault()
    confirmPlan(msg)
  }
}

/** 计划卡出现时把焦点移进去，键盘可直接 Enter 确认（不抢输入框的焦点） */
function focusPlanCard() {
  nextTick(() => {
    const card = document.querySelector('.plan-approval-box[data-plan-state="pending"]')
    if (!card) return
    if (document.activeElement === questionInput.value) {
      // 正在输入下一句话时不打断（点击卡片即可进入键盘操作）
      if ((questionInput.value?.value?.length || 0) > 0) return
    }
    card.focus?.()
  })
}

/**
 * 一轮问答/恢复流的事件处理器（send 与挂起恢复共用同一套渲染逻辑）。
 * 处理器通过模块级 streamMsg 写入目标助手消息：调用前先把它指到正确的气泡。
 *
 * 事件→状态 的映射本体已抽到 composables/useAgentStream.js（可单测，见
 * scripts/check_stream_handlers.mjs）；这里只注入组件作用域的依赖：
 * - state：只读的 ref/对象；
 * - actions：组件副作用回调；
 * - live：需要实时读写的组件级 let（值捕获会读到旧值）。
 */
function buildStreamHandlers() {
  return createStreamHandlers({
    state: { activeId, currentTitle, currentTool, streamMsg, pendingText },
    actions: {
      closeThinkingSegment,
      dropStreamedRound,
      flushTypewriter,
      focusPermissionCard,
      focusPlanCard,
      pushSuspendedCards,
      scrollToBottom,
      startTypewriter,
      toolName,
      upsertSession,
    },
    live: {
      getSkipNextToken: typewriter.getSkipNextToken,
      setSkipNextToken: typewriter.setSkipNextToken,
      getStreamedLen: typewriter.getStreamedLen,
      setStreamedLen: typewriter.setStreamedLen,
      setSuspended: (v) => {
        suspended = v
      },
    },
  })
}

/** 把挂起的待审批项渲染成卡片（挂起事件与刷新恢复共用同一形态） */
function pushSuspendedCards(msg, ops) {
  for (const op of ops || []) {
    msg.permissions.push({
      id: op.call_id,
      tool: op.name,
      summary: op.summary || `${toolName(op.name)} 需要确认`,
      args_text: formatArgs(op.arguments),
      args: op.arguments || {},
      status: 'pending',
      reason: '',
      rememberable: op.name === 'bash' || op.name === 'command_tool',
      _focusIndex: 0,
      _suspended: true,
      _decision: null,
    })
  }
}

/** 挂起批次是否还有未决定的操作/计划（用于禁用输入框/发送键） */
const hasSuspendedPending = computed(() =>
  messages.value.some(
    (m) =>
      (m.permissions || []).some((p) => p._suspended && !p._decision) ||
      (m.planApproval &&
        m.planApproval._suspended &&
        m.planApproval.state === 'pending'),
  ),
)

/** 当前挂起批次的全部卡片（跨消息收集，按 _suspended 标记） */
function suspendedCards() {
  const out = []
  for (const msg of messages.value) {
    for (const p of msg.permissions || []) {
      if (p._suspended) out.push(p)
    }
  }
  return out
}

/** 全部卡片都已决定 → 一次性提交，开恢复流续跑同一条回答 */
async function maybeSubmitSuspension() {
  if (!suspended || suspended.submitted) return
  if (suspended.planPending) return // 等计划卡确认/拒绝：由 confirmPlan/cancelPlan 提交
  const cards = suspendedCards()
  if (!cards.length || cards.some((c) => !c._decision)) return
  suspended.submitted = true
  await runResumeStream(
    suspended.conversationId,
    cards.map((c) => c._decision),
    false,
  )
}

/** 挂起期间点「全部拒绝并停止」：全拒 + stopped，收尾语义与停止生成一致 */
async function stopSuspended() {
  if (!suspended || suspended.submitted) return
  const cards = suspendedCards()
  for (const c of cards) {
    if (!c._decision) {
      c._decision = { call_id: c.id, approve: false, reason: '用户停止了回答' }
      c.status = 'denied'
    }
  }
  const plan = suspended.planPending
    ? { approved: false, steps: [], reason: '用户停止了回答' }
    : null
  suspended.submitted = true
  await runResumeStream(
    suspended.conversationId,
    cards.map((c) => c._decision),
    true,
    plan,
  )
}

/** 提交决定后恢复挂起运行：新 SSE 流继续写回同一条助手消息 */
async function runResumeStream(conversationId, decisions, stopped, plan = null) {
  if (!conversationId || loading.value) return
  flushTypewriter()
  if (suspended && suspended.msg) streamMsg = suspended.msg
  loading.value = true
  currentTool.value = '正在继续…'
  abortController = new AbortController()
  const body = {
    conversation_id: conversationId,
    decisions,
    stopped: !!stopped,
  }
  if (plan) body.plan = plan
  try {
    await resumeAgentChat(
      body,
      buildStreamHandlers(),
      { signal: abortController.signal },
    )
  } catch (error) {
    flushTypewriter()
    if (error.name === 'AbortError' || error.message === 'aborted') {
      dropStreamedRound()
      if (streamMsg) streamMsg.content = streamMsg.content || '（已停止生成）'
    } else {
      const detail = error.message || '恢复执行失败，请确认后端服务已启动'
      ElMessage.error(detail)
      if (streamMsg) streamMsg.content = streamMsg.content || `恢复失败：${detail}`
    }
  } finally {
    abortController = null
    loading.value = false
    currentTool.value = ''
    if (streamMsg) streamMsg._streaming = false
    suspended = null
    await loadSessions()
    scrollToBottom()
  }
}

/**
 * 刷新/切换会话后按后端状态恢复未完成的一轮。
 * 挂起/中断的一轮还没落库（finalize 才写 MySQL），不恢复的话会话会被闸门挡住。
 */
async function restorePendingApproval(conversationId) {
  if (!conversationId || loading.value) return
  try {
    const res = await getPendingApproval(conversationId)
    if (!res) return
    // 幂等：先清掉上一次恢复出的提示条/卡片载体
    messages.value = messages.value.filter((m) => !m._unfinishedBar)
    if (!res.pending && !res.interrupted) return
    let target = [...messages.value].reverse().find((m) => m.role === 'assistant')
    if (!target || res.interrupted) {
      target = {
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
        _streaming: false,
      }
      messages.value.push(target)
    }
    target.permissions = target.permissions || []
    streamMsg = target
    if (res.interrupted) {
      // 进程被杀/异常退出留下的半成品：给「继续执行/丢弃」入口
      target._unfinishedBar = true
      scrollToBottom()
      return
    }
    suspended = { conversationId, msg: target, submitted: false, planPending: false }
    pushSuspendedCards(target, res.ops)
    if (res.plan && (res.plan.steps || []).length) {
      suspended.planPending = true
      target.planApproval = {
        text: (res.plan.steps || []).map((s) => String(s)).join('\n'),
        steps: (res.plan.steps || []).map((s) => String(s)),
        summary: res.plan.summary || '',
        state: 'pending',
        _suspended: true,
      }
      focusPlanCard()
    } else {
      focusPermissionCard()
    }
    scrollToBottom()
  } catch {
    // 恢复失败不阻塞会话加载
  }
}

/** 续跑被中断的运行：新 SSE 流渲染进提示条所在气泡 */
async function continueInterrupted(msg) {
  if (!activeId.value || loading.value) return
  flushTypewriter()
  streamMsg = msg
  msg._unfinishedBar = false
  loading.value = true
  currentTool.value = '正在继续执行…'
  abortController = new AbortController()
  try {
    await continueAgentRun(activeId.value, buildStreamHandlers(), {
      signal: abortController.signal,
    })
  } catch (error) {
    flushTypewriter()
    if (error.name === 'AbortError' || error.message === 'aborted') {
      dropStreamedRound()
      if (streamMsg) streamMsg.content = streamMsg.content || '（已停止生成）'
    } else {
      const detail = error.message || '续跑失败，请确认后端服务已启动'
      ElMessage.error(detail)
      msg._unfinishedBar = true
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

/** 丢弃未完成的运行：删除原生检查点线程，解除闸门 */
async function abandonInterrupted(msg) {
  if (!activeId.value || loading.value) return
  try {
    await abandonRun(activeId.value)
    messages.value = messages.value.filter((m) => m !== msg)
    ElMessage.success('已丢弃未完成的运行，可以继续对话')
    scrollToBottom()
  } catch (error) {
    ElMessage.error(error.response?.data?.detail || '丢弃失败，请重试')
  }
}

async function send(options = {}) {
  flushTypewriter() // 清理上一轮残留（如异常中断）
  const hasImages = attachedImages.value.length > 0
  const text = question.value.trim() || (hasImages ? '请识别并描述这张图片' : '')
  if (!text || loading.value) return
  if (hasSuspendedPending.value) {
    ElMessage.warning('请先处理待确认的操作（批准或拒绝后自动继续）')
    return
  }
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
  typewriter.resetRound()
  question.value = ''
  // 发送成功：清空该会话草稿（内容已发出）
  drafts.value[currentDraftKey()] = ''
  attachedImages.value = []
  loading.value = true
  currentTool.value = '正在理解问题…'
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
  const payload = {
    question: text,
    images: imageDataUrls,
    conversation_id: activeId.value,
    tool_mode: effectiveMode,
    template_id: templateId.value || null,
  }
  try {
    await streamAgentChat(payload, buildStreamHandlers(), {
      signal: abortController.signal,
    })
  } catch (error) {
    flushTypewriter()
    if (error.name === 'AbortError' || error.message === 'aborted') {
      // 用户主动停止生成：撤回本轮未完成的正文（与后端"不落半截回答"一致），
      // 早前轮次已确认的正文保留
      dropStreamedRound()
      streamMsg.content = streamMsg.content || '（已停止生成）'
    } else {
      const detail = error.message || '请求失败，请确认后端服务已启动'
      ElMessage.error(detail)
      streamMsg.content = streamMsg.content || `请求失败：${detail}`
      if (detail.includes('待确认') || detail.includes('被中断')) {
        // 后端闸门：该会话还有挂起审批/中断未跑完 → 恢复卡片或提示条，让用户先处理
        await restorePendingApproval(activeId.value)
      }
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
  loadQuickProviders()
  // 窄屏（<1024）默认收起会话栏（抽屉模式，见样式媒体查询）
  if (window.innerWidth < 1024) sessionCollapsed.value = true
  window.addEventListener('keydown', onPermissionKeydown, true)
})

// keep-alive 回到本页时刷新供应商列表（设置页可能改过启用/名称）
onActivated(loadQuickProviders)

onBeforeUnmount(() => {
  window.removeEventListener('keydown', onPermissionKeydown, true)
  typewriter.dispose() // 停掉打字机定时器，避免回调打到已卸载组件上
})
</script>

<style scoped>
.chat-page {
  height: calc(100vh - 32px);
  display: flex;
  gap: 16px;
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
  border-radius: var(--radius-md);
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

/* 顶部动作按钮：中性灰、悬浮升蓝（蓝色留给发送等主动作） */
.chat-header :deep(.el-button.is-text) {
  color: var(--text-2);
}

.chat-header :deep(.el-button.is-text:hover) {
  color: var(--primary-text);
  background: var(--bg-hover);
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
  border-radius: var(--radius-sm);
  color: var(--text-3);
  cursor: pointer;
  transition: all 0.2s;
}

.header-toggle:hover {
  background: var(--bg-hover);
  color: var(--primary);
}

/* 供应商快捷切换 chip：当前「供应商 · 模型」，点击展开下拉 */
.model-switch-chip {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  max-width: 300px;
  padding: 4px 12px;
  border: 1px solid var(--border);
  border-radius: 999px;
  background: var(--bg-card);
  font-size: var(--font-xs);
  color: var(--text-1);
  cursor: pointer;
  white-space: nowrap;
  transition: all 0.2s;
}

.model-switch-chip:hover {
  border-color: var(--primary);
  color: var(--primary-text);
}

.model-switch-chip.busy {
  opacity: 0.6;
  pointer-events: none;
}

.model-switch-model {
  color: var(--text-3);
  overflow: hidden;
  text-overflow: ellipsis;
}

.chat-title {
  font-size: var(--font-lg);
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
  border-radius: var(--radius-lg);
  color: var(--text-on-brand);
  background: var(--primary-grad);
  box-shadow: 0 8px 24px color-mix(in srgb, var(--primary) 30%, transparent);
  margin-bottom: 20px;
}

.welcome-glyph {
  font-family: var(--font-display);
  font-size: 30px;
  font-weight: 700;
  line-height: 1;
  letter-spacing: 0.02em;
  transform: translateY(-1px);
}

.welcome-title {
  font-size: var(--font-2xl);
  font-weight: 700;
  margin-bottom: 8px;
}

.welcome-sub {
  color: var(--text-3);
  font-size: var(--font-base);
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
  border-radius: var(--radius-md);
  background: var(--bg-card-2);
  color: var(--text-2);
  font-size: var(--font-base);
  cursor: pointer;
  text-align: left;
  transition: border-color var(--dur-base) var(--ease-out),
    background var(--dur-base) var(--ease-out), box-shadow var(--dur-base) var(--ease-out),
    transform var(--dur-base) var(--ease-out);
}

.suggestion:hover {
  border-color: var(--primary);
  background: var(--bg-active);
  color: var(--primary);
  transform: translateY(-2px);
  box-shadow: 0 6px 16px color-mix(in srgb, var(--primary) 12%, transparent);
}

.suggestion-icon {
  flex-shrink: 0;
  width: 28px;
  height: 28px;
  display: flex;
  align-items: center;
  justify-content: center;
  border-radius: var(--radius-sm);
  background: var(--bg-active);
  color: var(--primary);
}

.suggestions {
  animation: fade-up var(--dur-slow) var(--ease-out) both;
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
  border-radius: var(--radius-md);
}

.avatar.user {
  background: var(--user-avatar-grad);
  color: var(--text-on-brand);
  box-shadow: 0 4px 10px rgba(255, 154, 90, 0.3);
}

.avatar.assistant {
  background: var(--primary-grad);
  color: var(--text-on-brand);
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
  /* 撑满整行：否则 msg-body 收缩到气泡宽度，气泡的 max-width:70%
     以自身宽度为参照（自引用收缩），实际换行宽度只有整行的 ~62% */
  flex: 1 1 auto;
  min-width: 0;
}

.user-bubble {
  max-width: 70%;
  padding: 10px 16px;
  border-radius: var(--radius-md) var(--radius-md) var(--radius-xs) var(--radius-md);
  background: var(--user-bubble-bg);
  border: 1px solid var(--user-bubble-border);
  color: var(--user-bubble-text);
  line-height: 1.7;
  white-space: pre-wrap;
  word-break: break-word;
  font-size: var(--font-base);
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
  border-radius: var(--radius-md);
  object-fit: cover;
  display: block;
  border: 1px solid color-mix(in srgb, var(--primary) 18%, transparent);
}

/* 消息级回退：悬停用户消息出现"回退"入口 */
.user-msg-group {
  display: flex;
  flex-direction: column;
  align-items: flex-end;
  /* 占满整行：否则 group 收缩到气泡宽度，气泡 max-width:70% 以自身为参照
     （自引用收缩），短文本也会被压窄换行（如两个字拆成两行） */
  width: 100%;
}

.user-rewind {
  margin-top: 3px;
  opacity: 0;
  transition: opacity 0.15s;
}

.msg.user:hover .user-rewind {
  opacity: 1;
}

.rewind-btn {
  display: inline-flex;
  align-items: center;
  gap: 3px;
  font-size: var(--font-xs);
  color: var(--text-3);
  cursor: pointer;
  user-select: none;
}

.rewind-btn:hover {
  color: var(--primary);
}

/* 上下文占用面板 */
.ctx-stats {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.ctx-row {
  display: flex;
  justify-content: space-between;
  align-items: baseline;
  font-size: var(--font-sm);
}

.ctx-label {
  color: var(--text-3);
}

.ctx-value {
  font-weight: 600;
  font-variant-numeric: tabular-nums;
}

.ctx-bar {
  height: 6px;
  border-radius: var(--radius-xs);
  background: var(--bg-card-3, rgba(128, 128, 128, 0.12));
  overflow: hidden;
}

.ctx-bar i {
  display: block;
  height: 100%;
  border-radius: var(--radius-xs);
  background: linear-gradient(90deg, var(--primary), var(--success));
  transition: width 0.3s;
}

.ctx-note {
  margin-top: 4px;
  font-size: var(--font-xs);
  line-height: 1.6;
  color: var(--text-3);
}

.ctx-sub {
  font-size: var(--font-2xs);
  color: var(--text-3);
}

.assistant-content {
  position: relative;
  padding: 14px 18px;
  border-radius: var(--radius-md) var(--radius-md) var(--radius-md) var(--radius-xs);
  background: var(--bg-card);
  border: 1px solid var(--border);
  box-shadow: var(--shadow-soft);
}

.status-time {
  color: var(--text-3);
  font-weight: 400;
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
  border-radius: var(--radius-sm);
}

.copy-btn:hover {
  color: var(--primary);
  background: var(--bg-active);
}

.typing-tool {
  color: var(--primary);
  font-size: var(--font-base);
}

.status-chip {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  padding: 5px 12px;
  border-radius: var(--radius-pill);
  background: var(--bg-active);
  color: var(--primary);
  font-size: var(--font-sm);
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

/* 挂起条基样式：供「中断未跑完」条复用（审批卡内的一份随 PermissionCard 组件走） */
.suspension-bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  padding: 6px 10px;
  border-radius: var(--radius-sm);
  border: 1px dashed var(--border);
  background: var(--bg-soft, transparent);
  font-size: var(--font-xs);
  color: var(--text-2);
}

/* 中断未跑完（进程被杀/异常退出）：警示色 */
.interrupted-bar {
  border-color: var(--warning);
  color: var(--warning-text);
}

/* 会话快照时间线（抽屉） */
.timeline-hint {
  color: var(--text-2);
  font-size: var(--font-sm);
  padding: 8px 2px;
}

.timeline-list {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.timeline-item {
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  padding: 8px 10px;
}

.timeline-head {
  display: flex;
  align-items: center;
  gap: 8px;
}

.timeline-time {
  flex: 1;
  font-size: var(--font-sm);
  font-weight: 600;
}

.timeline-meta {
  margin-top: 4px;
  font-size: var(--font-xs);
  color: var(--text-2);
}

.timeline-plan {
  margin-top: 4px;
  font-size: var(--font-xs);
  color: var(--text-2);
  line-height: 1.5;
}

.suspension-actions {
  display: inline-flex;
  gap: 6px;
  flex-shrink: 0;
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

/* 开关配色由 theme.css 的 EP 映射层统一（--el-color-primary），不再就地打补丁 */

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
  border-radius: var(--radius-md);
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
  color: var(--text-on-brand);
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
  border-radius: var(--radius-md);
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
  font-size: var(--font-base);
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
  background: linear-gradient(135deg, var(--primary) 0%, var(--primary-2) 100%);
  border: none;
  box-shadow: 0 4px 10px color-mix(in srgb, var(--primary) 30%, transparent);
}

.send-btn.is-disabled {
  background: var(--border-strong);
  box-shadow: none;
}

.stop-btn {
  background: var(--danger);
  box-shadow: 0 4px 10px rgba(245, 108, 108, 0.3);
}

.stop-btn:hover {
  background: color-mix(in srgb, var(--danger) 88%, white);
}

.input-foot {
  margin-top: 8px;
  text-align: center;
  color: var(--text-3);
  font-size: var(--font-xs);
}

/* 失败提示 */
.failed-bar {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: 8px;
  padding: 8px 12px;
  border-radius: var(--radius-md);
  background: var(--danger-bg);
  border: 1px solid var(--danger-border);
  color: var(--danger);
  font-size: var(--font-sm);
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
  border-radius: var(--radius-md);
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
  font-size: var(--font-base);
}

.tpl-desc {
  color: var(--text-3);
  font-size: var(--font-xs);
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
  font-size: var(--font-sm);
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
  font-size: var(--font-sm);
}

.memory-list {
  max-height: 420px;
  overflow-y: auto;
}

.memory-item {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
  padding: 10px 12px;
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  margin-bottom: 8px;
}

.memory-item.is-archived {
  opacity: 0.6;
}

.memory-item:hover {
  border-color: color-mix(in srgb, var(--primary) 18%, var(--border));
}

.memory-main {
  flex: 1;
  min-width: 0;
  display: flex;
  align-items: flex-start;
  gap: 10px;
}

.memory-main .memory-tag {
  flex-shrink: 0;
}

.memory-body {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.memory-content {
  font-size: var(--font-base);
  color: var(--text-1);
  line-height: 1.6;
  /* 长路径/URL 等无空格长串要能折行，否则会溢出顶住右侧的编辑/归档/删除按钮 */
  overflow-wrap: anywhere;
  word-break: break-word;
  /* 默认两行截断、点击展开（条目多时保证列表可扫读） */
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
  cursor: pointer;
}

.memory-content.expanded {
  -webkit-line-clamp: unset;
  display: block;
  overflow: visible;
}

.memory-meta {
  display: flex;
  gap: 10px;
  font-size: var(--font-xs);
  color: var(--text-3);
}

.memory-archived-flag {
  color: var(--warning-text);
}

.memory-ops {
  flex-shrink: 0;
  display: flex;
  align-items: center;
  white-space: nowrap;
}

/* 操作降权：默认灰字（悬浮才出提示色，删除悬浮才红） */
.memory-ops :deep(.el-button.is-link) {
  color: var(--text-3);
}

.memory-ops :deep(.el-button.is-link:hover) {
  color: var(--primary-text);
}

.memory-ops :deep(.el-button--danger.is-link) {
  color: var(--text-3);
}

.memory-ops :deep(.el-button--danger.is-link:hover) {
  color: var(--danger-text);
}

.memory-archived-toggle {
  margin-right: 4px;
}

/* ============ 响应式（阶段4） ============ */
@media (max-width: 1024px) {

  .chat-header {
    overflow-x: auto;
  }
}

@media (max-width: 768px) {
  .chat-page {
    height: calc(100vh - 16px);
  }

  .messages {
    padding: 12px 10px;
  }

  .chat-header {
    padding: 0 10px;
  }

  .msg {
    max-width: 100%;
  }
}
/* ============ 界面风格细节（设置页切换；令牌见 theme.css）============ */
.welcome-title,
.chat-title {
  font-family: var(--font-display);
}

/* 静奢雅致：AI 头像去渐变（墨版），整体更静 */
:global(html[data-style='quiet'] .avatar.assistant) {
  background: var(--text-1);
  box-shadow: none;
}

/* 书斋暖读：圆形头像 + 墨色印记 + 琥珀动作 */
:global(html[data-style='salon'] .avatar) {
  border-radius: 50%;
}

:global(html[data-style='salon'] .avatar.assistant) {
  background: var(--text-1);
  box-shadow: none;
}

:global(html[data-style='salon'] .welcome-mark) {
  border-radius: 50%;
  background: var(--text-1);
  color: var(--bg-panel);
  box-shadow: 0 8px 22px color-mix(in srgb, var(--text-1) 26%, transparent);
}

:global(html[data-style='salon'] .welcome-mark .welcome-glyph) {
  font-family: var(--font-display);
}

:global(html[data-style='salon'] .send-btn) {
  background: var(--text-1);
  box-shadow: 0 4px 12px color-mix(in srgb, var(--text-1) 25%, transparent);
}

:global(html[data-style='salon'] .send-btn:hover:not(.is-disabled)) {
  background: var(--primary);
}

</style>
