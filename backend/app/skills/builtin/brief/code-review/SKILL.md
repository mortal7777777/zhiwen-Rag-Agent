---
name: brief/code-review
description: 生成只读代码审查子代理的 brief（范围/严重度/证据/不修代码）。
when_to_use: 准备用 dispatch_subtasks 的 review/explore 模式审查代码、配置或文档时
when_not_to_use: 需要直接修改代码（改用 execute 模式）或只问单行代码含义
tags: [brief, dispatch, review]
origin: zhiwen_builtin
created_at: 2026-11-21
---

# Brief 模板：只读代码审查

审查子代理只报问题、不给修复补丁；主代理拿到 findings 后决定是否派 execute。

## 模板

```text
目标：审查 <路径/模块> 的 <关注点：并发/错误处理/安全/性能/契约>，找出真实缺陷。
范围：<明确文件或目录>；必要时可读取 <依赖文件>，不要扩展到无关模块。
已知事实：<主代理已确认的改动/约束；没有就写"无">。
产出：按严重度输出 findings（CRITICAL/HIGH/MEDIUM/LOW），每条包含：
  path:line、claim（问题是什么）、evidence（代码证据）、trigger（触发场景）、confidence。
  最后附一个 ```json 围栏（summary/findings/open_questions），没有发现就给空数组。
验收：每条问题都有 path:line 与可复现触发场景；不把风格偏好当缺陷；不编造行号。
禁止：不要修改任何文件；不要执行命令；不要给修复补丁或完整重写。
```

## 两个例句

- 空泛：`看看这个文件有没有问题。`
- 合格：`目标：审查 backend/app/agent/context_pack.py 的超时与异常处理。范围：该文件，可读 subagent.py 的调用点。产出：findings（path:line/claim/evidence/trigger/confidence）+ 最后一个 json 围栏。验收：每条有行号与触发场景；区分"真实缺陷"与"风格建议"。禁止：不改文件、不执行命令、不给补丁。`

## 主代理检查清单

- [ ] 范围写死到文件/目录，允许读取的依赖也点名
- [ ] 关注点具体（不要"全面审查"）
- [ ] 要求 path:line、触发场景、置信度
- [ ] 明确只读与"不给修复"
- [ ] 要求末尾 json 围栏，便于主代理按严重度处理
