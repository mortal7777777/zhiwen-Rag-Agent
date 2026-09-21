# 记忆系统设计与实现（知问 ZhiWen）

> 本文讲清楚记忆系统的**五层结构、每层的写入/读取链路、关键参数与设计决策**。
> 代码以函数名为准（`agent/context.py`、`project_memory.py`、`trajectory.py`、
> `db/models.py`、`api/memories.py`）；行号可能漂移。
> 相关文档：`AGENT_ORCHESTRATION_DETAILED.md`（编排）、`DEV_PITFALLS.md`（缓存坑）、
> `INTERVIEW_TECH_DETAILS.md`（面试口径）。

---

## 0. 总览：为什么是"五层"而不是一个记忆库

记忆的核心问题是**生命周期不同、成本不同、可控性不同**——所以按层拆分，各用各的机制：

| 层 | 存活范围 | 存储媒介 | 写入时机 | 读取/注入 |
|---|---|---|---|---|
| ① 会话消息 | 单会话 | MySQL `messages` | 每轮 finalize「发送即落库」 | prepare 重建历史（D 块之前） |
| ② 滚动摘要 | 单会话 | `conversations.summary` + `summary_up_to_id` | 高水位触发 / 手动 `/compact` | D 块："以下是对本会话早期内容的摘要" |
| ③ 长期事实记忆 | 跨会话 | MySQL `memories` + `app_meta.memory_summary` | 回答后台提取（显式"记住"强制） | 每轮 prepare 语义召回，注入 D 块 |
| ④ 文件型项目记忆 | 跨会话/跨项目 | `AGENTS.md`（项目根 + `~/.zhiwen/`） | 整合后自动导出（无变化不写盘） | D 块："项目记忆（AGENTS.md）" |
| ⑤ 任务过程摘要 | 单会话 | `app_meta.trajectory_summary_{conv}` | finalize 后（工具调用 ≥3 次） | D 块 + 断点恢复链尾 |

**一条铁律**：所有记忆注入都发生在 **D 块（消息链尾部、问题之前）**——每轮变化的字节
只影响链尾本身，不破坏前缀缓存（见 §6）。

```
一次提问的记忆流：

prepare：重建历史① → 摘要② → 项目记忆④ → todos → 轨迹⑤ → 召回记忆③ → 时间
                                                         ↓ 全部收进 D 块
回答（agent ⇄ tools）→ finalize 落库①
        ↓ 流结束后的后台线程（不阻塞用户）
提取事实③(force/自动) → 去重入库 → 间隔整合③ → 导出 AGENTS.md④
```

---

## 1. 第一层：会话与消息（最底层存储）

- **表**：`conversations`（含 `summary` / `summary_up_to_id` / `template_id`）、
  `messages`（含工具轮行）。
- **写入不变量**：finalize 把"问题之后"的全部消息按链序落库（含 system 行、工具行、
  reasoning_content）——下一轮从 DB 重建与上一轮实际发送**逐字节一致**（纯追加链，
  见 `finalize.py` 的"纯追加链落库"段）。
- **读取**：prepare 用 `list_messages_with_id` 拉取，按预算裁剪后拼进消息链。
- 这一层不叫"记忆"，但它是其余四层的地基：摘要压缩的是它，提取事实也读它。

## 2. 第二层：滚动摘要（会话内压缩）

**目标**：长会话不爆预算，同时尽量不破坏前缀缓存。

**机制**（`ContextService.compact_conversation`）：
- 状态 = `(summary, summary_up_to_id)`——**窗口起点粘滞**：窗口 = 摘要边界之后的消息，
  没触发压缩时窗口逐字节不变（跨轮缓存稳）；
- **高水位触发**：窗口条数 > `1.5 × history_max_messages` 或 token > `1.25 × 预算`
  才压缩一次，压到上限条数。预算按模板类别差异化（general/knowledge 64k，
  coding/writing 96k——`history_budget_for`）；
- **摘要失败不推进边界**：否则窗口前移但内容没进摘要 = 静默丢历史；
- 手动 `/compact`（`compact_now`）：不看阈值立即压，保留 `max(4, 总数/4)` 条，语义对齐
  Claude Code 的 /compact；
- 消息被删除后校验边界：若摘要覆盖了被删区间则整段摘要作废、下次重新生成。

**为什么是高水位而不是"超上限就压"**（真实实测）：压到恰好等于上限，工具轮多的会话
下一轮立刻又超限、又只丢几条 → 每轮压缩、每轮断缓存，命中率从 ~90% 掉到 ~29%；
留 0.5× 余量后"压一次稳定多轮"。同类思路还有 `trim_history_for_budget` 的
15% 最小回收门控。

**注入**：D 块 SystemMessage「以下是对本会话早期内容的摘要（供参考）」。

## 3. 第三层：长期事实记忆（系统核心）

### 3.1 存储（`db/models.py: Memory`）

```
memories 表
  id                    BIGINT 主键
  content               TEXT（一句话、第三人称客观事实）
  category              profile(画像)/preference(偏好)/project(项目)
                        /decision(决定)/lesson(教训)/other
  status                active | archived（软删除：被覆盖/过时归档，不物理删）
  source_conversation_id 来源会话（可追溯）
  created_at / updated_at
```

配套 `app_meta` 两个键：`memory_summary`（≤150 字用户画像摘要）、
`memory_last_consolidated_at`（整合时间戳）。

### 3.2 写入链：提取（`extract_facts` → `add_facts`）

**触发时机**：每轮回答完成后，在**后台线程**里执行（`langgraph_agent.py` 流结束后 /
`agent.py` 老路径同构）——不阻塞流式输出、失败只记日志。

**触发条件**（`memory_auto_extract_min_chars` 控制成本）：
- **显式"记住"信号**：`has_memory_intent` 命中 `MEMORY_INTENT_PATTERNS`（记住/我喜欢/
  我是/以后/我的…）→ `force=True`，prompt 强调"逐条完整提取、不遗漏、不推测"；
- **自动提取**：回答长度 ≥ 400 字符才提取（短问答跳过，省一轮 LLM 调用）。

**提取实现**（`extract_facts`）：
- 输入：当轮 user 消息 + 最终回答（各截 1500 字符）；
- 六分类约束 + "只提稳定可复用信息、忽略闲聊；纠正类信息提取为当前状态的新事实
  （如'我不住北京了'→ 供整合时覆盖旧记忆）"；
- **结构化输出优先**（`FactsList` schema 约束），失败回退正文 JSON + 正则抽取（旧路径）；
- 输出 `[{category, content}]`，空则 `[]`。

**去重入库**（`add_facts` + `_is_duplicate`）：
- 精确文本比对 + **向量相似度 > 0.95 跳过**（只与最近 10 条比对——新事实最可能与近期重复，
  且控制 embedding 成本）；
- 通过后 `repo.add_memory(...)`，记录来源会话 id。

### 3.3 读取链：语义召回（`retrieve_memories`）

每轮 prepare 调用，**四段漏斗**（设计重点是"别每轮全量 embedding"）：

```
① 全量拉取 active 记忆（≤500 条，按 updated_at 倒序）
② 粗筛候选 _memory_candidates：
     问题拆 2~4 字 n-gram（去停用词，无 jieba 依赖）命中 content
     + 最近 50 条补位（recent_fallback）
     → 候选上限 100（memory_candidate_limit）
③ 只对候选做嵌入：问题向量 × 候选向量，余弦 ≥ memory_min_score(0.35) 保留
④ 取 top-k（默认 3）→ 不足时用"最近记忆"补位（"介绍一下我"这类问题与具体
   事实相似度低，但近期记忆对个人助手同样重要）→ token 预算 600 截断
```

**注入**：D 块两条 SystemMessage——常驻的「关于用户（长期记忆摘要，默认背景）」+
按需的「相关的长期记忆（如与本轮相关可参考）」。

**参数表**（`config.py`，全部可由环境变量覆盖）：

| 参数 | 默认 | 含义 |
|---|---|---|
| `memory_enabled` | True | 总开关（关掉则召回/提取/整合全部停机） |
| `memory_top_k` | 3 | 每次注入条数 |
| `memory_min_score` | 0.35 | 召回余弦阈值（BGE） |
| `memory_max_tokens` | 600 | 注入预算 |
| `memory_candidate_limit` | 100 | 粗筛候选上限 |
| `memory_recent_fallback` | 50 | 候选/召回补位条数 |
| `memory_auto_extract_min_chars` | 400 | 自动提取的回答长度门槛 |
| `memory_consolidate_interval_hours` | 24 | 自动整合间隔 |
| `memory_consolidate_threshold` | 10 | 活跃记忆达到该条数才触发整合 |

### 3.4 整合链：整理（`maybe_consolidate` → `consolidate_memories`）

**触发**：每个后台提取流程尾部顺带调用 `maybe_consolidate`——间隔 ≥24h **且**活跃记忆
≥10 条才真正执行（双门槛，防频繁烧 token）；也可手动 `POST /memories/consolidate`。
`_consolidate_lock` 防并发。

**机制**（一次 LLM 调用，结构化输出 `ConsolidateOutput`）：
- 输入：全部活跃记忆（`id: [分类] 内容`，截 12000 字符）；
- 输出三件套：
  1. `facts`：合并重复/高度相似的多条 → 一条（`source_ids` 指定被合并项）；
  2. `archived_ids`：新旧矛盾时**以新为准**，旧记忆归档；
  3. `summary`：≤150 字用户画像摘要 → 写入 `app_meta.memory_summary`（成为每轮常驻的
     最便宜记忆）；
- 合并写入策略：保留 `source_ids` 里**最新的一条为主记录**（更新其内容），其余归档；
- 完成后更新 `memory_last_consolidated_at`，并触发 **AGENTS.md 导出**（§4）。

**设计要点**：归档而非删除（审计可回溯、误判可恢复）；"画像摘要"把 N 条记忆的认知
压缩成 150 字常驻上下文——召回漏了的宏观印象由它兜底。

### 3.5 管理面

- API（`api/memories.py`）：`GET/POST/PATCH/DELETE /memories` + 手动整合；前端记忆页可
  查看/编辑/删除；
- 单条 content ≤2000 字符；分类白名单校验（非法归 other）。

## 4. 第四层：文件型项目记忆（AGENTS.md）

**语义**：等价于 Claude Code 的 CLAUDE.md / Codex 的 AGENTS.md——**人可读、可手改、
可随项目迁移**的记忆副本。

- **导出**（`export_project_memory`）：用户画像（memory_summary）+ 前 30 条活跃记忆 +
  最近 5 次运行记录 → 写 `rag_knowledge_base/AGENTS.md`（路径可配 `project_memory_file`）；
- **加载**（`load_project_memory`）：
  - 默认：项目根 AGENTS.md + 用户级 `~/.zhiwen/AGENTS.md`；
  - **CLI 场景**：从启动目录**逐级向上**收集 AGENTS.md（与 CLAUDE.md 语义一致）——
    在子目录工作时自动继承父级项目记忆；
  - 单文件截 4000 字符、总量上限 8000；
- **注入**：每轮 prepare 作为 D 块 SystemMessage「项目记忆（AGENTS.md）」。

**关键坑（缓存）**：导出函数有"**内容无变化不写盘**"的短路（代码注释原文：重写相同
字节会改变项目记忆 SystemMessage——它是缓存前缀的一部分——导致跨 run 首调整个历史
前缀失效）。写文件→读文件→注入，任何一个字节变化都等于一次缓存断点，所以导出必须
幂等。

**与②③的分工**：结构化数据在 DB（查询/召回/整合用它），文件是**单向下行的可编辑
副本**（DB → 文件）；人工编辑文件只影响注入内容，不回写 DB。

## 5. 第五层：任务过程摘要（trajectory）

- **目的**：与滚动摘要互补——滚动摘要压"对话历史"，轨迹摘要压"**单次任务的中间过程**"；
  长任务的计划与工具轨迹若只留在被压缩的历史里会丢失。
- **写入**（`compress_trajectory`，finalize 后）：工具调用 ≥3 次才生成；LLM 把
  计划 + 前 10 条工具轨迹压成 ≤300 字"任务过程摘要"（做了什么、结论、未完成/存疑点），
  存 `app_meta.trajectory_summary_{conversation_id}`；
- **读取**：prepare 注入 D 块「最近任务过程摘要」；checkpoint 恢复链尾的 resume_notes
  也会带上它（断点续跑不丢任务上下文）。

## 6. 与上下文/缓存系统的关系（设计不变量）

1. **注入位置**：五层中所有"每轮变化"的内容（摘要/todos/记忆/时间）全部收在 D 块
   （链尾）——静态核心与历史之前的部分保持字节稳定，前缀缓存跨轮命中（详见
   `INTERVIEW_TECH_DETAILS.md` 问题 13）；
2. **压缩的缓存代价被显式管理**：滚动摘要的"高水位 + 压一次稳定多轮"就是为缓存设计
   （§2 的实测：每轮压 = 命中率 90%→29%）；
3. **导出幂等**：AGENTS.md 无变化不写盘（§4）——文件字节 = 缓存前缀字节；
4. **提取/整合全在后台**：用户感知不到记忆维护的延迟，失败降级只落日志。

## 7. 面试深挖问答（备答）

**Q：为什么不用向量库存记忆？**
> 量级不对等——记忆是几百条级别、注入只要 top-3。我的做法是"n-gram 粗筛 + 只对
> 候选嵌入"，全量 embedding 每轮都做太浪费；存储用 MySQL 就够了，不为一个功能引入
> 新基础设施（OpenSearch 留给知识库这种十万块级别的场景）。

**Q：LLM 自动提取的记忆会不会污染/幻觉？**
> 五道闸：① 只提"稳定可复用"信息、禁止推测（prompt 约束）；② 入库前去重（精确 +
> 0.95 相似度）；③ 整合时新旧矛盾以新为准、旧的归档可追溯；④ 全部可人工编辑/删除；
> ⑤ 来源会话 id 可溯源。另外显式"记住"和自动提取走不同强度的 prompt，用户纠正类
> 信息提取为"当前状态"供整合覆盖。

**Q：记忆会不会越积越多污染上下文？**
> 不会——注入侧有三重预算：常驻只有 150 字画像；召回 top-3、600 token 上限；整合
> 会把 N 条合并压缩并归档过时项。存储侧归档是软删除，量级健康。

**Q：为什么要五层？用一个 memories 表不就行了？**
> 生命周期和成本不同：消息层按会话销毁；摘要和轨迹是"压缩产物"（解决预算）；长期
> 记忆跨会话（解决个性化）；文件层解决"可迁移/可手改/脱离系统也看得懂"。混成一层
> 就没法分别控制"每轮常驻什么、按需召回什么、什么时候压缩什么"。

**Q：这套系统的成本是多少？**
> 常驻成本 = 150 字画像；每轮召回 = 1 次问题嵌入 + ≤100 条候选嵌入（本地 BGE，GPU
> 锁内）；后台成本 = 长回答/显式记住才触发的 1 次提取；整合 = 24h 间隔 + ≥10 条阈值
> 才 1 次。短问答零额外 LLM 调用。

---

## 附：一次完整时间线（对照代码）

```
用户提问「记住：我最近在做一个招生 Agent 项目」
├─ prepare：召回旧记忆（retrieve_memories）→ 注入 D 块
├─ 回答完成 → finalize 落库
└─ 后台线程：
   ├─ has_memory_intent 命中「记住」→ force=True
   ├─ extract_facts → [{category: "project", content: "用户最近在开发招生 Agent 项目"}]
   ├─ add_facts → 去重（0.95 相似度）→ 入库（source_conversation_id 记录）
   ├─ maybe_consolidate →（若 ≥24h 且 ≥10 条）合并/归档/生成画像摘要
   └─ export_project_memory → AGENTS.md 内容变化才写盘
```
