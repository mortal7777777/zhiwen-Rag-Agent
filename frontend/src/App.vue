<template>
  <el-container class="app-container">
    <!-- 左侧全局导航（可折叠） -->
    <el-aside :width="navCollapsed ? '64px' : '212px'" class="app-aside">
      <div class="logo" @click="$router.push('/')">
        <div class="logo-mark">
          <el-icon :size="20"><MagicStick /></el-icon>
        </div>
        <transition name="fade">
          <span v-if="!navCollapsed" class="logo-text">AI 知识助手</span>
        </transition>
      </div>

      <el-menu
        :default-active="$route.path"
        router
        :collapse="navCollapsed"
        :collapse-transition="false"
        class="app-menu"
      >
        <el-menu-item index="/">
          <el-icon :size="18"><ChatDotRound /></el-icon>
          <template #title>智能助手</template>
        </el-menu-item>
        <el-menu-item index="/knowledge">
          <el-icon :size="18"><FolderOpened /></el-icon>
          <template #title>知识库</template>
        </el-menu-item>
        <el-menu-item index="/runs">
          <el-icon :size="18"><DataAnalysis /></el-icon>
          <template #title>运行记录</template>
        </el-menu-item>
      </el-menu>

      <div class="aside-footer">
        <el-tooltip content="设置" placement="right">
          <div class="collapse-btn" @click="settingsVisible = true">
            <el-icon :size="18"><Setting /></el-icon>
          </div>
        </el-tooltip>
        <el-tooltip :content="navCollapsed ? '展开导航' : '收起导航'" placement="right">
          <div class="collapse-btn" @click="navCollapsed = !navCollapsed">
            <el-icon :size="18">
              <Expand v-if="navCollapsed" />
              <Fold v-else />
            </el-icon>
          </div>
        </el-tooltip>
      </div>
    </el-aside>

    <el-main class="app-main">
      <router-view v-slot="{ Component }">
        <keep-alive include="ChatView">
          <component :is="Component" />
        </keep-alive>
      </router-view>
    </el-main>

    <!-- ================= 设置：居中圆角卡片 + 背景模糊 ================= -->
    <el-dialog
      v-model="settingsVisible"
      class="settings-dialog"
      modal-class="settings-modal"
      width="min(940px, calc(100vw - 32px))"
      align-center
      :show-close="false"
      append-to-body
    >
      <div class="settings-card">
        <!-- 顶部横向导航 -->
        <header class="settings-topbar">
          <div class="settings-brand">
            <div class="brand-mark">
              <el-icon :size="16"><Setting /></el-icon>
            </div>
            <span class="brand-title">设置</span>
          </div>
          <nav class="settings-tabs">
            <div
              v-for="item in navItems"
              :key="item.key"
              :class="['settings-tab', { active: activeTab === item.key }]"
              @click="activeTab = item.key"
            >
              <el-tooltip :content="item.desc" placement="bottom" :offset="6">
                <span class="settings-tab-inner">
                  <el-icon :size="15"><component :is="item.icon" /></el-icon>
                  <span>{{ item.label }}</span>
                </span>
              </el-tooltip>
            </div>
          </nav>
          <el-button circle text class="settings-close" @click="settingsVisible = false">
            <el-icon :size="16"><Close /></el-icon>
          </el-button>
        </header>

        <!-- 右侧内容 -->
        <main class="settings-content">
          <!-- ============ 外观 ============ -->
          <div v-show="activeTab === 'appearance'" class="settings-pane">
            <div class="pane-head">
              <div class="pane-title">外观</div>
              <div class="pane-sub">主题与内容布局</div>
            </div>

            <div class="settings-section">
              <div class="settings-label">主题</div>
              <div class="theme-options">
                <div
                  v-for="opt in themeOptions"
                  :key="opt.value"
                  :class="['theme-card', { active: theme === opt.value }]"
                  @click="theme = opt.value"
                >
                  <div class="theme-preview" :class="opt.value">
                    <span class="preview-side"></span>
                    <span class="preview-main">
                      <i class="preview-line"></i>
                      <i class="preview-line short"></i>
                      <i class="preview-bubble"></i>
                    </span>
                  </div>
                  <div class="theme-meta">
                    <span class="theme-name">{{ opt.label }}</span>
                    <el-icon v-if="theme === opt.value" class="theme-check">
                      <CircleCheckFilled />
                    </el-icon>
                  </div>
                </div>
              </div>
            </div>

            <div class="settings-section">
              <div class="settings-label">内容宽度</div>
              <el-segmented
                v-model="contentWidth"
                :options="widthOptions"
                class="width-segmented"
                @change="applyWidth"
              />
              <div class="width-feedback">
                <div class="width-preview-track">
                  <div
                    class="width-preview-bar"
                    :style="{ width: contentWidth === '100%' ? '100%' : contentWidth }"
                  ></div>
                </div>
                <span class="width-value">当前：{{ widthLabel }}</span>
              </div>
              <div class="settings-tip">
                按窗口比例控制聊天区宽度，各档位始终有明显区别，点击立即生效。
              </div>
            </div>
          </div>

          <!-- ============ 模型与供应商 ============ -->
          <div v-show="activeTab === 'models'" class="settings-pane">
            <div class="pane-head">
              <div class="pane-title">模型与供应商</div>
              <div class="pane-sub">参考 cc-switch：多供应商管理，切换立即生效</div>
            </div>

            <div class="settings-section">
              <div class="section-head">
                <span class="settings-label">对话模型</span>
                <el-button size="small" type="primary" plain @click="openProviderDialog('chat')">
                  <el-icon :size="13"><Plus /></el-icon>
                  添加供应商
                </el-button>
              </div>
              <div v-if="!chatProviders.length" class="provider-empty">
                还没有对话模型供应商，点击"添加供应商"配置一个
              </div>
              <div
                v-for="p in chatProviders"
                :key="p.id"
                :class="['provider-card', { inactive: !p.enabled }]"
              >
                <div class="provider-head">
                  <el-radio
                    v-model="activeChatId"
                    :value="p.id"
                    :disabled="!p.enabled"
                    class="provider-radio"
                  >
                    当前
                  </el-radio>
                  <span class="provider-name">{{ p.name }}</span>
                  <el-tag size="small" effect="plain" type="primary">对话</el-tag>
                  <span class="provider-flex"></span>
                  <el-switch v-model="p.enabled" size="small" @change="onProviderEnabledChange(p)" />
                  <el-tooltip content="删除供应商" placement="top">
                    <el-button link type="danger" size="small" class="provider-del" @click="removeProvider(p)">
                      <el-icon :size="14"><Delete /></el-icon>
                    </el-button>
                  </el-tooltip>
                </div>
                <div class="provider-grid">
                  <div class="field">
                    <label>接口地址 Base URL</label>
                    <el-input v-model="p.base_url" placeholder="https://api.example.com" />
                  </div>
                  <div class="field">
                    <label>默认模型</label>
                    <el-select
                      v-model="p.model"
                      filterable
                      allow-create
                      default-first-option
                      placeholder="选择或输入模型 ID"
                    >
                      <el-option v-for="m in p.models" :key="m" :label="m" :value="m" />
                    </el-select>
                  </div>
                  <div class="field wide">
                    <label>模型列表（逗号分隔）</label>
                    <el-input v-model="p._modelsText" placeholder="model-a, model-b" />
                  </div>
                  <div class="field">
                    <label>API Key</label>
                    <el-input
                      v-model="p._keyInput"
                      type="password"
                      show-password
                      :placeholder="p.api_key ? `已设置 ${p.api_key}（留空保持不变）` : '未设置'"
                    />
                  </div>
                </div>
              </div>
            </div>

            <div class="settings-section">
              <div class="section-head">
                <span class="settings-label">视觉模型（识图 / OCR）</span>
                <el-button size="small" type="primary" plain @click="openProviderDialog('vision')">
                  <el-icon :size="13"><Plus /></el-icon>
                  添加供应商
                </el-button>
              </div>
              <div v-if="!visionProviders.length" class="provider-empty">
                还没有视觉模型供应商（可选，未配置时图片功能不可用）
              </div>
              <div
                v-for="p in visionProviders"
                :key="p.id"
                :class="['provider-card', { inactive: !p.enabled }]"
              >
                <div class="provider-head">
                  <el-radio
                    v-model="activeVisionId"
                    :value="p.id"
                    :disabled="!p.enabled"
                    class="provider-radio"
                  >
                    当前
                  </el-radio>
                  <span class="provider-name">{{ p.name }}</span>
                  <el-tag size="small" effect="plain" type="success">视觉</el-tag>
                  <span class="provider-flex"></span>
                  <el-switch v-model="p.enabled" size="small" />
                  <el-tooltip content="删除供应商" placement="top">
                    <el-button link type="danger" size="small" class="provider-del" @click="removeProvider(p)">
                      <el-icon :size="14"><Delete /></el-icon>
                    </el-button>
                  </el-tooltip>
                </div>
                <div class="provider-grid">
                  <div class="field">
                    <label>接口地址 Base URL</label>
                    <el-input v-model="p.base_url" placeholder="https://token.sensenova.cn/v1" />
                  </div>
                  <div class="field">
                    <label>默认模型</label>
                    <el-select
                      v-model="p.model"
                      filterable
                      allow-create
                      default-first-option
                      placeholder="选择或输入模型 ID"
                    >
                      <el-option v-for="m in p.models" :key="m" :label="m" :value="m" />
                    </el-select>
                  </div>
                  <div class="field wide">
                    <label>模型列表（逗号分隔）</label>
                    <el-input v-model="p._modelsText" placeholder="sensenova-6.8-flash-lite" />
                  </div>
                  <div class="field">
                    <label>API Key</label>
                    <el-input
                      v-model="p._keyInput"
                      type="password"
                      show-password
                      :placeholder="p.api_key ? `已设置 ${p.api_key}（留空保持不变）` : '未设置'"
                    />
                  </div>
                </div>
              </div>
            </div>

            <div class="settings-section">
              <div class="settings-label">通用</div>
              <div class="generic-form">
                <div class="generic-row">
                  <span class="generic-label">回答温度</span>
                  <el-input-number v-model="temperature" :min="0" :max="2" :step="0.1" size="small" />
                </div>
                <div class="generic-row">
                  <span class="generic-label">联网搜索</span>
                  <el-select v-model="webProvider" size="small" class="generic-select">
                    <el-option label="DuckDuckGo（免费）" value="duckduckgo" />
                    <el-option label="Tavily" value="tavily" />
                    <el-option label="关闭" value="off" />
                  </el-select>
                </div>
                <div v-if="webProvider === 'tavily'" class="generic-row">
                  <span class="generic-label">Tavily Key</span>
                  <el-input
                    v-model="tavilyKey"
                    type="password"
                    show-password
                    size="small"
                    :placeholder="tavilyMasked ? `已设置 ${tavilyMasked}（留空保持不变）` : '未设置'"
                    class="generic-select"
                  />
                </div>
                <div class="generic-row">
                  <span class="generic-label">结果条数</span>
                  <el-input-number v-model="webMaxResults" :min="1" :max="20" size="small" />
                </div>
              </div>
            </div>

            <div class="settings-save-row">
              <el-button type="primary" :loading="modelSaving" @click="saveModelSettings">
                保存并立即生效
              </el-button>
            </div>
          </div>

          <!-- ============ 技能 ============ -->
          <div v-show="activeTab === 'skills'" class="settings-pane">
            <div class="pane-head">
              <div class="pane-title">技能</div>
              <div class="pane-sub">浏览本机 Skills，选择本助手要使用的能力</div>
            </div>

            <div class="skill-master">
              <div class="skill-master-text">
                <div class="skill-master-title">启用技能工具</div>
                <div class="skill-master-desc">
                  Agent 通过 skill_lookup 按需检索下方已启用的技能指令
                </div>
              </div>
              <el-switch v-model="skillsEnabled" @change="saveSkillsEnabled" />
            </div>

            <div class="skill-filter-row">
              <el-input
                v-model="skillQuery"
                placeholder="搜索技能（写作 / PDF / 调试 / 研究…）"
                clearable
                class="skill-search"
                @input="onSkillSearch"
              >
                <template #prefix>
                  <el-icon><Search /></el-icon>
                </template>
              </el-input>
              <el-select
                v-model="skillGroupFilter"
                class="skill-group-select"
                placeholder="按功能筛选"
              >
                <el-option label="全部功能" value="all" />
                <el-option
                  v-for="(label, key) in groupOptions"
                  :key="key"
                  :label="label"
                  :value="key"
                />
              </el-select>
            </div>
            <div class="skill-source-filter">
              <span
                v-for="f in sourceFilters"
                :key="f.value"
                :class="['source-chip', { active: skillSourceFilter === f.value }]"
                @click="switchSourceFilter(f.value)"
              >
                {{ f.label }}
              </span>
            </div>

            <div class="skill-summary">
              <el-tag size="small" effect="plain" type="success">
                已启用 {{ enabledCount }}
              </el-tag>
              <span class="skill-summary-text">
                共 {{ skills.length + hiddenSkills.length }} 个可用技能
              </span>
            </div>

            <div v-if="skillsLoading" class="skills-loading">加载中…</div>
            <div v-else-if="!filteredSkills.length" class="skills-empty">没有匹配的技能</div>
            <div v-else class="skill-list">
              <div v-for="s in filteredSkills" :key="s.name" class="skill-item">
                <div class="skill-icon">
                  <el-icon :size="16"><component :is="skillIcon(s)" /></el-icon>
                </div>
                <div class="skill-main">
                  <div class="skill-head">
                    <span class="skill-name">{{ s.name }}</span>
                    <el-tag size="small" :type="sourceTagType(s.source)">{{ sourceLabel(s.source) }}</el-tag>
                    <el-tag v-if="s.group && s.group !== 'other'" size="small" type="info" effect="plain">
                      {{ groupLabel(s.group) }}
                    </el-tag>
                  </div>
                  <div class="skill-desc">{{ s.description }}</div>
                </div>
                <div class="skill-ops">
                  <el-switch v-model="s.enabled" size="small" @change="persistSkills" />
                  <el-tooltip content="从本助手移除（不影响其他 Agent 的文件）" placement="top">
                    <el-button link type="info" size="small" class="skill-remove" @click="hideSkill(s)">
                      <el-icon :size="13"><Hide /></el-icon>
                    </el-button>
                  </el-tooltip>
                </div>
              </div>
            </div>

            <template v-if="hiddenSkills.length">
              <div class="hidden-head">
                <span class="settings-label">已从本助手移除</span>
                <span class="hidden-hint">只影响本助手，不会修改其他 Agent 的技能文件</span>
              </div>
              <div class="skill-list">
                <div v-for="s in hiddenSkills" :key="s.name" class="skill-item muted">
                  <div class="skill-icon">
                    <el-icon :size="16"><component :is="skillIcon(s)" /></el-icon>
                  </div>
                  <div class="skill-main">
                    <div class="skill-head">
                      <span class="skill-name">{{ s.name }}</span>
                      <el-tag size="small" :type="sourceTagType(s.source)">{{ sourceLabel(s.source) }}</el-tag>
                    </div>
                    <div class="skill-desc">{{ s.description }}</div>
                  </div>
                  <div class="skill-ops">
                    <el-button size="small" @click="restoreSkill(s)">恢复</el-button>
                  </div>
                </div>
              </div>
            </template>

            <div class="skill-footer">
              <el-button link type="primary" size="small" @click="handleResetSkills">
                恢复默认精选手集
              </el-button>
              <el-button link size="small" @click="refreshSkills">
                重新扫描本机技能
              </el-button>
            </div>
          </div>

          <!-- ============ 工具与集成 ============ -->
          <div v-show="activeTab === 'tools'" class="settings-pane">
            <div class="pane-head">
              <div class="pane-title">工具与集成</div>
              <div class="pane-sub">受控执行 · MCP · 技能沙箱 · 项目记忆</div>
            </div>

            <div class="settings-section">
              <div class="section-head">
                <span class="settings-label">受控执行工具</span>
                <el-switch v-model="toolsForm.advanced_tools_enabled" size="small" />
              </div>
              <div class="provider-hint">
                读取类操作（列目录 / 读文件 / 搜索）自动执行；写入、编辑、删除、
                执行命令等敏感操作默认弹窗请求人工确认。工作目录限制在下方指定路径内。
              </div>
              <div class="generic-form">
                <div class="generic-row">
                  <span class="generic-label">工作目录</span>
                  <el-input
                    v-model="toolsForm.tool_workspace"
                    placeholder="留空 = 项目根"
                    size="small"
                    class="generic-select"
                  />
                </div>
                <div class="generic-row">
                  <span class="generic-label">命令白名单</span>
                  <el-input
                    v-model="toolsForm.command_allowlist"
                    placeholder="python, git status, npm test（命中即自动执行，无需确认）"
                    size="small"
                    class="generic-select"
                  />
                </div>
                <div class="generic-row">
                  <span class="generic-label">敏感操作确认</span>
                  <el-select v-model="toolsForm.tool_permission_mode" size="small" class="generic-select">
                    <el-option label="每次确认（推荐）" value="ask" />
                    <el-option label="自动批准（跳过确认）" value="allow" />
                  </el-select>
                </div>
                <div class="generic-row">
                  <span class="generic-label">确认超时（秒）</span>
                  <el-input-number
                    v-model="toolsForm.permission_timeout"
                    :min="30"
                    :max="3600"
                    :step="30"
                    size="small"
                  />
                </div>
                <div class="generic-row">
                  <span class="generic-label">执行超时（秒）</span>
                  <el-input-number v-model="toolsForm.command_timeout" :min="5" :max="300" size="small" />
                </div>
                <div class="generic-row">
                  <span class="generic-label">子代理并行</span>
                  <el-switch v-model="toolsForm.agent_subagents_enabled" size="small" />
                </div>
                <div class="generic-row">
                  <span class="generic-label">子代理最大轮数</span>
                  <el-input-number
                    v-model="toolsForm.agent_subagent_max_rounds"
                    :min="1"
                    :max="5"
                    size="small"
                  />
                </div>
                <div class="generic-row">
                  <span class="generic-label">写后验证命令</span>
                  <el-input
                    v-model="toolsForm.verify_command"
                    placeholder="如 pytest（留空=按文件类型自动检测）"
                    size="small"
                    class="generic-select"
                  />
                </div>
                <div class="generic-row">
                  <span class="generic-label">类型自动验证</span>
                  <el-switch v-model="toolsForm.verify_auto_detect" size="small" />
                  <span class="generic-hint">.py→py_compile / .js→node --check / JSON/YAML 语法</span>
                </div>
                <div class="generic-row">
                  <span class="generic-label">验证重试上限</span>
                  <el-input-number
                    v-model="toolsForm.verify_max_retries"
                    :min="0"
                    :max="5"
                    size="small"
                  />
                </div>
                <div class="generic-row">
                  <span class="generic-label">命令执行环境</span>
                  <el-select v-model="toolsForm.command_sandbox" size="small" class="generic-select">
                    <el-option label="本机子进程（默认）" value="subprocess" />
                    <el-option label="Docker 沙箱" value="docker" />
                  </el-select>
                </div>
                <div class="generic-row">
                  <span class="generic-label">沙箱镜像</span>
                  <el-input
                    v-model="toolsForm.sandbox_image"
                    placeholder="python:3.11-slim"
                    size="small"
                    class="generic-select"
                  />
                </div>
                <div class="generic-row">
                  <span class="generic-label">沙箱内工作目录只读</span>
                  <el-switch v-model="toolsForm.sandbox_workspace_readonly" size="small" />
                </div>
                <div class="generic-row">
                  <span class="generic-label">技能沙箱执行</span>
                  <el-switch v-model="toolsForm.skill_sandbox_enabled" size="small" />
                </div>
              </div>
              <div class="provider-hint">
                白名单命令自动放行；未命中的命令在“每次确认”模式下仍会弹窗请求批准。
                “Docker 沙箱”把 bash 放进容器执行；“技能沙箱执行”只控制技能命令的
                执行接口，不影响技能是否加载/注入上下文。“工作目录只读”开启后，
                项目以只读挂载，写入发生在沙箱内存盘 /scratch。
                “子代理并行”把计划里的工具型步骤派给独立上下文的子代理同时执行。
                “自动批准”模式相当于 Claude Code 的 --dangerously-skip-permissions，请谨慎使用。
              </div>
              <div class="tools-test-row">
                <el-button size="small" @click="testTool('file')">测试：列出工作目录</el-button>
                <el-button size="small" @click="testTool('command')">测试：执行命令</el-button>
              </div>
            </div>

            <div class="settings-section">
              <div class="section-head">
                <span class="settings-label">MCP 服务器</span>
                <el-switch v-model="toolsForm.mcp_enabled" size="small" />
                <el-button size="small" type="primary" plain @click="openMcpDialog()">
                  <el-icon :size="13"><Plus /></el-icon>
                  添加服务器
                </el-button>
              </div>
              <div class="provider-hint">
                MCP 工具会自动注册为 Agent 可用工具（stdio 本地进程 / HTTP 远程）。
              </div>
              <div v-if="!mcpServers.length" class="provider-empty">
                还没有 MCP 服务器，点击"添加服务器"接入外部能力
              </div>
              <div v-for="s in mcpServers" :key="s.id" class="provider-card">
                <div class="provider-head">
                  <span class="provider-name">{{ s.name }}</span>
                  <el-tag size="small" effect="plain" :type="s.type === 'http' ? 'success' : 'primary'">
                    {{ s.type === 'http' ? 'HTTP' : 'stdio' }}
                  </el-tag>
                  <span class="provider-flex"></span>
                  <el-button link size="small" @click="testMcp(s)">测试</el-button>
                  <el-switch v-model="s.enabled" size="small" @change="saveMcpList" />
                  <el-button link type="danger" size="small" @click="removeMcp(s)">删除</el-button>
                </div>
                <div class="provider-grid">
                  <div class="field wide">
                    <label>{{ s.type === 'http' ? 'URL' : '命令' }}</label>
                    <el-input
                      v-model="s.command"
                      size="small"
                      :placeholder="s.type === 'http' ? 'https://host/mcp' : 'npx -y @modelcontextprotocol/server-filesystem'"
                    />
                  </div>
                </div>
              </div>
            </div>

            <div class="settings-section">
              <div class="section-head">
                <span class="settings-label">技能沙箱</span>
                <el-switch v-model="toolsForm.skill_sandbox_enabled" size="small" />
              </div>
              <div class="settings-tip">
                开启后可在技能页预览并执行 SKILL.md 中的命令，仍受命令白名单与超时限制。
              </div>
            </div>

            <div class="settings-section">
              <div class="section-head">
                <span class="settings-label">项目记忆（AGENTS.md）</span>
                <el-button size="small" @click="exportProjectMemory">导出</el-button>
                <el-button size="small" @click="viewProjectMemory">查看</el-button>
              </div>
              <div class="settings-tip">
                把用户画像与长期记忆导出为可编辑文件（类 CLAUDE.md / AGENTS.md），
                每次会话自动加载进上下文。
              </div>
            </div>

            <div class="settings-section">
              <div class="settings-label">高级开关</div>
              <div class="generic-form">
                <div class="generic-row">
                  <span class="generic-label">思考摘要（已深度思考）</span>
                  <el-switch v-model="toolsForm.reasoning_summary_enabled" size="small" />
                </div>
                <div class="generic-row">
                  <span class="generic-label">会话 checkpoint 恢复</span>
                  <el-switch v-model="toolsForm.checkpoint_enabled" size="small" />
                </div>
                <div class="generic-row">
                  <span class="generic-label">轨迹压缩摘要</span>
                  <el-switch v-model="toolsForm.trajectory_compress_enabled" size="small" />
                </div>
              </div>
            </div>

            <div class="settings-save-row">
              <el-button type="primary" :loading="toolsSaving" @click="saveToolsSettings">
                保存工具设置
              </el-button>
            </div>
          </div>
        </main>
      </div>
    </el-dialog>

    <!-- 新增供应商对话框 -->
    <el-dialog
      v-model="providerDialogVisible"
      :title="providerDialogType === 'chat' ? '添加对话模型供应商' : '添加视觉模型供应商'"
      width="460px"
      append-to-body
    >
      <el-form :model="providerForm" label-position="top">
        <el-form-item label="供应商名称">
          <el-input v-model="providerForm.name" placeholder="例如：DeepSeek / 自定义 OpenAI 兼容" />
        </el-form-item>
        <el-form-item label="接口地址 Base URL">
          <el-input v-model="providerForm.base_url" placeholder="https://api.deepseek.com" />
        </el-form-item>
        <el-form-item label="默认模型">
          <el-input v-model="providerForm.model" placeholder="模型 ID，例如 deepseek-v4-flash" />
        </el-form-item>
        <el-form-item label="模型列表（逗号分隔，可选）">
          <el-input v-model="providerForm.modelsText" placeholder="model-a, model-b" />
        </el-form-item>
        <el-form-item label="API Key">
          <el-input v-model="providerForm.apiKey" type="password" show-password placeholder="sk-..." />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="providerDialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="providerSaving" @click="confirmAddProvider">
          添加
        </el-button>
      </template>
    </el-dialog>

    <!-- 添加 MCP 服务器 -->
    <el-dialog v-model="mcpDialogVisible" title="添加 MCP 服务器" width="480px" append-to-body>
      <el-form :model="mcpForm" label-position="top">
        <el-form-item label="名称">
          <el-input v-model="mcpForm.name" placeholder="例如：filesystem / database" />
        </el-form-item>
        <el-form-item label="传输类型">
          <el-radio-group v-model="mcpForm.type">
            <el-radio-button value="stdio">stdio（本地进程）</el-radio-button>
            <el-radio-button value="http">HTTP（远程）</el-radio-button>
          </el-radio-group>
        </el-form-item>
        <el-form-item v-if="mcpForm.type === 'stdio'" label="启动命令">
          <el-input v-model="mcpForm.command" placeholder="npx -y @modelcontextprotocol/server-filesystem" />
        </el-form-item>
        <el-form-item v-else label="URL">
          <el-input v-model="mcpForm.url" placeholder="https://host/mcp" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="mcpDialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="mcpTesting" @click="addMcpServer">
          添加
        </el-button>
      </template>
    </el-dialog>

    <!-- 项目记忆查看 -->
    <el-dialog v-model="memoryDialogVisible" title="项目记忆（AGENTS.md）" width="640px" append-to-body>
      <div class="memory-path">{{ memoryPath }}</div>
      <el-input
        v-model="memoryContent"
        type="textarea"
        :rows="16"
        readonly
        class="memory-textarea"
      />
    </el-dialog>
  </el-container>
</template>

<script setup>
import { computed, onMounted, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  Brush,
  Calendar,
  ChatDotRound,
  CircleCheckFilled,
  Close,
  Connection,
  Cpu,
  DataAnalysis,
  Delete,
  Document,
  EditPen,
  Expand,
  Fold,
  FolderOpened,
  Hide,
  Link,
  MagicStick,
  Plus,
  Search,
  Setting,
} from '@element-plus/icons-vue'
import {
  executeTool,
  exportProjectMemory as apiExportProjectMemory,
  getProjectMemory,
  getSettings,
  listSkills,
  listMcpServers,
  resetSkillPrefs,
  saveSettings,
  saveSkillPrefs,
  saveMcpServers,
  searchSkills,
  testMcpServer,
} from './api'

const navCollapsed = ref(false)
const settingsVisible = ref(false)
const activeTab = ref('appearance')

// 左侧功能导航
const navItems = [
  { key: 'appearance', label: '外观', desc: '主题与内容布局', icon: Brush },
  { key: 'models', label: '模型与供应商', desc: '对话 / 视觉 / 联网', icon: Cpu },
  { key: 'skills', label: '技能', desc: '本机 Skills 管理', icon: MagicStick },
  { key: 'tools', label: '工具与集成', desc: 'MCP / 执行 / 记忆', icon: Connection },
]

// ---------------- 外观 ----------------
const THEME_KEY = 'rag_theme'
const WIDTH_KEY = 'rag_content_width'
const theme = ref('system')
const contentWidth = ref('1200px')

const themeOptions = [
  { value: 'light', label: '浅色' },
  { value: 'dark', label: '深色' },
  { value: 'system', label: '跟随系统' },
]
const widthOptions = [
  { label: '窄', value: '55%' },
  { label: '标准', value: '70%' },
  { label: '宽', value: '85%' },
  { label: '全宽', value: '100%' },
]

const widthLabel = computed(
  () => widthOptions.find((o) => o.value === contentWidth.value)?.label || contentWidth.value,
)

function resolveDark() {
  if (theme.value === 'dark') return true
  if (theme.value === 'light') return false
  return window.matchMedia('(prefers-color-scheme: dark)').matches
}

function applyTheme() {
  const dark = resolveDark()
  document.documentElement.classList.toggle('dark', dark)
  document.documentElement.dataset.theme = dark ? 'dark' : 'light'
}

function applyWidth() {
  document.documentElement.style.setProperty(
    '--chat-content-width',
    contentWidth.value,
  )
}

watch(theme, () => {
  applyTheme()
  localStorage.setItem(THEME_KEY, theme.value)
})
watch(contentWidth, () => {
  applyWidth()
  localStorage.setItem(WIDTH_KEY, contentWidth.value)
})

// ---------------- 模型与供应商 ----------------
const settingsModel = ref(null)
const providers = ref([])
const toolsForm = ref({
  advanced_tools_enabled: false,
  agent_subagents_enabled: true,
  agent_subagent_max_rounds: 2,
  verify_command: '',
  verify_auto_detect: true,
  verify_max_retries: 1,
  tool_workspace: '',
  command_allowlist: '',
  command_timeout: 60,
  command_sandbox: 'subprocess',
  sandbox_image: 'python:3.11-slim',
  sandbox_workspace_readonly: false,
  tool_permission_mode: 'ask',
  permission_timeout: 300,
  skill_sandbox_enabled: false,
  reasoning_summary_enabled: true,
  checkpoint_enabled: true,
  trajectory_compress_enabled: true,
  mcp_enabled: true,
})
const toolsSaving = ref(false)

// MCP 服务器管理
const mcpServers = ref([])
const mcpDialogVisible = ref(false)
const mcpTesting = ref(false)
const mcpForm = ref({ name: '', type: 'stdio', command: '', url: '' })

// 项目记忆查看
const memoryDialogVisible = ref(false)
const memoryPath = ref('')
const memoryContent = ref('')
const activeChatId = ref('')
const activeVisionId = ref('')
const temperature = ref(0.5)
const webProvider = ref('duckduckgo')
const tavilyKey = ref('')
const tavilyMasked = ref('')
const webMaxResults = ref(6)
const modelSaving = ref(false)

const providerDialogVisible = ref(false)
const providerDialogType = ref('chat')
const providerSaving = ref(false)
const providerForm = ref({ name: '', base_url: '', model: '', modelsText: '', apiKey: '' })

const chatProviders = computed(() => providers.value.filter((p) => p.type === 'chat'))
const visionProviders = computed(() => providers.value.filter((p) => p.type === 'vision'))

async function loadSettings() {
  try {
    settingsModel.value = await getSettings()
    const raw = settingsModel.value.providers || []
    providers.value = raw.map((p) => ({
      ...p,
      _keyInput: '',
      _modelsText: (p.models || []).join(', '),
    }))
    activeChatId.value =
      settingsModel.value.active_chat_provider || chatProviders.value[0]?.id || ''
    activeVisionId.value =
      settingsModel.value.active_vision_provider || visionProviders.value[0]?.id || ''
    temperature.value = settingsModel.value.editable.chat_temperature ?? 0.5
    webProvider.value = settingsModel.value.editable.web_search_provider || 'duckduckgo'
    webMaxResults.value = settingsModel.value.editable.web_search_max_results || 6
    tavilyMasked.value = settingsModel.value.editable.tavily_api_key || ''
    tavilyKey.value = ''
    const editable = settingsModel.value.editable || {}
    toolsForm.value = {
      advanced_tools_enabled: editable.advanced_tools_enabled !== false,
      agent_subagents_enabled: editable.agent_subagents_enabled !== false,
      agent_subagent_max_rounds: editable.agent_subagent_max_rounds ?? 2,
      verify_command: editable.verify_command || '',
      verify_auto_detect: editable.verify_auto_detect !== false,
      verify_max_retries: editable.verify_max_retries ?? 1,
      tool_workspace: editable.tool_workspace || '',
      command_allowlist: editable.command_allowlist || '',
      command_timeout: editable.command_timeout ?? 60,
      command_sandbox: editable.command_sandbox || 'subprocess',
      sandbox_image: editable.sandbox_image || 'python:3.11-slim',
      sandbox_workspace_readonly: editable.sandbox_workspace_readonly === true,
      tool_permission_mode: editable.tool_permission_mode || 'ask',
      permission_timeout: editable.permission_timeout ?? 300,
      skill_sandbox_enabled: editable.skill_sandbox_enabled === true,
      reasoning_summary_enabled: editable.reasoning_summary_enabled !== false,
      checkpoint_enabled: editable.checkpoint_enabled !== false,
      trajectory_compress_enabled: editable.trajectory_compress_enabled !== false,
      mcp_enabled: editable.mcp_enabled !== false,
    }
  } catch (error) {
    ElMessage.error(error.response?.data?.detail || '加载设置失败')
  }
}

function openProviderDialog(type) {
  providerDialogType.value = type
  providerForm.value = {
    name: '',
    base_url: type === 'vision' ? 'https://token.sensenova.cn/v1' : '',
    model: '',
    modelsText: '',
    apiKey: '',
  }
  providerDialogVisible.value = true
}

async function confirmAddProvider() {
  const form = providerForm.value
  if (!form.name.trim() || !form.base_url.trim() || !form.model.trim()) {
    ElMessage.warning('名称、接口地址和默认模型不能为空')
    return
  }
  providerSaving.value = true
  try {
    const models = form.modelsText
      ? form.modelsText.split(/[,，]/).map((m) => m.trim()).filter(Boolean)
      : [form.model.trim()]
    if (!models.includes(form.model.trim())) models.unshift(form.model.trim())
    providers.value.push({
      id: `p_${Date.now()}`,
      name: form.name.trim(),
      type: providerDialogType.value,
      base_url: form.base_url.trim(),
      api_key: form.apiKey.trim() || '',
      model: form.model.trim(),
      models,
      enabled: true,
      note: '',
      _keyInput: '',
      _modelsText: models.join(', '),
    })
    const added = providers.value[providers.value.length - 1]
    if (providerDialogType.value === 'chat') activeChatId.value = added.id
    if (providerDialogType.value === 'vision') activeVisionId.value = added.id
    providerDialogVisible.value = false
    await saveModelSettings(true)
    ElMessage.success('供应商已添加')
  } catch (error) {
    ElMessage.error(error.response?.data?.detail || '添加失败')
  } finally {
    providerSaving.value = false
  }
}

function onProviderEnabledChange(p) {
  if (!p.enabled) {
    if (p.type === 'chat' && activeChatId.value === p.id) {
      activeChatId.value = chatProviders.value.find((x) => x.id !== p.id && x.enabled)?.id || ''
    }
    if (p.type === 'vision' && activeVisionId.value === p.id) {
      activeVisionId.value = visionProviders.value.find((x) => x.id !== p.id && x.enabled)?.id || ''
    }
  }
}

async function removeProvider(p) {
  try {
    await ElMessageBox.confirm(
      `确定删除供应商「${p.name}」吗？`,
      '提示',
      { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  providers.value = providers.value.filter((x) => x.id !== p.id)
  if (activeChatId.value === p.id) activeChatId.value = chatProviders.value[0]?.id || ''
  if (activeVisionId.value === p.id) activeVisionId.value = visionProviders.value[0]?.id || ''
  await saveModelSettings(true)
  ElMessage.success('供应商已删除')
}

async function saveModelSettings(silent = false) {
  modelSaving.value = true
  try {
    const updates = {
      providers: providers.value.map((p) => ({
        id: p.id,
        name: p.name,
        type: p.type,
        base_url: p.base_url,
        api_key: p._keyInput || p.api_key,
        model: p.model,
        models: p._modelsText
          ? p._modelsText.split(/[,，]/).map((m) => m.trim()).filter(Boolean)
          : [p.model],
        enabled: p.enabled,
        note: p.note || '',
      })),
      active_chat_provider: activeChatId.value,
      active_vision_provider: activeVisionId.value,
      chat_temperature: temperature.value,
      web_search_provider: webProvider.value,
      web_search_max_results: webMaxResults.value,
    }
    if (tavilyKey.value.trim()) updates.tavily_api_key = tavilyKey.value.trim()
    await saveSettings(updates)
    await loadSettings()
    if (!silent) ElMessage.success('设置已保存并生效')
  } catch (error) {
    ElMessage.error(error.response?.data?.detail || '保存设置失败')
  } finally {
    modelSaving.value = false
  }
}

// ---------------- 工具与集成 ----------------

async function loadMcp() {
  try {
    const data = await listMcpServers()
    mcpServers.value = data.servers || []
  } catch {
    mcpServers.value = []
  }
}

async function saveToolsSettings() {
  toolsSaving.value = true
  try {
    const f = toolsForm.value
    await saveSettings({
      advanced_tools_enabled: f.advanced_tools_enabled,
      agent_subagents_enabled: f.agent_subagents_enabled,
      agent_subagent_max_rounds: f.agent_subagent_max_rounds,
      verify_command: f.verify_command,
      verify_auto_detect: f.verify_auto_detect,
      verify_max_retries: f.verify_max_retries,
      tool_workspace: f.tool_workspace,
      command_allowlist: f.command_allowlist,
      command_timeout: f.command_timeout,
      command_sandbox: f.command_sandbox,
      sandbox_image: f.sandbox_image,
      sandbox_workspace_readonly: f.sandbox_workspace_readonly,
      tool_permission_mode: f.tool_permission_mode,
      permission_timeout: f.permission_timeout,
      skill_sandbox_enabled: f.skill_sandbox_enabled,
      reasoning_summary_enabled: f.reasoning_summary_enabled,
      checkpoint_enabled: f.checkpoint_enabled,
      trajectory_compress_enabled: f.trajectory_compress_enabled,
      mcp_enabled: f.mcp_enabled,
    })
    await loadSettings()
    ElMessage.success('工具设置已保存并生效')
  } catch (error) {
    ElMessage.error(error.response?.data?.detail || '保存失败')
  } finally {
    toolsSaving.value = false
  }
}

async function testTool(type) {
  try {
    if (type === 'command') {
      const { value } = await ElMessageBox.prompt(
        '输入要测试的命令（必须命中白名单）',
        '测试执行',
        {
          inputValue: 'dir',
          confirmButtonText: '执行',
          cancelButtonText: '取消',
          inputPlaceholder: '例如：dir / python -V',
        },
      )
      const result = await executeTool({ type: 'command', command: value })
      ElMessageBox.alert(
        result.error || result.output || '（无输出）',
        `执行结果（exit=${result.exit_code ?? '-'}）`,
        { confirmButtonText: '知道了' },
      )
    } else {
      const result = await executeTool({ type: 'file', operation: 'list', path: '.' })
      const names = (result.entries || []).map((e) => e.name).join('\n') || '（空目录）'
      ElMessageBox.alert(
        result.error || `工作目录：${result.workspace || '-'}\n\n${names}`,
        '文件工具测试',
        { confirmButtonText: '知道了' },
      )
    }
  } catch (error) {
    if (error === 'cancel' || error === 'close') return
    ElMessage.error(error.response?.data?.detail || '测试失败')
  }
}

function openMcpDialog() {
  mcpForm.value = { name: '', type: 'stdio', command: '', url: '' }
  mcpDialogVisible.value = true
}

async function addMcpServer() {
  const f = mcpForm.value
  if (!f.name.trim() || (f.type === 'stdio' ? !f.command.trim() : !f.url.trim())) {
    ElMessage.warning('名称和启动命令/URL 不能为空')
    return
  }
  mcpTesting.value = true
  try {
    const cfg = {
      id: `mcp_${Date.now()}`,
      name: f.name.trim(),
      type: f.type,
      command: f.command.trim(),
      args: [],
      url: f.url.trim(),
      enabled: true,
    }
    mcpServers.value.push(cfg)
    await saveMcpList()
    mcpDialogVisible.value = false
    ElMessage.success('MCP 服务器已添加')
  } catch (error) {
    ElMessage.error(error.response?.data?.detail || '添加失败')
  } finally {
    mcpTesting.value = false
  }
}

async function saveMcpList() {
  try {
    await saveMcpServers(mcpServers.value)
  } catch (error) {
    ElMessage.error(error.response?.data?.detail || '保存 MCP 配置失败')
  }
}

async function testMcp(s) {
  try {
    const result = await testMcpServer(s)
    if (result.ok) {
      ElMessageBox.alert(
        `连接成功，工具：${(result.tools || []).join(', ') || '（无）'}`,
        'MCP 测试',
        { confirmButtonText: '知道了' },
      )
    } else {
      ElMessageBox.alert(result.error || '连接失败', 'MCP 测试', { confirmButtonText: '知道了' })
    }
  } catch (error) {
    ElMessage.error(error.response?.data?.detail || '测试失败')
  }
}

async function removeMcp(s) {
  try {
    await ElMessageBox.confirm(`确定删除 MCP 服务器「${s.name}」吗？`, '提示', {
      type: 'warning',
      confirmButtonText: '删除',
      cancelButtonText: '取消',
    })
  } catch {
    return
  }
  mcpServers.value = mcpServers.value.filter((x) => x.id !== s.id)
  await saveMcpList()
}

async function exportProjectMemory() {
  try {
    const result = await apiExportProjectMemory()
    if (result.ok) {
      ElMessage.success(`已导出：${result.path}`)
    } else {
      ElMessage.error(result.error || '导出失败')
    }
  } catch (error) {
    ElMessage.error(error.response?.data?.detail || '导出失败')
  }
}

async function viewProjectMemory() {
  try {
    const data = await getProjectMemory()
    memoryPath.value = data.path || ''
    memoryContent.value = data.content || '（暂无内容，先点击"导出"生成）'
    memoryDialogVisible.value = true
  } catch (error) {
    ElMessage.error(error.response?.data?.detail || '读取失败')
  }
}

// ---------------- 技能 ----------------
const skills = ref([])
const hiddenSkills = ref([])
const skillsEnabled = ref(true)
const skillQuery = ref('')
const skillSourceFilter = ref('all')
const skillGroupFilter = ref('all')
const skillsLoading = ref(false)

const groupOptions = {
  documents: '文档处理',
  writing: '写作内容',
  research: '研究资料',
  productivity: '效率办公',
  design: '设计创意',
  coding: '编程调试',
  github: 'GitHub 协作',
  'data-ml': '数据与 AI',
  media: '媒体处理',
  email: '邮件沟通',
}

const sourceFilters = [
  { value: 'all', label: '全部' },
  { value: 'codex', label: 'Codex' },
  { value: 'hermes', label: 'Hermes' },
  { value: 'claude', label: 'Claude' },
]

const enabledCount = computed(() => skills.value.filter((s) => s.enabled).length)
const filteredSkills = computed(() => {
  const q = skillQuery.value.trim().toLowerCase()
  return skills.value.filter((s) => {
    const primary = (s.source || '').split(' + ')[0]
    if (skillSourceFilter.value !== 'all' && primary !== skillSourceFilter.value) return false
    if (skillGroupFilter.value !== 'all' && (s.group || 'other') !== skillGroupFilter.value) {
      return false
    }
    if (!q) return true
    return (
      s.name.toLowerCase().includes(q) ||
      s.description.toLowerCase().includes(q) ||
      (s.tags || []).some((t) => t.toLowerCase().includes(q))
    )
  })
})

async function refreshSkills() {
  skillsLoading.value = true
  try {
    const data = await listSkills(true)
    skills.value = (data.items || []).filter((s) => !s.hidden)
    hiddenSkills.value = (data.items || []).filter((s) => s.hidden)
  } catch {
    skills.value = []
    hiddenSkills.value = []
  } finally {
    skillsLoading.value = false
  }
}

async function onSkillSearch() {
  const q = skillQuery.value.trim()
  if (!q) {
    await refreshSkills()
    return
  }
  try {
    const data = await searchSkills(q)
    skills.value = (data.items || []).filter((s) => !s.hidden)
  } catch {
    // ignore
  }
}

function switchSourceFilter(value) {
  skillSourceFilter.value = value
  if (skillQuery.value.trim()) {
    onSkillSearch()
  } else {
    refreshSkills()
  }
}

async function persistSkills() {
  const enabled = skills.value.filter((s) => s.enabled).map((s) => s.name)
  const hidden = hiddenSkills.value.map((s) => s.name)
  try {
    await saveSkillPrefs(enabled, hidden)
  } catch (error) {
    ElMessage.error(error.response?.data?.detail || '保存技能偏好失败')
  }
}

async function hideSkill(s) {
  try {
    await ElMessageBox.confirm(
      `确定从本助手移除技能「${s.name}」吗？\n这只影响本助手（skill_lookup 不再使用它），不会修改其他 Agent 的技能文件。`,
      '提示',
      { type: 'info', confirmButtonText: '移除', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  s.enabled = false
  hiddenSkills.value.push(s)
  skills.value = skills.value.filter((x) => x.name !== s.name)
  await persistSkills()
}

async function restoreSkill(s) {
  hiddenSkills.value = hiddenSkills.value.filter((x) => x.name !== s.name)
  s.hidden = false
  s.enabled = true
  skills.value.push(s)
  skills.value.sort((a, b) => a.source.localeCompare(b.source) || a.name.localeCompare(b.name))
  await persistSkills()
}

async function handleResetSkills() {
  try {
    await ElMessageBox.confirm(
      '将恢复为默认精选手集（启用最实用的技能，隐藏项清空）。确定继续吗？',
      '提示',
      { type: 'warning' },
    )
  } catch {
    return
  }
  try {
    await resetSkillPrefs()
    await refreshSkills()
    ElMessage.success('已恢复默认精选手集')
  } catch (error) {
    ElMessage.error(error.response?.data?.detail || '恢复失败')
  }
}

async function saveSkillsEnabled() {
  try {
    await saveSettings({ skills_enabled: skillsEnabled.value })
    ElMessage.success(skillsEnabled.value ? '技能工具已开启' : '技能工具已关闭')
  } catch (error) {
    ElMessage.error(error.response?.data?.detail || '保存失败')
  }
}

// ---------------- 工具函数 ----------------

function skillIcon(s) {
  const name = s.name.toLowerCase()
  const tags = (s.tags || []).map((t) => t.toLowerCase()).join(' ')
  const hay = `${name} ${s.description.toLowerCase()} ${tags}`
  if (/pdf|docx|xlsx|powerpoint|ocr|document|nano-pdf/.test(hay)) return Document
  if (/write|draft|polish|humanize|resume|email|meeting|blog/.test(hay)) return EditPen
  if (/arxiv|research|paper|llm-wiki|citation|youtube/.test(hay)) return Search
  if (/notion|obsidian|calendar|airtable|google|map|notes|teams/.test(hay)) return Calendar
  if (/design|canvas|diagram|infographic|frontend|ascii|web/.test(hay)) return Brush
  if (/debug|python|node|github|code|test|review|spike|plan/.test(hay)) return Cpu
  if (/link|cite|source/.test(hay)) return Link
  return MagicStick
}

function sourceLabel(source) {
  const primary = (source || '').split(' + ')[0]
  return (
    {
      codex: 'Codex',
      hermes: 'Hermes',
      claude: 'Claude',
    }[primary] || primary
  )
}

function sourceTagType(source) {
  const primary = (source || '').split(' + ')[0]
  return (
    {
      codex: 'primary',
      hermes: 'success',
      claude: 'info',
    }[primary] || 'info'
  )
}

function groupLabel(group) {
  return groupOptions[group] || group || '其他'
}

watch(settingsVisible, (visible) => {
  if (visible) {
    loadSettings()
    refreshSkills()
    loadMcp()
  }
})

// 记住导航折叠状态
try {
  navCollapsed.value = localStorage.getItem('rag_nav_collapsed') === '1'
  theme.value = localStorage.getItem(THEME_KEY) || 'system'
  const savedWidth = localStorage.getItem(WIDTH_KEY)
  // 旧版 px 档位迁移到按比例档位
  const legacyMap = { '960px': '55%', '1200px': '70%', '1440px': '85%', none: '100%' }
  contentWidth.value = legacyMap[savedWidth] || savedWidth || '70%'
} catch {
  // ignore
}

watch(navCollapsed, (value) => {
  try {
    localStorage.setItem('rag_nav_collapsed', value ? '1' : '0')
  } catch {
    // ignore
  }
})

onMounted(() => {
  applyTheme()
  applyWidth()
  window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', applyTheme)
})
</script>

<style>
/* ==================== 全局导航 ==================== */
.app-container {
  height: 100%;
}

.app-aside {
  display: flex;
  flex-direction: column;
  background: var(--bg-sidebar);
  border-right: 1px solid var(--border);
  transition: width 0.25s ease;
  overflow: hidden;
}

.logo {
  display: flex;
  align-items: center;
  gap: 10px;
  height: 60px;
  padding: 0 16px;
  cursor: pointer;
  flex-shrink: 0;
}

.logo-mark {
  width: 34px;
  height: 34px;
  flex-shrink: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  border-radius: 10px;
  color: #fff;
  background: var(--primary-grad);
  box-shadow: var(--shadow-pop);
}

.logo-text {
  font-size: 16px;
  font-weight: 700;
  letter-spacing: 0.5px;
  color: var(--text-1);
  white-space: nowrap;
}

.app-menu {
  flex: 1;
  border-right: none;
  padding: 4px 10px;
  overflow-y: auto;
}

.app-menu:not(.el-menu--collapse) {
  width: 100%;
}

.app-menu .el-menu-item {
  height: 44px;
  margin-bottom: 4px;
  border-radius: 10px;
  color: var(--text-2);
}

.app-menu .el-menu-item:hover {
  background: var(--bg-hover);
  color: var(--primary);
}

.app-menu .el-menu-item.is-active {
  background: var(--bg-active);
  color: var(--primary);
  font-weight: 600;
}

.app-menu.el-menu--collapse {
  width: 64px;
}

.app-menu.el-menu--collapse .el-menu-item {
  justify-content: center;
  padding: 0 !important;
}

.app-menu.el-menu--collapse .el-menu-item .el-tooltip__trigger {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 100%;
}

.app-menu.el-menu--collapse .el-menu-item .el-icon {
  margin: 0 !important;
}

.aside-footer {
  display: flex;
  justify-content: center;
  gap: 4px;
  padding: 12px 0;
  border-top: 1px solid var(--border);
  flex-shrink: 0;
}

.collapse-btn {
  width: 36px;
  height: 36px;
  display: flex;
  align-items: center;
  justify-content: center;
  border-radius: 10px;
  color: var(--text-3);
  cursor: pointer;
  transition: all 0.2s;
}

.collapse-btn:hover {
  background: var(--bg-hover);
  color: var(--primary);
}

.app-main {
  padding: 16px;
  background: var(--bg-app);
  overflow: hidden;
}

.fade-enter-active,
.fade-leave-active {
  transition: opacity 0.2s;
}

.fade-enter-from,
.fade-leave-to {
  opacity: 0;
}

/* ==================== 设置：居中圆角卡片 ==================== */
.settings-modal {
  backdrop-filter: blur(6px) saturate(1.1);
  -webkit-backdrop-filter: blur(6px) saturate(1.1);
  background: rgba(12, 14, 18, 0.42) !important;
}

.settings-dialog {
  border-radius: 18px;
  overflow: hidden;
  padding: 0;
  background: var(--bg-panel);
  border: 1px solid var(--border);
  box-shadow: 0 24px 64px rgba(0, 0, 0, 0.22);
}

.settings-dialog .el-dialog__header {
  display: none;
}

.settings-dialog .el-dialog__body {
  padding: 0;
}

.settings-card {
  display: flex;
  flex-direction: column;
  height: 640px;
}

/* ---- 顶部横向导航 ---- */
.settings-topbar {
  flex-shrink: 0;
  display: flex;
  align-items: center;
  gap: 16px;
  padding: 12px 20px;
  border-bottom: 1px solid var(--border);
  background: var(--bg-card-2);
}

.settings-brand {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-shrink: 0;
}

.brand-mark {
  width: 30px;
  height: 30px;
  flex-shrink: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  border-radius: 9px;
  color: #fff;
  background: var(--primary-grad);
}

.brand-title {
  font-size: 15px;
  font-weight: 700;
  color: var(--text-1);
}

.settings-tabs {
  flex: 1;
  display: flex;
  gap: 6px;
  justify-content: center;
}

.settings-tab {
  padding: 0;
  border: none;
  background: transparent;
  cursor: pointer;
  border-radius: 10px;
}

.settings-tab-inner {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 8px 16px;
  border-radius: 10px;
  font-size: 13.5px;
  color: var(--text-2);
  transition: all 0.2s;
  user-select: none;
  white-space: nowrap;
}

.settings-tab:hover .settings-tab-inner {
  background: var(--bg-hover);
  color: var(--primary);
}

.settings-tab.active .settings-tab-inner {
  background: var(--primary-grad);
  color: #fff;
  font-weight: 600;
  box-shadow: var(--shadow-pop);
}

.settings-close {
  flex-shrink: 0;
  color: var(--text-3);
}

/* ---- 右侧内容 ---- */
.settings-content {
  flex: 1;
  min-width: 0;
  overflow-y: auto;
  padding: 24px 28px 28px;
}

.pane-head {
  margin-bottom: 18px;
}

.pane-title {
  font-size: 18px;
  font-weight: 700;
  color: var(--text-1);
}

.pane-sub {
  margin-top: 3px;
  font-size: 12.5px;
  color: var(--text-3);
}

.settings-section {
  margin-bottom: 22px;
}

.settings-label {
  font-size: 13px;
  font-weight: 600;
  color: var(--text-2);
  margin-bottom: 10px;
}

.section-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 8px;
}

.section-head .settings-label {
  margin-bottom: 0;
}

.settings-tip {
  margin-top: 10px;
  font-size: 12px;
  color: var(--text-3);
  line-height: 1.7;
}

.settings-save-row {
  padding-top: 4px;
}

.tools-test-row {
  display: flex;
  gap: 8px;
  margin-top: 10px;
}

.memory-path {
  font-size: 12px;
  color: var(--text-3);
  margin-bottom: 10px;
  word-break: break-all;
}

.memory-textarea :deep(.el-textarea__inner) {
  font-family: Consolas, Monaco, monospace;
  font-size: 12.5px;
  line-height: 1.6;
}

/* ---- 外观 ---- */
.theme-options {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 10px;
}

.theme-card {
  border: 1px solid var(--border);
  border-radius: 12px;
  padding: 10px;
  cursor: pointer;
  background: var(--bg-card-2);
  transition: all 0.2s;
}

.theme-card:hover {
  border-color: var(--primary-2);
}

.theme-card.active {
  border-color: var(--primary);
  background: var(--bg-active);
  box-shadow: 0 0 0 2px var(--primary-soft);
}

.theme-preview {
  height: 52px;
  border-radius: 8px;
  display: flex;
  overflow: hidden;
  border: 1px solid var(--border);
  margin-bottom: 8px;
}

.theme-preview.light {
  background: #f3f4f8;
}

.theme-preview.dark {
  background: #0e1014;
}

.theme-preview.system {
  background: linear-gradient(135deg, #f3f4f8 50%, #0e1014 50%);
}

.preview-side {
  width: 26%;
  background: rgba(127, 127, 127, 0.14);
  border-right: 1px solid rgba(127, 127, 127, 0.2);
}

.preview-main {
  flex: 1;
  padding: 8px;
  display: flex;
  flex-direction: column;
  gap: 5px;
}

.preview-line {
  height: 4px;
  border-radius: 2px;
  background: var(--primary);
  opacity: 0.75;
  width: 70%;
}

.preview-line.short {
  width: 45%;
  opacity: 0.35;
}

.preview-bubble {
  align-self: flex-end;
  width: 38%;
  height: 12px;
  border-radius: 6px 6px 2px 6px;
  background: var(--accent);
  opacity: 0.7;
}

.theme-meta {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.theme-name {
  font-size: 13px;
  color: var(--text-1);
}

.theme-check {
  color: var(--primary);
  font-size: 15px;
}

.width-segmented {
  width: 100%;
}

.width-segmented :deep(.el-segmented__item) {
  flex: 1;
}

.width-feedback {
  margin-top: 12px;
  display: flex;
  align-items: center;
  gap: 12px;
}

.width-preview-track {
  flex: 1;
  height: 8px;
  border-radius: 999px;
  background: var(--bg-hover);
  border: 1px solid var(--border);
  overflow: hidden;
}

.width-preview-bar {
  height: 100%;
  border-radius: 999px;
  background: var(--primary-grad);
  transition: width 0.25s ease;
}

.width-value {
  flex-shrink: 0;
  font-size: 12px;
  color: var(--text-3);
  min-width: 56px;
  text-align: right;
}

/* ---- 模型供应商 ---- */
.provider-empty {
  padding: 18px 12px;
  text-align: center;
  font-size: 12.5px;
  color: var(--text-3);
  border: 1px dashed var(--border-strong);
  border-radius: 10px;
  background: var(--bg-card-2);
}

.provider-card {
  border: 1px solid var(--border);
  border-radius: 12px;
  padding: 12px;
  margin-bottom: 10px;
  background: var(--bg-card-2);
  transition: all 0.2s;
}

.provider-card:hover {
  border-color: var(--border-strong);
}

.provider-card.inactive {
  opacity: 0.72;
}

.provider-head {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 10px;
}

.provider-radio :deep(.el-radio__label) {
  font-size: 12px;
  color: var(--text-3);
  padding-left: 4px;
}

.provider-name {
  font-size: 14px;
  font-weight: 600;
  color: var(--text-1);
}

.provider-flex {
  flex: 1;
}

.provider-del {
  margin-left: 2px;
}

.provider-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 10px;
}

.provider-grid .field {
  display: flex;
  flex-direction: column;
  gap: 5px;
}

.provider-grid .field.wide {
  grid-column: 1 / -1;
}

.provider-grid .field label {
  font-size: 12px;
  color: var(--text-3);
}

.provider-grid :deep(.el-input__wrapper),
.provider-grid :deep(.el-select__wrapper) {
  background: var(--bg-chat);
  box-shadow: 0 0 0 1px var(--border) inset;
}

.provider-grid :deep(.el-input__inner) {
  color: var(--text-1);
}

.generic-form {
  border: 1px solid var(--border);
  border-radius: 12px;
  padding: 6px 12px;
  background: var(--bg-card-2);
}

.generic-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 9px 0;
  border-bottom: 1px solid var(--border);
}

.generic-row:last-child {
  border-bottom: none;
}

.generic-label {
  font-size: 13px;
  color: var(--text-2);
  flex-shrink: 0;
}

.generic-select {
  width: 210px;
}

.generic-hint {
  font-size: 12px;
  color: var(--text-3, #999);
  line-height: 1.4;
}

/* ---- 技能 ---- */
.skill-master {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 12px 14px;
  border: 1px solid var(--border);
  border-radius: 12px;
  background: var(--bg-card-2);
  margin-bottom: 14px;
}

.skill-master-title {
  font-size: 14px;
  font-weight: 600;
  color: var(--text-1);
}

.skill-master-desc {
  margin-top: 3px;
  font-size: 12px;
  color: var(--text-3);
}

.skill-filter-row {
  display: flex;
  gap: 8px;
  margin-bottom: 10px;
}

.skill-search {
  flex: 1;
}

.skill-group-select {
  width: 140px;
  flex-shrink: 0;
}

.skill-source-filter {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-bottom: 12px;
}

.source-chip {
  padding: 4px 12px;
  border-radius: 999px;
  font-size: 12px;
  color: var(--text-2);
  background: var(--bg-card-2);
  border: 1px solid var(--border);
  cursor: pointer;
  transition: all 0.2s;
}

.source-chip:hover {
  color: var(--primary);
  border-color: var(--primary-2);
}

.source-chip.active {
  color: var(--primary);
  background: var(--bg-active);
  border-color: var(--primary);
  font-weight: 600;
}

.skill-summary {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 10px;
}

.skill-summary-text {
  font-size: 12px;
  color: var(--text-3);
}

.skills-loading,
.skills-empty {
  padding: 26px 0;
  text-align: center;
  color: var(--text-3);
  font-size: 13px;
}

.skill-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.skill-item {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 10px 12px;
  border: 1px solid var(--border);
  border-radius: 12px;
  background: var(--bg-card-2);
  transition: border-color 0.2s;
}

.skill-item:hover {
  border-color: var(--border-strong);
}

.skill-item.muted {
  opacity: 0.66;
}

.skill-icon {
  width: 34px;
  height: 34px;
  flex-shrink: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  border-radius: 10px;
  color: var(--primary);
  background: var(--bg-active);
}

.skill-main {
  flex: 1;
  min-width: 0;
}

.skill-head {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
}

.skill-name {
  font-weight: 600;
  font-size: 13.5px;
  color: var(--text-1);
}

.skill-desc {
  margin-top: 4px;
  font-size: 12px;
  color: var(--text-3);
  line-height: 1.5;
  overflow: hidden;
  text-overflow: ellipsis;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
}

.skill-ops {
  display: flex;
  align-items: center;
  gap: 4px;
  flex-shrink: 0;
}

.skill-remove {
  color: var(--text-3);
}

.skill-remove:hover {
  color: var(--danger);
}

.hidden-head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  margin: 18px 0 10px;
}

.hidden-head .settings-label {
  margin-bottom: 0;
}

.hidden-hint {
  font-size: 11.5px;
  color: var(--text-3);
}

.skill-footer {
  display: flex;
  justify-content: space-between;
  margin-top: 12px;
}
</style>
