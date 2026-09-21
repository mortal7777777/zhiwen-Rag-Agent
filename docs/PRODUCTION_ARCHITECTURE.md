# 生产化架构:并发、成本、容量与编排模式

> 面向"本项目对外提供服务"的架构推演:行业怎么做并发与性能优化、
> 成本构成与容量估算、可观测性选型,以及项目内部两个容易被问到的
> 设计(子代理开关、Workflow vs Agent)。

---

## 1. 并发与性能优化:行业通行做法 vs 本项目现状

线上 RAG 知识库服务(Notion AI、各类 RAG SaaS、企业知识库)的性能架构,
核心是**"入库离线化、在线轻量化"**:

| 环节 | 行业通行做法 | 本项目现状 |
|---|---|---|
| 架构分层 | 文档入库走**离线异步队列**(worker 慢慢嵌入),在线只做"检索+生成",在线绝不现算 embedding | ✅ 增量索引 + 指纹账本(`manifest.json`),文件没变直接跳过 |
| 向量化 | 入库时**批量预计算** embedding(可用小模型/免费 API,不限时) | ✅ 批量嵌入(batch_size 128) |
| 向量检索 | HNSW 近似检索毫秒级;分片/副本水平扩展 | ✅ HNSW + 混合检索(单机) |
| 缓存 | 热门查询结果缓存、向量缓存、LLM 前缀缓存 | ✅ 同问题 10 分钟缓存 + DeepSeek 前缀缓存(轮内 95%+) |
| 生成成本 | 上下文瘦身、候选收敛、流式输出、必要时小模型兜底 | ✅ `MAX_PARENTS=10`、rerank Top 6、SSE 流式 |
| 服务层 | 无状态 API + 多副本负载均衡 + 连接池 | ✅ 无状态 FastAPI + httpx 连接池(代理回退直连) |
| 在线 rerank | 候选集收敛后再精排(避免全量打分) | ✅ 召回 60 → 每查询 RRF 32（跨查询 source 轮流合并 40）→ 精排 6 |

**结论:本项目已经是主流 RAG 服务的小型化版本**,差距在"水平扩展"和"多用户",
不在检索设计本身。

---

## 2. 成本构成:对外服务贵在哪?

### 2.1 成本大头排序

1. **LLM token(最大头)**:每次问答必付,输入(上下文)+ 输出;
2. **embedding/rerank API**:入库一次性 + 在线低频(如果走 API);
3. **向量库**:自托管 = 一台服务器(几十~几百/月);云向量库按量;
4. **带宽/存储**:文档与索引,通常忽略。

> 注意反直觉的一点:**本地跑 embedding/rerank 恰恰是省钱的**——
> 它们是高频小模型,本地推理边际成本 ≈ 0;LLM 大模型才值得用 API。

### 2.2 单次问答成本估算(DeepSeek 按量价)

| 项 | 量 | 说明 |
|---|---|---|
| 查询扩展 | 2~3 次小调用,各几百 token | Multi-Query / HyDE / 多轮补全 |
| 主生成输入 | ~5~6k token | 历史 + 检索结果 + 系统提示词 |
| 主生成输出 | ~500 token | 最终回答 |

一次典型问答 ≈ **几分钱**(普通 chat 档);用 `flash` 档低数倍。
1000 用户 × 每天 10 次 = 1 万次/天 ≈ **每天几十到一两百元**(取决于档位与缓存命中)。

---

## 3. 容量:不用本地模型能撑多少用户?

**瓶颈排序(从硬到软):**

1. **LLM API 配额** —— 第一瓶颈
   - 付费按量档:无硬限制,卡的是"钱";
   - 免费档:硬限制(RPM/RPD,如 60 RPM 级别),基本只够自用演示;
2. **embedding/rerank API 速率限制** —— SiliconFlow 等免费档同样有 RPM;
   付费档按量,不是硬墙;
3. **向量库吞吐** —— 单机 OpenSearch 几十~几百 QPS,千级日活日常够用,
   **不是瓶颈**;
4. **后端并发** —— 本项目 `MAX_CONCURRENCY=4`(GPU 检索)、
   `LLM_MAX_CONCURRENCY=8`:单进程上限 ≈ 0.8 QPS(每次 ~10s),
   一天理论 ~7 万次;实际被 API 配额和账单卡住。

**结论:百级到千级日活(知识库问答这类低并发场景)没问题,真正卡你的是
token 账单和 API 配额,不是向量库。**

### 3.1 要真正上线,还缺什么(提示词注入之外的清单)

- 用户体系 + 鉴权(登录/Token);
- 每用户数据隔离(user_id 过滤会话/记忆/文档/工作目录,**服务端强制**,不靠提示词);
- 每用户配额与限流(防止单个用户刷爆账单);
- 工具越权防护(工作目录沙箱 + 服务端权限门,模板不可信化——复用
  `skills.py` 的 `sanitize_skill_text` 清洗管道);
- 审计与计量(按用户计费)。

> 与 `AGENT_COMPARISON.md` 的既定结论一致:单用户本地是第一优先级,
> 多用户走"多配置文件"路线,不引入租户体系。

---

## 4. 可观测性:LangSmith / Langfuse / 现状

| 方案 | 是什么 | 费用 |
|---|---|---|
| **LangSmith** | LangChain 官方 LLM 应用可观测平台:逐调用 trace、token/成本、评测、prompt 管理 | 有免费额度(每月约几千条 trace),超出按量付费;企业版更贵 |
| **Langfuse** | 开源替代,功能类似,可自托管 | 免费(自托管) |
| **本项目现状** | 本地 JSONL trace + `agent_runs` 表 + `/api/metrics`;`tracing.py` 已预留 Langfuse 接入点 | 免费、隐私友好 |

结论:现在够用;想可视化 trace 时优先 **Langfuse 自托管**(免费且与预留点对接)。

---

## 5. 子代理开关:图不变,路由短路

设置页关闭"子代理"时,**节点没有从图里消失**,图结构完全不变。

**实现**(`langgraph_agent.py`):

- 图构建(`build_agent_graph`)始终注册全部 7 个节点:
  `prepare / dispatch / subagent / merge / agent / tools / finalize`;
- 开关在**运行期**由条件路由 `_route_after_prepare`(2829 行)读取:
  `effective(settings, "agent_subagents_enabled")` 为 false 时,直接返回 `"agent"`,
  dispatch/subagent/merge 分支**永远不被路由进入**。

```text
开关打开:  prepare → dispatch → Send → subagent×N → merge → agent ⇄ tools → finalize
开关关闭:  prepare ───────────────────────────────────→ agent ⇄ tools → finalize
          (节点还在图里,条件边永远不选它们)
```

效果:复杂问题不再 fan-out 并行,全部步骤由主 agent 串行完成——
更慢、token 更省、行为更可预测。这是 LangGraph 的标准用法:
**静态图 + 运行时条件路由**。

---

## 6. Workflow vs Agent:本项目的定位

LangGraph 官方定义:

| 模式 | 定义 | 本项目对应 |
|---|---|---|
| **Workflow** | 预定义节点顺序,固定管道 | prepare/dispatch/subagent/merge/finalize 段 |
| **Agent** | LLM 自主决定行动序列(循环) | `agent ⇄ tools` 主循环:模型决定调不调工具、调什么 |

本项目是**"workflow 骨架 + 模型驱动循环"的混合体**,术语上叫
**Agentic Workflow**;对外一般说"LangGraph Agent"。

**面试表述**:主循环是模型驱动的 Agent(LangGraph Agent 模式),
复杂任务经 workflow 式 fan-out/fan-in 并行拆解;两者在同一张状态图上,
条件路由决定何时走哪条边。

---

## 7. 面试一句话

"项目的并发/成本架构是主流 RAG 服务的小型化:入库离线化、在线只检索不现算
向量、前缀缓存与查询缓存压 token 成本;瓶颈分析是 LLM 账单与 API 配额,
向量库单机即可支撑千级日活;可观测性用本地 JSONL(预留 Langfuse);
编排上,主循环是模型驱动的 Agent,并行段是固定管道,子代理开关通过
运行时条件路由实现,图结构不变。"
