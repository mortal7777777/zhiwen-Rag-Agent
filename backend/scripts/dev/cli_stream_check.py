"""CLI 增量流式回归：用本地 stub SSE 服务驱动 stream_question，检查渲染字节流。

验证三件事（对应 2026-09-28 的 CLI 改造）：
1. 正文随 stream_delta 边到边打印（不再等整轮 token）；
2. 工具轮的过渡文本被 stream_end{keep:false} 按行数撤回（ANSI 上移 + 清屏）；
3. stream_end{keep:true} 后那条全文 token 被跳过 —— 正文只出现一次。

用法：D:/conda_envs/pytorch_env/python.exe backend/scripts/dev/cli_stream_check.py
"""

from __future__ import annotations

import json
import os
import pathlib
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

import cli_agent as c  # noqa: E402

TRANSITIONAL = "先说明思路：\n我要先看看目录结构，再回答你的问题。"
ANSWER = "目录里有 5 个条目：\n- a.txt\n- b.txt\n"

EVENTS = [
    ("session", {"run_id": "r1", "conversation_id": 1}),
    ("status", {"phase": "thinking", "text": "正在思考并生成回答…"}),
    ("stream_delta", TRANSITIONAL[:14]),
    ("stream_delta", TRANSITIONAL[14:]),
    ("stream_end", {"keep": False}),
    ("tool_start", {"id": "t1", "name": "list_dir", "arguments": {"path": "."}}),
    ("tool_result", {"id": "t1", "name": "list_dir", "summary": "列出 5 个条目", "duration_ms": 120}),
    ("stream_delta", ANSWER[:10]),
    ("stream_delta", ANSWER[10:]),
    ("stream_end", {"keep": True}),
    ("token", ANSWER),
    ("done", {"ok": True, "stopped": False, "sources": [], "tool_trace": []}),
]


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # 静音
        pass

    def do_POST(self):  # noqa: N802
        if self.path != "/api/agent/stream":
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        for name, data in EVENTS:
            frame = json.dumps({"event": name, "data": data}, ensure_ascii=False)
            self.wfile.write(f"data: {frame}\n\n".encode())
            self.wfile.flush()
            time.sleep(0.05)
        self.close_connection = True

    def do_GET(self):  # noqa: N802
        # fetch_usage / fetch_context_stats：返回空，避免污染
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b"[]")


class _Reader:
    def drain(self):
        return []

    def unget(self, key):
        pass


def main() -> int:
    # 不碰真实会话状态与工具留档
    c.save_session = lambda *a, **k: None
    c.record_tool_output = lambda *a, **k: None

    live = os.environ.get("LIVE") == "1"
    if live:
        # 对真实后端跑（需要后端已启动 + 模型可用）：肉眼/断言看真实模型行为
        base = os.environ.get("BASE", "http://127.0.0.1:8000")
        question = "先用一句话说明你的思路，然后调用 list_dir 查看工作目录，最后告诉我目录里有多少个条目。"
    else:
        srv = ThreadingHTTPServer(("127.0.0.1", 8899), Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        base, question = "http://127.0.0.1:8899", "看看目录"

    c.stream_question(base, question, "none", 1, _Reader(), c.Screen())

    out = CAPTURED.getvalue()
    failures = 0

    def check(ok, label, detail=""):
        nonlocal failures
        print(
            ("PASS " if ok else "FAIL ") + label + (f" — {detail}" if detail else ""),
            file=sys.__stdout__,
        )
        if not ok:
            failures += 1

    if live:
        # 真实模型：文案不可预期，只做结构断言（stub 模式已确定性覆盖 token 跳过）
        erase_at, tool_at = out.find("\033[J"), out.find("⏺")
        check(0 <= erase_at < tool_at, "工具轮过渡文本在工具卡片前被撤回",
              f"erase@{erase_at} tool@{tool_at}")
        head = out[:erase_at] if erase_at > 0 else ""
        cut = max(head.rfind("\033[2K"), head.rfind("\033[J"))
        printed = head[cut + 4 :].strip() if cut >= 0 else head.strip()
        check(bool(printed), "撤回前确实打印过过渡文本", repr(printed[:40]))
        check("⏺" in out and "⎿" in out, "工具卡片正常渲染")
    else:
        check("我先看看目录结构" in out or "先说明思路" in out, "过渡文本被增量打印")
        check("\033[1A\r\033[J" in out, "过渡文本按行数撤回（上移 1 行清屏）")
        erase_at = out.find("\033[1A\r\033[J")
        tool_at = out.find("list_dir")
        check(0 < erase_at < tool_at, "撤回发生在 tool_start 之前",
              f"erase@{erase_at} tool@{tool_at}")
        check(out.count("目录里有 5 个条目") == 1, "正文只出现一次（全文 token 被跳过）",
              f"出现 {out.count('目录里有 5 个条目')} 次")
        # 正文经 Markdown 渲染，列表项会被渲染成项目符号，故按内容断言
        check("b.txt" in out and "•" in out, "正文完整打印（列表已渲染）")

    dump = (
        pathlib.Path(__file__).resolve().parents[3]
        / "scratch"
        / "check_outputs"
        / "cli_stream_check.out.txt"
    )
    dump.parent.mkdir(parents=True, exist_ok=True)
    dump.write_text(repr(out), encoding="utf-8")
    print(f"\n原始字节流已存：{dump}", file=sys.__stdout__)
    print(
        f"结果：{'全部通过' if failures == 0 else str(failures) + ' 项未通过'}",
        file=sys.__stdout__,
    )
    return 0 if failures == 0 else 1


class _Capture:
    def __init__(self):
        self._buf: list[str] = []
        self._real = sys.stdout

    def getvalue(self):
        return "".join(self._buf)

    def write(self, s):
        self._buf.append(s)

    def flush(self):
        pass

    def isatty(self):
        return False

    def __getattr__(self, item):
        return getattr(self._real, item)


CAPTURED = _Capture()
sys.stdout = CAPTURED
code = main()
CAPTURED.flush()
sys.stdout = CAPTURED._real
sys.exit(code)
