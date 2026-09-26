"""模型输出内容归一化。

OpenAI 兼容格式的 message.content 是纯字符串；Anthropic 兼容格式
（ChatAnthropic，如 DeepSeek /anthropic 端点）是块列表，形如
[{'type': 'thinking', ...}, {'type': 'text', 'text': '正文'}]。
管道统一用 message_text() 取正文，thinking/tool_use 等非正文块不计入。
"""

from __future__ import annotations


def message_text(content) -> str:
    """把模型输出 content 归一化为纯文本（块列表时只拼接 text 块）。"""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                block_type = item.get("type")
                if block_type == "text" or ("text" in item and block_type is None):
                    parts.append(str(item.get("text") or ""))
        return "".join(parts)
    return str(content)


def extract_thinking_blocks(content) -> list[dict]:
    """从 Anthropic 格式的 content 里抽出 thinking 块（原样保留 signature）。

    为什么需要：DeepSeek 的 `/anthropic` 端点把思考放在 content 块里，
    `additional_kwargs` 是空的（实测 2026-09-26）；而启用 thinking 时，
    带 tool_use 的 assistant 消息**必须把思考块原样回传**，否则 400：
    "The `content[].thinking` in the thinking mode must be passed back to the API"。
    本函数负责抽出来暂存，回传时由 `_AnthropicSystemNormalizer` 还原成块。
    """
    if not isinstance(content, list):
        return []
    out: list[dict] = []
    for item in content:
        if not isinstance(item, dict) or item.get("type") != "thinking":
            continue
        if not str(item.get("thinking") or "").strip():
            continue
        keep = {
            k: item[k] for k in ("type", "thinking", "signature") if k in item
        }
        out.append(keep)
    return out
