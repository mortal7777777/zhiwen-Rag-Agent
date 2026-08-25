"""文本切分器：递归切分（child）+ 段落分组切分（parent）。

设计说明（基于中文书籍切分实验）：
- 纯递归切分只有约 8% 的块能落在句子边界，语义常被切断；
- 按段落分组切分约 47% 落在句子边界，语义完整度高很多；
- Parent-Child 方案：child 用递归小窗口（精度），parent 用段落分组（上下文）。
"""

from __future__ import annotations

import re

from langchain_text_splitters import RecursiveCharacterTextSplitter

# 按 段落 -> 换行 -> 句号 -> 问号 -> 分号 -> 逗号 -> 空格 的优先级切分
DEFAULT_SEPARATORS = ["\n\n", "\n", "。", "！", "？", "；", "，", " ", ""]


def build_text_splitter(
    chunk_size: int = 500,
    chunk_overlap: int = 80,
) -> RecursiveCharacterTextSplitter:
    """构建中文优化的递归切分器（用于 child 或兼容旧逻辑）。"""
    return RecursiveCharacterTextSplitter(
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
        separators=DEFAULT_SEPARATORS,
    )


class ParagraphGroupTextSplitter:
    """按段落分组切分：优先保持段落完整，语义不被拦腰切断。

    - 多个短段落合并到接近 target_chunk_size；
    - 单段超过 max_chunk_size 时，按句子边界兜底切分；
    - 块与块之间以 \n\n 连接，天然保留段落边界。
    """

    def __init__(
        self,
        target_chunk_size: int = 600,
        max_chunk_size: int = 900,
    ):
        self.target_chunk_size = target_chunk_size
        self.max_chunk_size = max_chunk_size
        self._fallback = build_text_splitter(
            chunk_size=max_chunk_size,
            chunk_overlap=0,
        )

    def split_text(self, text: str) -> list[str]:
        paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
        groups: list[str] = []
        current: list[str] = []
        current_len = 0

        for paragraph in paragraphs:
            if len(paragraph) > self.max_chunk_size:
                if current:
                    groups.append("\n\n".join(current))
                    current, current_len = [], 0
                groups.extend(self._split_long_paragraph(paragraph))
                continue

            if current and current_len + len(paragraph) > self.target_chunk_size:
                groups.append("\n\n".join(current))
                current, current_len = [], 0
            current.append(paragraph)
            current_len += len(paragraph)

        if current:
            groups.append("\n\n".join(current))
        return groups

    def _split_long_paragraph(self, paragraph: str) -> list[str]:
        """超长段落：优先在句子边界切，个别超长句子最后才硬切。"""
        chunks: list[str] = []
        current = ""
        for sentence in re.split(r"(?<=[。！？；])", paragraph):
            if not sentence:
                continue
            if len(current) + len(sentence) <= self.max_chunk_size:
                current += sentence
            else:
                if current:
                    chunks.append(current)
                if len(sentence) <= self.max_chunk_size:
                    current = sentence
                else:
                    chunks.extend(self._fallback.split_text(sentence))
                    current = ""
        if current:
            chunks.append(current)
        return chunks


def build_parent_child_splitters(
    parent_chunk_size: int = 600,
    parent_max_chunk_size: int = 900,
    child_chunk_size: int = 220,
    child_overlap: int = 40,
) -> tuple[ParagraphGroupTextSplitter, RecursiveCharacterTextSplitter]:
    """构建 parent（段落分组）与 child（递归小窗口）切分器。"""
    parent_splitter = ParagraphGroupTextSplitter(
        target_chunk_size=parent_chunk_size,
        max_chunk_size=parent_max_chunk_size,
    )
    child_splitter = build_text_splitter(
        chunk_size=child_chunk_size,
        chunk_overlap=child_overlap,
    )
    return parent_splitter, child_splitter
