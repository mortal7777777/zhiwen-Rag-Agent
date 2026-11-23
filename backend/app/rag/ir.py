"""文档 IR：页面 Markdown → 结构块 → 跨页 parent / 表格原子块。

目标（2026-11-18）：
- 跨页题目/段落不再按页截断：书籍/讲义连续累积，遇到标题/题目边界或大小上限才切；
- 表格作为原子块，不跨 parent 切开；子块按行分组并重复表头；
- 课件（slide）默认按页边界，只有明显的跨页续句才合并；
- 每块保留 page / image 引用，parent 记录 pages / heading_path / block_kinds。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_HTML_TABLE_RE = re.compile(r"<table\b.*?</table>", re.I | re.S)
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_MD_TABLE_ROW_RE = re.compile(r"^\s*\|.*\|\s*$")
_MD_TABLE_SEP_RE = re.compile(r"^\s*\|?[\s:|-]+\|?\s*$")
_QUESTION_RE = re.compile(r"^\s*(【(?:例|检测|练习)[^】]*】|例\s*\d+)")
_IMG_MARK_RE = re.compile(r"\[\[IMG:([^\]]+)\]\]")
_HTML_IMG_RE = re.compile(r"<div[^>]*>\s*<img[^>]*>\s*</div>", re.S | re.I)
_IMG_TAG_RE = re.compile(r"<img[^>]*>", re.I)
_MD_IMG_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_TERMINAL = tuple("。！？；.!?;")
_CONT_CUE = tuple("，,、：:（(【[")


def strip_image_markers(text: str) -> str:
    """去掉 [[IMG:...]] 标记（图片信息由 metadata.image_refs 承载）。"""
    cleaned = _IMG_MARK_RE.sub("", text)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def insert_image_markers(
    markdown: str,
    slots: list[str | None],
    ocr_texts: dict[str, str] | None = None,
) -> tuple[str, list[str]]:
    """把页内图片块的 [[IMG:id]] 标记按阅读顺序插回原位。

    slots 与图片块一一对应（None = 该块被丢弃/裁剪失败），替换掉 PP-Structure
    输出的 `<div><img></div>`；未匹配到标签的图片由调用方追加到页尾。
    返回 (新 Markdown, 已插回的图片 id 列表)。
    """
    ocr_texts = ocr_texts or {}
    placed: list[str] = []
    slot_iter = iter(slots)

    def _snippet(image_id: str) -> str:
        placed.append(image_id)
        text = (ocr_texts.get(image_id) or "").strip()
        return f"[[IMG:{image_id}]]" + (f"\n{text}" if text else "")

    def _replace(match: re.Match) -> str:
        # 公式图片不占图片块槽位（公式正文由 Markdown/LaTeX 承载）
        if "formula" in match.group(0).lower():
            return ""
        try:
            image_id = next(slot_iter)
        except StopIteration:
            return ""
        return "" if image_id is None else _snippet(image_id)

    markdown = _HTML_IMG_RE.sub(_replace, markdown)
    markdown = _IMG_TAG_RE.sub(_replace, markdown)
    # 兜底：Markdown 图片语法（无对应图片块时直接移除，避免残留指向已删临时文件）
    markdown = _MD_IMG_RE.sub("", markdown)
    return markdown, placed


@dataclass
class Block:
    kind: str  # heading | paragraph | table
    text: str
    page: int
    level: int = 0
    image_ids: list[str] = field(default_factory=list)


@dataclass
class ParentBlock:
    text: str
    page: int
    pages: list[int]
    block_kinds: list[str]
    image_ids: list[str]
    heading_path: list[str]
    is_table: bool = False


def html_table_to_markdown(html: str) -> str:
    """把 PP-Structure/pymupdf4llm 的 HTML 表格转成 Markdown 表格。"""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    rows: list[list[str]] = []
    for tr in soup.find_all("tr"):
        cells = []
        for cell in tr.find_all(["td", "th"]):
            text = re.sub(r"\s+", " ", cell.get_text(" ", strip=True))
            cells.append(text.replace("|", "\\|"))
        if any(c.strip() for c in cells):
            rows.append(cells)
    if not rows:
        return ""
    width = max(len(row) for row in rows)
    rows = [row + [""] * (width - len(row)) for row in rows]
    out = [
        "| " + " | ".join(rows[0]) + " |",
        "|" + "|".join(["---"] * width) + "|",
    ]
    out.extend("| " + " | ".join(row) + " |" for row in rows[1:])
    return "\n".join(out)


def _split_text_segment(segment: str, page: int) -> list[Block]:
    # PP-Structure 的表格外常包一层 <div>，剥离后文本段才干净
    segment = re.sub(r"</?div[^>]*>", "", segment, flags=re.I)
    lines = segment.splitlines()
    blocks: list[Block] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if (
            _MD_TABLE_ROW_RE.match(line)
            and i + 1 < len(lines)
            and _MD_TABLE_SEP_RE.match(lines[i + 1])
            and "-" in lines[i + 1]
        ):
            table = [line, lines[i + 1]]
            i += 2
            while i < len(lines) and _MD_TABLE_ROW_RE.match(lines[i]):
                table.append(lines[i])
                i += 1
            blocks.append(Block("table", "\n".join(table), page))
            continue

        heading = _HEADING_RE.match(line)
        if heading:
            blocks.append(
                Block(
                    "heading",
                    heading.group(2).strip(),
                    page,
                    level=len(heading.group(1)),
                )
            )
            i += 1
            continue

        if not line.strip():
            i += 1
            continue

        para = [line]
        i += 1
        while i < len(lines) and lines[i].strip():
            if _HEADING_RE.match(lines[i]):
                break
            if (
                _MD_TABLE_ROW_RE.match(lines[i])
                and i + 1 < len(lines)
                and _MD_TABLE_SEP_RE.match(lines[i + 1])
                and "-" in lines[i + 1]
            ):
                break
            para.append(lines[i])
            i += 1
        text = "\n".join(para).strip()
        if not text:
            continue
        # 题目边界：同一段落里的多个【例N】拆成独立块
        parts = re.split(
            r"(?m)^(?=(?:【(?:例|检测|练习)[^】]*】|例\s*\d+))", text
        )
        for part in parts:
            part = part.strip()
            if part:
                blocks.append(
                    Block(
                        "paragraph",
                        part,
                        page,
                        image_ids=_IMG_MARK_RE.findall(part),
                    )
                )
    return blocks


def parse_page_blocks(text: str, page: int) -> list[Block]:
    """解析单页 Markdown：HTML 表格 → Markdown 原子块，其余按标题/段落切。"""
    blocks: list[Block] = []
    pos = 0
    for match in _HTML_TABLE_RE.finditer(text):
        blocks.extend(_split_text_segment(text[pos : match.start()], page))
        md = html_table_to_markdown(match.group(0))
        if md:
            blocks.append(Block("table", md, page))
        pos = match.end()
    blocks.extend(_split_text_segment(text[pos:], page))
    return [block for block in blocks if block.text.strip()]


def _is_question_start(text: str) -> bool:
    return bool(_QUESTION_RE.match(text.strip()))


def _continues(prev_text: str, next_text: str) -> bool:
    prev = prev_text.rstrip()
    nxt = next_text.lstrip()
    if not prev or not nxt:
        return False
    if prev[-1] in _TERMINAL:
        return False
    if re.match(
        r"^(#{1,6})\s|^【(?:例|检测|练习)|^例\s*\d+|^\d+[.、]|^[A-D][.、)]",
        nxt,
    ):
        return False
    return len(prev) >= 80 or prev[-1] in _CONT_CUE


def build_parents(
    blocks: list[Block],
    *,
    target: int = 600,
    max_size: int = 900,
    layout: str = "book",
) -> list[ParentBlock]:
    """把块组装成跨页 parent；表格强制独立 parent。"""
    parents: list[ParentBlock] = []
    cur: list[Block] = []
    heading_path: list[str] = []
    last_page: int | None = None

    def flush() -> None:
        nonlocal cur
        if not cur:
            return
        page = min(block.page for block in cur)
        pages = sorted({block.page for block in cur})
        text = "\n\n".join(block.text for block in cur).strip()
        image_ids: list[str] = []
        for block in cur:
            for image_id in block.image_ids:
                if image_id not in image_ids:
                    image_ids.append(image_id)
        parents.append(
            ParentBlock(
                text=text,
                page=page,
                pages=pages,
                block_kinds=[block.kind for block in cur],
                image_ids=image_ids,
                heading_path=list(heading_path),
            )
        )
        cur = []

    for block in blocks:
        if block.kind == "heading":
            if any(item.kind != "heading" for item in cur):
                flush()
            level = max(1, min(6, block.level or 1))
            heading_path = heading_path[: level - 1] + [block.text]
            cur.append(block)
            last_page = block.page
            continue

        if block.kind == "table":
            prefix = [item.text for item in cur if item.kind == "heading"]
            if any(item.kind != "heading" for item in cur):
                flush()
                prefix = []
            text = ("\n".join(prefix) + "\n\n" if prefix else "") + block.text
            parents.append(
                ParentBlock(
                    text=text.strip(),
                    page=block.page,
                    pages=[block.page],
                    block_kinds=["table"],
                    image_ids=block.image_ids,
                    heading_path=list(heading_path),
                    is_table=True,
                )
            )
            cur = []
            last_page = block.page
            continue

        if (
            layout == "slide"
            and cur
            and last_page is not None
            and block.page != last_page
            and not _continues(cur[-1].text, block.text)
        ):
            flush()

        if _is_question_start(block.text) and any(
            item.kind != "heading" for item in cur
        ):
            flush()

        current_len = sum(len(item.text) for item in cur)
        # target 是软阈值：攒够目标长度就先收口；max_size 是硬上限
        if cur and (
            current_len >= target or current_len + len(block.text) > max_size
        ):
            flush()
        cur.append(block)
        last_page = block.page

    flush()
    return parents


def split_table_children(text: str, child_size: int = 320) -> list[str]:
    """表格 parent 的子块：按行分组，每个子块重复表头。"""
    lines = [line for line in text.splitlines() if line.strip()]
    start = next(
        (i for i, line in enumerate(lines) if _MD_TABLE_ROW_RE.match(line)), None
    )
    if start is None or start + 1 >= len(lines):
        return []
    prefix = "\n".join(lines[:start]).strip()
    header = [lines[start], lines[start + 1]]
    rows = [line for line in lines[start + 2 :] if _MD_TABLE_ROW_RE.match(line)]
    base = ([prefix] if prefix else []) + header
    base_len = sum(len(line) for line in base)
    chunks: list[str] = []
    current: list[str] = []
    current_len = base_len
    for row in rows:
        if current and current_len + len(row) > child_size:
            chunks.append("\n".join(base + current))
            current = []
            current_len = base_len
        current.append(row)
        current_len += len(row)
    if current or not chunks:
        chunks.append("\n".join(base + current))
    return chunks
