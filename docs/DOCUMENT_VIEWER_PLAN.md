# 文档查看器升级计划（供新会话直接开工）

> 2026-08-16 与用户讨论定稿：**采用专业开源查看器（路线 B）**，同时实现
> 「查看原文（引用跳转）」与「位置记忆」。本文档是给下一个会话的完整上下文，
> 开工前先读 `PROJECT_HANDOFF.md` §0-§3 了解全局。

## 0. 目标（用户原话归纳）

1. 知识库预览升级为**专业查看器**：美观、像真正的文档阅读器；
   支持目录索引直接跳转章节；**按用户阅读位置按需加载渲染**（快）。
2. Agent 回答中的引用（如 `[1]`）来源卡上加**「查看原文」按钮**，
   点击打开预览并**定位到被引用的内容**（PDF 应跳到对应页）。
3. 预览**记住位置**：同一文档下次打开回到上次查看处，方便查询和学习。

## 1. 现状与已具备的数据链路（重要，省去重新调研）

- 现有预览：`GET /api/documents/preview?path=...`（`backend/app/api/documents.py`）
  返回提取文本（md/txt 原文，pdf/doc/docx/epub/xlsx 经 `rag/loader.py` 提取），
  20 万字符封顶；前端在 `KnowledgeView.vue` 用 el-drawer + MarkdownContent/pre 全量渲染。
- **`[n]` → 来源卡跳转已实现**：`ChatView.vue` 的 `handleMessageClick`
  （`data-cite` → `.source-item` 闪烁）。缺的只是来源卡 → 原文的入口。
- **知识库来源已带页码**：`extract_sources()`（`agent/tools.py`）输出的
  KB 来源含 `source`（文档名）、`content`（引用片段）、`score`、**`page`**
  （来源卡已显示「第 N 页」，PDF 检索链路把页信息传出来了）——
  这是「查看原文跳页」的现成数据。
- 预览抽屉目前只在 KnowledgeView；ChatView 也要能打开 → 需抽公共组件。

## 2. 技术选型（路线 B：按格式分发）

| 格式 | 方案 | 说明 |
|---|---|---|
| PDF | **pdf.js**（推荐 `vue-pdf-embed` 封装） | 原排版/图片/表格；`getOutline()` 书签即目录；按页懒渲染天然满足"按需加载"；页码跳转直接用来源的 `page` |
| EPUB | **epub.js** | 章节目录、翻页；**CFI 书签原生就是位置记忆** |
| DOCX | **docx-preview** | 保留 Word 排版渲染为 HTML |
| MD/TXT/DOC 提取文本 | 自研轻量方案 | 按标题分节 + **CSS `content-visibility: auto` + `contain-intrinsic-size`**（浏览器自动跳过屏外渲染，DOM 全在、渲染只做可视区）+ 标题目录锚点跳转 |

安装：`npm i vue-pdf-embed docx-preview epubjs`（pdf.js worker 配置注意 vite）。

## 3. 后端改动（都很小）

1. **只读文件服务**：`GET /api/documents/file/{relative_path:path}`
   —— 返回原始字节（pdf/epub/docx 需要），复用 `preview_document` 里已有的
   路径校验（拒绝穿越、限 data_dir 内）；大文件建议 `FileResponse`。
2. **`extract_sources` 补 `relative_path`**（`agent/tools.py`，一行级改动）：
   现在只有 `source`=文件名，子目录重名文档会匹配错；让检索结果直接带
   相对路径，来源卡「查看原文」不必做文件名反查。
3. 预览端点可加 `kind` 判断原始文件是否走专业查看器（按扩展名即可，无需新参数）。

## 4. 前端改动

1. **抽公共组件 `components/DocumentViewer.vue`**（从 KnowledgeView 抽出）：
   props：`relativePath`、`locator`（可选：`{page?, anchorText?}`）；
   内部按扩展名分发到四种渲染器；管理位置记忆。
2. **目录索引**：pdf 用 outline + 页码列表；epub 用 spine 目录；
   文本类解析标题生成锚点目录；当前节高亮跟随滚动。
3. **「查看原文」按钮**（`ChatView.vue` KB 来源卡，web 来源已有"打开链接"）：
   点击 → 打开 DocumentViewer，定位优先级：
   `page`（PDF 跳页）→ `anchorText`（引用片段前 ~80 字在全文定位，
   正则化空白后 indexOf，毫秒级）→ 上次记住的位置。
   定位后短暂高亮目标区域。
4. **位置记忆**（localStorage，按 `relative_path` 为键）：
   - 存 `{位置, doc_mtime}`，mtime 变化即失效；
   - 文本类：最近标题锚 id + 页内偏移；PDF：页码 + scroll；
     epub：CFI；
   - 滚动防抖 ~500ms 写入；
   - **引用打开（明确意图）优先于记忆位置**。
5. 性能要求：pdf.js 只渲染可视页 ±1~2 页缓冲；文本类靠
   content-visibility（无需手动虚拟滚动）。

## 5. 难度与验收

| 项 | 难度 | 预估 |
|---|---|---|
| DocumentViewer 组件抽取 + 四格式分发骨架 | 低 | ~1h |
| pdf.js 接入（含 worker、outline、页跳转、懒渲染） | 中 | ~1 天（大 PDF 内存是主要风险点） |
| 文本类 content-visibility + TOC | 中低 | ~半天 |
| 查看原文按钮 + 定位（page/anchorText） | 中低 | ~2-3h |
| 位置记忆（三类格式） | 低 | ~1h |
| epub.js / docx-preview | 中高 / 低-中 | 各 ~1 天 / ~半天（可后置，先降级文本渲染） |

**验收清单**（2026-08-16 实施完成，实测情况见各项备注）：
- [x] PDF 打开显示原始排版，目录可跳章节，屏外页不渲染（滚动流畅）；
  实测：示例文集 297 页，outline 目录 97 项，点第 40 项跳到 93 页；懒渲染=IO 可视页±1，屏外清空画布
- [x] EPUB 按章节目录跳转；实测：示例书 15.7MB，目录 140 项（CFI 位置记忆已接 relocated 事件）
- [x] 聊天中点 KB 来源卡「查看原文」→ 打开该文档并定位到引用处/对应页，有高亮；
  实测：旧索引来源（无 relative_path）走文件名反查兜底打开 34.8MB doc，锚点定位到引用段
- [x] 同一文档关闭再打开回到上次位置（文档更新后失效）；实测：PDF 关闭重开回到 94 页（之前在 93/94）
- [x] 知识库页与聊天页共用同一查看器（components/DocumentViewer.vue，两处挂载）
- [x] `npm run build` 通过；后端 pytest 88 全过（含新增 6 例）；路径穿越仍被拒绝（400 非法路径）

实施说明（与计划的差异）：
- PDF 未用 vue-pdf-embed，直接用 pdfjs-dist 自管懒渲染（vue-pdf-embed 默认全页渲染，
  大 PDF 内存风险即计划中提到的主风险点），worker 用 `?url` 导入由 Vite 产出独立资源；
- 三个重组件（Pdf/Epub/Docx Viewer）在 DocumentViewer 内 defineAsyncComponent 按需分包
  （DocumentViewer 壳 84KB，pdf 371KB/epub 354KB/docx 177KB 懒加载）；
- `relative_path` 写入点在 `loader.load_documents`（一处覆盖全格式，新索引生效）；
  旧索引来源前端按文件名反查 `/api/documents` 兜底（重名文档需重建索引才能精确）；
- 引用定位顺序：page（PDF，并用引用文本在 ±2 页校验修偏）→ anchorText（压平文本 DOM 检索）
  → 上次记住的位置；
- PDF 来源页码来自 pymupdf4llm（1 基）；文本校验兜住了 PyPDFLoader 0 基的 off-by-one。

## 6. 注意事项（前几轮踩过的坑）

- 后端在跑时编辑 `backend/` 文件会触发热重载（watchfiles 曾失灵过一轮，
  16:18-16:33 的编辑没生效）——**重大改动后手动重启后端一次**；
  `run.py` 已配 `reload_excludes`（cli_agent.py/日志不会误触发）。
- Git Bash 的 curl 传中文 body 会编码错误（"error parsing the body"），
  冒烟测试用 Python urllib 或 body 落盘 `--data-binary @file`。
- 测试后端起 8001：`MYSQL_URL=mysql+pymysql://root:CHANGE_ME@127.0.0.1:3306/rag_assistant?charset=utf8mb4 python -m uvicorn app.main:app --port 8001`；
  测试会话/元数据用完删除，别动用户真实会话。
- 提交走 git（pre-commit 会跑 82 个 pytest，basetemp 在 .git 内，沙箱外可直接跑）。
