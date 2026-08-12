# 评测方案（当前状态与建议）

## 现状

- `backend/evaluate_retrieval.py`：8 个手工问题 + 关键词命中率，只验证检索管道通不通；
- `backend/evaluate_agent.py` + `backend/eval_questions.json`（新增）：14 题种子集，
  覆盖知识库问答、通用推理、联网、文件工具、安全拒绝、多步规划；
  逐题调用 `/api/agent/stream`，记录延迟/工具调用/计划/答案长度/运行状态到
  `backend/eval_report.jsonl`，方便每次改动后对比回归。

## 推荐开源评测集（按用途）

### RAG 检索质量（离线，可自动跑分）
- **RAGAS**（`ragas` 包）：生成式 RAG 指标 —— faithfulness（忠实度）、
  answer correctness、context precision/recall；可接本项目的 DeepSeek 打分。
- **BEIR** / **C-MTEB**：通用/中文检索基准，评测 kNN/BM25/RRF 召回质量
  （recall@k、MRR、nDCG@k）。
- 中文阅读理解：**CMRC 2018**、**DuReader**（问答对可直接当 RAG 测试集）。

### Agent 能力（整体任务）
- **GAIA**：需要联网/工具/推理的通用任务，接近真实使用；
- **AgentBench** / **τ-bench**：工具调用、多轮任务、越权拒绝；
- **WebArena**（网页操作）对本项目偏重，暂不建议。

### 自建集（最贴合本项目）
以 `eval_questions.json` 为骨架，按三个维度扩充：
1. **知识库**：从用户真实文档（示例选集、示例书等）每本抽 5~10 问，
   答案以文档原文为准（可先人工标注 20 条 gold answer）；
2. **行为约束**：安全拒绝、审批流程、任务清单正确性、引用编号正确性；
3. **体验指标**：首 token 延迟、总延迟、工具调用次数、token 成本。

## 建议的回归门槛（后续接 RAGAS 后）

- 检索：recall@k ≥ 0.8（知识库相关问题上），MRR ≥ 0.75；
- 生成：faithfulness ≥ 0.85，answer correctness ≥ 0.7；
- 行为：安全类问题 100% 拒绝；任务清单结束时 100% 全勾；
- 性能：知识库模式单轮 ≤ 20s（不开联网兜底），联网模式 ≤ 40s。

## 使用方式

```powershell
cd backend
python evaluate_retrieval.py        # 检索管道冒烟
python evaluate_agent.py --limit 5  # Agent 端到端（5 题先试跑）
python evaluate_agent.py --category kb
```

改动前后各跑一遍，对比 `eval_report.jsonl` 中的 latency/tools/status 即可发现回归。
