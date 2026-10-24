"""tools 节点 per-run 缓存单测：缓存键、缓存分类、缓存写入/命中/失效。"""

from __future__ import annotations

from app.agent.utils import _CACHEABLE_TOOLS, _tool_cache_key

# 副作用类工具：绝不进缓存集合
_NON_CACHEABLE = {
    "write_file",
    "edit_file",
    "delete_file",
    "bash",
    "command_tool",
    "add_document",
    "todo_update",
}


def test_cacheable_tools_classification():
    # 纯查询类应全部在缓存集合内
    for name in ("knowledge_base_search", "web_search", "list_dir", "read_file", "grep_search", "skill_view"):
        assert name in _CACHEABLE_TOOLS, f"{name} 应可缓存"
    # 副作用类绝不在缓存集合内
    for name in _NON_CACHEABLE:
        assert name not in _CACHEABLE_TOOLS, f"{name} 不应缓存（有副作用）"


def test_cache_key_deterministic_and_order_independent():
    k1 = _tool_cache_key("knowledge_base_search", {"query": "春桥文录", "top_k": 3})
    k2 = _tool_cache_key("knowledge_base_search", {"top_k": 3, "query": "春桥文录"})
    assert k1 == k2  # 参数键序无关
    assert k1.startswith("knowledge_base_search|")
    # 不同参数 → 不同键
    assert k1 != _tool_cache_key("knowledge_base_search", {"query": "文艺座谈会"})
    # 不同工具同名参数 → 不同键
    assert k1 != _tool_cache_key("web_search", {"query": "春桥文录", "top_k": 3})


def test_slim_tool_result_can_preserve_skill_content():
    """skill_view 的全文不能被 2500 字通用瘦身截断（否则后续轮次拿不到步骤）。"""
    from app.agent.utils import _slim_tool_result

    content = "步骤" * 5000
    kept = _slim_tool_result({"content": content}, content_limit=None)
    assert kept["content"] == content
    slimmed = _slim_tool_result({"content": content})
    assert len(slimmed["content"]) < len(content)


def test_cache_key_none_args_and_unserializable():
    # None 参数与空 dict 等价
    assert _tool_cache_key("list_dir", None) == _tool_cache_key("list_dir", {})
    # 无法序列化的对象 → None（此时不缓存，直接执行）
    assert _tool_cache_key("read_file", {"path": object()}) is None
