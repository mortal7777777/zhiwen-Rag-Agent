"""Anthropic 兼容格式（DeepSeek /anthropic 端点）冒烟测试。

验证走 build_chat_model 工厂的真实链路：
1. 简单问答可用、usage 字段结构（cache_read 供 tracing 映射）；
2. 工具轮往返：用 _rows_to_history 的方式重建 AIMessage(content="", tool_calls=...)
   后能否被接受（DeepSeek 是否强制回传 thinking 块）；
3. thinking disabled（标题/摘要类辅助调用）是否被接受；
4. thinking 开启 + temperature 是否冲突。

用法（backend 目录）：
    python smoke_anthropic_provider.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool


def load_provider_cfg() -> dict:
    """读取当前激活供应商凭据，base_url 换成 Anthropic 兼容端点。"""
    from run import load_local_env

    load_local_env()
    import app.db.database as db_mod

    db_mod.init_db()
    from app.config import get_settings
    from app.runtime_config import chat_provider_config, load_overrides

    if db_mod.SessionLocal is not None:
        s = db_mod.SessionLocal()
        try:
            load_overrides(s)
        finally:
            s.close()
    cfg = dict(chat_provider_config(get_settings()) or {})
    cfg["base_url"] = "https://api.deepseek.com/anthropic"
    cfg["model"] = "deepseek-v4-flash"
    return cfg


@tool
def get_number(name: str) -> str:
    """获取指定名称对应的数字。"""
    return "42"


def main() -> None:
    from app.llm_text import message_text
    from app.runtime_config import build_chat_model, thinking_extra_body, thinking_param

    cfg = load_provider_cfg()
    print(f"base_url={cfg.get('base_url')} model={cfg.get('model')}")

    llm = build_chat_model(
        cfg,
        temperature=0.5,
        timeout=120,
        thinking=thinking_param(cfg),
        extra_body=thinking_extra_body(cfg),
    )

    print("\n=== 1) 简单问答 ===")
    r = llm.invoke("用一句话回答：1+1 等于几？")
    text = message_text(r.content)
    print("content 类型:", type(r.content).__name__)
    print("归一化文本:", text[:160])
    assert text.strip(), "message_text 归一化后为空"
    if isinstance(r.content, list):
        # thinking 块不得泄漏进正文
        for block in r.content:
            if isinstance(block, dict) and block.get("type") == "thinking":
                fragment = str(block.get("thinking") or "")[:20]
                assert not fragment or fragment not in text, "thinking 块泄漏进正文"
    print("usage_metadata:", json.dumps(r.usage_metadata or {}, ensure_ascii=False))
    print("additional_kwargs keys:", list((r.additional_kwargs or {}).keys()))

    print("\n=== 2) 工具轮往返（重建消息后二轮调用） ===")
    llm_t = llm.bind_tools([get_number])
    m1 = llm_t.invoke("请调用工具获取 magic 对应的数字，然后告诉我。")
    print("第 1 轮 tool_calls:", [tc["name"] for tc in (m1.tool_calls or [])])
    print("第 1 轮 content 类型:", type(m1.content).__name__)
    if not m1.tool_calls:
        print("!! 模型未发起工具调用，跳过往返测试")
        return
    tc = m1.tool_calls[0]
    rebuilt = AIMessage(
        content="",
        tool_calls=m1.tool_calls,
        additional_kwargs={"reasoning_content": ""},
    )
    msgs = [
        HumanMessage(content="请调用工具获取 magic 对应的数字，然后告诉我。"),
        rebuilt,
        ToolMessage(content="42", tool_call_id=tc["id"], name=tc["name"]),
    ]
    m2 = llm_t.invoke(msgs)
    print("第 2 轮 ok | tool_calls:", [t["name"] for t in (m2.tool_calls or [])])
    print("第 2 轮文本:", message_text(m2.content)[:120])

    print("\n=== 3) thinking disabled（辅助调用） ===")
    helper = build_chat_model(
        cfg,
        temperature=0.0,
        timeout=60,
        thinking={"type": "disabled"},
        extra_body={"thinking": {"type": "disabled"}},
    )
    h = helper.invoke("只回复两个字的标题：缓存")
    print("ok |", str(h.content)[:80])

    print("\n=== 4) [1m] 后缀（长窗口） ===")
    cfg_1m = dict(cfg, model="deepseek-v4-flash[1m]")
    llm_1m = build_chat_model(cfg_1m, temperature=0.0, timeout=60, thinking={"type": "disabled"})
    r1m = llm_1m.invoke("回复 ok")
    print("ok |", str(r1m.content)[:40])

    print("\n全部通过。")


if __name__ == "__main__":
    main()
