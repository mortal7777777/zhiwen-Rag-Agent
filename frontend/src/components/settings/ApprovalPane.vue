<template>
  <div class="settings-pane">
    <div class="pane-head">
      <div class="pane-title">审批与安全</div>
      <div class="pane-sub">敏感操作确认 · 命令白名单 · 沙箱与超时</div>
    </div>

    <div class="settings-section">
      <div class="settings-label">人工确认</div>
      <div class="provider-hint">
        敏感操作（写文件 / 编辑 / 删除 / 执行命令）默认逐项弹卡请求批准；
        白名单命令与「本会话记住」过的命令自动放行。
      </div>
      <div class="generic-form">
        <div class="generic-row">
          <span class="generic-label">敏感操作确认</span>
          <el-select v-model="toolsForm.tool_permission_mode" size="small" class="generic-select">
            <el-option label="每次确认（推荐）" value="ask" />
            <el-option label="自动接受文件编辑" value="acceptedits" />
            <el-option label="自动批准（跳过确认）" value="allow" />
          </el-select>
        </div>
        <div class="generic-row">
          <span class="generic-label">确认超时（秒）</span>
          <el-input-number
            v-model="toolsForm.permission_timeout"
            :min="0"
            :max="3600"
            :step="30"
            size="small"
          />
        </div>
        <div class="provider-hint timeout-hint">
          0 = 无限等待（交互式 TUI 推荐）；超时后服务端会自动拒绝该批操作并继续执行，
          客户端审批卡会提示「已超时自动处理」。
        </div>
        <div class="generic-row">
          <span class="generic-label">命令白名单</span>
          <TagListInput
            v-model="toolsForm.command_allowlist"
            :suggestions="ALLOWLIST_SUGGESTIONS"
            placeholder="回车添加一条命令"
          />
        </div>
      </div>
      <div class="provider-hint">
        白名单按前缀匹配，且只对**单条**命令生效：命中即无需确认直接执行
        （如 `git status` 放行 `git status -s`）；含管道 / 重定向 / 串联的命令
        一律退回人工确认。未命中的命令在「每次确认」模式下仍会弹卡。
        「自动接受文件编辑」自动批准工作区内的写/改/删，但 `.env`、Git 钩子、
        后端代码等高危路径与命令仍确认；「自动批准」相当于 Claude Code 的
        --dangerously-skip-permissions，请谨慎使用。
      </div>
    </div>

    <div class="settings-section">
      <div class="settings-label">命令执行环境</div>
      <div class="generic-form">
        <div class="generic-row">
          <span class="generic-label">执行超时（秒）</span>
          <el-input-number v-model="toolsForm.command_timeout" :min="5" :max="300" size="small" />
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
          <span class="generic-label">沙箱内命令免确认</span>
          <el-switch v-model="toolsForm.sandbox_auto_allow" size="small" />
        </div>
      </div>
      <div class="provider-hint">
        “Docker 沙箱”把 bash 放进容器执行；“工作目录只读”开启后项目以只读挂载，
        写入发生在沙箱内存盘 /scratch。“沙箱内命令免确认”只在该环境下生效：
        普通命令直接执行，高危命令（磁盘/系统目录/远程脚本等）仍会弹卡。
      </div>
    </div>

    <div class="settings-save-row">
      <el-button type="primary" :loading="toolsSaving" @click="saveToolsSettings">
        保存审批设置
      </el-button>
    </div>
  </div>
</template>

<script setup>
/**
 * 设置页 · 审批与安全面板（从 App.vue 抽出，2026-10-09 设置页拆分）。
 * 表单域与保存逻辑来自 useSettingsForm（App.vue provide，四页共用）。
 * ALLOWLIST_SUGGESTIONS 是展示数据，随面板走。
 */
import { inject } from 'vue'
import TagListInput from '../TagListInput.vue'

const { toolsForm, toolsSaving, saveToolsSettings } = inject('settingsForm')

// 命令白名单的常用建议（点击即添加；回车也可自填任意命令）
const ALLOWLIST_SUGGESTIONS = [
  'python',
  'python3',
  'pytest',
  'node',
  'npm test',
  'git status',
  'git diff',
  'git log',
  'ls',
  'cat',
  'head',
  'tail',
  'grep',
  'wc',
  'tree',
]
</script>
