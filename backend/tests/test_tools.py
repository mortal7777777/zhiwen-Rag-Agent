"""Agent 工具纯函数单测：联网结果可信度标注。"""

from __future__ import annotations

from app.agent.tools import _credibility_for


def test_credibility_high():
    assert _credibility_for("https://www.reuters.com/world/")[0] == "high"
    assert _credibility_for("https://www.gov.cn/zhengce/")[0] == "high"
    assert _credibility_for("https://news.bbc.com/")[0] == "high"


def test_credibility_medium():
    assert _credibility_for("https://www.zhihu.com/question/1")[0] == "medium"
    assert _credibility_for("https://example.com/unknown")[0] == "medium"


def test_credibility_low():
    assert _credibility_for("https://www.reddit.com/r/tech/")[0] == "low"
    assert _credibility_for("https://t.co/abc")[0] == "low"
    assert _credibility_for("")[0] == "low"
