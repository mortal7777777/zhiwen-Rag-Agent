"""上传重复检测：全文件 sha256 / 归一化文件名 / 字符 k-gram MinHash 近重复。

纯 CPU、无 DB / 索引依赖：结果只作为"警告"供前端确认，不拦截上传。
性能：现有文件的哈希/签名按 (path, size, mtime_ns) 内存缓存，每次上传只算新文件；
首次调用需全量读一遍 data/（本库 32 文件约 1-2 秒），可用 DedupChecker.warmup()
在启动期后台预热。
"""

from __future__ import annotations

import hashlib
import heapq
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from .loader import TEXT_SUFFIXES, decode_text_bytes, list_data_files

# 近重复参数：5 字窗口对中文书足够稳健；bottom-k 签名 64 个哈希，
# Jaccard 估计误差 ~±12%，仅用于"警告"场景足够。
SHINGLE_K = 5
SKETCH_SIZE = 64
SHINGLE_CAP = 50_000
NEAR_DUP_THRESHOLD = 0.7
MIN_TEXT_CHARS = 1000
NEAR_DUP_SUFFIXES = TEXT_SUFFIXES | {".csv"}

_NON_WORD_RE = re.compile(r"[\W_]+", re.UNICODE)
_WS_RE = re.compile(r"\s+")


def normalize_name(name: str) -> str:
    """文件名归一化：去扩展名 → NFKC（全角转半角）→ lower → 去掉全部空白与标点。

    例："《示例书名 3》.TXT" → "示例书名3"；归一化后为空则返回空串（调用方跳过）。
    """
    s = str(name or "").strip()
    if not s:
        return ""
    s = Path(s).stem
    if s.startswith("."):  # 隐藏文件（.gitignore 之类）不参与名称比对
        return ""
    s = unicodedata.normalize("NFKC", s).lower()
    return _NON_WORD_RE.sub("", s)


def bytes_sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def file_sha256(path: Path, chunk_size: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def normalize_text(text: str) -> str:
    """近重复专用文本归一化：去 BOM/空白折叠 → NFKC → lower（再移除全部空白）。"""
    s = str(text or "").replace("﻿", "")
    s = unicodedata.normalize("NFKC", s).lower()
    return _WS_RE.sub("", s)


def text_shingles(text: str, k: int = SHINGLE_K, cap: int = SHINGLE_CAP) -> set[str]:
    """字符 k-gram 集合 + 确定性 stride 抽样（不随机、同文两次结果一致）。"""
    n = len(text) - k + 1
    if n <= 0:
        return set()
    stride = max(1, n // cap)
    return {text[i : i + k] for i in range(0, n, stride)}


def minhash_sketch(text: str, size: int = SKETCH_SIZE, k: int = SHINGLE_K) -> tuple[int, ...]:
    """bottom-k MinHash：每个 shingle 一次 blake2b(8 字节) 取最小 size 个哈希值。"""
    shingles = text_shingles(text, k=k)
    if not shingles:
        return ()
    hashes = (
        int.from_bytes(hashlib.blake2b(s.encode("utf-8"), digest_size=8).digest(), "big")
        for s in shingles
    )
    return tuple(heapq.nsmallest(size, hashes))


def jaccard_estimate(a: tuple[int, ...], b: tuple[int, ...]) -> float:
    """bottom-k Jaccard 估计；两集合各自 ≥k 个 shingle 时无偏。"""
    if not a or not b:
        return 0.0
    union = set(a) | set(b)
    sample = heapq.nsmallest(min(len(a), len(b)), union)
    if not sample:
        return 0.0
    sa, sb = set(a), set(b)
    both = sum(1 for x in sample if x in sa and x in sb)
    return both / len(sample)


@dataclass
class _Entry:
    size: int
    mtime_ns: int
    sha256: str
    sketch: tuple[int, ...] | None = None


class DedupChecker:
    """data/ 现有文件的哈希/签名缓存 + 新上传文件/新文件名的冲突检测。"""

    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self._cache: dict[str, _Entry] = {}

    # ---------- 缓存 ----------

    def _entry(self, path: Path) -> _Entry | None:
        try:
            st = path.stat()
        except OSError:
            return None
        key = str(path.resolve())
        entry = self._cache.get(key)
        if entry is not None and entry.size == st.st_size and entry.mtime_ns == st.st_mtime_ns:
            return entry
        entry = _Entry(size=st.st_size, mtime_ns=st.st_mtime_ns, sha256=file_sha256(path))
        self._cache[key] = entry
        return entry

    def _relative(self, path: Path) -> str:
        return path.relative_to(self.data_dir).as_posix()

    def _existing_files(self) -> list[Path]:
        files = list_data_files(self.data_dir)
        alive = {str(p.resolve()) for p in files}
        for gone in [k for k in self._cache if k not in alive]:
            self._cache.pop(gone, None)
        return files

    def _sketch_for(self, path: Path) -> tuple[int, ...] | None:
        if path.suffix.lower() not in NEAR_DUP_SUFFIXES:
            return None
        entry = self._entry(path)
        if entry is None:
            return None
        if entry.sketch is None:
            text = normalize_text(decode_text_bytes(path.read_bytes()))
            if len(text) < MIN_TEXT_CHARS:
                entry.sketch = ()
            else:
                entry.sketch = minhash_sketch(text)
        return entry.sketch or None

    def warmup(self) -> None:
        """预热：一次性补算全部现有文件的 sha256 与文本签名。"""
        for path in list_data_files(self.data_dir):
            self._entry(path)
            self._sketch_for(path)

    # ---------- 检测 ----------

    def check(self, file_name: str, raw: bytes) -> list[dict]:
        """返回冲突列表（same_name → identical → similar_name → near_duplicate）。

        identical 命中后不再做名称相似/近重复（已无信息增量）。
        """
        conflicts: list[dict] = []
        files = self._existing_files()

        # 1) 同名（Windows 大小写不敏感）
        for path in files:
            if path.name == file_name or path.name.lower() == file_name.lower():
                conflicts.append(
                    {
                        "kind": "same_name",
                        "existing_name": path.name,
                        "existing_relative_path": self._relative(path),
                        "similarity": None,
                        "message": f"已存在同名文件《{path.name}》，确认后旧文件将归档为历史版本",
                    }
                )
                break

        # 2) 全文件完全相同（sha256）
        my_sha = bytes_sha256(raw)
        for path in files:
            entry = self._entry(path)
            if entry is not None and entry.sha256 == my_sha:
                conflicts.append(
                    {
                        "kind": "identical",
                        "existing_name": path.name,
                        "existing_relative_path": self._relative(path),
                        "similarity": None,
                        "message": f"内容与现有文件《{path.name}》完全相同（sha256 一致）",
                    }
                )
                return conflicts

        # 3) 文件名归一化相似
        norm = normalize_name(file_name)
        if norm:
            for path in files:
                if normalize_name(path.name) == norm and path.name != file_name:
                    conflicts.append(
                        {
                            "kind": "similar_name",
                            "existing_name": path.name,
                            "existing_relative_path": self._relative(path),
                            "similarity": None,
                            "message": f"文件名与现有文件《{path.name}》高度相似",
                        }
                    )
                    break

        # 4) 近重复（仅纯文本类；best-effort，取相似度最高者报一条）
        suffix = Path(file_name).suffix.lower()
        if suffix in NEAR_DUP_SUFFIXES:
            text = normalize_text(decode_text_bytes(raw))
            if len(text) >= MIN_TEXT_CHARS:
                my_sketch = minhash_sketch(text)
                best: tuple[float, str] | None = None
                for path in files:
                    sketch = self._sketch_for(path)
                    if not sketch:
                        continue
                    sim = jaccard_estimate(my_sketch, sketch)
                    if best is None or sim > best[0]:
                        best = (sim, path.name)
                if best is not None and best[0] >= NEAR_DUP_THRESHOLD:
                    conflicts.append(
                        {
                            "kind": "near_duplicate",
                            "existing_name": best[1],
                            "existing_relative_path": self._relative(
                                next(p for p in files if p.name == best[1])
                            ),
                            "similarity": round(best[0], 4),
                            "message": f"疑似与《{best[1]}》内容重复（相似度 {best[0]:.0%}）",
                        }
                    )
        return conflicts

    def check_name_taken(self, new_name: str, exclude_rel: str | None = None) -> str | None:
        """改名用：目标名与现有文件完全相同或归一化相同 → 返回冲突的相对路径。

        exclude_rel 排除"自己"（Windows 上大小写改名时目标会被判成同名）。
        """
        norm = normalize_name(new_name)
        for path in self._existing_files():
            rel = self._relative(path)
            if exclude_rel is not None and rel == exclude_rel:
                continue
            if path.name == new_name or path.name.lower() == new_name.lower():
                return rel
            if norm and normalize_name(path.name) == norm:
                return rel
        return None
