"""高危命令判定：独立于权限模式的最后一道人工确认底线。

即使 `tool_permission_mode=allow`，或命令命中了自动放行白名单，只要命中本模块
也会强制走人工确认。判定偏保守：宁可多问一次，也不让系统级/不可逆操作静默执行。

边界：
- 不判断普通写操作（写/删文件属于 SENSITIVE_TOOLS 的职责）；
- 不负责拦截执行——用户批准后仍由 tools_extra 执行；
- 白名单是"自动放行前缀"，解释器命令（python/bash/powershell...）的命令体
  不受前缀约束，因此永远不能靠白名单自动放行（见 is_interpreter_command）。
"""

from __future__ import annotations

import re
import shlex

# 复合命令检测（原在 tools_extra.py；内置只读白名单也需要它，故下移共用）。
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
    hits: set[str] = set()
    for t in toks:
        if t and all(c in _COMPOUND_OPS for c in t):
            hits.update(_COMPOUND_OPS[c] for c in t)
    return ("命令含" + "、".join(sorted(hits))) if hits else ""

# 解释器前缀不能作为自动放行白名单：`python -c "..."`、`bash -c "..."` 的
# 命令体可以任意，前缀匹配等于没有限制。这里只影响"自动放行"，
# 人工批准后仍可执行。
INTERPRETER_COMMANDS = {
    "python", "python3", "py", "pip", "pip3",
    "bash", "sh", "zsh", "dash", "fish",
    "cmd", "cmd.exe", "powershell", "powershell.exe", "pwsh", "pwsh.exe",
    "node", "nodejs", "npm", "npx", "yarn", "pnpm",
    "perl", "ruby", "php", "lua", "rscript",
    "wscript", "cscript", "mshta", "rundll32", "regsvr32",
}

# 整条命令级特征（管道/编码命令/fork bomb 等，无法靠单段首命令判断）
_WHOLE_PATTERNS: list[tuple[re.Pattern, str]] = [
    (
        re.compile(
            r"\b(?:curl|wget|iwr|irm|invoke-webrequest|invoke-restmethod)\b"
            r"[^|;\n]*\|\s*(?:sudo\s+)?(?:sh|bash|zsh|dash|iex|powershell|pwsh|cmd)\b",
            re.I,
        ),
        "远程脚本直接执行",
    ),
    (
        re.compile(
            r"\b(?:powershell|pwsh)(?:\.exe)?\b[^\n]*\s(?:-e|-enc|-encodedcommand)\s+[a-z0-9+/=]{16,}",
            re.I,
        ),
        "PowerShell 编码命令",
    ),
    (
        re.compile(r"\b(?:mshta|rundll32)\b[^\n]*(?:https?://|javascript:)", re.I),
        "远程/脚本宿主执行",
    ),
    (
        re.compile(r":\s*\(\s*\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:", re.I),
        "fork 炸弹",
    ),
    (
        re.compile(r"\d?>{1,2}\s*/dev/(?:sd|hd|nvme|disk|vd)", re.I),
        "写裸磁盘设备",
    ),
]

# 单段命令里出现这些解释器内联代码特征时，按高危处理（保守判定）
_INLINE_DESTRUCTIVE = re.compile(
    r"\b(?:rm\s+-[a-z]*[rf]|rmtree|os\.remove|os\.rmdir|os\.system|"
    r"shutil\.rmtree|rmsync|rmdirsync|unlinksync|execsync|spawnsync|fs\.rm|"
    r"remove-item|del\s+/[fsq]|rd\s+/s|format\s+[a-z]:|mkfs|diskpart|"
    r"shutdown|reboot|poweroff|dd\s+of=)\b",
    re.I,
)

_SYSTEM_PATHS = {
    "/", "~", "$home", "%systemdrive%", "%windir%", "%systemroot%",
    "/etc", "/usr", "/bin", "/sbin", "/boot", "/var", "/opt", "/home",
    "/root", "/dev", "/proc", "/sys",
}


def _norm(command: str) -> str:
    return re.sub(r"\s+", " ", str(command or "")).strip().lower()


def _strip_quotes(token: str) -> str:
    return str(token or "").strip().strip("\"'")


def _basename(program: str) -> str:
    base = _strip_quotes(program).replace("\\", "/").rsplit("/", 1)[-1].lower()
    base = re.sub(r"\.(?:exe|cmd|bat|com)$", "", base)
    if re.fullmatch(r"python3\.\d+", base):
        return "python3"
    return base


def _split_segments(command: str) -> list[str]:
    """按未加引号的 ; && || | & 换行拆成简单命令段（引号内的分隔符不拆）。"""
    out: list[str] = []
    buf: list[str] = []
    quote = ""
    i = 0
    while i < len(command):
        ch = command[i]
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = ""
            i += 1
            continue
        if ch in "\"'":
            quote = ch
            buf.append(ch)
            i += 1
            continue
        if ch == ";" or command.startswith("&&", i) or command.startswith("||", i):
            out.append("".join(buf))
            buf = []
            i += 2 if command.startswith(("&&", "||"), i) else 1
            continue
        if ch in "&|\n":
            out.append("".join(buf))
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    out.append("".join(buf))
    return [s.strip() for s in out if s.strip()]


_SUDO_VALUE_FLAGS = {"-u", "-g", "-p", "-c", "-t", "-u", "-r", "-h", "-U"}
_ENV_VALUE_FLAGS = {"-u", "-s", "-i", "--unset", "--split-string"}


def _first_tokens(segment: str) -> list[str]:
    """拆出命令段的 token，跳过 sudo/env/timeout 等包装器与环境变量赋值。"""
    toks = segment.strip().split()
    i = 0
    while i < len(toks):
        t = _strip_quotes(toks[i]).lower()
        if re.match(r"^[a-z_][a-z0-9_]*=", t):
            i += 1
            continue
        if t in ("sudo", "doas"):
            i += 1
            while i < len(toks) and toks[i].startswith("-"):
                if toks[i].lower() in _SUDO_VALUE_FLAGS and i + 1 < len(toks):
                    i += 2
                else:
                    i += 1
            continue
        if t in ("env", "command", "nohup", "time"):
            i += 1
            while i < len(toks) and (
                toks[i].startswith("-")
                or re.match(r"^[a-z_][a-z0-9_]*=", toks[i].lower())
            ):
                if t == "env" and toks[i].lower() in _ENV_VALUE_FLAGS and i + 1 < len(toks):
                    i += 2
                else:
                    i += 1
            continue
        if t == "timeout":
            i += 1
            while i < len(toks) and toks[i].startswith("-"):
                i += 1
            if i < len(toks):
                i += 1  # duration
            continue
        if t == "nice":
            i += 1
            if i < len(toks) and toks[i].lower() == "-n" and i + 1 < len(toks):
                i += 2
            elif i < len(toks) and toks[i].startswith("-"):
                i += 1
            continue
        break
    return toks[i:]


def _first_program(segment: str) -> str:
    toks = _first_tokens(segment)
    return _basename(toks[0]) if toks else ""


def _is_system_path(token: str) -> bool:
    """目标是否为系统根/家目录/盘根等不可逆位置（不看普通子目录）。"""
    t = _strip_quotes(token).lower().replace("\\", "/")
    t = re.sub(r"/\*$", "", t)
    t = re.sub(r"/+$", "", t)
    if not t or t == "*":
        return True
    if t in _SYSTEM_PATHS:
        return True
    if re.fullmatch(r"[a-z]:", t):
        return True
    if re.fullmatch(
        r"[a-z]:/(?:windows|program files|program files \(x86\)|programdata|users)",
        t,
    ):
        return True
    return False


def _has_recursive_flag(args: list[str]) -> bool:
    for a in args:
        low = _strip_quotes(a).lower()
        if low in ("-r", "-rf", "-fr", "-recurse", "--recursive"):
            return True
        if low.startswith("--") and "recursive" in low:
            return True
        if low.startswith("-") and "r" in low.strip("-"):
            return True
    return False


def _targets(args: list[str]) -> list[str]:
    """过滤掉命令行开关，保留操作目标。

    只把 `-x` 与 Windows 单字母开关（`/s`、`/q`、`/f`）当开关；
    Unix 绝对路径 `/`、`/*` 是目标，不能被过滤掉。
    """
    out = []
    for arg in args:
        token = _strip_quotes(arg)
        if not token:
            continue
        if token.startswith("-"):
            continue
        if re.fullmatch(r"/[a-zA-Z?]", token):
            continue
        out.append(token)
    return out


def _segment_risk(segment: str) -> str | None:
    toks = _first_tokens(segment)
    if not toks:
        return None
    prog = _basename(toks[0])
    args = toks[1:]

    # ---- 解释器内联代码：递归检查命令体 ----
    if prog in INTERPRETER_COMMANDS:
        inline = _interpreter_inline_payload(prog, args)
        if inline:
            if _INLINE_DESTRUCTIVE.search(inline):
                return "解释器内联执行破坏性操作"
            # 递归一层：处理 powershell -Command "Remove-Item ..." 这类
            for sub in _split_segments(inline):
                reason = _segment_risk(sub)
                if reason:
                    return reason
        return None

    # ---- 递归删除 / 删除系统目录 ----
    if prog == "rm":
        if _has_recursive_flag(args) and any(
            _is_system_path(t) for t in _targets(args)
        ):
            return "递归删除系统/家目录"
        return None
    if prog in ("rd", "rmdir"):
        flags = [a.lower() for a in args if a.startswith("/") or a.startswith("-")]
        recursive = any(f in ("/s", "-r", "-recurse", "--recursive") for f in flags)
        if recursive and any(_is_system_path(t) for t in _targets(args)):
            return "递归删除系统目录"
        return None
    if prog in ("del", "erase"):
        flags = [a.lower() for a in args if a.startswith("/")]
        if "/s" in flags and any(_is_system_path(t) for t in _targets(args)):
            return "递归删除系统目录"
        return None
    if prog == "remove-item":
        if _has_recursive_flag(args) and any(
            _is_system_path(t) for t in _targets(args)
        ):
            return "递归删除系统目录"
        return None

    # ---- 磁盘 / 分区 ----
    if prog == "format":
        return "格式化磁盘"
    if prog == "diskpart" or prog.startswith("mkfs") or prog in (
        "mke2fs",
        "fdisk",
        "parted",
        "wipefs",
    ):
        return "磁盘分区/擦除"
    if prog == "dd":
        joined = _norm(" ".join(args))
        if "of=/dev/" in joined or "of=\\\\.\\" in joined:
            return "写裸磁盘设备"
        return None

    # ---- 系统电源 ----
    if prog in ("shutdown", "reboot", "poweroff", "halt"):
        return "关闭/重启系统"

    # ---- 权限 / 所有权 ----
    if prog in ("chmod", "chown", "chgrp"):
        # 即使不加 -R，改系统根目录的权限也可能让系统不可用
        if any(_is_system_path(t) for t in _targets(args)):
            return "修改系统目录权限"
        return None

    # ---- Windows 备份/还原/注册表 ----
    joined = _norm(" ".join(args))
    if prog == "vssadmin" and "delete" in joined and "shadow" in joined:
        return "删除系统还原点"
    if prog == "wmic" and "shadowcopy" in joined and "delete" in joined:
        return "删除系统还原点"
    if prog == "wbadmin" and "delete" in joined:
        return "删除系统备份"
    if prog == "cipher" and "/w" in joined:
        return "擦除磁盘空闲空间"
    if prog == "reg" and "delete" in joined and "hklm" in joined:
        return "删除机器级注册表项"
    if prog == "bcdedit" and any(
        a.lower() in ("/delete", "/deletevalue") for a in args
    ):
        return "修改启动配置"

    return None


def _interpreter_inline_payload(program: str, args: list[str]) -> str | None:
    """取出解释器的内联代码参数（-c / -Command / /c / -e），没有则 None。"""
    lowered = [a.lower() for a in args]
    inline_flags = {
        "python": ("-c",),
        "python3": ("-c",),
        "py": ("-c",),
        "bash": ("-c",),
        "sh": ("-c",),
        "zsh": ("-c",),
        "dash": ("-c",),
        "fish": ("-c",),
        "cmd": ("/c", "/k"),
        "cmd.exe": ("/c", "/k"),
        "powershell": ("-command", "-c"),
        "powershell.exe": ("-command", "-c"),
        "pwsh": ("-command", "-c"),
        "pwsh.exe": ("-command", "-c"),
        "node": ("-e", "--eval", "-p", "--print"),
        "nodejs": ("-e", "--eval"),
        "perl": ("-e",),
        "ruby": ("-e",),
        "php": ("-r",),
    }.get(program)
    if not inline_flags:
        return None
    for i, low in enumerate(lowered):
        if low in inline_flags and i + 1 < len(args):
            return " ".join(args[i + 1 :])
    return None


def high_risk_command(command: str) -> str | None:
    """返回高危原因；None 表示未命中（普通命令仍需按权限模式处理）。"""
    raw = str(command or "")
    if not raw.strip():
        return None
    normalized = _norm(raw)
    for pattern, reason in _WHOLE_PATTERNS:
        if pattern.search(normalized):
            return reason
    for segment in _split_segments(raw):
        reason = _segment_risk(segment)
        if reason:
            return reason
    return None


def is_interpreter_command(command: str) -> bool:
    """命令首个可执行程序是否为解释器（白名单不得自动放行这类命令）。"""
    segments = _split_segments(command)
    if not segments:
        return False
    return _first_program(segments[0]) in INTERPRETER_COMMANDS


# ---------------- 内置只读命令白名单 ----------------
# 与用户可编辑的 command_allowlist 不同：这些是系统内置的低风险只读命令，
# 不需要用户逐条配置；仍受高危、复合命令、提权包装与命令替换约束。
SAFE_READONLY_COMMANDS = {
    "echo", "pwd", "ls", "dir", "cat", "type", "head", "tail", "wc",
    "grep", "which", "where", "whoami", "stat", "file",
    "md5sum", "sha1sum", "sha256sum", "du", "df", "sort", "uniq",
    "cut", "tr", "ps", "netstat", "tasklist", "ver", "tree",
}

_SAFE_GIT_SUBCOMMANDS = {
    "status", "diff", "log", "show", "rev-parse", "remote", "branch",
}
_GIT_MUTATING_FLAGS = {
    "-d", "-D", "--delete", "-m", "-M", "--move", "-c", "--copy",
    "--edit-description", "--output", "--ext-diff", "--textconv",
}

_HIGH_RISK_FILE_OPS = {"write_file", "edit_file", "delete_file"}
_SENSITIVE_FILE_SUBOPS = {"write", "append", "edit", "delete", "move"}


def high_risk_file_operation(name: str, args: dict | None) -> str | None:
    """高危文件写/删判定：触碰动态工具/后端源码/Git钩子/环境变量文件时强制人工确认。"""
    args = args or {}
    if name == "file_tool":
        op = str(args.get("operation") or "").strip().lower()
        if op not in _SENSITIVE_FILE_SUBOPS:
            return None
    elif name not in _HIGH_RISK_FILE_OPS:
        return None

    raw_path = str(args.get("path") or "").strip().replace("\\", "/")
    if not raw_path:
        return None
    norm = re.sub(r"/+", "/", raw_path).strip("/").lower()
    parts = [p for p in norm.split("/") if p and p != "."]
    if not parts:
        return None
    base = parts[-1]
    joined = "/".join(parts)

    # 1. 环境密钥文件（.env / .env.local / .env.production 等，.env.example 除外）
    if (base == ".env" or base.startswith(".env.")) and base != ".env.example":
        return "修改环境变量/密钥文件（.env）"

    # 2. Git 钩子与配置（可植入任意命令在 commit/checkout 时执行）
    if ".git/hooks" in joined or joined.endswith(".git/config") or "scripts/githooks" in joined:
        return "修改 Git 钩子或仓库配置"

    # 3. 动态工具目录与后端应用代码（user_tools 会在下次请求 prepare 时自动 exec_module）
    if (
        "user_tools/" in joined
        or joined.endswith("/user_tools")
        or joined.startswith("backend/app/")
        or "/backend/app/" in f"/{joined}/"
        or joined.startswith("app/user_tools/")
    ):
        return "修改后端核心代码或自写工具目录"

    return None


def safe_readonly_command(command: str) -> str | None:
    """内置只读命令判定：None = 可自动放行；否则返回拒绝原因。

    保守边界：
    - 只接受单段命令（复合命令一律人工确认）；
    - 拒绝 sudo/doas/env 包装；
    - 拒绝命令替换（反引号 / $()）与参数展开（${...} / $'...'），即使被引号包裹；
    - git 只放行无全局选项（禁 -c/--exec-path 等）的查询子命令；
    - sort -o、uniq 的第二个位置参数（输出文件）不放行。
    """
    line = str(command or "")
    if "`" in line or "$(" in line or "${" in line or "$'" in line:
        return "命令含命令替换或参数展开"
    compound = _compound_reason(line)
    if compound:
        return compound
    segments = _split_segments(line)
    if len(segments) != 1:
        return "复合命令"
    segment = segments[0]
    try:
        raw_tokens = shlex.split(segment)
    except ValueError:
        return "命令引号不闭合"
    for tok in raw_tokens:
        low = tok.lower()
        if re.match(r"^[a-z_][a-z0-9_]*=", low):
            continue
        if low in ("sudo", "doas", "env"):
            return "提权/环境包装"
        break
    toks = _first_tokens(segment)
    if not toks:
        return "空命令"
    prog = _basename(toks[0])
    args = toks[1:]
    if prog == "git":
        if not args or args[0].startswith("-"):
            return "git 全局选项不允许自动放行"
        sub = args[0].lower()
        if sub not in _SAFE_GIT_SUBCOMMANDS:
            return f"git {sub or '(无子命令)'} 不在只读集合"
        rest = args[1:]
        for arg in rest:
            low = arg.lower()
            if (
                low in _GIT_MUTATING_FLAGS
                or low.startswith("--output")
                or low.startswith("--ext-diff")
                or low.startswith("--textconv")
            ):
                return "git 参数会修改文件、分支或调用外部程序"
        if sub == "branch" and any(not a.startswith("-") for a in rest):
            return "git branch 只允许列表/查询"
        if sub == "remote" and any(
            a.lower() not in ("-v", "--verbose") for a in rest
        ):
            return "git remote 只允许 -v"
        return None
    if prog not in SAFE_READONLY_COMMANDS:
        return "不在只读命令集合"
    if prog == "sort" and any(
        a.lower() == "-o"
        or a.lower().startswith("--output")
        or (a.startswith("-o") and len(a) > 2)
        for a in args
    ):
        return "sort 的 -o 会写文件"
    if prog == "uniq" and len(_targets(args)) > 1:
        return "uniq 的第二个位置参数是输出文件"
    return None
