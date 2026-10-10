<template>
  <div class="sources">
    <div class="sources-head" @click="emit('update:open', !open)">
      <el-icon :size="13"><Link /></el-icon>
      引用来源（{{ sources.length }}）
      <el-icon :size="12" class="arrow" :class="{ open }">
        <ArrowDown />
      </el-icon>
    </div>
    <div v-if="open" class="sources-body">
      <div v-for="(src, i) in sources" :key="i" class="source-item">
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
          <a class="source-link" @click.prevent="emit('open-doc', src)">
            查看原文 {{ src.page != null ? `· 第 ${src.page} 页` : '' }} ↗
          </a>
        </template>
      </div>
    </div>
  </div>
</template>

<script setup>
/**
 * 引用来源列表（从 ChatView 抽出，2026-10-09 物理拆分）。
 *
 * 展开态 `open` 由父组件持有（`v-model:open` 绑定 msg._sourcesOpen）：
 * 拆分前就有多个写入点——消息归一化的默认值、正文明文引用 [n] 点击后
 * 自动展开（handleMessageClick），组件只发 update:open 不自己存状态。
 * 「查看原文」发 `open-doc`（父组件的 openSourceDoc → 文档查看器）。
 *
 * 契约（勿改）：
 * - `.source-item` 类名与 DOM 结构被父组件 handleMessageClick 的
 *   querySelectorAll + classList.add('flash') 依赖（引用跳转高亮），
 *   `.source-item.flash` 动画随组件走；
 * - 条目字段形态：非 web 为知识库来源（source/page/score/relative_path），
 *   web 为联网来源（title/url）。
 */
import { ArrowDown, Link } from '@element-plus/icons-vue'

defineProps({
  sources: { type: Array, required: true },
  open: { type: Boolean, default: false },
})
const emit = defineEmits(['update:open', 'open-doc'])
</script>

<style scoped>
.sources {
  margin-top: 8px;
  border-radius: var(--radius-md);
  background: var(--bg-card-2);
  border: 1px solid var(--border);
  overflow: hidden;
}

.sources-head {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 9px 14px;
  font-size: var(--font-sm);
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
  font-size: var(--font-xs);
  margin-bottom: 5px;
  flex-wrap: wrap;
}

.source-score {
  color: var(--primary);
  background: var(--bg-active);
  padding: 1px 8px;
  border-radius: var(--radius-sm);
}

.source-tag {
  color: var(--text-2);
  background: var(--bg-hover);
  padding: 1px 8px;
  border-radius: var(--radius-sm);
  font-size: var(--font-2xs);
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
  font-size: var(--font-sm);
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
  border-radius: var(--radius-sm);
  background: var(--primary);
  color: var(--text-on-brand);
  font-size: var(--font-2xs);
  font-weight: 600;
}

.source-item.flash {
  animation: flash-bg 1.6s ease;
}

@keyframes flash-bg {
  0%,
  40% {
    background: var(--bg-active);
    border-radius: var(--radius-sm);
  }
  100% {
    background: transparent;
  }
}

.source-content {
  font-size: var(--font-sm);
  color: var(--text-2);
  line-height: 1.7;
  max-height: 130px;
  overflow-y: auto;
}
</style>
