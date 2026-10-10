"""展示格式化：Markdown 渲染、时长/数字/token 格式化。

从 cli_agent.py 拆出（2026-10-08 物理拆分）：只做移动，未改逻辑。"""

from __future__ import annotations

import re
from cli.term import clip, pad_right, paint, strwidth, wrap_styled


_MD_HR_RE = re.compile(r"^\s{0,3}([-*_])\1{2,}\s*$")


_MD_HEAD_RE = re.compile(r"^(#{1,6})\s+(.*)$")


_MD_QUOTE_RE = re.compile(r"^\s{0,3}>\s?(.*)$")


_MD_LIST_RE = re.compile(r"^(\s{0,4})([-*+]|\d{1,3}[.)])\s+(.*)$")


_MD_FENCE_RE = re.compile(r"^\s{0,3}(`{3,}|~{3,})(.*)$")


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


def inline_md(text: str) -> str:
    """行内 Markdown：`code`、**bold**、*italic*、[text](url)。"""
    text = re.sub(r"`([^`\n]+)`", lambda m: paint(m.group(1), "cyan"), text)
    text = re.sub(
        r"\*\*([^*\n]+)\*\*", lambda m: paint(m.group(1), bold=True), text
    )
    text = re.sub(
        r"(?<!\*)\*([^*\n]+)\*(?!\*)", lambda m: paint(m.group(1), italic=True), text
    )
    text = re.sub(
        r"\[([^\]\n]+)\]\(([^)\s]+)\)",
        lambda m: paint(m.group(1), "cyan", italic=False)
        + paint(f" ({m.group(2)})", "dim"),
        text,
    )
    return text


class MarkdownStream:
    """流式 Markdown 渲染器：按行渲染，缓冲不完整行。"""

    def __init__(self, width: int) -> None:
        self.width = max(20, width)
        self._line = ""
        self._flushed = 0
        self._in_fence = False
        self._fence_char = ""

    def feed(self, text: str) -> str:
        """输入模型文本，返回本次可以打印的完整内容（含换行）。"""
        out: list[str] = []
        self._line += text
        while "\n" in self._line:
            raw, self._line = self._line.split("\n", 1)
            out.extend(self._complete(raw))
            self._flushed = 0
        return "".join(out)

    def has_partial(self) -> bool:
        return len(self._line) > self._flushed

    def partial(self) -> str:
        """把部分行的未打印片段立即输出（inline 渲染，不换行）。"""
        remaining = self._line[self._flushed :]
        self._flushed = len(self._line)
        if not remaining:
            return ""
        if self._in_fence:
            return paint(remaining, "dim")
        return inline_md(remaining)

    def flush(self) -> str:
        out = self._complete(self._line) if self._line else []
        self._line = ""
        self._flushed = 0
        return "".join(out)

    def reset(self) -> None:
        """丢弃缓冲（撤回整轮输出后调用，避免残留片段再次被打印）。"""
        self._line = ""
        self._flushed = 0
        self._in_fence = False
        self._fence_char = ""

    def _complete(self, raw: str) -> list[str]:
        if self._flushed:
            # 前半段已按部分行打印，只补余下部分
            rest = raw[self._flushed :]
            if self._in_fence:
                m = _MD_FENCE_RE.match(raw)
                if m and m.group(1)[0] == self._fence_char:
                    self._in_fence = False
                    return [paint("└─", "dim") + "\n"]
                return [paint(rest, "dim") + "\n"]
            return [inline_md(rest) + "\n"]
        return self._render(raw)

    def _render(self, raw: str) -> list[str]:
        if self._in_fence:
            m = _MD_FENCE_RE.match(raw)
            if m and m.group(1)[0] == self._fence_char:
                self._in_fence = False
                return [paint("└─", "dim") + "\n"]
            return [paint(raw, "dim") + "\n"]
        m = _MD_FENCE_RE.match(raw)
        if m:
            self._in_fence = True
            self._fence_char = m.group(1)[0]
            label = m.group(2).strip() or "code"
            return [paint(f"┌─ {label}", "dim") + "\n"]
        if not raw.strip():
            return ["\n"]
        h = _MD_HEAD_RE.match(raw)
        if h:
            style = ("yellow", True) if len(h.group(1)) <= 2 else ("cyan", True)
            return [paint(inline_md(h.group(2)), *style) + "\n"]
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
            prefix = "  " + label + " "
            body = wrap_styled(
                inline_md(lst.group(3)), self.width, indent=strwidth(prefix)
            )
            lines = []
            for i, line in enumerate(body):
                pad = prefix if i == 0 else " " * strwidth(prefix)
                lines.append(pad + line + "\n")
            return lines
        if "|" in raw:
            return self._table(raw)
        return [line + "\n" for line in wrap_styled(inline_md(raw), self.width)]

    def _table(self, raw: str) -> list[str]:
        cells = [c.strip() for c in raw.strip().strip("|").split("|")]
        if not cells:
            return [raw + "\n"]
        if all(re.match(r"^:?-{2,}:?$", c) for c in cells):
            return [paint("├" + "─" * min(self.width - 2, 40) + "┤", "dim") + "\n"]
        n = len(cells)
        avail = max(20, self.width - (n * 3 + 1))
        colw = max(6, avail // n)
        rendered = [
            pad_right(" " + clip(inline_md(c), colw) + " ", colw + 2) for c in cells
        ]
        return ["│" + "│".join(rendered) + "│\n"]
