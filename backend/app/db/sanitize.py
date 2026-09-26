"""落库长度保护：MySQL TEXT 列上限 65535 字节，超限会让整条 INSERT 失败。

2026-09-24 实测：一轮含多次 read_file 的任务，`messages.tool_trace` 累积到
10.7 万字符，finalize 落库报 `(1406) Data too long for column 'tool_trace'`，
连带 agent_runs 记录与轨迹压缩在同一 Session 事务里一起回滚——整轮对话
与运行记录全部丢失，agent_runs 里甚至出现 ID 跳号。

这里只做**保真降级**：宁可少存几条轨迹，也不能整条消息/整轮记录落不了库。
截断只作用于落库副本，不参与消息链重建（缓存前缀不变量不受影响）。
"""

from __future__ import annotations

import json

# TEXT 列 65535 字节；多字节字符按 UTF-8 计，留出 JSON 膨胀余量
_DEFAULT_MAX_BYTES = 60000
_DEFAULT_MAX_ITEMS = 200
# 首元素被读回用于重建工具消息配对（agent/utils._rows_to_history），
# 单条自身超限时只保留这些字段
_FIRST_KEEP_KEYS = ("tool_call_id", "name", "step", "duration_ms", "summary")


def _size(value) -> int:
    return len(json.dumps(value, ensure_ascii=False).encode("utf-8"))


def _slim_item(item) -> object:
    """oversize 单条的降级形态：保留配对所需字段 + 截断 summary。"""
    if not isinstance(item, dict):
        return str(item)[:500]
    slim = {k: item[k] for k in _FIRST_KEEP_KEYS if k in item}
    summary = slim.get("summary")
    if isinstance(summary, str) and len(summary) > 500:
        slim["summary"] = summary[:500] + "…"
    return slim


def truncate_tool_trace(
    trace,
    max_bytes: int = _DEFAULT_MAX_BYTES,
    max_items: int = _DEFAULT_MAX_ITEMS,
):
    """把工具轨迹压到 TEXT 列容量内，返回截断后的新列表。

    - 首元素永不丢弃；单条自身超限时降级为白名单字段。
    - 其余条目按序保留，超预算即停止并追加 {"_truncated": true, "omitted": n}。
    """
    if not isinstance(trace, list) or not trace:
        return trace

    first = trace[0]
    head = [_slim_item(first)] if _size(first) > max_bytes else [first]
    budget = max_bytes - _size({"_truncated": True, "omitted": len(trace)})
    used = _size(head)
    for item in trace[1:]:
        if len(head) >= max_items:
            break
        cost = _size(item) + 1
        if used + cost > budget:
            break
        head.append(item)
        used += cost

    omitted = len(trace) - len(head)
    if omitted > 0:
        head.append({"_truncated": True, "omitted": omitted})
    return head


def sanitize_token_usage(usage, max_bytes: int = _DEFAULT_MAX_BYTES):
    """token_usage 兜底：超限时截断长列表字段（如 chain_hashes）、截短长字符串。"""
    if not isinstance(usage, dict) or _size(usage) <= max_bytes:
        return usage
    out: dict = {}
    for key, value in usage.items():
        if isinstance(value, list):
            out[key] = truncate_tool_trace(value, max_bytes=max_bytes // 3)
        elif isinstance(value, str) and len(value) > 2000:
            out[key] = value[:2000] + "…"
        else:
            out[key] = value
        if _size(out) > max_bytes:
            out["_truncated"] = True
            break
    return out


def sanitize_sources(sources, max_items: int = 20):
    """来源列表兜底：正文/摘要类字段截短，条数封顶。"""
    if not isinstance(sources, list):
        return sources
    out: list = []
    for item in sources[:max_items]:
        if not isinstance(item, dict):
            out.append(item)
            continue
        slim = dict(item)
        for key in ("content", "snippet", "text"):
            val = slim.get(key)
            if isinstance(val, str) and len(val) > 600:
                slim[key] = val[:600] + "…"
        out.append(slim)
    return out
