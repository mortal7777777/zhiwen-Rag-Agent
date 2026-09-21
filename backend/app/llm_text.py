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
