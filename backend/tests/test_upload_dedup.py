"""上传查重单测：文件名归一化 / MinHash 近重复 / DedupChecker 四档检测 / 版本号工具。

纯 CPU，不依赖 GPU / MySQL / 网络。
"""

from types import SimpleNamespace

import pytest

from app.rag import dedup as dedup_mod
from app.rag.dedup import (
    DedupChecker,
    bytes_sha256,
    jaccard_estimate,
    minhash_sketch,
    normalize_name,
    normalize_text,
    text_shingles,
)
from app.rag.versioning import (
    archive_path,
    suggest_next_version,
    validate_version_no,
    version_file_name,
    versions_dir,
)

BASE = "".join(
    f"第{i}章，山中无甲子，寒尽不知年。春眠不觉晓，处处闻啼鸟。"
    for i in range(60)
)
NEAR = BASE.replace("春眠", "秋眠").replace("处处", "时时")
OTHER = "完全不同的一段文字，讲的是另一件事，与上面的内容毫无关系。" * 60


# ---------------- 文件名归一化 ----------------


def test_normalize_name_basic():
    assert normalize_name("《示例书名 3》.TXT") == "示例书名3"
    assert normalize_name("示例文集.txt") == "示例文集"
    assert normalize_name("示例论(1).txt") == normalize_name("示例论1.pdf")


def test_normalize_name_edge():
    assert normalize_name("") == ""
    assert normalize_name("  .txt") == ""
    assert normalize_name("示例文集【网络文档收集】.pdf") == "示例文集网络文档收集"


# ---------------- shingle / sketch / jaccard ----------------


def test_text_shingles_deterministic():
    t = normalize_text(BASE)
    assert text_shingles(t) == text_shingles(t)
    assert text_shingles("短") == set()


def test_minhash_sketch_stable_and_distinct():
    a1 = minhash_sketch(normalize_text(BASE))
    a2 = minhash_sketch(normalize_text(BASE))
    b = minhash_sketch(normalize_text(OTHER))
    assert a1 == a2 and len(a1) > 0
    assert a1 != b


def test_jaccard_identical_and_disjoint():
    a = minhash_sketch(normalize_text(BASE))
    b = minhash_sketch(normalize_text(OTHER))
    assert jaccard_estimate(a, a) == 1.0
    assert jaccard_estimate(a, b) < 0.2
    assert jaccard_estimate((), a) == 0.0


def test_jaccard_near_copy_high():
    a = minhash_sketch(normalize_text(BASE))
    n = minhash_sketch(normalize_text(NEAR))
    assert jaccard_estimate(a, n) >= 0.7


# ---------------- DedupChecker ----------------


def _make_checker(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    return data, DedupChecker(data)


def test_check_same_name_and_identical(tmp_path):
    data, checker = _make_checker(tmp_path)
    (data / "a.txt").write_text(BASE, encoding="utf-8")
    conflicts = checker.check("a.txt", BASE.encode("utf-8"))
    kinds = [c["kind"] for c in conflicts]
    assert kinds == ["same_name", "identical"]
    assert conflicts[0]["existing_relative_path"] == "a.txt"


def test_check_identical_cross_name(tmp_path):
    data, checker = _make_checker(tmp_path)
    (data / "a.txt").write_text(BASE, encoding="utf-8")
    conflicts = checker.check("副本.txt", BASE.encode("utf-8"))
    assert [c["kind"] for c in conflicts] == ["identical"]


def test_check_similar_name(tmp_path):
    data, checker = _make_checker(tmp_path)
    (data / "示例论(1).txt").write_text("短内容", encoding="utf-8")
    conflicts = checker.check("示例论1.txt", "另一个短内容".encode("utf-8"))
    assert [c["kind"] for c in conflicts] == ["similar_name"]


def test_check_near_duplicate(tmp_path):
    data, checker = _make_checker(tmp_path)
    (data / "原作.txt").write_text(BASE, encoding="utf-8")
    conflicts = checker.check("新书.txt", NEAR.encode("utf-8"))
    assert [c["kind"] for c in conflicts] == ["near_duplicate"]
    assert conflicts[0]["similarity"] >= 0.7
    assert "疑似" in conflicts[0]["message"]


def test_check_short_text_skips_near_dup(tmp_path):
    data, checker = _make_checker(tmp_path)
    (data / "原作.txt").write_text(BASE, encoding="utf-8")
    assert checker.check("小文件.txt", "很短".encode("utf-8")) == []


def test_check_name_taken_exact_and_normalized(tmp_path):
    data, checker = _make_checker(tmp_path)
    (data / "示例论(1).txt").write_text("x", encoding="utf-8")
    assert checker.check_name_taken("示例论(1).txt") == "示例论(1).txt"
    assert checker.check_name_taken("示例论1.txt") == "示例论(1).txt"
    assert checker.check_name_taken("无关文件.txt") is None


def test_check_name_taken_exclude_self(tmp_path):
    data, checker = _make_checker(tmp_path)
    (data / "a.txt").write_text("x", encoding="utf-8")
    assert checker.check_name_taken("a.txt", exclude_rel="a.txt") is None


def test_cache_reuse_no_rehash(tmp_path, monkeypatch):
    data, checker = _make_checker(tmp_path)
    (data / "a.txt").write_text("一", encoding="utf-8")
    (data / "b.txt").write_text("二", encoding="utf-8")

    calls = {"n": 0}
    real = dedup_mod.file_sha256

    def counting(path, *a, **kw):
        calls["n"] += 1
        return real(path, *a, **kw)

    monkeypatch.setattr(dedup_mod, "file_sha256", counting)
    checker.check("新1.txt", b"short")
    assert calls["n"] == 2  # 两个现有文件各算一次
    checker.check("新2.txt", b"short2")
    assert calls["n"] == 2  # 命中缓存，不再读盘
    (data / "c.txt").write_text("三", encoding="utf-8")
    checker.check("新3.txt", b"short3")
    assert calls["n"] == 3  # 新文件补算


# ---------------- 版本号与路径工具 ----------------


@pytest.mark.parametrize("ok", ["v1", "V2", "初稿", "2026-09 修订"])
def test_validate_version_no_ok(ok):
    assert validate_version_no(ok) == ok.strip()


@pytest.mark.parametrize("bad", ["", "   ", "a/b", "a\\b", "a:b", ".v1", "v1.", "x" * 51])
def test_validate_version_no_bad(bad):
    with pytest.raises(ValueError):
        validate_version_no(bad)


def test_suggest_next_version():
    assert suggest_next_version(["v1", "v2", "初稿"]) == "v3"
    assert suggest_next_version([]) == "v1"
    assert suggest_next_version(["初稿"]) == "v1"


def test_version_file_name_and_path_guard(tmp_path):
    assert version_file_name("v1", "示例文档.txt") == "v1__示例文档.txt"
    settings = SimpleNamespace(data_dir=tmp_path / "data")
    p = archive_path(settings, "示例书.pdf", "v1__示例书名3.txt")
    assert p.parent == versions_dir(settings) / "示例书.pdf"
    long_doc = "很长" * 120 + ".txt"
    with pytest.raises(ValueError):
        archive_path(settings, long_doc, "v1__x.txt")


def test_bytes_sha256_stable():
    assert bytes_sha256(b"abc") == bytes_sha256(b"abc")
    assert bytes_sha256(b"abc") != bytes_sha256(b"abd")
