"""受控执行工具：文件读写 + 命令执行（工作目录白名单 + 超时 + 人工确认）。

两类工具集：
- make_agent_tools()：Agent 使用的完整工具集（类 Claude Code）：
  list_dir / read_file / grep_search 为只读自动执行；
  write_file / edit_file / delete_file / bash 为敏感操作，默认需人工确认
  （确认逻辑在 LangGraph tools 节点，通过 permissions.py 触发 HITL）。
- make_file_tool() / make_command_tool()：设置页测试用（旧行为，命令仍走白名单硬门槛）。

安全边界：
- 文件操作限制在 workspace 根目录内（默认项目根），拒绝路径穿越；
- 命令白名单是可选的"自动放行前缀"，未命中的命令走人工确认；
- 命令超时自动终止，输出截断；
- 删除操作不直接物理删除，移入工作目录下 .agent_trash/ 可恢复。
"""

from __future__ import annotations

import logging
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from langchain_core.tools import BaseTool, StructuredTool

from .runtime_config import effective

logger = logging.getLogger(__name__)


def _resolve_workspace(settings, project_dir: str | None = None) -> Path:
    root = (effective(settings, "tool_workspace") or "").strip()
    if root:
        p = Path(root).expanduser().resolve()
    elif project_dir:
        # 会话工作目录：CLI 从哪个目录启动，文件/命令工具就在哪个目录工作
        p = Path(project_dir).expanduser().resolve()
    else:
        p = Path(__file__).resolve().parents[2]  # rag_knowledge_base/
    p.mkdir(parents=True, exist_ok=True)
    return p


def _safe_path(workspace: Path, path: str) -> Path:
    """把路径解析到工作目录内；支持相对路径与工作目录内的绝对路径，拒绝穿越。

    绝对路径只要落在工作目录内就自动接受（模型经常拿到绝对路径），
    落在外面则给出可操作的错误：工作目录根 + 改用相对路径的建议。
    """
    p = Path(path or "").expanduser()
    if not p.is_absolute():
        p = workspace / p
    target = p.resolve()
    if workspace != target and workspace not in target.parents:
        raise ValueError(
            f"路径超出工作目录（工作目录：{workspace}）。"
            f"请使用工作目录内的相对路径，如 `practice/notes.md`；"
            f"若确实需要在 {p} 等外部位置操作，请先让用户在设置中修改「工作目录」。"
        )
    return target


def _truncate(text: str, limit: int = 4000) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n…（输出已截断，共 {len(text)} 字符）"


def _atomic_write(target: Path, content: str) -> None:
    """原子写入：先写临时文件再替换，避免进程中断产生半截文件。"""
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp, target)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


_COMPOUND_OPS = {
    ">": "重定向", "<": "输入重定向", "|": "管道", "&": "后台/串联",
    ";": "串联", "(": "子 shell/命令替换", ")": "子 shell/命令替换",
}


def _compound_reason(line: str) -> str:
    """检测未加引号包裹的 shell 操作符；安全则返回空串。

    前缀匹配挡不住复合命令：`ls > x.txt`、`ls | xargs rm`、`echo $(rm -rf x)`
    都以白名单前缀开头，却会写文件或串联破坏性操作。用 shlex 按 shell 词法
    拆分——裸操作符会成为独立 token，引号内的保持在同一 token 里，正好区分
    `python -c "import a; print(b)"`（分号是普通字符）和 `ls; rm -rf x`。
    `/dev/null` 重定向与 `2>&1` 属于无害形式，先剔除再判。
    """
    body = re.sub(r"\d?>{1,2}\s*(?:/dev/null|&\d+)", "", line)
    try:
        toks = list(shlex.shlex(body, posix=True, punctuation_chars="();<>|&;"))
    except ValueError:
        return "命令引号不闭合，无法判定"
    # shlex 会把连续的同类标点合并成一个 token（`>>`、`&&`、`||`），
    # 所以按字符判定而不是整 token 查表
    hits: set[str] = set()
    for t in toks:
        if t and all(c in _COMPOUND_OPS for c in t):
            hits.update(_COMPOUND_OPS[c] for c in t)
    return ("命令含" + "、".join(sorted(hits))) if hits else ""


def command_allowed(settings, command_line: str) -> tuple[bool, str]:
    """命令白名单判断：命中前缀即自动放行；空白名单 = 全部走人工确认。

    白名单只覆盖**单条**只读命令；含重定向/管道/串联/命令替换的一律不自动
    放行，退回人工确认（见 _compound_reason）。
    """
    raw = (effective(settings, "command_allowlist") or "").strip()
    line = command_line.strip()
    if not line:
        return False, "命令为空"
    if not raw:
        return False, ""
    for prefix in [p.strip() for p in raw.split(",") if p.strip()]:
        if not line.startswith(prefix):
            continue
        reason = _compound_reason(line)
        if reason:
            return False, f"{reason}（白名单只对单条只读命令生效）"
        return True, ""
    return False, f"命令不在自动放行白名单内：{line[:80]}"


def _bash_argv(command: str) -> list[str] | None:
    """Windows 上改用 Git Bash 执行命令；返回 None 表示沿用默认 shell。

    `shell=True` 在 Windows 上走 COMSPEC（cmd.exe）：模型训练分布以 Unix
    语法为主，写 grep/find/sed/ls 全部失效，转而用 cd /d 这类 cmd 语法反复
    试错，白耗工具预算（实测单轮 5 次 bash 里 2 次 exit=255）。改成显式走
    Git Bash 后，命令语义与 Claude Code 在 Windows 上的行为一致。
    非 Windows 或找不到 bash 时返回 None，保持原行为。
    """
    if os.name != "nt":
        return None
    bash = shutil.which("bash") or shutil.which("bash.exe")
    if not bash:
        return None
    return [bash, "-c", command]


def _kill_process_tree(proc: subprocess.Popen) -> None:
    """超时后尽力杀掉整棵进程树（子 shell 可能又起了后台进程）。"""
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True,
                timeout=15,
            )
            return
        import signal

        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def _run_command(
    settings,
    command: str,
    cwd: str = "",
    project_dir: str | None = None,
    sandbox_override: str | None = None,
) -> dict:
    """执行命令：默认在工作目录根执行（与文件工具一致），支持子目录 + 超时 + 截断。

    sandbox_override：请求级沙箱覆盖（CLI --sandbox / /sandbox 命令），
    优先于设置页的 command_sandbox；空则跟随设置。
    """
    workspace = _resolve_workspace(settings, project_dir)
    workdir = workspace if not cwd else _safe_path(workspace, cwd)
    sandbox = str(
        sandbox_override
        if sandbox_override is not None
        else effective(settings, "command_sandbox")
        or "subprocess"
    ).strip().lower()
    if sandbox == "docker":
        return _run_docker_command(settings, command, workdir)
    try:
        argv = _bash_argv(command)
        timeout = max(1, int(effective(settings, "command_timeout") or 60))
        # 输出走**临时文件**而不是管道：命令里用 `&` 起的后台进程（例如预览页面时
        # `python -m http.server 8123 &`）会继承并长期持有管道，而
        # subprocess.run(capture_output=True) 在子 shell 退出后仍要等管道 EOF
        # → 工具永久挂住、整轮对话僵死（2026-09-26 实测卡死 45 分钟，
        # 超时 kill 也救不了：TimeoutExpired 之后仍会再等一次管道）。
        # 写文件则父进程只等子进程本身，后台进程继续跑（正是起静态服务想要的语义）。
        with tempfile.TemporaryFile(
            "w+", encoding="utf-8", errors="replace"
        ) as out_f, tempfile.TemporaryFile(
            "w+", encoding="utf-8", errors="replace"
        ) as err_f:
            proc = subprocess.Popen(
                argv if argv else command,
                shell=argv is None,
                cwd=workdir,
                stdout=out_f,
                stderr=err_f,
                text=True,
                encoding="utf-8",
                errors="replace",
                # 独立进程组：超时能整棵树杀掉（含它起的子进程）
                start_new_session=os.name != "nt",
                creationflags=(
                    subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
                ),
            )
            timed_out = False
            try:
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                _kill_process_tree(proc)
            out_f.seek(0)
            err_f.seek(0)
            stdout = out_f.read()
            stderr = err_f.read()
        output = stdout + (("\n[stderr] " + stderr) if stderr else "")
        if timed_out:
            return {
                "error": f"命令超时（>{timeout}s）已终止",
                "summary": "命令超时已终止",
                "output": _truncate(output),
            }
        # "伪成功"盲区提示：exit=0 但 stdout/stderr 全空——命令很可能没按预期
        # 执行（Windows git-bash 下引号转义、python -c 多行参数、cmd 语法
        # cd /d 等），不提示的话模型会换写法无限重试直到撞图步数上限。
        if proc.returncode == 0 and not stdout and not stderr:
            summary = (
                "命令执行完成（exit=0）但没有任何输出——命令可能没按预期执行："
                "请检查引号转义、python -c 多行参数、路径写法（cd /d 是 cmd "
                "语法，bash 里不适用）。"
            )
        else:
            summary = f"命令执行完成（exit={proc.returncode}）"
        return {
            "summary": summary,
            "exit_code": proc.returncode,
            "output": _truncate(output),
        }
    except Exception as exc:
        return {"error": str(exc), "summary": f"命令执行失败：{exc}"}


# ============================================================
# Agent 工具集（类 Claude Code，敏感操作走人工确认）
# ============================================================


def _run_docker_command(settings, command: str, workdir: Path) -> dict:
    """在 Docker 容器内执行命令（真正的进程/网络沙箱）。

    - 容器根文件系统只读（--read-only），临时目录用内存盘（/tmp）；
    - 挂载工作目录到容器 /workspace；开启只读模式时以 :ro 挂载、工作目录为 /scratch；
    - 默认 --network=none（沙箱内无外网）、限内存/CPU/进程数、去掉全部 Linux 能力；
    - 超时 = command_timeout + 30s（含镜像启动开销）。
    """
    cmd = _docker_run_cmd(settings, command, workdir)
    timeout = max(1, int(effective(settings, "command_timeout") or 60)) + 30
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            encoding="utf-8",
            errors="replace",
        )
        output = (proc.stdout or "") + (
            ("\n[stderr] " + proc.stderr) if proc.stderr else ""
        )
        return {
            "summary": f"沙箱命令执行完成（exit={proc.returncode}）",
            "exit_code": proc.returncode,
            "sandbox": "docker",
            "output": _truncate(output),
        }
    except subprocess.TimeoutExpired:
        return {
            "error": f"沙箱命令超时（>{timeout - 30}s）已终止",
            "summary": "沙箱命令超时已终止",
        }
    except FileNotFoundError:
        return {
            "error": "未找到 docker 命令，请确认 Docker Desktop 已启动",
            "summary": "沙箱执行失败：docker 不可用",
        }
    except Exception as exc:
        return {"error": str(exc), "summary": f"沙箱命令执行失败：{exc}"}


def _docker_run_cmd(settings, command: str, workdir: Path) -> list[str]:
    """构建 docker run 参数列表（独立成函数便于单测）。"""
    image = str(effective(settings, "sandbox_image") or "python:3.11-slim").strip()
    host_dir = str(workdir.resolve())
    readonly = bool(effective(settings, "sandbox_workspace_readonly"))
    mount = f"{host_dir}:/workspace:ro" if readonly else f"{host_dir}:/workspace"
    container_wd = "/scratch" if readonly else "/workspace"
    options = [
        "docker",
        "run",
        "--rm",
        "--network=none",
        "--memory=512m",
        "--cpus=1",
        "--pids-limit=256",
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--tmpfs",
        "/tmp:rw,size=256m",
    ]
    if readonly:
        options.extend(["--tmpfs", "/scratch:rw,size=256m"])
    options.extend(
        [
        "-v",
        mount,
        "-w",
        container_wd,
        ]
    )
    return options + [image, "sh", "-c", command]


def make_agent_tools(
    settings, project_dir: str | None = None, sandbox_override: str | None = None
) -> list[BaseTool]:
    """Agent 可用的文件/命令工具：读类自动，写/命令类敏感（HITL）。

    project_dir：会话工作目录（CLI 启动目录），未配置 tool_workspace 时生效。
    sandbox_override：请求级沙箱覆盖（subprocess|docker），空则跟随设置。
    """
    return [
        make_list_dir_tool(settings, project_dir),
        make_read_file_tool(settings, project_dir),
        make_grep_search_tool(settings, project_dir),
        make_write_file_tool(settings, project_dir),
        make_edit_file_tool(settings, project_dir),
        make_delete_file_tool(settings, project_dir),
        make_bash_tool(settings, project_dir, sandbox_override),
    ]


def make_list_dir_tool(settings, project_dir: str | None = None) -> BaseTool:
    workspace = _resolve_workspace(settings, project_dir)

    def _invoke(path: str = ".") -> dict:
        try:
            target = _safe_path(workspace, path)
            base = target if target.is_dir() else target.parent
            entries = [
                {
                    "name": p.name,
                    "is_dir": p.is_dir(),
                    "size": p.stat().st_size if p.is_file() else 0,
                }
                for p in sorted(base.iterdir())[:100]
            ]
            return {
                "summary": f"列出 {base.name or base} 的 {len(entries)} 项",
                "path": str(base),
                "entries": entries,
            }
        except Exception as exc:
            return {"error": str(exc), "summary": f"列出目录失败：{exc}"}

    return StructuredTool.from_function(
        func=_invoke,
        name="list_dir",
        description=(
            "列出工作目录（或子目录）中的文件和文件夹。path 为相对工作目录的路径，"
            "如 '.'、'data'、'backend/app'。用于了解项目结构时优先调用。"
        ),
        args_schema=None,
    )


def make_read_file_tool(settings, project_dir: str | None = None) -> BaseTool:
    workspace = _resolve_workspace(settings, project_dir)

    def _invoke(path: str, offset: int = 0, limit: int = 200) -> dict:
        try:
            target = _safe_path(workspace, path)
            if not target.is_file():
                raise FileNotFoundError(f"文件不存在：{path}")
            lines = target.read_text(encoding="utf-8", errors="ignore").splitlines()
            total = len(lines)
            start = max(0, int(offset))
            end = min(total, start + max(1, int(limit)))
            body = "\n".join(lines[start:end])
            truncated = end < total
            return {
                "summary": f"已读取 {path}（{total} 行，返回 {start+1}-{end} 行）",
                "path": str(target),
                "line_count": total,
                "start_line": start + 1,
                "content": _truncate(body, 8000),
                "truncated": truncated,
            }
        except Exception as exc:
            return {"error": str(exc), "summary": f"读取文件失败：{exc}"}

    return StructuredTool.from_function(
        func=_invoke,
        name="read_file",
        description=(
            "读取文本文件内容（UTF-8）。path 相对工作目录；大文件用 offset/limit "
            "按行分页读取。适合先读取文件了解现状再修改。"
        ),
        args_schema=None,
    )


def make_grep_search_tool(settings, project_dir: str | None = None) -> BaseTool:
    workspace = _resolve_workspace(settings, project_dir)
    # .pytest_cache 在 Windows 上可能有 ACL 权限异常（WinError 5），必须跳过
    _SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build", ".agent_trash", ".pytest_cache"}

    def _walk(path: Path, include: str | None, budget: list[int]):
        if budget[0] <= 0:
            return
        for p in path.iterdir():
            if budget[0] <= 0:
                return
            if p.is_dir():
                if p.name in _SKIP_DIRS:
                    continue
                yield from _walk(p, include, budget)
            elif p.is_file():
                if include and not re.search(include, p.name):
                    continue
                budget[0] -= 1
                yield p

    def _invoke(pattern: str, path: str = ".", include: str = "") -> dict:
        try:
            root = _safe_path(workspace, path)
            if not root.is_dir():
                root = root.parent
            regex = re.compile(pattern)
            matches: list[dict] = []
            budget = [300]
            for fp in _walk(root, include.strip() or None, budget):
                try:
                    with open(fp, "r", encoding="utf-8", errors="ignore") as f:
                        for lineno, line in enumerate(f, 1):
                            if regex.search(line):
                                matches.append(
                                    {
                                        "file": str(fp.relative_to(workspace)).replace("\\", "/"),
                                        "line": lineno,
                                        "text": line.rstrip()[:200],
                                    }
                                )
                                if len(matches) >= 50:
                                    break
                except OSError:
                    continue
                if len(matches) >= 50:
                    break
            return {
                "summary": f"在 {root} 中找到 {len(matches)} 处匹配",
                "pattern": pattern,
                "matches": matches,
            }
        except re.error as exc:
            return {"error": f"正则表达式错误：{exc}", "summary": f"正则错误：{exc}"}
        except Exception as exc:
            return {"error": str(exc), "summary": f"搜索失败：{exc}"}

    return StructuredTool.from_function(
        func=_invoke,
        name="grep_search",
        description=(
            "在工作目录内用正则搜索文本内容（跳过 .git/node_modules 等目录）。"
            "pattern 为正则表达式，path 为起始目录，include 可选文件名校验。"
            "适合定位符号、错误信息、关键词出现的位置。"
        ),
        args_schema=None,
    )


def make_write_file_tool(settings, project_dir: str | None = None) -> BaseTool:
    workspace = _resolve_workspace(settings, project_dir)

    def _invoke(path: str, content: str) -> dict:
        try:
            target = _safe_path(workspace, path)
            existed = target.exists()
            _atomic_write(target, content)
            return {
                "summary": f"{'覆盖' if existed else '新建'} {path}（{len(content)} 字符）",
                "path": str(target),
                "relative_path": str(target.relative_to(workspace)).replace("\\", "/"),
                # 产物证据：created 区分"本步新建"与"覆盖旧文件"，
                # 子代理回报与主 agent 核验都用它（2026-09-24 复用旧产物事故）
                "created": not existed,
                "bytes": len(content.encode("utf-8")),
                "lines": content.count("\n") + 1,
            }
        except Exception as exc:
            return {"error": str(exc), "summary": f"写入文件失败：{exc}"}

    return StructuredTool.from_function(
        func=_invoke,
        name="write_file",
        description=(
            "创建新文件或覆盖已有文件（UTF-8），content 为完整文件内容。"
            "敏感操作：执行前会请求用户确认。修改已有文件时优先用 edit_file。"
        ),
        args_schema=None,
    )


def make_edit_file_tool(settings, project_dir: str | None = None) -> BaseTool:
    workspace = _resolve_workspace(settings, project_dir)

    def _invoke(
        path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
    ) -> dict:
        try:
            target = _safe_path(workspace, path)
            if not target.is_file():
                raise FileNotFoundError(f"文件不存在：{path}")
            text = target.read_text(encoding="utf-8")
            count = text.count(old_string)
            if count == 0:
                return {
                    "error": "old_string 在文件中不存在，请提供文件中真实存在的完整片段（注意缩进与换行）",
                    "summary": "编辑失败：未找到要替换的文本",
                }
            if replace_all:
                new_text = text.replace(old_string, new_string)
            else:
                new_text = text.replace(old_string, new_string, 1)
            _atomic_write(target, new_text)
            preview_old = old_string.strip().splitlines()[0][:60]
            # diff 审批用：截取替换处前后各 1 行上下文，供前端做可视化对比
            before, after = _diff_context(text, old_string, new_string, replace_all)
            return {
                "summary": f"已编辑 {path}：替换 {count if replace_all else 1} 处"
                f"（'{preview_old}…'）",
                "path": str(target),
                "replaced": count if replace_all else 1,
                "remaining_occurrences": max(0, count - (count if replace_all else 1)),
                "diff": {"before": before, "after": after},
                # 产物证据：edit 只描述"改了哪个文件"，created 恒为 False
                "created": False,
                "bytes": len(new_text.encode("utf-8")),
                "lines": new_text.count("\n") + 1,
            }
        except Exception as exc:
            return {"error": str(exc), "summary": f"编辑文件失败：{exc}"}

    return StructuredTool.from_function(
        func=_invoke,
        name="edit_file",
        description=(
            "精确编辑文件：把 old_string 替换为 new_string（需与文件内容完全一致，"
            "含缩进换行）。replace_all=true 时替换所有出现处，否则只替换第一处。"
            "比 write_file 更安全，适合小改动。敏感操作：执行前会请求用户确认。"
        ),
        args_schema=None,
    )


def _diff_context(text: str, old_string: str, new_string: str, replace_all: bool) -> tuple[str, str]:
    """生成替换区域的 before/after 片段（含前后一行上下文），供 diff 审批展示。"""
    after_text = (
        text.replace(old_string, new_string)
        if replace_all
        else text.replace(old_string, new_string, 1)
    )
    return _diff_window(text, old_string), _diff_window(after_text, new_string)


def _diff_window(target: str, anchor: str) -> str:
    idx = target.find(anchor)
    if idx < 0:
        return anchor[:400]
    start = target.rfind("\n", 0, idx) + 1
    end = target.find("\n", idx + len(anchor))
    if end < 0:
        end = len(target)
    return target[start:end][:400]


def make_delete_file_tool(settings, project_dir: str | None = None) -> BaseTool:
    workspace = _resolve_workspace(settings, project_dir)

    def _invoke(path: str) -> dict:
        try:
            target = _safe_path(workspace, path)
            if not target.exists():
                raise FileNotFoundError(f"路径不存在：{path}")
            if target.is_dir():
                return {"error": "暂不支持删除目录，请逐文件删除", "summary": "删除失败：不支持目录"}
            trash = workspace / ".agent_trash" / time.strftime("%Y-%m-%d")
            trash.mkdir(parents=True, exist_ok=True)
            dest = trash / target.name
            n = 1
            while dest.exists():
                dest = trash / f"{target.stem}_{n}{target.suffix}"
                n += 1
            target.rename(dest)
            return {
                "summary": f"已删除 {path}（可恢复：.agent_trash/）",
                "path": str(target),
                "trash_path": str(dest),
            }
        except Exception as exc:
            return {"error": str(exc), "summary": f"删除失败：{exc}"}

    return StructuredTool.from_function(
        func=_invoke,
        name="delete_file",
        description=(
            "删除文件（不物理销毁，移入工作目录 .agent_trash/ 可恢复）。"
            "敏感操作：执行前会请求用户确认。"
        ),
        args_schema=None,
    )


def make_bash_tool(
    settings, project_dir: str | None = None, sandbox_override: str | None = None
) -> BaseTool:
    def _invoke(command: str, cwd: str = "") -> dict:
        return _run_command(settings, command, cwd, project_dir, sandbox_override)

    return StructuredTool.from_function(
        func=_invoke,
        name="bash",
        description=(
            "在工作目录内执行 shell 命令（Python/Node/Git 等），带超时与输出截断。"
            "用于运行脚本、跑测试、查看目录、执行简单工具。"
            "环境是 Git Bash（Unix 语法）：grep/find/sed/ls 均可用，路径用正斜杠"
            "（C:/Users/... 或 /c/Users/...），空设备写 /dev/null 而不是 NUL，"
            "不要用 cd /d 这类 cmd 语法。多行脚本先写入文件再执行，"
            "避免 python -c 的引号转义坑。敏感操作：默认每次执行都会请求用户确认；"
            "命中命令白名单的会自动放行。"
        ),
        args_schema=None,
    )


# ============================================================
# 旧版工具（设置页测试用，保持原行为）
# ============================================================


def make_file_tool(settings) -> BaseTool:
    """文件工具：白名单目录内的 list/read/write/append。"""
    workspace = _resolve_workspace(settings)

    def _invoke(operation: str, path: str, content: str = "") -> dict:
        try:
            target = _safe_path(workspace, path)
            op = operation.strip().lower()
            if op in ("list", "ls"):
                base = target if target.is_dir() else target.parent
                entries = [
                    {"name": p.name, "is_dir": p.is_dir(), "size": p.stat().st_size}
                    for p in sorted(base.iterdir())[:50]
                ]
                return {
                    "summary": f"列出 {len(entries)} 项",
                    "entries": entries,
                    "workspace": str(workspace),
                }
            if op in ("read", "cat"):
                if not target.is_file():
                    raise FileNotFoundError(f"文件不存在：{path}")
                text = target.read_text(encoding="utf-8", errors="ignore")
                return {
                    "summary": f"已读取 {path}（{len(text)} 字符）",
                    "content": _truncate(text, 4000),
                }
            if op in ("write", "append"):
                target.parent.mkdir(parents=True, exist_ok=True)
                mode = "a" if op == "append" else "w"
                with open(target, mode, encoding="utf-8") as f:
                    f.write(content)
                return {"summary": f"已{'追加' if op == 'append' else '写入'} {path}"}
            return {"error": f"不支持的操作：{operation}（支持 list/read/write/append）"}
        except Exception as exc:
            return {"error": str(exc), "summary": f"文件操作失败：{exc}"}

    return StructuredTool.from_function(
        func=_invoke,
        name="file_tool",
        description=(
            "在受控工作目录内操作文件：list（列出目录）、read（读取文本）、"
            "write（写入覆盖）、append（追加）。仅限工作目录内，禁止访问其他路径。"
            "当任务需要读写用户项目/知识库文件、整理文档时使用。"
        ),
        args_schema=None,
    )


def make_command_tool(settings) -> BaseTool:
    """命令工具：白名单命令 + 超时执行（设置页测试用）。"""

    def _invoke(command: str, cwd: str = "") -> dict:
        allowed, reason = command_allowed(settings, command)
        if not allowed:
            return {"error": reason or "命令执行未开启（未配置 COMMAND_ALLOWLIST 白名单）", "summary": reason or "命令被拒绝"}
        return _run_command(settings, command, cwd)

    return StructuredTool.from_function(
        func=_invoke,
        name="command_tool",
        description=(
            "在受控沙箱中执行白名单命令（需管理员在设置中开启并配置白名单）。"
            "用于运行脚本、查看目录、执行简单工具；不在白名单的命令会被拒绝。"
        ),
        args_schema=None,
    )


def extract_commands_from_skill(text: str, limit: int = 8) -> list[str]:
    """从 SKILL.md 的 bash 代码块提取命令（供沙箱执行）。"""
    blocks = re.findall(r"```(?:bash|sh|powershell|cmd)?\s*\n(.*?)```", text, re.DOTALL)
    commands: list[str] = []
    for block in blocks:
        for line in block.splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                commands.append(line)
            if len(commands) >= limit:
                return commands
    return commands


def run_sandbox_command(settings, command: str, timeout: int | None = None) -> dict:
    """技能沙箱执行入口：白名单 + 临时目录 + 超时。"""
    allowed, reason = command_allowed(settings, command)
    if not allowed:
        return {"ok": False, "error": reason or "命令未在白名单内"}
    try:
        with tempfile.TemporaryDirectory(prefix="rag_skill_") as tmp:
            proc = subprocess.run(
                command,
                shell=True,
                cwd=tmp,
                capture_output=True,
                text=True,
                timeout=timeout or max(1, int(effective(settings, "command_timeout") or 60)),
                encoding="utf-8",
                errors="replace",
            )
            return {
                "ok": proc.returncode == 0,
                "exit_code": proc.returncode,
                "output": _truncate((proc.stdout or "") + proc.stderr, 3000),
            }
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "命令超时已终止"}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
