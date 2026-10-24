<template>
  <div class="settings-pane">
    <div class="pane-head">
      <div class="pane-title">技能</div>
      <div class="pane-sub">浏览本机 Skills，选择本助手要使用的能力</div>
    </div>

    <div
      v-if="agentPendingCount > 0"
      class="agent-banner"
      @click="switchSourceFilter('agent')"
    >
      <el-icon :size="14"><MagicStick /></el-icon>
      Agent 新建了 {{ agentPendingCount }} 个自创技能待启用，点击查看
    </div>

    <div class="skill-master">
      <div class="skill-master-text">
        <div class="skill-master-title">启用技能工具</div>
        <div class="skill-master-desc">
          Agent 会看到下方已启用技能的全量索引，并按名称用 skill_view 加载完整步骤
        </div>
      </div>
      <el-switch v-model="skillsEnabled" @change="saveSkillsEnabled" />
    </div>

    <el-collapse class="skill-advanced">
      <el-collapse-item title="索引与加载参数（高级）" name="advanced">
        <el-form label-width="190px" size="small" class="skill-advanced-form">
          <el-form-item label="索引字符预算">
            <el-input-number
              v-model="toolsForm.skills_index_max_chars"
              :min="2000"
              :max="100000"
              :step="1000"
            />
            <span class="skill-advanced-hint">超出后按类目降 names-only，名称不隐藏</span>
          </el-form-item>
          <el-form-item label="全文返回上限（字符）">
            <el-input-number
              v-model="toolsForm.skills_full_view_max_chars"
              :min="4000"
              :max="200000"
              :step="1000"
            />
            <span class="skill-advanced-hint">超过此大小的技能返回章节目录</span>
          </el-form-item>
          <el-form-item label="重复查看去重">
            <el-switch v-model="toolsForm.skills_view_dedup" />
          </el-form-item>
          <el-form-item label="记录技能用量">
            <el-switch v-model="toolsForm.skills_usage_tracking" />
          </el-form-item>
          <el-form-item label="自创技能归档阈值（天）">
            <el-input-number
              v-model="toolsForm.skills_curator_stale_days"
              :min="7"
              :max="3650"
            />
          </el-form-item>
          <el-form-item>
            <el-button
              type="primary"
              size="small"
              :loading="toolsSaving"
              @click="saveToolsSettings"
            >
              保存参数
            </el-button>
          </el-form-item>
        </el-form>
      </el-collapse-item>
    </el-collapse>

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
            <el-tag
              v-if="s.shared && isAgentSkill(s)"
              size="small"
              type="success"
              effect="plain"
            >
              已共享
            </el-tag>
            <span v-if="s.created_at" class="skill-date">{{ s.created_at }}</span>
            <el-tag v-if="s.group && s.group !== 'other'" size="small" type="info" effect="plain">
              {{ groupLabel(s.group) }}
            </el-tag>
          </div>
          <div class="skill-desc">{{ s.description }}</div>
        </div>
        <div class="skill-ops">
          <el-switch v-model="s.enabled" size="small" @change="persistSkills" />
          <template v-if="isAgentSkill(s)">
            <el-button link type="primary" size="small" @click="viewAgentSkill(s)">
              查看
            </el-button>
            <el-button link size="small" @click="shareAgentSkill(s)">
              {{ s.shared ? '取消共享' : '共享' }}
            </el-button>
            <el-button link type="danger" size="small" @click="deleteAgentSkill(s)">
              删除
            </el-button>
          </template>
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

    <!-- 自创技能内容预览：append-to-body 已传送至 body，不受面板 v-show 影响 -->
    <el-dialog v-model="agentPreviewVisible" title="自创技能内容" width="720px" append-to-body>
      <pre class="agent-preview">{{ agentPreviewContent }}</pre>
      <template #footer>
        <el-button size="small" @click="agentPreviewVisible = false">关闭</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
/**
 * 设置页 · 技能面板（从 App.vue 抽出，2026-10-09 设置页拆分）。
 * 全部状态与操作来自 useSkillsAdmin（App.vue provide）；
 * 自创技能预览对话框随本面板走（append-to-body 已传送，不受 v-show 影响）。
 */
import { inject } from 'vue'
import { Hide, MagicStick, Search } from '@element-plus/icons-vue'

const { toolsForm, toolsSaving, saveToolsSettings } = inject('settingsForm')

const {
  skills,
  hiddenSkills,
  skillsEnabled,
  skillQuery,
  skillSourceFilter,
  skillGroupFilter,
  skillsLoading,
  groupOptions,
  sourceFilters,
  enabledCount,
  filteredSkills,
  agentPreviewVisible,
  agentPreviewContent,
  agentPendingCount,
  skillIcon,
  sourceLabel,
  sourceTagType,
  groupLabel,
  isAgentSkill,
  saveSkillsEnabled,
  onSkillSearch,
  switchSourceFilter,
  shareAgentSkill,
  deleteAgentSkill,
  viewAgentSkill,
  hideSkill,
  restoreSkill,
  handleResetSkills,
} = inject('settingsSkills')
</script>

<style scoped>
.skill-advanced {
  margin: 4px 0 12px;
  border-radius: 8px;
  border: 1px solid var(--el-border-color-lighter);
  padding: 0 12px;
}
.skill-advanced-form :deep(.el-form-item) {
  margin-bottom: 10px;
}
.skill-advanced-hint {
  margin-left: 10px;
  color: var(--el-text-color-secondary);
  font-size: 12px;
}
</style>
