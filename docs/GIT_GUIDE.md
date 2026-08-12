# Git 基础使用指南（本项目）

## 仓库位置

本项目仓库根目录：

```text
C:\Users\user\PycharmProjects\PythonProjectPytorch1\langchain01\rag_knowledge_base
```

在 PowerShell 里进入该目录后，所有 `git` 命令都在这里执行：

```powershell
cd C:\Users\user\PycharmProjects\PythonProjectPytorch1\langchain01\rag_knowledge_base
```

> 运行数据（`data/` 上传文档、`opensearch_meta/` 索引账本与 trace、日志、
> `node_modules/`、`dist/`）已在 `.gitignore` 中排除，不会进版本库。

## 最小工作流（改代码 → 存档）

每次想“保存一个版本”，只需三步：

```powershell
# 1. 看改了什么（红色 = 已修改未暂存，绿色 = 已暂存待提交）
git status

# 2. 把改动加入暂存区（可以 git add 具体文件，或 git add -A 全部）
git add -A

# 3. 提交并写一句说明
git commit -m "fix: 修复 xxx"
```

提交后 `git log --oneline` 能看到一条新记录，这就是一个可回滚的版本。

## 常用命令速查

| 想做什么 | 命令 |
|---|---|
| 查看当前状态 | `git status` |
| 查看具体改了什么 | `git diff`（未暂存）/ `git diff --cached`（已暂存） |
| 加入暂存区 | `git add 文件名` 或 `git add -A`（全部） |
| 提交 | `git commit -m "说明"` |
| 查看提交历史 | `git log --oneline` / `git log --oneline -10` |
| 查看某次提交改了什么 | `git show <commit-id>` |
| 撤销某个文件的未暂存改动 | `git checkout -- 文件名`（⚠️ 会丢失该文件改动） |
| 撤销暂存（保留改动） | `git reset HEAD 文件名` |
| 回退到上一个提交（保留工作区） | `git reset --soft HEAD~1` |
| 回退到上一个提交（丢弃改动） | `git reset --hard HEAD~1`（⚠️ 慎用，改动不可恢复） |
| 新建分支 | `git branch 分支名` / `git switch -c 分支名` |
| 切换分支 | `git switch 分支名` |
| 合并分支 | `git switch 主分支 && git merge 分支名` |
| 查看忽略规则是否生效 | `git check-ignore 路径` |

## 日常建议

1. **提交前先看**：`git status` + `git diff`，确认只提交你想提交的东西。
2. **提交信息写清楚**：`fix:` 修 bug、`feat:` 新功能、`docs:` 文档、
   `refactor:` 重构、`test:` 测试。例如 `fix: 修复知识库查询 CUDA 降级`。
3. **小步提交**：一个逻辑改动一个提交，方便以后单独回滚。
4. **危险命令少用**：`git reset --hard`、`git checkout --` 会丢改动；
   不确定时先备份或问一下。
5. **目前没有远程仓库**：`git log` 都是本地的。以后要备份到 GitHub/Gitee 时，
   在远端建空仓库后执行：
   ```powershell
   git remote add origin https://github.com/你的用户名/仓库名.git
   git push -u origin master
   ```

## 回滚示例

改坏了想回到上一个版本：

```powershell
# 先看历史，找到想回到的提交
git log --oneline -5

# 方式一：把某次提交的改动“撤销”（生成一次新提交，历史保留，推荐）
git revert <commit-id>

# 方式二：硬回退（历史被改写，且之后所有改动丢失，不推荐）
git reset --hard <commit-id>
```
