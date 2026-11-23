<template>
  <div :class="['pw-page', viewClass]">
    <header class="pw-header">
      <div>
        <h2 class="pw-title">提示词工作台</h2>
        <div class="pw-subtitle">
          模板 / 片段 / 版本 / 影响面 / 试跑；编辑只影响新会话，旧会话需手动升级。
        </div>
      </div>
      <div class="pw-header-actions">
        <ViewLayoutSwitch v-model="pwView" :options="pwViewOptions" />
        <el-button size="small" @click="openImpact">影响面</el-button>
        <el-button size="small" :loading="auditLoading" @click="runAudit">体检</el-button>
        <el-button size="small" @click="exportData">导出</el-button>
        <el-button size="small" @click="triggerImport">导入</el-button>
        <el-button size="small" @click="setAdminToken">令牌</el-button>
      </div>
    </header>

    <div v-loading="loading" class="pw-grid">
      <aside class="pw-list">
        <div class="pw-list-head">
          <span>模板</span>
          <el-button size="small" text @click="createTemplateDraft">新建</el-button>
        </div>
        <div class="pw-list-body">
          <div
            v-for="item in templates"
            :key="`t-${item.id}`"
            :class="['pw-item', { active: selected.type === 'template' && selected.id === item.id }]"
            @click="selectTemplate(item)"
          >
            <div class="pw-item-title">
              <span>{{ item.name }}</span>
              <el-tag v-if="item.is_system" size="small" type="info">内置</el-tag>
              <el-tag v-if="item.archived" size="small" type="warning">归档</el-tag>
              <el-tag v-if="item.has_draft" size="small" type="success">草稿</el-tag>
            </div>
            <div class="pw-item-meta">
              v{{ item.latest_version_no ?? '?' }} · {{ item.conversation_count ?? 0 }} 个会话
            </div>
          </div>
        </div>

        <div class="pw-list-head pw-list-head-fragments">
          <span>片段</span>
        </div>
        <div class="pw-list-body">
          <div
            v-for="item in fragments"
            :key="`f-${item.key}`"
            :class="['pw-item', { active: selected.type === 'fragment' && selected.key === item.key }]"
            @click="selectFragment(item)"
          >
            <div class="pw-item-title">
              <span>{{ item.title }}</span>
              <el-tag v-if="item.is_customized" size="small" type="success">自定义</el-tag>
              <el-tag v-if="item.has_draft" size="small" type="warning">草稿</el-tag>
              <el-tag v-if="item.default_changed" size="small" type="danger">默认已更新</el-tag>
            </div>
            <div class="pw-item-meta">
              v{{ item.version_no ?? '?' }} · {{ positionLabel(item.position) }}
            </div>
          </div>
        </div>
      </aside>

      <main class="pw-editor">
        <template v-if="selected.item">
          <div class="pw-editor-head">
            <div>
              <div class="pw-editor-title">
                {{ selected.type === 'template' ? editor.name || '未命名模板' : selected.item.title }}
                <el-tag v-if="selected.type === 'fragment' && selected.item.default_changed" size="small" type="danger">
                  代码默认已更新
                </el-tag>
                <el-tag v-if="selected.item.has_draft" size="small" type="success">有草稿</el-tag>
              </div>
              <div class="pw-editor-sub">
                <template v-if="selected.type === 'template'">
                  {{ selected.item.is_system ? '内置模板：只读，可克隆' : '自定义模板' }}
                  <template v-if="selected.item.archived"> · 已归档</template>
                </template>
                <template v-else>
                  {{ selected.item.key }} · {{ positionLabel(selected.item.position) }}
                  <template v-if="selected.item.placeholder">
                    · 占位符 {{ selected.item.placeholder }}
                  </template>
                </template>
              </div>
            </div>
            <div class="pw-editor-actions">
              <el-button size="small" text @click="normalizeEditor">合并换行</el-button>
              <el-button size="small" text @click="openPreview">预览</el-button>
              <el-button
                v-if="selected.type === 'template'"
                size="small"
                text
                @click="cloneSelected"
              >
                克隆
              </el-button>
              <el-button
                v-if="selected.type === 'template' && !selected.item.is_system"
                size="small"
                text
                @click="toggleArchive"
              >
                {{ selected.item.archived ? '恢复' : '归档' }}
              </el-button>
              <el-button
                v-if="selected.type === 'fragment'"
                size="small"
                text
                @click="resetSelectedFragment"
              >
                恢复默认
              </el-button>
            </div>
          </div>

          <el-alert
            v-if="selected.type === 'fragment' && selected.item.default_changed"
            type="warning"
            :closable="false"
            class="pw-alert"
            title="代码内置默认值已更新；当前自定义内容基于旧默认。保存/发布会以当前代码默认作为新基线。"
          />

          <div v-if="selected.type === 'template'" class="pw-form">
            <el-form label-width="64px" size="small">
              <el-form-item label="名称">
                <el-input v-model="editor.name" :disabled="readOnly" />
              </el-form-item>
              <el-form-item label="分类">
                <el-select v-model="editor.category" :disabled="readOnly" style="width: 100%">
                  <el-option label="通用" value="general" />
                  <el-option label="知识库" value="knowledge" />
                  <el-option label="编程" value="coding" />
                  <el-option label="写作" value="writing" />
                  <el-option label="翻译" value="translate" />
                  <el-option label="学习" value="learning" />
                </el-select>
              </el-form-item>
              <el-form-item label="描述">
                <el-input v-model="editor.description" :disabled="readOnly" />
              </el-form-item>
            </el-form>
          </div>

          <el-input
            v-model="editor.content"
            type="textarea"
            :rows="18"
            :disabled="readOnly"
            class="pw-content"
            placeholder="提示词内容"
          />
          <div class="pw-editor-foot">
            <div class="pw-note-row">
              <el-input
                v-model="editor.note"
                size="small"
                :disabled="readOnly"
                placeholder="草稿说明（可选）"
              />
              <span class="pw-chars">{{ editor.content.length }} 字</span>
            </div>
            <div
              v-if="selected.type === 'template'"
              class="pw-muted pw-save-hint"
            >
              草稿只保存正文；名称/分类/描述在发布时写入。
            </div>
            <div class="pw-save-actions">
              <el-button size="small" :disabled="readOnly" :loading="saving" @click="saveDraft">
                保存草稿
              </el-button>
              <el-button
                v-if="selected.item.has_draft"
                size="small"
                :disabled="readOnly"
                :loading="saving"
                @click="publishSavedDraft"
              >
                发布草稿
              </el-button>
              <el-button
                v-if="selected.item.has_draft"
                size="small"
                text
                :disabled="readOnly"
                @click="discardDraft"
              >
                丢弃草稿
              </el-button>
              <el-button
                size="small"
                type="primary"
                :disabled="readOnly"
                :loading="publishing"
                @click="publish"
              >
                发布
              </el-button>
            </div>
          </div>
        </template>
        <el-empty v-else description="选择左侧模板或片段" />
      </main>

      <aside class="pw-side">
        <el-tabs v-model="sideTab">
          <el-tab-pane label="版本" name="versions">
            <div class="pw-side-actions">
              <el-button size="small" text @click="loadVersions">刷新</el-button>
              <el-button
                v-if="selected.type === 'template' && selectedVersion"
                size="small"
                text
                @click="openPin"
              >
                锁定到会话
              </el-button>
            </div>
            <div v-if="!versions.length" class="pw-muted">暂无版本</div>
            <div v-else class="pw-version-list">
              <div
                v-for="v in versions"
                :key="v.id"
                :class="['pw-version-item', { active: selectedVersion?.id === v.id }]"
                @click="selectVersion(v)"
              >
                <div class="pw-version-head">
                  <strong>v{{ v.version_no }}</strong>
                  <el-tag v-if="v.version_no === versions[0]?.version_no" size="small" type="success">
                    当前
                  </el-tag>
                </div>
                <div class="pw-version-meta">{{ v.chars }} 字 · {{ v.note || '无说明' }}</div>
              </div>
            </div>
            <template v-if="selectedVersion">
              <div class="pw-side-actions">
                <el-button size="small" text @click="versionDiffMode = !versionDiffMode">
                  {{ versionDiffMode ? '看原文' : '对比当前' }}
                </el-button>
                <el-button
                  size="small"
                  text
                  :disabled="selected.type === 'template' && selected.item?.is_system"
                  @click="restoreSelectedVersion"
                >
                  恢复此版本
                </el-button>
              </div>
              <div v-if="versionDiffMode" class="pw-diff">
                <div
                  v-for="(line, index) in versionDiff"
                  :key="index"
                  :class="['pw-diff-line', line.type]"
                >
                  <span class="pw-diff-sign">{{ line.sign }}</span>
                  <span>{{ line.text || ' ' }}</span>
                </div>
              </div>
              <pre v-else class="pw-version-content">{{ versionContent }}</pre>
            </template>
          </el-tab-pane>

          <el-tab-pane label="影响面" name="impact">
            <div class="pw-impact-summary">
              <div class="pw-impact-number">{{ impactCount }}</div>
              <div class="pw-muted">个会话锁定旧版本</div>
              <el-button size="small" type="primary" plain class="pw-impact-btn" @click="openImpact">
                查看并升级
              </el-button>
            </div>
          </el-tab-pane>

          <el-tab-pane label="A/B" name="ab">
            <div class="pw-muted">同一问题用两个模板版本试跑；不使用工具，结果会落库。</div>
            <el-select v-model="abVersionA" size="small" placeholder="版本 A" class="pw-full">
              <el-option
                v-for="v in versions"
                :key="`a-${v.id}`"
                :label="`v${v.version_no}`"
                :value="v.id"
              />
            </el-select>
            <el-select v-model="abVersionB" size="small" placeholder="版本 B" class="pw-full">
              <el-option
                v-for="v in versions"
                :key="`b-${v.id}`"
                :label="`v${v.version_no}`"
                :value="v.id"
              />
            </el-select>
            <el-input
              v-model="abQuestion"
              type="textarea"
              :rows="3"
              size="small"
              placeholder="输入同一道测试问题"
            />
            <el-button
              size="small"
              type="primary"
              :loading="trialLoading"
              :disabled="selected.type !== 'template' || !abQuestion.trim()"
              class="pw-full"
              @click="runAB"
            >
              开始 A/B 试跑
            </el-button>
            <div v-if="abResult" class="pw-trial-result">
              <div class="pw-trial-col">
                <strong>A · v{{ abResult.a_version }}</strong>
                <pre>{{ abResult.a.text }}</pre>
                <span class="pw-muted">{{ abResult.a.latency_ms }}ms · {{ abResult.a.chars }} 字</span>
              </div>
              <div class="pw-trial-col">
                <strong>B · v{{ abResult.b_version }}</strong>
                <pre>{{ abResult.b.text }}</pre>
                <span class="pw-muted">{{ abResult.b.latency_ms }}ms · {{ abResult.b.chars }} 字</span>
              </div>
            </div>
          </el-tab-pane>

          <el-tab-pane label="记录" name="trials">
            <div v-if="!trials.length" class="pw-muted">暂无试跑/评测记录</div>
            <div v-else class="pw-trial-list">
              <div v-for="trial in trials" :key="trial.id" class="pw-trial-item">
                <div class="pw-item-title">
                  <el-tag size="small" :type="trial.kind === 'ab' ? 'primary' : 'success'">
                    {{ trial.kind === 'ab' ? 'A/B' : '评测' }}
                  </el-tag>
                  <span class="pw-trial-question">{{ trial.question }}</span>
                </div>
                <div class="pw-item-meta">
                  {{ formatTime(trial.created_at) }} · {{ trial.status }}
                </div>
                <el-button size="small" text @click="viewTrial(trial)">查看结果</el-button>
              </div>
            </div>
          </el-tab-pane>

          <el-tab-pane label="评测" name="eval">
            <div class="pw-muted">对当前模板版本跑轻量问题集（无工具、无裁判，只记录回答）。</div>
            <el-input-number v-model="evalLimit" :min="1" :max="10" size="small" />
            <el-button
              size="small"
              type="primary"
              :loading="trialLoading"
              :disabled="selected.type !== 'template'"
              class="pw-full"
              @click="runEval"
            >
              运行冒烟评测
            </el-button>
            <div v-if="evalResult" class="pw-eval-result">
              <div v-for="item in evalResult.items" :key="item.id" class="pw-eval-item">
                <div class="pw-eval-question">{{ item.question }}</div>
                <pre v-if="item.ok">{{ item.text }}</pre>
                <div v-else class="pw-error">{{ item.error }}</div>
              </div>
            </div>
          </el-tab-pane>
        </el-tabs>
      </aside>
    </div>

    <PromptPreviewDialog
      v-model="previewVisible"
      :request="previewRequest"
      :title="selected.type === 'template' ? '模板预览' : '片段预览'"
    />
    <PromptImpactDialog v-model="impactVisible" @changed="loadImpactCount" />

    <el-dialog v-model="pinVisible" title="锁定到会话" width="480px" append-to-body>
      <div class="pw-muted">把当前模板的选中版本锁定到指定会话；下一轮缓存会失效一次。</div>
      <el-select v-model="pinConversationId" filterable placeholder="选择会话" class="pw-full">
        <el-option
          v-for="conv in conversations"
          :key="conv.id"
          :label="conv.title"
          :value="conv.id"
        />
      </el-select>
      <template #footer>
        <el-button @click="pinVisible = false">取消</el-button>
        <el-button type="primary" :loading="busy" @click="doPin">锁定</el-button>
      </template>
    </el-dialog>

    <input
      ref="importInput"
      type="file"
      accept="application/json,.json"
      class="pw-hidden"
      @change="onImportFile"
    />
  </div>
</template>

<script setup>
import { computed, onMounted, reactive, ref } from 'vue'
import { Grid, Edit, Reading } from '@element-plus/icons-vue'
import ViewLayoutSwitch from '../components/ViewLayoutSwitch.vue'
import { usePageLayout } from '../composables/usePageLayout'
import { ElMessage } from 'element-plus/es/components/message/index'
import { ElMessageBox } from 'element-plus/es/components/message-box/index'
import {
  archiveTemplate,
  auditPrompts,
  cloneTemplate,
  createTemplate,
  discardPromptFragmentDraft,
  discardTemplateDraft,
  exportPrompts,
  getDefaultTemplate,
  getPromptFragmentVersion,
  getPromptImpact,
  getTemplateVersion,
  importPrompts,
  listConversations,
  lintPromptFragment,
  lintTemplate,
  listPromptFragments,
  listPromptFragmentVersions,
  listPromptTrials,
  listTemplateVersions,
  listTemplates,
  normalizePrompt,
  pinConversationPrompt,
  previewPrompt,
  publishPromptFragmentDraft,
  publishTemplateDraft,
  resetPromptFragment,
  restorePromptFragment,
  restoreTemplateVersion,
  runPromptAB,
  runPromptEval,
  savePromptFragmentDraft,
  saveTemplateDraft,
  unarchiveTemplate,
  updatePromptFragment,
  updateTemplate,
} from '../api'
import PromptPreviewDialog from '../components/prompt/PromptPreviewDialog.vue'
import PromptImpactDialog from '../components/prompt/PromptImpactDialog.vue'
import { diffLines } from '../utils/promptDiff'

defineOptions({ name: 'PromptWorkbenchView' })

const loading = ref(false)

// 页面布局变体（三栏 / 编辑优先 / 预览优先），差异见 styles/view-variants.css
const pwViewOptions = [
  { value: 'three', label: '三栏', icon: Grid, hint: '三栏：列表 260 / 编辑 / 预览 340' },
  { value: 'editor', label: '编辑优先', icon: Edit, hint: '编辑优先：列表收窄，编辑区更宽' },
  { value: 'preview', label: '预览优先', icon: Reading, hint: '预览优先：预览列加宽到 440' },
]
const { variant: pwView, viewClass } = usePageLayout('rag_pw_view', pwViewOptions, 'three')
const saving = ref(false)
const publishing = ref(false)
const auditLoading = ref(false)
const busy = ref(false)
const templates = ref([])
const fragments = ref([])
const selected = ref({ type: 'template', id: null, key: '', item: null })
const editor = reactive({
  name: '',
  category: 'general',
  description: '',
  content: '',
  note: '',
  versionNote: '',
})
const versions = ref([])
const selectedVersion = ref(null)
const versionContent = ref('')
const versionDiffMode = ref(false)
const sideTab = ref('versions')
const previewVisible = ref(false)
const previewRequest = ref({})
const impactVisible = ref(false)
const impactCount = ref(0)
const importInput = ref(null)
const conversations = ref([])
const pinVisible = ref(false)
const pinConversationId = ref(null)
const abVersionA = ref(null)
const abVersionB = ref(null)
const abQuestion = ref('')
const abResult = ref(null)
const evalLimit = ref(3)
const evalResult = ref(null)
const trialLoading = ref(false)
const trials = ref([])

const readOnly = computed(() => {
  const item = selected.value.item
  if (!item) return true
  if (selected.value.type !== 'template') return false
  return !!item.is_system || !!item.archived
})

const versionDiff = computed(() => {
  if (!selectedVersion.value || !selected.value.item) return []
  return diffLines(selectedVersion.value.content || '', selected.value.item.content || '')
})

function positionLabel(position) {
  return {
    core: '静态核心',
    pre_history: '历史前动态块',
    post_history: '历史后动态块',
  }[position] || position || ''
}

function fillEditor(item) {
  editor.name = item.name || ''
  editor.category = item.category || 'general'
  editor.description = item.description || ''
  editor.content = item.draft_content ?? item.content ?? ''
  editor.note = item.draft_note || ''
  editor.versionNote = ''
}

async function loadAll() {
  loading.value = true
  try {
    const [tpls, frags] = await Promise.all([
      listTemplates(),
      listPromptFragments(),
    ])
    const defaultTpl = await getDefaultTemplate().catch(() => null)
    const list = Array.isArray(tpls) ? [...tpls] : []
    if (defaultTpl && !list.some((item) => item.id === defaultTpl.id)) {
      list.push({ ...defaultTpl, hidden: true })
    }
    templates.value = list
    fragments.value = frags.fragments || []
    if (!selected.value.item) {
      if (templates.value[0]) selectTemplate(templates.value[0])
      else if (fragments.value[0]) selectFragment(fragments.value[0])
    } else {
      const current = selected.value.type === 'template'
        ? templates.value.find((item) => item.id === selected.value.id)
        : fragments.value.find((item) => item.key === selected.value.key)
      if (current) {
        selected.value.item = current
        fillEditor(current)
      }
    }
    loadImpactCount()
    loadConversations()
    loadTrials()
  } catch (error) {
    ElMessage.error(error.response?.data?.detail || '加载提示词数据失败')
  } finally {
    loading.value = false
  }
}

function selectTemplate(item) {
  selected.value = { type: 'template', id: item.id, key: '', item }
  fillEditor(item)
  selectedVersion.value = null
  versionContent.value = ''
  versionDiffMode.value = false
  loadVersions()
  loadTrials()
}

function selectFragment(item) {
  selected.value = { type: 'fragment', id: null, key: item.key, item }
  fillEditor(item)
  selectedVersion.value = null
  versionContent.value = ''
  versionDiffMode.value = false
  loadVersions()
  loadTrials()
}

async function loadVersions() {
  const item = selected.value.item
  if (!item) return
  try {
    const data = selected.value.type === 'template'
      ? await listTemplateVersions(item.id, false)
      : await listPromptFragmentVersions(item.key, false)
    versions.value = data.versions || []
    abVersionA.value = versions.value[0]?.id || null
    abVersionB.value = versions.value[1]?.id || versions.value[0]?.id || null
    if (versions.value[0]) await selectVersion(versions.value[0])
    else {
      selectedVersion.value = null
      versionContent.value = ''
    }
  } catch (error) {
    ElMessage.error(error.response?.data?.detail || '加载版本失败')
  }
}

async function selectVersion(version) {
  selectedVersion.value = version
  versionDiffMode.value = false
  try {
    const data = selected.value.type === 'template'
      ? await getTemplateVersion(selected.value.id, version.id)
      : await getPromptFragmentVersion(selected.value.key, version.id)
    selectedVersion.value = { ...version, ...data }
    versionContent.value = data.content || ''
  } catch (error) {
    versionContent.value = version.content || ''
  }
}

async function createTemplateDraft() {
  try {
    const { value } = await ElMessageBox.prompt('模板名称', '新建模板', {
      confirmButtonText: '创建',
      cancelButtonText: '取消',
      inputPattern: /\S+/,
      inputErrorMessage: '名称不能为空',
    })
    const created = await createTemplate({
      name: value.trim(),
      description: '',
      category: 'general',
      content: '# 角色\n你是用户的私人助手，按用户要求严谨、简洁地完成任务。',
    })
    await loadAll()
    const item = templates.value.find((tpl) => tpl.id === created.id)
    if (item) selectTemplate(item)
    ElMessage.success('已创建模板，编辑后发布')
  } catch (error) {
    if (error !== 'cancel' && error !== 'close') {
      ElMessage.error(error.response?.data?.detail || '创建模板失败')
    }
  }
}

async function normalizeEditor() {
  if (readOnly.value && selected.value.type === 'template') return
  try {
    const result = await normalizePrompt({ content: editor.content })
    if (!result.changed) {
      ElMessage.info('没有检测到句中硬换行')
      return
    }
    editor.content = result.content
    ElMessage.success('已合并句中硬换行')
  } catch (error) {
    ElMessage.error(error.response?.data?.detail || '合并换行失败')
  }
}

function openPreview() {
  if (!selected.value.item) return
  previewRequest.value = selected.value.type === 'template'
    ? { template_id: selected.value.id, template_content: editor.content }
    : { fragment_key: selected.value.key, fragment_content: editor.content }
  previewVisible.value = true
}

async function saveDraft() {
  if (!selected.value.item || readOnly.value) return
  saving.value = true
  try {
    if (selected.value.type === 'template') {
      await saveTemplateDraft(selected.value.id, {
        content: editor.content,
        note: editor.note,
      })
    } else {
      await savePromptFragmentDraft(selected.value.key, {
        content: editor.content,
        note: editor.note,
      })
    }
    ElMessage.success('草稿已保存')
    await loadAll()
  } catch (error) {
    ElMessage.error(error.response?.data?.detail || '保存草稿失败')
  } finally {
    saving.value = false
  }
}

async function publishSavedDraft() {
  if (!selected.value.item || readOnly.value) return
  saving.value = true
  try {
    if (selected.value.type === 'template') {
      await publishTemplateDraft(selected.value.id, { note: editor.note })
    } else {
      await publishPromptFragmentDraft(selected.value.key, { note: editor.note })
    }
    ElMessage.success('草稿已发布')
    await loadAll()
  } catch (error) {
    ElMessage.error(error.response?.data?.detail || '发布草稿失败')
  } finally {
    saving.value = false
  }
}

async function discardDraft() {
  if (!selected.value.item) return
  try {
    if (selected.value.type === 'template') {
      await discardTemplateDraft(selected.value.id)
    } else {
      await discardPromptFragmentDraft(selected.value.key)
    }
    ElMessage.success('草稿已丢弃')
    await loadAll()
  } catch (error) {
    ElMessage.error(error.response?.data?.detail || '丢弃草稿失败')
  }
}

async function publish() {
  if (!selected.value.item || readOnly.value) return
  if (!editor.content.trim()) {
    ElMessage.warning('内容不能为空')
    return
  }
  publishing.value = true
  try {
    if (selected.value.type === 'template') {
      const check = await lintTemplate({ content: editor.content })
      if (check.warnings?.length) {
        try {
          await ElMessageBox.confirm(
            check.warnings.join('；'),
            '发布前提示（仍要发布？）',
            { type: 'warning', confirmButtonText: '仍然发布', cancelButtonText: '返回修改' },
          )
        } catch {
          return
        }
      }
      await updateTemplate(selected.value.id, {
        name: editor.name,
        category: editor.category,
        description: editor.description,
        content: editor.content,
        version_note: editor.versionNote || editor.note,
      })
    } else {
      const check = await lintPromptFragment(selected.value.key, {
        content: editor.content,
      })
      if (!check.ok) {
        ElMessage.error((check.errors || []).join('；') || '发布前检查未通过')
        return
      }
      if (check.warnings?.length) {
        try {
          await ElMessageBox.confirm(
            check.warnings.join('；'),
            '发布前提示（仍要发布？）',
            { type: 'warning', confirmButtonText: '仍然发布', cancelButtonText: '返回修改' },
          )
        } catch {
          return
        }
      }
      await updatePromptFragment(selected.value.key, {
        content: editor.content,
        note: editor.versionNote || editor.note,
      })
    }
    ElMessage.success('已发布新版本')
    await loadAll()
  } catch (error) {
    ElMessage.error(error.response?.data?.detail || '发布失败')
  } finally {
    publishing.value = false
  }
}

async function cloneSelected() {
  if (!selected.value.item || selected.value.type !== 'template') return
  try {
    const cloned = await cloneTemplate(selected.value.id)
    await loadAll()
    const item = templates.value.find((tpl) => tpl.id === cloned.id)
    if (item) selectTemplate(item)
    ElMessage.success('已克隆为自定义模板')
  } catch (error) {
    ElMessage.error(error.response?.data?.detail || '克隆失败')
  }
}

async function toggleArchive() {
  if (!selected.value.item || selected.value.type !== 'template') return
  try {
    if (selected.value.item.archived) {
      await unarchiveTemplate(selected.value.id)
    } else {
      await archiveTemplate(selected.value.id)
    }
    await loadAll()
    ElMessage.success(selected.value.item.archived ? '已恢复模板' : '已归档模板')
  } catch (error) {
    ElMessage.error(error.response?.data?.detail || '操作失败')
  }
}

async function resetSelectedFragment() {
  if (selected.value.type !== 'fragment') return
  try {
    await ElMessageBox.confirm(
      '恢复为代码默认值？会生成一个新版本，已有会话不受影响。',
      '恢复默认',
      { type: 'warning', confirmButtonText: '恢复', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  try {
    await resetPromptFragment(selected.value.key)
    ElMessage.success('已恢复默认值')
    await loadAll()
  } catch (error) {
    ElMessage.error(error.response?.data?.detail || '恢复失败')
  }
}

async function restoreSelectedVersion() {
  if (!selectedVersion.value) return
  if (selected.value.type === 'template' && selected.value.item?.is_system) return
  try {
    await ElMessageBox.confirm(
      `恢复 v${selectedVersion.value.version_no} 会以该内容新建一个版本，继续？`,
      '恢复版本',
      { type: 'warning', confirmButtonText: '恢复', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  try {
    if (selected.value.type === 'template') {
      await restoreTemplateVersion(selected.value.id, {
        version_id: selectedVersion.value.id,
      })
    } else {
      await restorePromptFragment(selected.value.key, {
        version_id: selectedVersion.value.id,
      })
    }
    ElMessage.success('已恢复并生成新版本')
    await loadAll()
  } catch (error) {
    ElMessage.error(error.response?.data?.detail || '恢复失败')
  }
}

function formatTime(value) {
  if (!value) return ''
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString()
}

async function loadTrials() {
  try {
    const data = await listPromptTrials({ limit: 30 })
    const templateId = selected.value.type === 'template' ? selected.value.id : null
    trials.value = (data.trials || []).filter(
      (trial) => !templateId || trial.template_id === templateId,
    )
  } catch {
    trials.value = []
  }
}

function viewTrial(trial) {
  if (!trial.result) {
    ElMessage.warning('该记录没有可用结果')
    return
  }
  if (trial.kind === 'ab') {
    const result = trial.result || {}
    if (!result.a || !result.b) {
      ElMessage.warning('该 A/B 记录结果不完整')
      return
    }
    abQuestion.value = trial.question || ''
    abVersionA.value = trial.version_a_id
    abVersionB.value = trial.version_b_id
    abResult.value = {
      a: result.a,
      b: result.b,
      a_version: versions.value.find((v) => v.id === trial.version_a_id)?.version_no,
      b_version: versions.value.find((v) => v.id === trial.version_b_id)?.version_no,
    }
    sideTab.value = 'ab'
  } else {
    evalResult.value = trial.result || null
    sideTab.value = 'eval'
  }
}

async function loadImpactCount() {
  try {
    const data = await getPromptImpact(500)
    impactCount.value = data.count ?? 0
  } catch {
    impactCount.value = 0
  }
}

function openImpact() {
  impactVisible.value = true
}

async function runAudit() {
  auditLoading.value = true
  try {
    const result = await auditPrompts()
    if (result.ok) {
      ElMessage.success('体检通过：模板与片段均未发现问题')
    } else {
      const lines = []
      for (const item of result.templates || []) {
        lines.push(`模板「${item.name}」：${item.warnings.join('；')}`)
      }
      for (const item of result.fragments || []) {
        lines.push(`片段「${item.title}」：${item.warnings.join('；')}`)
      }
      await ElMessageBox.alert(lines.join('；'), `体检发现 ${result.issue_count} 项`, {
        confirmButtonText: '知道了',
      })
    }
  } catch (error) {
    ElMessage.error(error.response?.data?.detail || '体检失败')
  } finally {
    auditLoading.value = false
  }
}

async function exportData() {
  try {
    const data = await exportPrompts()
    const blob = new Blob([JSON.stringify(data, null, 2)], {
      type: 'application/json',
    })
    const url = URL.createObjectURL(blob)
    const link = document.createElement('a')
    link.href = url
    link.download = `zhiwen-prompts-${new Date().toISOString().slice(0, 10)}.json`
    link.click()
    URL.revokeObjectURL(url)
    ElMessage.success('已导出')
  } catch (error) {
    ElMessage.error(error.response?.data?.detail || '导出失败')
  }
}

function triggerImport() {
  importInput.value?.click()
}

async function setAdminToken() {
  try {
    const current = localStorage.getItem('zhiwen_prompt_admin_token') || ''
    const { value } = await ElMessageBox.prompt(
      '提示词管理令牌（留空清除）。后端需设置 PROMPT_ADMIN_TOKEN 环境变量。',
      '管理令牌',
      {
        inputValue: current,
        confirmButtonText: '保存',
        cancelButtonText: '取消',
      },
    )
    if (value.trim()) {
      localStorage.setItem('zhiwen_prompt_admin_token', value.trim())
    } else {
      localStorage.removeItem('zhiwen_prompt_admin_token')
    }
    ElMessage.success('令牌已更新，下一次请求生效')
  } catch {
    // 取消
  }
}

async function onImportFile(event) {
  const file = event.target.files?.[0]
  event.target.value = ''
  if (!file) return
  try {
    const text = await file.text()
    const data = JSON.parse(text)
    await ElMessageBox.confirm(
      '导入会创建模板副本，并给已有片段追加新版本。继续？',
      '导入提示词',
      { type: 'warning', confirmButtonText: '导入', cancelButtonText: '取消' },
    )
    const result = await importPrompts(data)
    ElMessage.success(
      `导入模板 ${result.templates?.length || 0} 个，片段 ${result.fragments?.length || 0} 个`,
    )
    await loadAll()
  } catch (error) {
    ElMessage.error(error.response?.data?.detail || '导入失败：不是有效的 JSON')
  }
}

async function loadConversations() {
  try {
    conversations.value = await listConversations()
  } catch {
    conversations.value = []
  }
}

function openPin() {
  if (!selectedVersion.value || selected.value.type !== 'template') return
  pinVisible.value = true
}

async function doPin() {
  if (!pinConversationId.value || !selectedVersion.value) return
  busy.value = true
  try {
    await pinConversationPrompt(pinConversationId.value, {
      version_id: selectedVersion.value.id,
    })
    ElMessage.success('已锁定到该会话')
    pinVisible.value = false
    await loadImpactCount()
  } catch (error) {
    ElMessage.error(error.response?.data?.detail || '锁定失败')
  } finally {
    busy.value = false
  }
}

async function runAB() {
  if (selected.value.type !== 'template' || !abVersionA.value || !abVersionB.value) {
    ElMessage.warning('请选择两个模板版本')
    return
  }
  trialLoading.value = true
  try {
    const result = await runPromptAB({
      template_id: selected.value.id,
      version_a_id: abVersionA.value,
      version_b_id: abVersionB.value,
      question: abQuestion.value,
    })
    abResult.value = {
      a: result.trial.result.a,
      b: result.trial.result.b,
      a_version: versions.value.find((v) => v.id === abVersionA.value)?.version_no,
      b_version: versions.value.find((v) => v.id === abVersionB.value)?.version_no,
    }
    await loadTrials()
  } catch (error) {
    ElMessage.error(error.response?.data?.detail || 'A/B 试跑失败')
  } finally {
    trialLoading.value = false
  }
}

async function runEval() {
  if (selected.value.type !== 'template' || !selectedVersion.value) return
  trialLoading.value = true
  try {
    const result = await runPromptEval({
      template_id: selected.value.id,
      version_id: selectedVersion.value.id,
      limit: evalLimit.value,
    })
    evalResult.value = result.trial.result
    await loadTrials()
  } catch (error) {
    ElMessage.error(error.response?.data?.detail || '评测失败')
  } finally {
    trialLoading.value = false
  }
}

onMounted(loadAll)
</script>

<style scoped>
.pw-page {
  height: 100%;
  padding: 18px;
  box-sizing: border-box;
  background: var(--el-bg-color-page);
}
.pw-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 14px;
}
.pw-title {
  margin: 0;
  font-size: var(--font-xl);
}
.pw-subtitle {
  margin-top: 4px;
  color: var(--el-text-color-secondary);
  font-size: var(--font-xs);
}
.pw-header-actions {
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
}
.pw-grid {
  display: grid;
  grid-template-columns: 260px minmax(0, 1fr) 340px;
  gap: 12px;
  height: calc(100vh - 150px);
  min-height: 520px;
}
.pw-list,
.pw-editor,
.pw-side {
  min-height: 0;
  overflow: auto;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: var(--radius-md);
  background: var(--el-bg-color);
  padding: 12px;
}
.pw-list {
  padding: 8px;
}
.pw-list-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 4px 6px 6px;
  font-size: var(--font-xs);
  font-weight: 600;
  color: var(--el-text-color-secondary);
}
.pw-list-head-fragments {
  margin-top: 10px;
  border-top: 1px solid var(--el-border-color-lighter);
}
.pw-list-body {
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.pw-item {
  padding: 8px 9px;
  border: 1px solid transparent;
  border-radius: var(--radius-sm);
  cursor: pointer;
}
.pw-item:hover {
  background: var(--el-fill-color-light);
}
.pw-item.active {
  background: var(--el-fill-color-light);
  border-color: var(--el-color-primary-light-7);
}
.pw-item-title {
  display: flex;
  align-items: center;
  gap: 5px;
  flex-wrap: wrap;
  font-size: var(--font-sm);
  font-weight: 600;
}
.pw-item-meta {
  margin-top: 3px;
  font-size: var(--font-xs);
  color: var(--el-text-color-secondary);
}
.pw-editor {
  padding: 14px;
}
.pw-editor-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 10px;
  margin-bottom: 10px;
}
.pw-editor-title {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: var(--font-lg);
  font-weight: 600;
}
.pw-editor-sub {
  margin-top: 4px;
  font-size: var(--font-xs);
  color: var(--el-text-color-secondary);
}
.pw-editor-actions {
  display: flex;
  flex-wrap: wrap;
  justify-content: flex-end;
  gap: 2px;
}
.pw-alert {
  margin-bottom: 10px;
}
.pw-form {
  margin-bottom: 8px;
}
.pw-content :deep(.el-textarea__inner) {
  font-family: var(--font-mono);
  font-size: var(--font-xs);
  line-height: 1.65;
}
.pw-editor-foot {
  margin-top: 8px;
}
.pw-note-row {
  display: flex;
  align-items: center;
  gap: 10px;
}
.pw-chars {
  flex-shrink: 0;
  color: var(--el-text-color-secondary);
  font-size: var(--font-xs);
}
.pw-save-hint {
  margin-top: 6px;
  text-align: right;
}
.pw-save-actions {
  display: flex;
  flex-wrap: wrap;
  justify-content: flex-end;
  gap: 6px;
  margin-top: 8px;
}
.pw-side {
  padding: 6px 10px 10px;
}
.pw-side-actions {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 4px;
  margin-bottom: 6px;
}
.pw-version-list {
  display: flex;
  flex-direction: column;
  gap: 4px;
  max-height: 240px;
  overflow: auto;
}
.pw-version-item {
  padding: 6px 8px;
  border: 1px solid transparent;
  border-radius: var(--radius-sm);
  cursor: pointer;
}
.pw-version-item:hover,
.pw-version-item.active {
  background: var(--el-fill-color-light);
  border-color: var(--el-border-color-lighter);
}
.pw-version-head {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: var(--font-xs);
}
.pw-version-meta {
  margin-top: 2px;
  color: var(--el-text-color-secondary);
  font-size: var(--font-xs);
}
.pw-version-content,
.pw-trial-result pre,
.pw-eval-item pre {
  margin: 6px 0;
  max-height: 220px;
  overflow: auto;
  padding: 8px;
  border-radius: var(--radius-sm);
  background: var(--el-fill-color-lighter);
  white-space: pre-wrap;
  word-break: break-word;
  font-family: var(--font-mono);
  font-size: var(--font-xs);
  line-height: 1.6;
}
.pw-diff {
  max-height: 220px;
  overflow: auto;
  padding: 8px;
  border-radius: var(--radius-sm);
  background: var(--el-fill-color-lighter);
  font-family: var(--font-mono);
  font-size: var(--font-xs);
  line-height: 1.6;
}
.pw-diff-line {
  display: flex;
  gap: 4px;
  white-space: pre-wrap;
  word-break: break-word;
}
.pw-diff-line.add {
  background: var(--el-color-success-light-9);
  color: var(--el-color-success-dark-2);
}
.pw-diff-line.del {
  background: var(--el-color-danger-light-9);
  color: var(--el-color-danger-dark-2);
}
.pw-diff-sign {
  width: 10px;
  flex-shrink: 0;
}
.pw-impact-summary {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 4px;
  padding: 24px 8px;
}
.pw-impact-number {
  font-size: var(--font-3xl);
  font-weight: 700;
  color: var(--el-color-warning);
}
.pw-impact-btn {
  margin-top: 8px;
}
.pw-full {
  width: 100%;
  margin-top: 8px;
}
.pw-trial-result {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 8px;
  margin-top: 8px;
}
.pw-trial-col {
  min-width: 0;
}
.pw-eval-result {
  margin-top: 8px;
}
.pw-trial-list {
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.pw-trial-item {
  padding: 7px 8px;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: var(--radius-sm);
}
.pw-trial-question {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-weight: 400;
  color: var(--el-text-color-regular);
}
.pw-eval-item {
  border-bottom: 1px solid var(--el-border-color-lighter);
  padding: 6px 0;
}
.pw-eval-question {
  font-size: var(--font-xs);
  font-weight: 600;
}
.pw-muted {
  color: var(--el-text-color-secondary);
  font-size: var(--font-xs);
  line-height: 1.6;
}
.pw-error {
  color: var(--el-color-danger);
  font-size: var(--font-xs);
}
.pw-hidden {
  display: none;
}
@media (max-width: 1100px) {
  .pw-grid {
    grid-template-columns: 220px minmax(0, 1fr);
    height: auto;
  }
  .pw-side {
    grid-column: 1 / -1;
  }
}
</style>
