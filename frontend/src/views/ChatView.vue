<template>
  <div class="chat-page">
    <!-- 左侧：会话列表（可折叠）。
         单列布局（Claude/Hermes）下传送进侧栏插槽，与导航同列。 -->
    <Teleport to="#nav-session-slot" :disabled="!singleColumn">
      <SessionList
        :collapsed="!singleColumn && sessionCollapsed"
        :sessions="sessions"
        :active-id="activeId"
        :loading="loading"
        :group-by-workspace="groupSessionsByWorkspace"
        :search-results="sessionSearchResults"
        :search-loading="sessionSearchLoading"
        @new-chat="startNewChat"
        @new-chat-in-workspace="startNewChatInWorkspace"
        @switch="handleSwitchSession"
        @rename="handleRename"
        @delete="handleDelete"
        @pin="handlePin"
        @search="onSessionSearch"
        @update:collapsed="sessionCollapsed = $event"
      />
    </Teleport>

    <ResizeHandle
      v-if="!singleColumn"
      var-name="--session-width"
      :min="184"
      :max="380"
      store-key="rag_session_width"
      label="拖动调整会话列表宽度"
    />

    <!-- 右侧：对话主区 -->
    <main class="chat-main">
      <header class="chat-header">
        <div class="header-left">
          <!-- 侧栏只有一个开关，放在标题左边：
               悬浮（收起）态 → 点击固定住侧栏；固定态 → 点击收起为悬浮 -->
          <el-tooltip :show-after="1000"
            v-if="canToggleSidebar"
            :content="sidebarFloating ? '固定侧栏' : '收起侧栏'"
            placement="bottom"
            :hide-after="800"
          >
            <button
              type="button"
              class="header-toggle"
              :class="{ floating: sidebarFloating }"
              :aria-label="sidebarFloating ? '固定侧栏' : '收起侧栏'"
              @click="toggleSidebarPinned"
            >
              <el-icon :size="16">
                <Expand v-if="sidebarFloating" />
                <Fold v-else />
              </el-icon>
            </button>
          </el-tooltip>
          <span class="chat-title">{{ currentTitle }}</span>
          <el-tooltip
            v-if="activeSession?.forked_from_conversation_id"
            :show-after="800"
            :content="forkSourceTooltip"
            placement="bottom"
            :hide-after="600"
          >
            <span class="fork-chip">
              <el-icon :size="12"><Share /></el-icon>
              分支
            </span>
          </el-tooltip>
          <button
            type="button"
            class="workspace-chip"
            :title="currentWorkspace ? `工作区：${currentWorkspace}` : '默认工作区（未绑定工作目录）'"
            @click="openWorkspaceDialog"
          >
            <el-icon :size="13"><FolderOpened /></el-icon>
            <span class="workspace-chip-name">{{ currentWorkspaceName }}</span>
          </button>
        </div>
        <div class="header-actions">
          <el-tooltip
            v-if="activeId || messages.length"
            :show-after="1000"
            content="会话面板"
            placement="bottom"
            :hide-after="800"
          >
            <button
              type="button"
              :class="['header-icon-btn', { active: inspectorOpen }]"
              aria-label="会话面板"
              @click="toggleInspector"
            >
              <el-icon :size="16"><DataBoard /></el-icon>
            </button>
          </el-tooltip>
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

          <!-- 思考强度快捷切换（当前对话供应商；对后续消息生效） -->
          <el-dropdown
            trigger="click"
            :disabled="loading || thinkingBusy || !quickActiveId"
            @command="onThinkingSwitch"
          >
            <span
              :class="['model-switch-chip', 'thinking-chip', { busy: thinkingBusy }]"
              title="思考强度（对后续消息生效）"
            >
              <el-icon :size="12"><MagicStick /></el-icon>
              思考 {{ quickThinkingLabel }}
            </span>
            <template #dropdown>
              <el-dropdown-menu>
                <el-dropdown-item command="off">关闭思考</el-dropdown-item>
                <el-dropdown-item
                  v-for="opt in thinkingOptions"
                  :key="opt.value"
                  :command="opt.value"
                  :disabled="quickThinkingEnabled && quickThinkingEffort === opt.value"
                >
                  {{ opt.label }}
                </el-dropdown-item>
              </el-dropdown-menu>
            </template>
          </el-dropdown>

          <el-tooltip :show-after="1000" content="上下文占用" placement="bottom" :hide-after="800">
            <button
              type="button"
              :class="['header-icon-btn', { disabled: !activeId }]"
              aria-label="上下文占用"
              @click="activeId && openContextDialog()"
            >
              <el-icon :size="16"><Histogram /></el-icon>
            </button>
          </el-tooltip>
          <el-tooltip :show-after="1000" content="会话快照" placement="bottom" :hide-after="800">
            <button
              type="button"
              :class="['header-icon-btn', { disabled: !activeId }]"
              aria-label="会话快照"
              @click="activeId && openTimeline()"
            >
              <el-icon :size="16"><Clock /></el-icon>
            </button>
          </el-tooltip>
          <el-dropdown trigger="click">
            <div class="header-icon-btn header-more-btn" title="更多">
              <el-icon :size="16"><MoreFilled /></el-icon>
            </div>
            <template #dropdown>
              <el-dropdown-menu>
                <el-dropdown-item @click="openTemplateManager">
                  <el-icon><Operation /></el-icon>提示词模板
                </el-dropdown-item>
                <el-dropdown-item @click="openMemories">
                  <el-icon><Memo /></el-icon>记忆
                </el-dropdown-item>
                <el-dropdown-item @click="$router.push('/runs')">
                  <el-icon><DataAnalysis /></el-icon>运行记录
                </el-dropdown-item>
              </el-dropdown-menu>
            </template>
          </el-dropdown>
        </div>
      </header>

      <div class="chat-body">
      <div class="chat-column">
      <div
        ref="messagesRef"
        class="messages"
        @click="handleMessageClick"
        @scroll="onMessagesScroll"
      >
        <!-- 欢迎页：四种样式共享结构，差异由 html[data-home] 与文案控制 -->
        <div v-if="!messages.length && !loading" class="welcome">
          <div class="welcome-inner">
            <div class="welcome-eyebrow">
              <span class="welcome-eyebrow-dot" aria-hidden="true"></span>
              {{ welcomeEyebrow }}
            </div>
            <div class="welcome-mark">
              <span class="welcome-glyph">问</span>
            </div>
            <div class="welcome-heading">
              <h2 class="welcome-title">{{ welcomeGreeting }}</h2>
              <p class="welcome-sub">{{ welcomeSubtitle }}</p>
            </div>
            <div v-if="suggestions.length" class="suggestions" :data-count="suggestions.length">
              <button
                v-for="(s, i) in suggestions"
                :key="s.text"
                type="button"
                class="suggestion"
                :style="{ '--suggestion-index': i }"
                @click="ask(s)"
              >
                <span class="suggestion-icon">
                  <el-icon :size="16"><component :is="s.icon" /></el-icon>
                </span>
                <span class="suggestion-body">
                  <span class="suggestion-text">{{ s.text }}</span>
                  <span class="suggestion-desc">{{ s.desc }}</span>
                </span>
                <span class="suggestion-arrow" aria-hidden="true">
                  <el-icon :size="14"><ArrowRight /></el-icon>
                </span>
              </button>
            </div>
            <!-- 工作台样式：把导航入口也铺在首屏，减少一次侧栏寻找 -->
            <div v-if="homeStyle === 'studio'" class="quick-actions">
              <button
                v-for="action in quickActions"
                :key="action.to"
                type="button"
                class="quick-action"
                @click="router.push(action.to)"
              >
                <span class="quick-action-icon">
                  <el-icon :size="16"><component :is="action.icon" /></el-icon>
                </span>
                <span class="quick-action-body">
                  <span class="quick-action-label">{{ action.label }}</span>
                  <span class="quick-action-desc">{{ action.desc }}</span>
                </span>
              </button>
            </div>
            <!-- 静态能力标签（纯展示，不联动开关状态） -->
            <div class="capability-chips">
              <span class="capability-chip" title="回答附带知识库原文出处">
                <el-icon :size="13"><Collection /></el-icon>知识库检索
              </span>
              <span class="capability-chip" title="需要最新信息时自动联网检索">
                <el-icon :size="13"><Position /></el-icon>联网搜索
              </span>
              <span class="capability-chip" title="支持图片理解与知识库图片问答">
                <el-icon :size="13"><Picture /></el-icon>图片理解
              </span>
              <span class="capability-chip" title="可读写文件、执行命令并回读验证">
                <el-icon :size="13"><Operation /></el-icon>工具执行
              </span>
              <span class="capability-chip" title="多轮上下文、滚动摘要与长期记忆">
                <el-icon :size="13"><Clock /></el-icon>多轮记忆
              </span>
            </div>
          </div>
        </div>

        <!-- 消息列表：key 用数据库 id / 本地 _localKey——rewind 会从中间 splice，
             数组下标作 key 会让 Vue 复用错组件（MarkdownContent 内部状态串台） -->
        <div
          v-for="(msg, index) in messages"
          :key="msg.id || msg._localKey || index"
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
                <div v-if="msg.files && msg.files.length" class="user-files">
                  <span
                    v-for="(file, i) in msg.files"
                    :key="i"
                    class="user-file-chip"
                  >
                    <el-icon :size="12"><Document /></el-icon>
                    {{ file.name }}
                  </span>
                </div>
                <span v-if="msg.content">{{ msg.content }}</span>
              </div>
              <!-- 消息级操作：回退重编 / 从此消息创建分支会话 -->
              <div v-if="msg.id && !loading" class="user-rewind">
                <el-tooltip :show-after="1000" content="回退到此消息（删除其后对话并重新编辑）" placement="top" :hide-after="800">
                  <span class="rewind-btn" @click="rewindTo(msg)">
                    <el-icon :size="12"><RefreshLeft /></el-icon>
                    回退
                  </span>
                </el-tooltip>
                <el-tooltip :show-after="1000" content="从此消息创建分支会话（复制此消息及之前的对话）" placement="top" :hide-after="800">
                  <span class="rewind-btn" @click="forkFrom(msg)">
                    <el-icon :size="12"><Share /></el-icon>
                    分支
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
                clickable
                @open-panel="openPlanPanel"
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
              <!-- 向用户提问卡：模型调用 ask_user_question 后渲染 -->
              <QuestionCard
                v-if="msg.questionRequest && msg.questionRequest.state !== 'cancelled'"
                :request="msg.questionRequest"
                :loading="loading"
                @submit="(answers) => submitQuestions(msg, answers)"
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

              <!-- 工具调用轨迹先于最终回答：回答应落在整段执行过程的最下方 -->
              <ToolTraceList
                v-if="msg.tool_trace && msg.tool_trace.length"
                :items="msg.tool_trace"
                :tool-name="toolName"
                @open-detail="openInspectorDetail"
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

              <div v-if="msg.content" class="assistant-content">
                <MarkdownContent :content="msg.content" :plain="!!msg._streaming" />
                <div class="msg-actions">
                  <el-tooltip :show-after="1000"
                    v-if="index === messages.length - 1"
                    content="重新生成"
                    placement="top"
                   :hide-after="800">
                    <el-icon class="copy-btn" @click="regenerateLast">
                      <RefreshRight />
                    </el-icon>
                  </el-tooltip>
                  <el-tooltip :show-after="1000" content="复制回答" placement="top" :hide-after="800">
                    <el-icon class="copy-btn" @click="copyText(msg.content)">
                      <CopyDocument />
                    </el-icon>
                  </el-tooltip>
                </div>
              </div>
              <div
                v-else-if="loading && index === messages.length - 1"
                class="assistant-content"
              >
                <span
                  v-if="activityLabel"
                  class="status-chip"
                  role="status"
                  aria-live="polite"
                >
                  <span class="status-spinner"></span>
                  {{ activityLabel }}
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

            <!-- 引用来源 -->
            <SourceList
              v-if="msg.sources && msg.sources.length"
              v-model:open="msg._sourcesOpen"
              :sources="msg.sources"
              @open-doc="openSourceDoc"
            />
          </div>
        </div>

        <!-- 正文已开始后的唯一活动行（空正文阶段由消息内 chip 承担，两者不同时出现） -->
        <div
          v-if="loading && streamCharCount > 0"
          class="run-activity"
          role="status"
          aria-live="polite"
        >
          <span class="status-spinner"></span>
          <span class="run-activity-text">{{ activityLabel || '处理中…' }}</span>
          <span class="run-activity-time">{{ statusElapsed }}s</span>
        </div>
      </div>

      <!-- 输入区（composer：textarea 在上、控制行在下） -->
      <div
        class="input-area"
        :class="{ 'drag-over': dragOver }"
        @dragover.prevent="onDragOver"
        @dragleave.prevent="onDragLeave"
        @drop.prevent="onDrop"
      >
        <!-- 上滑查看历史时浮现：一键回到底部继续跟随生成 -->
        <button
          v-if="showJumpToLatest"
          type="button"
          class="jump-latest"
          @click="jumpToLatest"
        >
          <el-icon :size="13"><Bottom /></el-icon>
          回到最新
        </button>

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

        <!-- 已附加文档预览（文件名 + 大小 + 提取字符数） -->
        <div v-if="attachedFiles.length" class="file-preview-row">
          <div
            v-for="file in attachedFiles"
            :key="file.id"
            :class="['file-chip', { warn: file.warning }]"
            :title="file.warning || file.name"
          >
            <el-icon :size="14"><Document /></el-icon>
            <span class="file-chip-name">{{ file.name }}</span>
            <span class="file-chip-meta">
              {{ formatFileSize(file.size) }}
              <template v-if="file.chars"> · {{ formatCount(file.chars) }} 字符</template>
            </span>
            <el-icon class="file-chip-remove" @click="removeAttachment(file)">
              <Close />
            </el-icon>
          </div>
        </div>

        <!-- 查看器划选的引用原文：随本轮问题一起发送 -->
        <div v-if="quote" class="quote-chip">
          <el-icon :size="13"><Document /></el-icon>
          <span class="quote-chip-text">{{ quotePreview }}</span>
          <span class="quote-chip-source">
            {{ quote.source ? quote.source.split('/').pop() : '选中内容' }}
            <template v-if="quote.page"> · p{{ quote.page }}</template>
          </span>
          <el-icon class="quote-chip-close" title="移除引用" @click="quote = null">
            <Close />
          </el-icon>
        </div>

        <!-- 下一步建议：Claude Code 式 ghost 文本（输入框内，Tab 采纳） -->

        <div class="input-box">
          <!-- 输入框 @ 引用的知识库文档候选 -->
          <div v-if="mention.open" class="mention-pop" role="listbox" aria-label="引用知识库文档">
            <div class="mention-head">引用知识库文档（↑↓ 选择，Enter 确认）</div>
            <div v-if="mention.loading" class="mention-empty">加载文档列表…</div>
            <div v-else-if="!mentionItems.length" class="mention-empty">没有匹配的文档</div>
            <button
              v-for="(item, i) in mentionItems"
              :key="item.path"
              type="button"
              :class="['mention-item', { active: i === mention.index }]"
              @mousedown.prevent="pickMention(item)"
              @mouseenter="mention.index = i"
            >
              <span class="mention-name">{{ item.name }}</span>
              <span class="mention-path">{{ item.path }}</span>
            </button>
          </div>
          <el-input
            ref="questionInput"
            v-model="question"
            type="textarea"
            :rows="2"
            resize="none"
            :placeholder="ghostVisible ? '' : '输入你的问题，Enter 发送，Shift+Enter 换行 · @ 引用文档 · 支持粘贴/拖拽图片与文档'"
            @keydown="onComposerKeydown"
            @input="onComposerInput"
            @blur="closeMention"
            @paste="onPaste"
          />
          <!-- ghost 建议：叠在空输入框的文本起点上方，打字即消失 -->
          <div v-if="ghostVisible" class="ghost-suggestion" aria-hidden="true">
            <span class="ghost-suggestion-text">{{ ghostSuggestion }}</span>
            <span
              class="ghost-suggestion-hint"
              @mousedown.prevent="acceptGhostSuggestion"
            >
              {{ nextSuggestionsLoading ? 'Tab 采纳 · 生成中' : 'Tab 采纳' }}
            </span>
          </div>
          <div class="composer-bar">
            <div class="composer-chips">
              <button
                type="button"
                :class="['composer-chip', { on: toolMode === 'auto' }]"
                :disabled="loading"
                title="自动模式：由模型自行决定检索与工具"
                @click="toolMode = toolMode === 'auto' ? 'manual' : 'auto'"
              >
                <el-icon :size="13"><MagicStick /></el-icon>自动
              </button>
              <button
                type="button"
                :class="['composer-chip', { on: useWebSearch }]"
                :disabled="loading || toolMode === 'auto'"
                @click="useWebSearch = !useWebSearch"
              >
                <el-icon :size="13"><Position /></el-icon>联网搜索
              </button>
              <button
                type="button"
                :class="['composer-chip', { on: useKnowledgeBase }]"
                :disabled="loading || toolMode === 'auto'"
                @click="useKnowledgeBase = !useKnowledgeBase"
              >
                <el-icon :size="13"><Collection /></el-icon>知识库
              </button>

              <!-- 敏感操作确认模式快捷切换（对后续消息生效） -->
              <el-dropdown
                trigger="click"
                :disabled="loading || modeBusy"
                @command="onPermissionModeSwitch"
              >
                <button
                  type="button"
                  :class="[
                    'composer-chip',
                    'mode-chip',
                    {
                      on: quickPermissionMode !== 'ask',
                      warn: quickPermissionMode === 'allow',
                    },
                  ]"
                  :disabled="loading || modeBusy"
                  title="敏感操作确认模式（对后续消息生效）"
                >
                  <el-icon :size="13"><Lock /></el-icon>{{ quickPermissionLabel }}
                </button>
                <template #dropdown>
                  <el-dropdown-menu>
                    <el-dropdown-item command="ask">每次确认</el-dropdown-item>
                    <el-dropdown-item command="acceptedits">自动接受文件编辑</el-dropdown-item>
                    <el-dropdown-item command="allow" divided>自动批准（谨慎）</el-dropdown-item>
                  </el-dropdown-menu>
                </template>
              </el-dropdown>
            </div>
            <el-select
              v-model="templateId"
              class="composer-template"
              placeholder="模板"
              :disabled="loading || hasSuspendedPending"
            >
              <el-option label="默认模板" :value="0" />
              <el-option
                v-for="t in availableTemplates"
                :key="t.id"
                :label="t.name"
                :value="t.id"
              />
            </el-select>
            <el-tooltip :show-after="1000"
              content="限定知识库检索范围（单文档 / 整本书）"
              placement="top"
             :hide-after="800">
              <button
                type="button"
                :class="['composer-chip', 'composer-scope', { on: !!kbScope }]"
                :disabled="loading || hasSuspendedPending"
                @click="openScopeDialog"
              >
                <el-icon :size="13"><Document /></el-icon>
                {{ kbScope ? `范围：${kbScope.label}` : '范围：全部' }}
                <span
                  v-if="kbScope"
                  class="scope-clear"
                  title="清除范围"
                  @click.stop="clearScope"
                >×</span>
              </button>
            </el-tooltip>
            <el-tooltip :show-after="1000"
              v-if="activeId && promptLockTag"
              :content="
                promptLockTag.upgradable
                  ? '有新提示词版本可应用（会断一次前缀缓存）'
                  : '本会话锁定的提示词版本'
              "
              placement="top"
             :hide-after="800">
              <el-tag
                :type="promptLockTag.type"
                size="small"
                class="composer-prompt-tag"
                @click="openSessionPrompt"
              >
                {{ promptLockTag.text }}
                <span v-if="promptLockTag.upgradable" class="prompt-dot" />
              </el-tag>
            </el-tooltip>
            <div class="composer-right">
              <el-tooltip :show-after="1000"
                content="上传图片或文档（支持 Ctrl+V 粘贴、拖拽）"
                placement="top"
               :hide-after="800">
                <button
                  type="button"
                  class="attach-btn"
                  aria-label="上传图片或文档"
                  @click="attachInputRef.click()"
                >
                  <el-icon :size="17"><Picture /></el-icon>
                </button>
              </el-tooltip>
              <input
                ref="attachInputRef"
                type="file"
                accept="image/*,.pdf,.doc,.docx,.xlsx,.csv,.epub,.txt,.md,.json,.py,.js,.ts,.vue,.html,.css,.xml,.yaml,.yml,.log,.sql,.sh,.ps1,.bat"
                multiple
                class="hidden-file-input"
                @change="onAttachFiles"
              />
              <el-button
                v-if="loading"
                circle
                class="send-btn stop-btn"
                @click="stop"
              >
                <el-icon :size="16"><VideoPause /></el-icon>
              </el-button>
              <el-button
                v-else
                type="primary"
                circle
                class="send-btn"
                :disabled="
                  hasSuspendedPending ||
                  uploadingAttachments ||
                  (!question.trim() && !attachedImages.length && !attachedFiles.length)
                "
                @click="send"
              >
                <el-icon :size="16"><Promotion /></el-icon>
              </el-button>
            </div>
          </div>
        </div>
        <div class="input-foot">
          <kbd>Enter</kbd> 发送 · <kbd>Shift</kbd> + <kbd>Enter</kbd> 换行 ·
          <kbd>Ctrl</kbd> + <kbd>V</kbd> 粘贴图片
        </div>
      </div>
      </div>

      <!-- 右侧会话面板（仿 Claude 桌面端）：拖拽条放在面板左侧，贴着聊天区 -->
      <ResizeHandle
        v-if="inspectorVisible"
        var-name="--inspector-width"
        :min="220"
        :max="480"
        store-key="rag_inspector_width"
        target="next"
        invert
        label="拖动调整会话面板宽度"
      />
      <WorkspacePanel
        v-if="inspectorVisible"
        ref="workspacePanelRef"
        :session="activeSession"
        :workspace="currentWorkspace"
        :workspace-name="currentWorkspaceName"
        :template-name="panelTemplateName"
        :prompt-tag="promptLockTag"
        :message-count="activeSession?.message_count || messages.length"
        :messages="messages"
        :active-id="activeId"
        :loading="loading"
        :plan="panelTask.plan"
        :todos="panelTask.todos"
        :plan-done="panelTask.planDone"
        :plan-total="panelTask.planTotal"
        :running="loading"
        :run-id="turn.runId || ''"
        :stats="contextStats"
        :compacting="compacting"
        :detail="inspectorDetail"
        @change-workspace="openWorkspaceDialog"
        @refresh-context="refreshPanelContext"
        @compact="handleCompact"
        @open-detail="openInspectorDetail"
        @close-detail="closeInspectorDetail"
      />
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
              <el-tag v-if="t.archived" size="small" type="warning">已归档</el-tag>
              <el-tag v-if="t.latest_version_no" size="small" effect="plain">
                v{{ t.latest_version_no }}
              </el-tag>
              <el-tag
                v-if="t.conversation_count"
                size="small"
                type="warning"
                effect="plain"
              >
                {{ t.conversation_count }} 个会话使用
              </el-tag>
            </div>
            <div class="tpl-desc">{{ t.description || '暂无描述' }}</div>
          </div>
          <div class="tpl-actions">
            <el-button size="small" text @click="openTemplateVersions(t)">版本</el-button>
            <el-button size="small" text @click="handleCloneTemplate(t)">克隆</el-button>
            <el-button v-if="!t.is_system" size="small" text @click="handleArchiveTemplate(t)">
              {{ t.archived ? '恢复' : '归档' }}
            </el-button>
            <el-button size="small" :disabled="t.archived" @click="openTemplateEdit(t)">编辑</el-button>
            <el-button
              v-if="!t.is_system && !t.conversation_count"
              size="small"
              type="danger"
              @click="handleDeleteTemplate(t)"
            >
              删除
            </el-button>
          </div>
        </div>
      </div>
      <div class="tpl-footer">
        <el-button type="primary" plain class="tpl-create" @click="openTemplateEdit(null)">
          新建模板
        </el-button>
        <el-button plain @click="openPromptFragments">系统片段</el-button>
        <el-button plain @click="auditPromptHealth">体检</el-button>
        <el-button plain @click="openDefaultTemplate">默认模板</el-button>
      </div>

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
          <el-form-item label="版本说明">
            <el-input
              v-model="templateForm.versionNote"
              placeholder="本次改动说明（可选，写入版本历史）"
            />
          </el-form-item>
        </el-form>
        <template #footer>
          <el-button text @click="normalizeTemplateBreaks">合并换行</el-button>
          <el-button text @click="openTemplatePreview">预览</el-button>
          <el-button @click="templateEditVisible = false">取消</el-button>
          <el-button type="primary" @click="saveTemplate">保存</el-button>
        </template>
      </el-dialog>
    </el-dialog>

    <el-dialog
      v-model="defaultTemplateVisible"
      title="默认助手（内置，只读）"
      width="760px"
      append-to-body
    >
      <div v-if="defaultTemplate" class="default-template-meta">
        {{ defaultTemplate.name }} · v{{ defaultTemplate.latest_version_no ?? '?' }}
        · {{ defaultTemplate.content.length }} 字
        <span v-if="defaultTemplate.description"> · {{ defaultTemplate.description }}</span>
      </div>
      <pre class="default-template-content">{{ defaultTemplate?.content }}</pre>
      <template #footer>
        <el-button @click="defaultTemplateVisible = false">关闭</el-button>
      </template>
    </el-dialog>

    <PromptVersionsDialog
      v-model="templateVersionsVisible"
      kind="template"
      :target-id="templateVersionTarget.id"
      :title="`版本历史 · ${templateVersionTarget.title}`"
      :readonly="templateVersionTarget.readonly"
      @changed="onPromptArtifactsChanged"
    />
    <PromptFragmentsPanel
      v-model="promptFragmentsVisible"
      @changed="onPromptArtifactsChanged"
    />
    <PromptPreviewDialog
      v-model="templatePreviewVisible"
      :request="templatePreviewRequest"
      :title="`模板预览 · ${templateForm.name || '未命名'}`"
    />
    <SessionPromptPanel
      v-model="sessionPromptVisible"
      :conversation-id="activeId"
      @applied="onSessionPromptApplied"
    />

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
          <span class="ctx-label">发送 token</span>
          <span class="ctx-value">
            {{ ctxSentTokens }} / {{ contextStats.token_budget }}
            <template v-if="ctxWindowTokens !== ctxSentTokens">
              （窗口估算 {{ ctxWindowTokens }}，已裁剪）
            </template>
          </span>
        </div>
        <div class="ctx-bar">
          <i :style="{ width: barPct(ctxSentTokens, contextStats.token_budget) }" />
        </div>
        <div class="ctx-row" v-if="contextStats.budget_source">
          <span class="ctx-label">预算来源</span>
          <span class="ctx-value">{{ contextStats.budget_source }}</span>
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

    <!-- 知识库范围选择：单文档 / 整本书 -->
    <el-dialog
      v-model="scopeDialogVisible"
      title="限定知识库范围"
      width="560px"
      append-to-body
    >
      <el-input
        v-model="scopeQuery"
        placeholder="搜索文档或书名"
        clearable
        class="scope-search"
      />
      <div class="scope-list">
        <el-checkbox-group v-model="scopeDraft" class="scope-group">
          <el-checkbox
            v-for="opt in filteredScopeOptions"
            :key="opt.key"
            :value="opt.key"
            class="scope-option"
          >
            <span class="scope-option-label">{{ opt.label }}</span>
            <span class="scope-option-meta">{{ opt.meta }}</span>
          </el-checkbox>
        </el-checkbox-group>
        <div v-if="!filteredScopeOptions.length" class="scope-empty">
          没有匹配的文档
        </div>
      </div>
      <template #footer>
        <el-button text @click="clearDraft">清空</el-button>
        <el-button @click="scopeDialogVisible = false">取消</el-button>
        <el-button type="primary" @click="applyScope">应用</el-button>
      </template>
    </el-dialog>

    <!-- 工作区选择：会话归组 + 文件/命令工具工作根 -->
    <el-dialog
      v-model="workspaceDialogVisible"
      title="选择工作区"
      width="560px"
      append-to-body
    >
      <div class="workspace-dialog">
        <div v-if="workspaceOptions.length" class="workspace-quick">
          <button
            v-for="opt in workspaceOptions"
            :key="opt.path"
            type="button"
            :class="['workspace-option', { active: workspaceDraft === opt.path }]"
            @click="workspaceDraft = opt.path"
          >
            <el-icon :size="13"><FolderOpened /></el-icon>
            <span class="workspace-option-name">{{ opt.name }}</span>
            <span class="workspace-option-path">{{ opt.path }}</span>
          </button>
          <button
            type="button"
            :class="['workspace-option', { active: !workspaceDraft }]"
            @click="workspaceDraft = ''"
          >
            <el-icon :size="13"><FolderOpened /></el-icon>
            <span class="workspace-option-name">默认工作区</span>
            <span class="workspace-option-path">不绑定工作目录</span>
          </button>
        </div>
        <div class="workspace-dialog-label">工作目录</div>
        <DirPicker v-model="workspaceDraft" placeholder="留空 = 默认工作区" />
        <div class="workspace-dialog-tip">
          工作区会作为文件与命令工具的工作根；会话列表按工作区分组。
        </div>
      </div>
      <template #footer>
        <el-button @click="workspaceDialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="workspaceSaving" @click="confirmWorkspace">
          确定
        </el-button>
      </template>
    </el-dialog>

    <!-- 引用「查看原文」文档查看器：划选后可解释/翻译/总结 -->
    <DocumentViewer ref="docViewerRef" @ask-selection="handleAskSelection" />
  </div>
</template>

<script setup>
import { computed, inject, nextTick, onActivated, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { pickGreeting } from '../utils/greetings.js'
import { ElMessage } from 'element-plus/es/components/message/index'
import {
  ArrowRight,
  Bottom,
  Clock,
  Collection,
  CopyDocument,
  DataAnalysis,
  DataBoard,
  Document,
  Expand,
  Fold,
  FolderOpened,
  Histogram,
  List,
  Lock,
  MagicStick,
  Memo,
  MoreFilled,
  Operation,
  Picture,
  Position,
  Promotion,
  RefreshLeft,
  RefreshRight,
  Share,
  User,
  VideoPause,
  WarningFilled,
  Close,
} from '@element-plus/icons-vue'
import MarkdownContent from '../components/MarkdownContent.vue'
import DirPicker from '../components/DirPicker.vue'
import DocumentViewer from '../components/DocumentViewer.vue'
import WorkspacePanel from '../components/chat/WorkspacePanel.vue'
import PermissionCard from '../components/chat/PermissionCard.vue'
import PlanApprovalCard from '../components/chat/PlanApprovalCard.vue'
import QuestionCard from '../components/chat/QuestionCard.vue'
import ToolTraceList from '../components/chat/ToolTraceList.vue'
import SourceList from '../components/chat/SourceList.vue'
import TodoPanel from '../components/chat/TodoPanel.vue'
import SessionList from '../components/chat/SessionList.vue'
import ResizeHandle from '../components/ResizeHandle.vue'
import PromptFragmentsPanel from '../components/prompt/PromptFragmentsPanel.vue'
import PromptPreviewDialog from '../components/prompt/PromptPreviewDialog.vue'
import PromptVersionsDialog from '../components/prompt/PromptVersionsDialog.vue'
import SessionPromptPanel from '../components/prompt/SessionPromptPanel.vue'

import { useAttachments } from '../composables/useAttachments'
import { useChatContext } from '../composables/useChatContext'
import { useChatPreferences } from '../composables/useChatPreferences'
import { useChatScroll } from '../composables/useChatScroll'
import { useChatSessions } from '../composables/useChatSessions'
import { useChatStream } from '../composables/useChatStream'
import { useChatTurn } from '../composables/useChatTurn'
import { useChatViewer } from '../composables/useChatViewer'
import { useKbScope } from '../composables/useKbScope'
import { consumeChatSeed } from '../composables/useChatSeed'
import { useMemoriesPanel } from '../composables/useMemoriesPanel'
import { usePromptAdmin } from '../composables/usePromptAdmin'
import { useQuickSwitch } from '../composables/useQuickSwitch'
import { useSuggestions } from '../composables/useSuggestions'
import { useSuspension } from '../composables/useSuspension'
import { useToolStatus } from '../composables/useToolStatus'
import { getNextSuggestions, searchConversations, updateConversation } from '../api'
import { isFailed, toolName } from '../composables/useChatUtils'
import { buildLocalNextSuggestions } from '../utils/nextSuggestions'

// keep-alive 按组件名匹配，显式声明
defineOptions({ name: 'ChatView' })

const question = ref('')
const loading = ref(false)
const messages = ref([])
const messagesRef = ref(null)
const questionInput = ref(null)
const templateId = ref(0)
const sessionCollapsed = ref(false)

// 单列布局：导航与会话列表同列（App 提供 appearance 实例）
const appearance = inject('settingsAppearance', null)
// 侧栏固定/悬浮：悬浮态下点标题左侧的图标即可固定回来
const sidebarFloating = computed(() => !!appearance?.sidebarCollapsed?.value)
const canToggleSidebar = computed(() => !!appearance && appearance.layoutMode.value !== 'topbar')

function toggleSidebarPinned() {
  if (!appearance?.sidebarCollapsed) return
  appearance.sidebarCollapsed.value = !appearance.sidebarCollapsed.value
  // 从悬浮态固定回来时，把导航列重置为展开宽度内的默认值（不覆盖用户拖过的宽度）
  document.documentElement.dataset.sidebarPeek = '0'
}

const singleColumn = computed(
  () =>
    !!appearance &&
    (['claude', 'hermes'].includes(appearance.layoutMode.value) ||
      !!appearance.mergedSidebar?.value),
)
// 会话列表按工作区分组（外观设置，默认开）
const groupSessionsByWorkspace = computed(
  () => !!appearance?.groupSessionsByWorkspace?.value,
)

// ---------------- 会话搜索（后端：标题 / 工作区 / 消息正文） ----------------
const sessionSearchResults = ref([])
const sessionSearchLoading = ref(false)
let sessionSearchSeq = 0

async function onSessionSearch(query) {
  const seq = ++sessionSearchSeq
  const text = String(query || '').trim()
  if (!text) {
    sessionSearchResults.value = []
    sessionSearchLoading.value = false
    return
  }
  sessionSearchLoading.value = true
  try {
    const items = await searchConversations(text)
    if (seq !== sessionSearchSeq) return
    sessionSearchResults.value = Array.isArray(items) ? items : []
  } catch {
    if (seq === sessionSearchSeq) sessionSearchResults.value = []
  } finally {
    if (seq === sessionSearchSeq) sessionSearchLoading.value = false
  }
}

/** 搜索结果可能不在当前列表快照里：先并入列表再切，避免工作区/标题显示为空。 */
function handleSwitchSession(session) {
  if (!session) return
  if (!sessions.value.some((s) => s.id === session.id)) {
    sessions.value = [session, ...sessions.value]
  }
  switchSession(session)
}
// 会话草稿：每个会话（含新对话 key='new'）独立保留输入框内容
const drafts = ref({})
// 会话消息内存缓存：LRU（最近访问优先，上限 5 个会话）。Map 保序，切换时
// 保存当前内存态（含生成中的消息）；超限淘汰最久未访问的（下次切回重新拉取）。
const MESSAGE_CACHE_LIMIT = 5
const sessionMessagesCache = new Map()
// 检索范围按会话缓存：避免上一个会话的范围泄漏到下一个会话
const scopeCache = {}
const activeId = ref(null)
const currentTitle = ref('新对话')

// ---------------- 工作区（会话 project_dir） ----------------
// 已有会话读写会话字段；新对话（尚未落库）先暂存在本地，首轮发送时随请求带给后端
const pendingWorkspace = ref(null)
const workspaceDialogVisible = ref(false)
const workspaceDraft = ref('')
const workspaceSaving = ref(false)

/** 工作区显示名：目录 basename（兼容 \ 与 /）；空 = 默认工作区 */
function workspaceDisplayName(path) {
  if (!path) return '默认工作区'
  const trimmed = String(path).replace(/[\\/]+$/, '')
  const index = Math.max(trimmed.lastIndexOf('\\'), trimmed.lastIndexOf('/'))
  const base = index >= 0 ? trimmed.slice(index + 1) : trimmed
  return base || trimmed || String(path)
}

// ---------------- 右侧会话面板（可收起） ----------------
const INSPECTOR_KEY = 'rag_inspector_open'

function readInspectorOpen() {
  try {
    return localStorage.getItem(INSPECTOR_KEY) !== '0'
  } catch {
    return true
  }
}

const inspectorOpen = ref(readInspectorOpen())
const inspectorVisible = computed(
  () => inspectorOpen.value && (activeId.value != null || messages.value.length > 0),
)
// 工具详情：保存原始 tool_trace 条目引用（流式期间详情要跟着更新），切会话即清空
const inspectorDetail = ref(null)
const workspacePanelRef = ref(null)

function openInspectorDetail(entry) {
  if (!entry) return
  inspectorDetail.value = entry
  inspectorOpen.value = true
}

function closeInspectorDetail() {
  inspectorDetail.value = null
}

/** 聊天里的计划/任务清单 → 打开面板的计划分区 */
function openPlanPanel() {
  inspectorDetail.value = null
  inspectorOpen.value = true
  nextTick(() => workspacePanelRef.value?.openSection('plan'))
}

watch(activeId, () => {
  inspectorDetail.value = null
})
// 面板里的任务区：取最后一条 assistant 消息的 todos/plan（流式期间实时更新）
const panelTask = computed(() => {
  const last = [...messages.value].reverse().find((m) => m.role === 'assistant')
  return {
    plan: last?.plan || [],
    todos: last?.todos || [],
    planDone: last?.planDone ?? null,
    planTotal: last?.planTotal ?? null,
  }
})

watch(inspectorOpen, (value) => {
  try {
    localStorage.setItem(INSPECTOR_KEY, value ? '1' : '0')
  } catch {
    // ignore
  }
})

function toggleInspector() {
  inspectorOpen.value = !inspectorOpen.value
  if (!inspectorOpen.value) inspectorDetail.value = null
  // 窄屏面板是右侧浮层：打开时收起会话抽屉，避免两个浮层叠在一起
  if (inspectorOpen.value && typeof window !== 'undefined' && window.innerWidth < 1024) {
    sessionCollapsed.value = true
  }
}

const INSPECTOR_WIDTH_KEY = 'rag_inspector_width'
const INSPECTOR_WIDTH_MIN = 220
const INSPECTOR_WIDTH_MAX = 480

/** 恢复拖动保存的面板宽度（ResizeHandle 只负责写 localStorage）。 */
function restoreInspectorWidth() {
  try {
    const px = parseInt(localStorage.getItem(INSPECTOR_WIDTH_KEY) || '', 10)
    if (!Number.isFinite(px)) return
    const clamped = Math.min(INSPECTOR_WIDTH_MAX, Math.max(INSPECTOR_WIDTH_MIN, px))
    document.documentElement.style.setProperty('--inspector-width', `${clamped}px`)
  } catch {
    // ignore
  }
}

function onGlobalKeydown(event) {
  if (!(event.ctrlKey || event.metaKey) || !event.shiftKey) return
  if (String(event.key || '').toLowerCase() !== 'b') return
  // keep-alive 下其他页仍持有监听：只在对话页响应
  if (route.path !== '/' && route.name !== 'chat') return
  event.preventDefault()
  toggleInspector()
}

const { scrollFollow, onMessagesScroll, scrollToBottom } = useChatScroll({
  messagesRef,
})

// 上滑离开底部时浮现「回到最新」
const showJumpToLatest = computed(
  () => !scrollFollow.value && messages.value.length > 0,
)

function jumpToLatest() {
  scrollFollow.value = true
  scrollToBottom(true)
}

// 本轮已输出字数（流式消息正文长度）
const streamCharCount = computed(() => {
  const last = messages.value[messages.value.length - 1]
  return last?.role === 'assistant' ? String(last.content || '').length : 0
})
const {
  currentTool,
  statusElapsed,
  activityLabel,
  setActivity,
  clearActivity,
} = useToolStatus()
const prefs = useChatPreferences({ templateId, sessionCollapsed })
const promptAdmin = usePromptAdmin({
  activeId,
  currentTitle,
  loading,
  templateId,
})
const attachments = useAttachments()
const { suggestions, loadSuggestions } = useSuggestions()
const quick = useQuickSwitch()
const route = useRoute()
const router = useRouter()
const {
  kbScope,
  scopeDocs,
  scopeDialogVisible,
  scopeQuery,
  scopeDraft,
  filteredScopeOptions,
  openScopeDialog,
  ensureScopeDocs,
  addDocPaths,
  clearDraft,
  applyScope,
  clearScope,
} = useKbScope()
// 查看器划选的原文（随本轮发送，发送后清空）
const quote = ref(null)
const quotePreview = computed(() => {
  const text = String(quote.value?.text || '').replace(/\s+/g, ' ').trim()
  return text.length > 120 ? `${text.slice(0, 120)}…` : text
})

// ---------------- 输入框 @ 引用知识库文档 ----------------
const mention = reactive({
  open: false,
  loading: false,
  query: '',
  index: 0,
})

function composerTextarea() {
  return questionInput.value?.$el?.querySelector?.('textarea') || null
}

function closeMention() {
  mention.open = false
  mention.loading = false
  mention.query = ''
  mention.index = 0
}

const mentionItems = computed(() => {
  const q = mention.query.trim().toLowerCase()
  const seen = new Set()
  const out = []
  for (const doc of scopeDocs.value) {
    const path = doc?.relative_path
    if (!path || seen.has(path)) continue
    const name = doc.name || path.split(/[\\/]/).pop() || path
    if (
      q &&
      !name.toLowerCase().includes(q) &&
      !path.toLowerCase().includes(q)
    ) {
      continue
    }
    seen.add(path)
    out.push({ path, name })
    if (out.length >= 8) break
  }
  return out
})

async function onComposerInput() {
  if (question.value.trim()) ghostDismissed.value = true
  const el = composerTextarea()
  if (!el) {
    closeMention()
    return
  }
  const cursor = el.selectionStart ?? question.value.length
  const before = question.value.slice(0, cursor)
  // 光标前最后一个 @ 起的裸词（中文通常不加空格，因此不要求 @ 前有空白）
  const match = before.match(/@([^\s@]*)$/)
  if (!match) {
    closeMention()
    return
  }
  mention.open = true
  mention.query = match[1]
  mention.index = 0
  if (!scopeDocs.value.length) {
    mention.loading = true
    await ensureScopeDocs()
    if (mention.open) mention.loading = false
  }
}

function pickMention(item) {
  if (!item) return
  const el = composerTextarea()
  const text = question.value
  // 以当前光标重新定位 @ 词，避免期间移动光标导致拼接错位
  const cursor = el?.selectionStart ?? text.length
  const match = text.slice(0, cursor).match(/@([^\s@]*)$/)
  const at = match ? cursor - match[1].length - 1 : -1
  if (at >= 0) {
    question.value = text.slice(0, at) + text.slice(cursor)
  }
  addDocPaths([{ relative_path: item.path, name: item.name }])
  closeMention()
  nextTick(() => {
    const box = composerTextarea()
    box?.focus()
    const pos = at >= 0 ? Math.min(at, question.value.length) : question.value.length
    box?.setSelectionRange(pos, pos)
  })
}

function isPlainEnter(event) {
  return (
    event.key === 'Enter' &&
    !event.shiftKey &&
    !event.ctrlKey &&
    !event.metaKey &&
    !event.altKey &&
    !event.isComposing
  )
}

function onComposerKeydown(event) {
  // ghost 建议：Tab 采纳 / Esc 忽略；打字走 input 事件自动消失
  if (event.key === 'Tab' && ghostVisible.value) {
    event.preventDefault()
    acceptGhostSuggestion()
    return
  }
  if (event.key === 'Escape' && ghostVisible.value) {
    event.preventDefault()
    ghostDismissed.value = true
    return
  }
  // Claude Code 同款：空输入框 Enter = 采纳并直接发送
  if (isPlainEnter(event) && ghostVisible.value) {
    event.preventDefault()
    question.value = ghostSuggestion.value
    ghostDismissed.value = true
    send()
    return
  }
  if (mention.open) {
    if (['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) {
      closeMention()
      return
    }
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault()
      const len = mentionItems.value.length
      if (len) {
        mention.index =
          (mention.index + (event.key === 'ArrowDown' ? 1 : -1) + len) % len
      }
      return
    }
    if (event.key === 'Escape') {
      event.preventDefault()
      closeMention()
      return
    }
    if (isPlainEnter(event)) {
      event.preventDefault()
      if (mentionItems.value.length) {
        pickMention(mentionItems.value[mention.index] || mentionItems.value[0])
      } else {
        closeMention()
        send()
      }
      return
    }
  }
  if (isPlainEnter(event)) {
    event.preventDefault()
    send()
  }
}

function handleAskSelection(payload) {
  if (!payload?.text) return
  quote.value = {
    text: payload.text,
    context: payload.context || '',
    source: payload.source || '',
    page: payload.page ?? null,
  }
  if (payload.source) {
    kbScope.value = {
      type: 'doc',
      value: payload.source,
      label: payload.source.split('/').pop(),
    }
  }
  const intents = {
    explain: '请解释这段内容',
    translate: '请翻译这段内容',
    summarize: '请总结这段内容',
  }
  if (!question.value.trim()) {
    question.value = intents[payload.intent] || intents.explain
  }
  docViewerRef.value?.close?.()
  nextTick(() => questionInput.value?.focus())
}
const { docViewerRef, openSourceDoc, handleMessageClick } = useChatViewer({
  messages,
})
const {
  turn,
  typewriter,
  closeThinkingSegment,
} = useChatTurn({ scrollToBottom })

// 范围跟随会话：离开时保存、进入时恢复（与草稿/消息缓存同口径）
function scopeKey() {
  return activeId.value ?? 'new'
}

function saveScopeForSession() {
  scopeCache[scopeKey()] = kbScope.value
}

function restoreScopeForSession() {
  kbScope.value = scopeCache[scopeKey()] || null
}

function dropScopeForSession(id) {
  delete scopeCache[id]
}

// 会话域：hooks 里的 suspension 在运行时才被调用（setup 期间不触发）
const {
  sessions,
  loadSessions,
  startNewChat,
  switchSession,
  handleRename,
  handlePin,
  handleDelete,
  upsertSession,
  rewindTo,
  forkFrom,
  reloadMessages,
} = useChatSessions({
  messages,
  loading,
  question,
  questionInput,
  activeId,
  currentTitle,
  sessionCollapsed,
  sessionMessagesCache,
  messageCacheLimit: MESSAGE_CACHE_LIMIT,
  drafts,
  scrollToBottom,
  hooks: {
    onLeave: () => {
      saveScopeForSession()
      suspension.reset()
    },
    onEnter: (id) => {
      promptAdmin.loadConversationPrompt(id)
      restoreScopeForSession()
    },
    onRestorePending: (id) => suspension.restorePendingApproval(id),
    onNewChat: () => {
      promptAdmin.clearConversationPrompt()
      restoreScopeForSession()
      pendingWorkspace.value = null
      rotateGreeting()
    },
    setTemplateFromSession: (value) => promptAdmin.setTemplateFromSession(value),
    onSessionDeleted: dropScopeForSession,
  },
})

// 欢迎页问候语：按时间段轮换，避免连续两次相同（localStorage 记住上一条）
const welcomeGreeting = ref('')
function rotateGreeting() {
  try {
    const last = localStorage.getItem('rag_last_greeting') || ''
    welcomeGreeting.value = pickGreeting(new Date(), last)
    localStorage.setItem('rag_last_greeting', welcomeGreeting.value)
  } catch {
    welcomeGreeting.value = '有什么可以帮你？'
  }
}
rotateGreeting()

// 主页样式（App.vue 提供的外观实例）：四种样式共用同一套 DOM，文案与排布由 CSS 切换
const homeStyle = appearance?.homeStyle || ref('classic')
const HOME_COPY = {
  classic: {
    eyebrow: '知问 · 个人知识助手',
    subtitle: '知识库、联网与工具，一条链路上的回答',
  },
  minimal: {
    eyebrow: '快速开始',
    subtitle: '选一条开始，或直接输入你的问题',
  },
  hero: {
    eyebrow: 'ZHIWEN · KNOWLEDGE ASSISTANT',
    subtitle: '把你的文档、笔记与网页，变成可以对话的知识',
  },
  studio: {
    eyebrow: '工作台',
    subtitle: '检索、执行、记录，从一屏开始今天的工作',
  },
}
const welcomeEyebrow = computed(
  () => (HOME_COPY[homeStyle.value] || HOME_COPY.classic).eyebrow
)
const welcomeSubtitle = computed(
  () => (HOME_COPY[homeStyle.value] || HOME_COPY.classic).subtitle
)
const quickActions = [
  { to: '/knowledge', label: '知识库', desc: '文档、索引与健康', icon: Collection },
  { to: '/runs', label: '运行记录', desc: '用量、轨迹与回放', icon: DataAnalysis },
  { to: '/prompts', label: '提示词工作台', desc: '模板、片段与版本', icon: List },
]

// ---------------- 工作区：当前值 / 候选列表 / 赋值 ----------------
const activeSession = computed(
  () => sessions.value.find((s) => s.id === activeId.value) || null,
)

/** 当前会话的 fork 来源（源会话已被删除时为 null，但会话本身仍标记为分支） */
const activeForkSource = computed(() => {
  const origin = activeSession.value?.forked_from_conversation_id
  return origin ? sessions.value.find((s) => s.id === origin) || null : null
})
const forkSourceTooltip = computed(() =>
  activeForkSource.value
    ? `分支自「${activeForkSource.value.title}」`
    : '分支会话（来源会话已删除或不可见）',
)
const currentWorkspace = computed(() =>
  activeId.value != null
    ? activeSession.value?.project_dir || ''
    : pendingWorkspace.value || '',
)
const currentWorkspaceName = computed(() => workspaceDisplayName(currentWorkspace.value))

/** 工作区头的「+」：新建对话并绑定该工作区（首轮发送把 project_dir 带给后端）。 */
function startNewChatInWorkspace(path) {
  startNewChat()
  pendingWorkspace.value = String(path || '').trim() || null
  nextTick(() => questionInput.value?.focus())
  if (pendingWorkspace.value) {
    ElMessage.success(
      `新对话已绑定工作区 ${workspaceDisplayName(pendingWorkspace.value)}（工具将在此目录下工作）`,
    )
  }
}
const workspaceOptions = computed(() => {
  const seen = new Map()
  for (const s of sessions.value) {
    const path = s.project_dir
    if (!path || seen.has(path)) continue
    seen.set(path, { path, name: workspaceDisplayName(path) })
  }
  return [...seen.values()]
})

function openWorkspaceDialog() {
  workspaceDraft.value = currentWorkspace.value
  workspaceDialogVisible.value = true
}

async function confirmWorkspace() {
  if (workspaceSaving.value) return
  const value = (workspaceDraft.value || '').trim()
  if (activeId.value == null) {
    pendingWorkspace.value = value || null
    workspaceDialogVisible.value = false
    ElMessage.success(value ? '新对话将归入该工作区' : '新对话使用默认工作区')
    return
  }
  workspaceSaving.value = true
  try {
    // "" = 清除（后端把空串归一化为 null）；null 会被当作"不修改"
    const updated = await updateConversation(activeId.value, { project_dir: value })
    if (activeSession.value) activeSession.value.project_dir = updated.project_dir
    workspaceDialogVisible.value = false
    ElMessage.success(
      updated.project_dir
        ? `已归入工作区 ${workspaceDisplayName(updated.project_dir)}`
        : '已切换到默认工作区',
    )
    await loadSessions()
  } catch (error) {
    ElMessage.error(error.response?.data?.detail || '设置工作区失败')
  } finally {
    workspaceSaving.value = false
  }
}

// 新会话首轮：SSE session 事件只带 id/title，立即刷新列表拿到 project_dir，
// 让会话马上落入正确的工作区组（发送收尾的 reloadSessions 再兜底一次）
watch(activeId, (id, prev) => {
  if (id != null && prev == null) loadSessions()
})

// ---------------- 输入框「下一步建议」 ----------------

const nextSuggestions = ref([])
const nextSuggestionsLoading = ref(false)
const ghostDismissed = ref(false)
let nextSuggestionsReq = 0

const ghostSuggestion = computed(() => nextSuggestions.value[0] || '')
// Claude Code 式：只在空输入框、不在生成中、无 @ 候选时显示
const ghostVisible = computed(
  () =>
    !loading.value &&
    !ghostDismissed.value &&
    !question.value.trim() &&
    !mention.open &&
    Boolean(ghostSuggestion.value),
)

/** 回答完成后：先用本地启发式即时展示，再异步换成小模型生成的 3 条。 */
async function loadNextSuggestions() {
  const convId = activeId.value
  if (!convId) return
  ghostDismissed.value = false
  const lastAssistant = [...messages.value]
    .reverse()
    .find((m) => m && m.role === 'assistant')
  const local = buildLocalNextSuggestions(lastAssistant)
  if (local.length) nextSuggestions.value = local
  const req = ++nextSuggestionsReq
  nextSuggestionsLoading.value = true
  try {
    const items = await getNextSuggestions(convId)
    // 请求期间切了会话/开始了新一轮：旧响应不能落到新界面
    if (req !== nextSuggestionsReq || activeId.value !== convId) return
    if (Array.isArray(items) && items.length) {
      nextSuggestions.value = items.slice(0, 3)
    }
  } catch {
    // 小模型失败/关闭：保留本地建议
  } finally {
    if (req === nextSuggestionsReq) nextSuggestionsLoading.value = false
  }
}

/** Tab / 点击「Tab 采纳」：把 ghost 建议填入输入框并聚焦（不自动发送）。 */
function acceptGhostSuggestion() {
  const text = ghostSuggestion.value
  if (!text) return
  ghostDismissed.value = true
  question.value = text
  nextTick(() => questionInput.value?.focus())
}

// 开始新一轮 / 切会话：清空旧建议，避免误导
watch(loading, (value) => {
  if (value) {
    nextSuggestions.value = []
    ghostDismissed.value = true
    nextSuggestionsReq += 1
    nextSuggestionsLoading.value = false
  }
})
watch(activeId, () => {
  nextSuggestions.value = []
  ghostDismissed.value = true
  nextSuggestionsReq += 1
  nextSuggestionsLoading.value = false
})

const suspension = useSuspension({
  activeId,
  currentTitle,
  loading,
  setActivity,
  clearActivity,
  messages,
  questionInput,
  turn,
  typewriter,
  closeThinkingSegment,
  scrollToBottom,
  upsertSession,
  reloadSessions: loadSessions,
  reloadPrompt: (id) => promptAdmin.loadConversationPrompt(id),
  reloadMessages,
  onAnswerComplete: () => loadNextSuggestions(),
})

const {
  autoRunning,
  hasSuspendedPending,
  decidePermission,
  confirmPlan,
  cancelPlan,
  onPlanCardKeydown,
  submitQuestions,
  stopSuspended,
  restorePendingApproval,
  continueInterrupted,
  abandonInterrupted,
} = suspension

const { send, stop, regenerateLast, ask } = useChatStream({
  activeId,
  question,
  messages,
  loading,
  setActivity,
  clearActivity,
  turn,
  typewriter,
  attachments,
  prefs,
  templateId,
  kbScope,
  quote,
  projectDir: currentWorkspace,
  suspension,
  scrollFollow,
  scrollToBottom,
  reloadSessions: loadSessions,
  reloadPrompt: promptAdmin.loadConversationPrompt,
  clearDraft: () => {
    drafts.value[activeId.value ?? 'new'] = ''
  },
})

const {
  contextVisible,
  contextStats,
  compacting,
  ctxEffectiveMessages,
  ctxSentTokens,
  ctxWindowTokens,
  ctxHighWater,
  barPct,
  fmtCtxTokens,
  openContextDialog,
  loadContextStats,
  onContextClose,
  timelineVisible,
  timelineLoading,
  timelineItems,
  openTimeline,
  formatCheckpointTime,
  rollbackTo,
  handleCompact,
} = useChatContext({
  activeId,
  loading,
  reloadMessages,
  scrollToBottom,
})

// 右侧面板上下文统计：打开面板、切换会话、每轮跑完时静默刷新
function refreshPanelContext() {
  loadContextStats(activeId.value)
}

watch([inspectorVisible, activeId], () => {
  if (inspectorVisible.value && activeId.value != null) loadContextStats(activeId.value)
})

watch(loading, (value, prev) => {
  if (prev && !value && inspectorVisible.value && activeId.value != null) {
    loadContextStats(activeId.value)
  }
})

const {
  memoriesVisible,
  memories,
  memoryEditVisible,
  consolidating,
  memoryForm,
  expandedMemoryIds,
  showArchived,
  categoryMap,
  categoryLabel,
  categoryTagType,
  formatMemoryDate: fmtMemoryDate,
  toggleMemoryExpand,
  openMemories,
  loadMemories,
  handleArchiveMemory,
  openMemoryEdit,
  saveMemory,
  handleConsolidate,
  handleDeleteMemory,
} = useMemoriesPanel()

const {
  attachedImages,
  attachedFiles,
  attachInputRef,
  uploadingAttachments,
  dragOver,
  onAttachFiles,
  onDragOver,
  onDragLeave,
  onDrop,
  onPaste,
  removeAttachment,
  removeImage,
} = attachments

const {
  quickProviders,
  quickActiveId,
  quickModel,
  quickBusy,
  quickActiveName,
  quickPermissionMode,
  quickPermissionLabel,
  quickThinkingEnabled,
  quickThinkingEffort,
  quickThinkingLabel,
  thinkingOptions,
  modeBusy,
  thinkingBusy,
  loadQuickProviders,
  onQuickSwitch,
  onPermissionModeSwitch,
  onThinkingSwitch,
} = quick

const {
  templates,
  availableTemplates,
  templateManagerVisible,
  templateEditVisible,
  templateForm,
  promptFragmentsVisible,
  templateVersionsVisible,
  templateVersionTarget,
  templatePreviewVisible,
  templatePreviewRequest,
  defaultTemplateVisible,
  defaultTemplate,
  sessionPromptVisible,
  promptLockTag,
  loadTemplates,
  openTemplateManager,
  loadConversationPrompt,
  openSessionPrompt,
  openTemplateVersions,
  openPromptFragments,
  openDefaultTemplate,
  handleCloneTemplate,
  handleArchiveTemplate,
  onPromptArtifactsChanged,
  onSessionPromptApplied,
  openTemplateEdit,
  openTemplatePreview,
  normalizeTemplateBreaks,
  auditPromptHealth,
  saveTemplate,
  handleDeleteTemplate,
} = promptAdmin

// 右侧面板显示用的模板名
const panelTemplateName = computed(() => {
  if (!templateId.value) return '默认模板'
  const found = (templates.value || []).find((t) => t.id === templateId.value)
  return found?.name || '自定义模板'
})

const { toolMode, useWebSearch, useKnowledgeBase, loadPrefs } = prefs

async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text)
    ElMessage.success('已复制')
  } catch {
    ElMessage.warning('复制失败，请手动选择复制')
  }
}

/** 知识库页「问此文档」跳转：?scope_doc=... / ?scope_book=... */
function applyScopeFromRoute() {
  const query = route.query
  if (query.scope_doc) {
    const path = String(query.scope_doc)
    kbScope.value = {
      type: 'doc',
      value: path,
      label: String(query.scope_label || path.split('/').pop()),
    }
  } else if (query.scope_book) {
    const title = String(query.scope_book)
    kbScope.value = { type: 'book', value: title, label: title }
  } else {
    return
  }
  router.replace({ path: route.path, query: {} })
}

/** 桌面深链：?conv=<id> 直接打开对应会话（会话列表可能仍在加载，短暂等待） */
async function applyConvFromRoute() {
  const convId = Number(route.query.conv)
  if (!Number.isFinite(convId) || convId <= 0) return
  router.replace({ path: route.path, query: {} })
  for (let i = 0; i < 20; i += 1) {
    const found = (sessions.value || []).find((s) => Number(s.id) === convId)
    if (found) {
      switchSession(found)
      return
    }
    await new Promise((resolve) => setTimeout(resolve, 150))
  }
}

/** 知识库页划选后通过模块 store 带到对话页 */
function applyChatSeed() {
  const seed = consumeChatSeed()
  if (!seed) return
  if (seed.quote) quote.value = seed.quote
  if (seed.scope) kbScope.value = seed.scope
  // 不覆盖用户已有的草稿：只在本轮输入为空时预填快捷问题
  if (seed.question && !question.value.trim()) question.value = seed.question
  nextTick(() => questionInput.value?.focus())
}

onMounted(() => {
  loadPrefs()
  loadSessions()
  loadTemplates()
  loadSuggestions()
  loadQuickProviders()
  applyScopeFromRoute()
  applyConvFromRoute()
  applyChatSeed()
  restoreInspectorWidth()
  window.addEventListener('keydown', onGlobalKeydown)
  // 窄屏（<1024）默认收起会话栏（抽屉模式，见样式媒体查询）
  if (window.innerWidth < 1024) sessionCollapsed.value = true
})

// keep-alive 回到本页时刷新供应商列表（设置页可能改过启用/名称）
onActivated(() => {
  loadQuickProviders()
  applyScopeFromRoute()
  applyConvFromRoute()
  applyChatSeed()
})

onBeforeUnmount(() => {
  typewriter.dispose() // 停掉打字机定时器，避免回调打到已卸载组件上
  window.removeEventListener('keydown', onGlobalKeydown)
})
</script>

<style scoped src="../styles/chat-view.css"></style>
