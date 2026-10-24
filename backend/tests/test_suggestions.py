"""欢迎页建议生成单测：新闻多级降级 / 最近任务 / 缓存 TTL / 兜底轮换。

背景（2026-10-14）：新闻条依赖 DDGS 走系统代理，Clash 未启动时永远走
"单一固定兜底文案"，表现为建议卡永远不变；本文件锁住新的降级与轮换行为。
全部用例无网络、无 DB、无模型调用。
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from types import SimpleNamespace

from app.api import suggestions as sug


class _FakeChat:
    def __init__(self, out=None, raises=None):
        self.out = out
        self.raises = raises
        self.prompts: list[str] = []

    def invoke(self, messages, config=None):
        self.prompts.append(messages[0].content)
        if self.raises:
            raise self.raises
        return SimpleNamespace(content=self.out)


class _FakeAgent:
    def __init__(self, out=None, raises=None):
        self.title_chat = _FakeChat(out=out, raises=raises)


def _raising(message: str):
    def fn():
        raise RuntimeError(message)

    return fn


def test_clean_question_strips_format_and_rejects_long():
    assert sug._clean_question('  "今天有什么新鲜事？"  ') == "今天有什么新鲜事？"
    assert sug._clean_question("## **今天的新闻**") == "今天的新闻"
    assert sug._clean_question("字" * 31) == ""
    assert sug._clean_question("今天\n有什么新闻？") == "今天 有什么新闻？"


# ---------------- 新闻：多级降级 ----------------

def test_news_prefers_ddgs_and_skips_baidu(monkeypatch):
    monkeypatch.setattr("app.network.proxy_reachable", lambda: True)
    called: list[str] = []
    monkeypatch.setattr(sug, "_ddgs_titles", lambda: called.append("ddgs") or ["标题A"])
    monkeypatch.setattr(sug, "_baidu_titles", lambda: called.append("baidu") or ["热榜B"])
    agent = _FakeAgent(out="今天有什么新鲜事？")

    assert sug._news_question(agent) == "今天有什么新鲜事？"
    assert called == ["ddgs"]
    assert "标题A" in agent.title_chat.prompts[0]


def test_news_falls_back_to_baidu_when_ddgs_fails(monkeypatch):
    monkeypatch.setattr("app.network.proxy_reachable", lambda: True)
    monkeypatch.setattr(sug, "_ddgs_titles", _raising("proxy refused"))
    monkeypatch.setattr(sug, "_baidu_titles", lambda: ["热搜一", "热搜二"])
    agent = _FakeAgent(out="今天的百度热搜讲了什么？")

    assert sug._news_question(agent) == "今天的百度热搜讲了什么？"
    assert "热搜一" in agent.title_chat.prompts[0]


def test_news_skips_ddgs_when_proxy_dead(monkeypatch):
    """系统代理不可达：不碰 DDGS（省十几秒白等），直接用百度热搜。"""
    monkeypatch.setattr("app.network.proxy_reachable", lambda: False)
    monkeypatch.setattr(sug, "_ddgs_titles", _raising("不应被调用"))
    monkeypatch.setattr(sug, "_baidu_titles", lambda: ["热搜三"])
    agent = _FakeAgent(out="热搜里说了什么？")

    assert sug._news_question(agent) == "热搜里说了什么？"
    assert "热搜三" in agent.title_chat.prompts[0]


def test_news_returns_none_when_all_sources_fail(monkeypatch):
    monkeypatch.setattr("app.network.proxy_reachable", lambda: True)
    monkeypatch.setattr(sug, "_ddgs_titles", _raising("x"))
    monkeypatch.setattr(sug, "_baidu_titles", _raising("y"))
    assert sug._news_question(_FakeAgent(out="不会用到")) is None


def test_news_returns_none_when_model_fails(monkeypatch):
    monkeypatch.setattr("app.network.proxy_reachable", lambda: True)
    monkeypatch.setattr(sug, "_ddgs_titles", lambda: ["标题"])
    assert sug._news_question(_FakeAgent(raises=RuntimeError("api down"))) is None


# ---------------- 最近任务 ----------------

def test_recent_question_uses_titles_and_last_user_message(monkeypatch):
    convs = [
        {"id": 9, "title": "SVG画猫执行计划待确认", "message_count": 5},
        {"id": 8, "title": "新对话", "message_count": 2},
        {"id": 7, "title": "空会话", "message_count": 0},
    ]
    monkeypatch.setattr(sug.repo, "list_conversations", lambda db, limit=15: convs)
    monkeypatch.setattr(
        sug.repo,
        "list_messages",
        lambda db, cid, limit=20: [
            {"role": "assistant", "content": "好的"},
            {"role": "user", "content": "帮我画只猫\n要SVG格式"},
        ],
    )
    agent = _FakeAgent(out="继续把 SVG 画猫页面完成？")

    assert sug._recent_question(None, agent) == "继续把 SVG 画猫页面完成？"
    prompt = agent.title_chat.prompts[0]
    assert "SVG画猫执行计划待确认" in prompt
    assert "新对话" not in prompt
    # 换行归一为空格，作为最近提问进入提示词
    assert "帮我画只猫 要SVG格式" in prompt


def test_recent_question_none_without_history(monkeypatch):
    monkeypatch.setattr(sug.repo, "list_conversations", lambda db, limit=15: [])
    assert sug._recent_question(None, _FakeAgent(out="x")) is None

    # 全部是默认标题（新对话）时同样没有可用信号
    monkeypatch.setattr(
        sug.repo,
        "list_conversations",
        lambda db, limit=15: [{"id": 1, "title": "新对话", "message_count": 3}],
    )
    assert sug._recent_question(None, _FakeAgent(out="x")) is None


# ---------------- 组装与兜底轮换 ----------------

def test_build_items_falls_back_to_rotation_pools(monkeypatch):
    monkeypatch.setattr(sug, "_kb_question", lambda service, day: "KB 问题")
    monkeypatch.setattr(sug, "_news_question", lambda agent: None)
    monkeypatch.setattr(sug, "_recent_question", lambda db, agent: None)

    items = sug._build_items(None, None, None)

    assert [i["type"] for i in items] == ["kb", "news", "recent"]
    assert items[0]["text"] == "KB 问题"
    assert items[1]["text"] in sug.NEWS_FALLBACK_POOL
    assert items[2]["text"] in sug.GENERAL_POOL


def test_rotation_pools_change_daily():
    day = date(2026, 10, 14).toordinal()
    assert len(sug.NEWS_FALLBACK_POOL) > 1
    assert sug._pick(sug.NEWS_FALLBACK_POOL, day) != sug._pick(sug.NEWS_FALLBACK_POOL, day + 1)
    assert sug._pick(sug.GENERAL_POOL, day) != sug._pick(sug.GENERAL_POOL, day + 1)


# ---------------- 知识库问题 ----------------

def test_kb_question_strips_prefix_and_filters_small_docs():
    class _Svc:
        def list_documents(self):
            return [
                {"name": "欢迎使用.pdf", "size": 9000},
                {"name": "小文件.md", "size": 100},
                {"name": "3_深入理解RAG.pdf", "size": 5000},
            ]

    question = sug._kb_question(_Svc(), 0)

    assert "《深入理解RAG》" in question
    assert "欢迎" not in question


# ---------------- 缓存 TTL ----------------

def _payload(items: list[dict], generated_at: datetime) -> str:
    return json.dumps(
        {"generated_at": generated_at.isoformat(), "items": items}, ensure_ascii=False
    )


def test_cache_served_within_ttl(monkeypatch):
    items = [{"type": "kb", "text": "缓存问题"}]
    monkeypatch.setattr(
        sug.repo, "get_meta", lambda db, key: _payload(items, datetime.now())
    )
    assert sug._load_cache(None) == items


def test_cache_expired_after_ttl(monkeypatch):
    old = datetime.now() - sug.CACHE_TTL - timedelta(minutes=1)
    monkeypatch.setattr(
        sug.repo,
        "get_meta",
        lambda db, key: _payload([{"type": "kb", "text": "x"}], old),
    )
    assert sug._load_cache(None) is None


def test_cache_corrupt_or_missing_returns_none(monkeypatch):
    monkeypatch.setattr(sug.repo, "get_meta", lambda db, key: "{bad json")
    assert sug._load_cache(None) is None

    monkeypatch.setattr(sug.repo, "get_meta", lambda db, key: None)
    assert sug._load_cache(None) is None


def test_save_cache_stores_timestamp_and_items(monkeypatch):
    captured: dict = {}
    monkeypatch.setattr(
        sug.repo, "set_meta", lambda db, key, value: captured.update(key=key, value=value)
    )
    items = [{"type": "recent", "text": "继续"}]

    sug._save_cache(None, items)

    assert captured["key"] == sug.CACHE_KEY
    payload = json.loads(captured["value"])
    assert payload["items"] == items
    assert datetime.now() - datetime.fromisoformat(payload["generated_at"]) < timedelta(minutes=1)
