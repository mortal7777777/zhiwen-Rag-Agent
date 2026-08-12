"""文本切分策略实验：对比不同切分器与尺寸的语义完整性。

指标说明：
- chunks：切块数量
- avg_len：平均字符数
- end_complete%：块结尾落在句号/问号/感叹号/分号等完整边界上的比例（越高越好）
- para_boundary%：块内包含段落边界（\n\n）的比例（越低说明越少把段落劈开）
- empty%：空块比例

运行（在 backend 目录下）：
    python chunk_experiment.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from langchain_community.document_loaders import PyPDFLoader
from langchain_text_splitters import RecursiveCharacterTextSplitter

from app.config import get_settings

SENTENCE_END = re.compile(r"[。！？；…」』”\"]$")
PARAGRAPH_BREAK = "\n\n"
SEPARATORS = ["\n\n", "\n", "。", "！", "？", "；", "，", " ", ""]


def recursive_splitter(chunk_size: int, overlap: int):
    return RecursiveCharacterTextSplitter(
        chunk_size=chunk_size,
        chunk_overlap=overlap,
        separators=SEPARATORS,
    )


def split_long_paragraph(paragraph: str, max_size: int) -> list[str]:
    """超长段落兜底：优先按句子边界切，仍然超长再硬切。"""
    if len(paragraph) <= max_size:
        return [paragraph]
    chunks: list[str] = []
    current = ""
    for sentence in re.split(r"(?<=[。！？；])", paragraph):
        if not sentence:
            continue
        if len(current) + len(sentence) <= max_size:
            current += sentence
        else:
            if current:
                chunks.append(current)
            if len(sentence) <= max_size:
                current = sentence
            else:
                # 单个句子超长：用递归切分器硬切
                chunks.extend(
                    recursive_splitter(max_size, 0).split_text(sentence)
                )
                current = ""
    if current:
        chunks.append(current)
    return chunks


def paragraph_group_splitter(text: str, target_size: int, max_size: int) -> list[str]:
    """按段落分组：优先保持段落完整，一组不超过 target；单段超 max 时按句子兜底。"""
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    groups: list[str] = []
    current: list[str] = []
    current_len = 0
    for paragraph in paragraphs:
        if len(paragraph) > max_size:
            if current:
                groups.append("\n\n".join(current))
                current, current_len = [], 0
            groups.extend(split_long_paragraph(paragraph, max_size))
            continue
        if current and current_len + len(paragraph) > target_size:
            groups.append("\n\n".join(current))
            current, current_len = [], 0
        current.append(paragraph)
        current_len += len(paragraph)
    if current:
        groups.append("\n\n".join(current))
    return groups


def metrics(chunks: list[str]) -> dict:
    total = len(chunks) or 1
    ends = sum(1 for c in chunks if SENTENCE_END.search(c.strip()))
    breaks = sum(1 for c in chunks if PARAGRAPH_BREAK in c)
    empty = sum(1 for c in chunks if not c.strip())
    avg_len = sum(len(c) for c in chunks) / total
    return {
        "chunks": len(chunks),
        "avg_len": round(avg_len, 1),
        "end_complete%": round(ends / total * 100, 1),
        "para_boundary%": round(breaks / total * 100, 1),
        "empty%": round(empty / total * 100, 1),
    }


def main() -> None:
    settings = get_settings()
    pdfs = sorted(settings.data_dir.glob("*.pdf"))
    if not pdfs:
        print("data 目录下没有 PDF，无法实验")
        return
    print(f"加载 {pdfs[0].name} ...")
    pages = [p.page_content for p in PyPDFLoader(str(pdfs[0])).load() if p.page_content.strip()]
    print(f"共 {len(pages)} 页，抽样做实验（前 60 页 + 每 10 页取 1 页，到第 600 页）")
    sample = pages[:60] + [pages[i] for i in range(60, min(len(pages), 600), 10)]
    text = "\n\n".join(sample)
    print(f"样本字符数：{len(text)}\n")

    strategies = [
        ("Recursive 500/80（现状）", lambda: recursive_splitter(500, 80).split_text(text)),
        ("Recursive 800/120", lambda: recursive_splitter(800, 120).split_text(text)),
        ("Recursive 1200/200", lambda: recursive_splitter(1200, 200).split_text(text)),
        ("ParagraphGroup 500/750", lambda: paragraph_group_splitter(text, 500, 750)),
        ("ParagraphGroup 800/1200", lambda: paragraph_group_splitter(text, 800, 1200)),
        ("ParagraphGroup 1200/1800", lambda: paragraph_group_splitter(text, 1200, 1800)),
    ]
    header = f"{'策略':<26} {'chunks':>7} {'avg_len':>8} {'end%':>7} {'para%':>7} {'empty%':>7}"
    print(header)
    print("-" * len(header))
    for name, fn in strategies:
        chunks = fn()
        m = metrics(chunks)
        print(
            f"{name:<26} {m['chunks']:>7} {m['avg_len']:>8} "
            f"{m['end_complete%']:>7} {m['para_boundary%']:>7} {m['empty%']:>7}"
        )


if __name__ == "__main__":
    main()
