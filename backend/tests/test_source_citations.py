"""引用来源：去重 / 按 [n] 编号排序 / 不因旧上限丢掉被引用来源。"""

from __future__ import annotations

from app.agent.nodes.finalize import MAX_SOURCES, dedupe_sources


def test_sources_sorted_by_index_not_completion_order():
    srcs = [{"index": i, "type": "web"} for i in (6, 7, 8, 9, 10, 1, 2, 3)]
    out = dedupe_sources(srcs)
    assert [s["index"] for s in out] == [1, 2, 3, 6, 7, 8, 9, 10]


def test_sources_beyond_eight_are_kept():
    srcs = [{"index": i} for i in range(1, 16)]
    out = dedupe_sources(srcs)
    # 旧实现截到 8 条：答案引用 [11] 时角标点不到任何来源
    assert len(out) == 15
    assert out[-1]["index"] == 15


def test_sources_limit_and_unindexed_last():
    srcs = [{"index": i} for i in range(1, MAX_SOURCES + 5)]
    srcs.append({"content": "无编号来源"})
    out = dedupe_sources(srcs)
    assert len(out) == MAX_SOURCES
    assert out[0]["index"] == 1


def test_sources_dedupe_by_index_keeps_first():
    srcs = [
        {"index": 2, "content": "a"},
        {"index": 2, "content": "b"},
        {"index": 1, "content": "c"},
    ]
    out = dedupe_sources(srcs)
    assert [s["index"] for s in out] == [1, 2]
    assert out[1]["content"] == "a"


def test_cited_sources_beyond_limit_are_kept():
    # 实测 msg 7755：多轮检索累计到 [100]，旧实现只存 50 条导致角标点不到来源
    srcs = [{"index": i, "content": "x" * 40} for i in range(1, 301)]
    cited = {51, 52, 77, 100, 250}
    out = dedupe_sources(srcs, cited=cited)
    indexes = {s["index"] for s in out}
    assert cited <= indexes  # 被引用的一律保留
    assert len(out) == MAX_SOURCES  # 其余仍受上限约束
    assert [s["index"] for s in out] == sorted(indexes)


def test_cited_over_limit_keeps_all_cited():
    srcs = [{"index": i} for i in range(1, 9)]
    cited = {2, 3, 4, 5, 6}
    out = dedupe_sources(srcs, limit=3, cited=cited)
    assert {s["index"] for s in out} == cited


def test_sources_fit_text_column_budget():
    from app.agent.nodes.finalize import SOURCES_MAX_BYTES

    srcs = [{"index": i, "content": "长" * 900} for i in range(1, 121)]
    cited = {1, 120}
    out = dedupe_sources(srcs, cited=cited)
    assert {1, 120} <= {s["index"] for s in out}
    payload = __import__("json").dumps(out, ensure_ascii=False).encode("utf-8")
    assert len(payload) <= SOURCES_MAX_BYTES
