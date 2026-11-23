"""展示格式化：Markdown 渲染、时长/数字/token 格式化。

从 cli_agent.py 拆出（2026-10-08 物理拆分）：只做移动，未改逻辑。"""

from __future__ import annotations

import re
from cli.term import clip, link, pad_right, paint, strwidth, wrap_styled


def todo_mark(done) -> str:
    """任务清单状态符号：✔ 已完成 / ○ 未完成（旧实现 [x]/[ ] 看起来像叉号）。"""
    return "✔" if done else "○"


_MD_HR_RE = re.compile(r"^\s{0,3}([-*_])\1{2,}\s*$")


_MD_HEAD_RE = re.compile(r"^(#{1,6})\s+(.*)$")


_MD_QUOTE_RE = re.compile(r"^\s{0,3}>\s?(.*)$")


_MD_LIST_RE = re.compile(r"^(\s{0,4})([-*+]|\d{1,3}[.)])\s+(.*)$")


_MD_FENCE_RE = re.compile(r"^\s{0,3}(`{3,}|~{3,})(.*)$")

# ---------------- 代码块轻量语法高亮（纯标准库，不引入 Pygments） ----------------

_CODE_STR = r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|`(?:\\.|[^`\\])*`'
_CODE_NUM = r"\b(?:0[xX][0-9a-fA-F]+|\d+(?:\.\d+)?)\b"
_CODE_KEYWORDS = {
    "python": "def class return if elif else for while try except finally with as import from "
    "pass raise yield lambda global nonlocal assert del in is not and or None True False async await",
    "js": "function const let var return if else for while try catch finally class extends new "
    "import export default async await yield throw typeof instanceof null undefined true false",
    "ts": "function const let var return if else for while try catch finally class extends new "
    "import export default async await yield throw typeof instanceof interface type enum "
    "implements public private readonly null undefined true false",
    "go": "func package import return if else for range switch case default defer go chan "
    "type struct interface map var const nil true false",
    "rust": "fn let mut pub use mod struct enum impl trait match if else for while loop return "
    "where async await move ref self Self true false",
    "sql": "select from where join left right inner outer on group by order having limit insert "
    "into values update set delete create table drop alter index primary key foreign references "
    "and or not null is in as distinct union all case when then else end",
    "shell": "if then else elif fi for while do done case esac function export local return "
    "in echo cd pwd set unset shift source",
}
_LANG_ALIASES = {
    "py": "python", "python3": "python",
    "javascript": "js", "node": "js", "jsx": "js",
    "typescript": "ts", "tsx": "ts",
    "golang": "go", "rs": "rust",
    "bash": "shell", "sh": "shell", "zsh": "shell", "ps1": "shell", "powershell": "shell",
    "yml": "yaml", "jsonc": "json",
}
_CODE_COLORS = {
    "comment": "dim",
    "string": "green",
    "number": "yellow",
    "keyword": "cyan",
    "key": "cyan",
}


def _code_pattern(lang: str) -> re.Pattern | None:
    lang = _LANG_ALIASES.get(lang, lang)
    kw = _CODE_KEYWORDS.get(lang)
    if lang == "json":
        return re.compile(
            rf'(?P<key>{_CODE_STR})(?=\s*:)|(?P<string>{_CODE_STR})'
            rf"|(?P<keyword>\b(?:true|false|null)\b)|(?P<number>{_CODE_NUM})"
        )
    if lang == "yaml":
        return re.compile(
            rf"(?P<comment>#.*)|(?P<key>^\s*[\w.-]+(?=\s*:))|(?P<string>{_CODE_STR})"
            rf"|(?P<number>{_CODE_NUM})|(?P<keyword>\b(?:true|false|null|yes|no)\b)",
            re.IGNORECASE,
        )
    if lang in ("python", "shell", "yaml"):
        comment = r"#.*"
    elif lang in ("js", "ts", "go", "rust"):
        comment = r"//.*"
    elif lang == "sql":
        comment = r"--.*"
    else:
        return None
    words = "|".join(kw.split()) if kw else ""
    return re.compile(
        rf"(?P<comment>{comment})|(?P<string>{_CODE_STR})|(?P<number>{_CODE_NUM})"
        + (rf"|(?P<keyword>\b(?:{words})\b)" if words else ""),
        re.IGNORECASE if lang == "sql" else 0,
    )


def highlight_code(line: str, lang: str = "") -> str:
    """单行代码着色；未知语言/非 token 部分保持正常前景（不再整段灰色）。"""
    pattern = _code_pattern((lang or "").strip().lower())
    if pattern is None:
        return line
    out: list[str] = []
    pos = 0
    for match in pattern.finditer(line):
        if match.start() > pos:
            out.append(line[pos : match.start()])
        kind = match.lastgroup or "string"
        out.append(
            paint(
                match.group(0),
                _CODE_COLORS.get(kind, ""),
                bold=kind == "keyword",
            )
        )
        pos = match.end()
    if pos < len(line):
        out.append(line[pos:])
    return "".join(out) or line


def fmt_dur(seconds: float) -> str:
    seconds = max(0.0, seconds)
    if seconds < 1.0:
        return f"{int(seconds * 1000)}ms"
    if seconds < 60:
        return f"{seconds:.1f}s"
    total = int(seconds)
    m, s = divmod(total, 60)
    if m < 60:
        return f"{m}m{s:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h{m:02d}m"


def fmt_num(n: float) -> str:
    if n >= 1000:
        return f"{n / 1000:.1f}k"
    return str(int(n))


def fmt_tokens(usage: dict) -> str:
    if not usage:
        return ""
    inp = usage.get("input_tokens") or usage.get("prompt_tokens")
    outp = usage.get("output_tokens") or usage.get("completion_tokens")
    parts = []
    if inp is not None:
        parts.append(f"输入 {fmt_num(inp)}")
    if outp is not None:
        parts.append(f"输出 {fmt_num(outp)}")
    return " · ".join(parts)


# 行内标记一次性扫描。曾经是四条独立的正则串行替换：上色后的文本里带着
# \x1b[1m 这类转义，链接正则的 \[ 会命中转义里的 "["，把 "1m" 当成链接标签
# 吞掉，终端上表现为 "m1m粗体" 残字（见 tests/test_cli_render.py）。
_INLINE_RE = re.compile(
    r"(?P<code>`[^`\n]+`)"
    r"|(?P<bold>\*\*[^*\n]+\*\*)"
    r"|(?P<link>\[[^\]\n]+\]\([^)\s]+\))"
    r"|(?P<italic>(?<!\*)\*(?!\*)[^*\n]+\*(?!\*))"
)


def inline_md(text: str) -> str:
    """行内 Markdown：`code`、**bold**、*italic*、[text](url)。

    单次扫描：匹配只发生在**未着色**的原文上，因此不会出现"正则吃掉
    ANSI 转义"的残字（原实现串行替换，链接正则会把 \x1b[1m 的 [1m 当标签）。
    """
    out: list[str] = []
    pos = 0
    for m in _INLINE_RE.finditer(text):
        if m.start() > pos:
            out.append(text[pos : m.start()])
        kind = m.lastgroup
        token = m.group(0)
        if kind == "code":
            out.append(paint(token[1:-1], "cyan"))
        elif kind == "bold":
            out.append(paint(token[2:-2], bold=True))
        elif kind == "italic":
            out.append(paint(token[1:-1], italic=True))
        elif kind == "link":
            label, url = token[1:-1].split("](", 1)
            out.append(link(paint(label, "cyan"), url) + paint(f" ({url})", "dim"))
        pos = m.end()
    if pos < len(text):
        out.append(text[pos:])
    return "".join(out)


class MarkdownStream:
    """按行缓冲的 Markdown 渲染器。

    只渲染完整行：半行提前输出会把表格/加粗/列表拆成原始 Markdown，
    也无法正确折行（实测表格分隔线和数据行变成裸 `|---|`）。
    """

    def __init__(self, width: int) -> None:
        self.width = max(20, width)
        self._line = ""
        self._in_fence = False
        self._fence_char = ""
        self._fence_lang = ""
        self._table_active = False
        self._table_cols = 0
        self._table_rows: list[list[str]] = []
        self._table_sep = False
        self._table_widths: list[int] | None = None

    def feed(self, text: str) -> str:
        """输入模型文本，返回本次可以打印的完整行内容。"""
        out: list[str] = []
        self._line += text
        while "\n" in self._line:
            raw, self._line = self._line.split("\n", 1)
            out.extend(self._render(raw))
        return "".join(out)

    def has_partial(self) -> bool:
        # 不再输出半行：结构/加粗/折行必须在完整行上判断
        return False

    def partial(self) -> str:
        return ""

    def flush(self) -> str:
        out = self._render(self._line) if self._line else []
        out.extend(self._flush_pending_table())
        self._line = ""
        return "".join(out)

    def reset(self) -> None:
        """丢弃缓冲（撤回整轮输出后调用，避免残留片段再次被打印）。"""
        self._line = ""
        self._in_fence = False
        self._fence_char = ""
        self._fence_lang = ""
        self._table_active = False
        self._table_cols = 0
        self._table_rows = []
        self._table_sep = False
        self._table_widths = None

    def _render(self, raw: str) -> list[str]:
        flushed: list[str] = []
        if "|" not in raw:
            flushed = self._flush_pending_table()
            self._table_active = False
            self._table_cols = 0
            self._table_widths = None
            self._table_sep = False
            self._table_rows = []
        return flushed + self._render_line(raw)

    def _render_line(self, raw: str) -> list[str]:
        if self._in_fence:
            m = _MD_FENCE_RE.match(raw)
            if m and m.group(1)[0] == self._fence_char:
                self._in_fence = False
                self._fence_lang = ""
                return [paint("└─", "dim") + "\n"]
            return [highlight_code(raw, self._fence_lang) + "\n"]
        is_table = "|" in raw
        m = _MD_FENCE_RE.match(raw)
        if m:
            info = m.group(2).strip()
            fence = m.group(1)
            # 单行围栏（```code```）：不要进入 fence 状态，否则后面整段会被当代码
            if fence in info:
                inner = info.split(fence, 1)[0]
                return [highlight_code(inner, "") + "\n"]
            self._in_fence = True
            self._fence_char = fence[0]
            self._fence_lang = info.split()[0].lower() if info else ""
            return [paint("┌─ ", "dim") + paint(info or "code", "cyan") + "\n"]
        if not raw.strip():
            return ["\n"]
        h = _MD_HEAD_RE.match(raw)
        if h:
            level = len(h.group(1))
            if level == 1:
                prefix, color = "█ ", "yellow"
            elif level == 2:
                prefix, color = "▌ ", "cyan"
            else:
                prefix, color = "▸ ", "cyan"
            body = wrap_styled(inline_md(h.group(2)), self.width - 2)
            return [
                paint(prefix, color, bold=True)
                + paint(line, color, bold=True)
                + "\n"
                for line in body
            ]
        if _MD_HR_RE.match(raw):
            return [paint("─" * min(self.width, 60), "dim") + "\n"]
        q = _MD_QUOTE_RE.match(raw)
        if q:
            body = wrap_styled(inline_md(q.group(1)), self.width - 2, indent=2)
            return [paint("│ ", "dim") + line + "\n" for line in body]
        lst = _MD_LIST_RE.match(raw)
        if lst:
            marker = lst.group(2)
            label = "•" if marker in "-*+" else marker.rstrip(".)") + "."
            indent = len(lst.group(1))
            prefix = " " * (2 + indent) + label + " "
            # first_indent 必须计入前缀宽度：只给 indent 会让首行按整宽排版，
            # 拼上前缀后就超出终端宽度（实测 width=40 时首行 45 列）。
            pad = strwidth(prefix)
            body = wrap_styled(
                inline_md(lst.group(3)),
                self.width,
                first_indent=pad,
                indent=pad,
            )
            # wrap_styled 的首行缩进是等宽空格，换成真实标记（"• " / "1. "）
            if body:
                body[0] = prefix + body[0][pad:]
            return [line + "\n" for line in body]
        if is_table:
            return self._table(raw)
        return [line + "\n" for line in wrap_styled(inline_md(raw), self.width)]

    def _equal_widths(self, cols: int) -> list[int]:
        avail = max(20, self.width - (cols * 3 + 1))
        return [max(4, avail // cols)] * cols

    def _calc_widths(self, header: list[str], rows: list[list[str]] | None) -> list[int]:
        """按表头 + **全部数据行** 分配列宽：短列（序号/备注）窄，长文本列宽。

        只按第一行数据算权重会让后续宽值（如 "310ms" 对 "42ms"）mid-token
        折行（"310m" / "s"），所以这里用每列所有行的最大宽度做提示。
        """
        cols = len(header)
        avail = max(20, self.width - (cols * 3 + 1))
        mins = [min(16, max(4, strwidth(c))) for c in header]
        total_min = sum(mins)
        if total_min >= avail:
            widths = [max(3, avail * m // max(1, total_min)) for m in mins]
        else:
            widths = list(mins)
            rest = avail - total_min
            hints = [0] * cols
            for row in rows or []:
                for i, c in enumerate(row[:cols]):
                    hints[i] = max(hints[i], strwidth(c))
            weights = [max(0, hints[i] - widths[i]) for i in range(cols)]
            wsum = sum(weights)
            if wsum <= 0:
                for i in range(cols):
                    widths[i] += rest // cols
                for i in range(rest % cols):
                    widths[i] += 1
            else:
                added = 0
                for i in range(cols):
                    add = rest * weights[i] // wsum
                    widths[i] += add
                    added += add
                for i in range(rest - added):
                    widths[i % cols] += 1
        while sum(widths) > avail and max(widths) > 3:
            idx = max(range(cols), key=lambda i: widths[i])
            widths[idx] -= 1
        return widths

    def _render_table_row(self, cells: list[str], header: bool = False) -> list[str]:
        widths = self._table_widths or self._equal_widths(len(cells))
        wrapped = []
        for i, c in enumerate(cells):
            lines = wrap_styled(inline_md(c), max(4, widths[i]))
            wrapped.append(lines or [""])
        height = max(len(lines) for lines in wrapped)
        out: list[str] = []
        for row in range(height):
            parts = []
            for i, lines in enumerate(wrapped):
                text = lines[row] if row < len(lines) else ""
                cell = " " + pad_right(text, widths[i]) + " "
                if header:
                    cell = paint(cell, "cyan", bold=True)
                parts.append(cell)
            out.append("│" + "│".join(parts) + "│\n")
        return out

    def _render_table_sep(self) -> str:
        widths = self._table_widths or []
        return (
            paint(
                "├" + "┼".join("─" * (w + 2) for w in widths) + "┤",
                "dim",
            )
            + "\n"
        )

    def _flush_pending_table(self) -> list[str]:
        """表格结束（遇到非表格行 / 流结束）时整表渲染。

        必须缓存整表：列宽依赖全部行的最大宽度，边到边渲染无法回头改宽。
        """
        if not self._table_rows:
            if self._table_sep:
                # 只有分隔线、没有表头（模型偶尔这么写）：按等宽补一条
                self._table_widths = self._equal_widths(self._table_cols or 1)
                self._table_sep = False
                return [self._render_table_sep()]
            return []
        rows = self._table_rows
        self._table_rows = []
        header, data = rows[0], rows[1:]
        self._table_widths = self._calc_widths(header, data)
        out = self._render_table_row(header, header=True)
        if self._table_sep:
            out.append(self._render_table_sep())
            self._table_sep = False
        for row in data:
            out.extend(self._render_table_row(row))
        return out

    def _table(self, raw: str) -> list[str]:
        cells = [c.strip() for c in raw.strip().strip("|").split("|")]
        if not cells:
            return [raw + "\n"]
        # 列数以本表首行为准：后续行多/少列时归一化，避免边框错位
        if not self._table_cols:
            self._table_cols = len(cells)
        cols = self._table_cols
        if len(cells) < cols:
            cells += [""] * (cols - len(cells))
        elif len(cells) > cols:
            cells = cells[: cols - 1] + [" | ".join(cells[cols - 1 :])]
        if all(re.match(r"^:?-{2,}:?$", c) for c in cells):
            self._table_sep = True
            self._table_active = True
            return []
        self._table_rows.append(cells)
        self._table_active = True
        return []
