"""CLI 分层契约（P1-1 物理拆分，2026-10-08）。

拆分后的依赖必须单向（只允许向下），否则"拆出去"会退化成"互相调用"：

    cli_agent.py（REPL/主循环）
      ├─ cli.http   -> term, format
      ├─ cli.input  -> term
      ├─ cli.format -> term
      └─ cli.term   -> 只依赖标准库

同时用"行数棘轮"防止 cli_agent.py 重新长回巨型文件。
"""

from __future__ import annotations

import ast
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
CLI_DIR = BACKEND / "cli"

# 允许的跨层依赖（key 只能导入 value 里的层）
ALLOWED_LAYER_IMPORTS = {
    "term": set(),
    "format": {"term"},
    "input": {"term"},
    "http": {"term", "format"},
}


def _imported_cli_layers(path: Path) -> set[str]:
    """该文件从 cli.* 导入了哪些兄弟层。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    layers: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("cli."):
            layers.add(node.module.split(".", 1)[1])
    return layers


def test_cli_layers_only_depend_downwards():
    for layer, allowed in ALLOWED_LAYER_IMPORTS.items():
        path = CLI_DIR / f"{layer}.py"
        assert path.exists(), f"缺少分层模块 {path.name}"
        imported = _imported_cli_layers(path)
        illegal = imported - allowed
        assert not illegal, (
            f"cli/{layer}.py 导入了 {sorted(illegal)}，违反单向依赖（只允许 {sorted(allowed)}）；"
            "需要跨层复用时请把共用部分下沉到 cli.term"
        )


def test_term_layer_is_stdlib_only():
    """最底层的 cli.term 不得导入任何 cli.* —— 否则分层失去意义。"""
    assert _imported_cli_layers(CLI_DIR / "term.py") == set()


def test_compat_layer_reexports_public_api():
    """cli_agent.py 作为兼容入口，必须仍能取到拆分前的那批名字。"""
    import cli_agent

    expected = [
        "paint", "strip_ansi", "strwidth", "clip", "wrap_styled", "Screen",
        "MarkdownStream", "inline_md", "fmt_dur", "fmt_num", "fmt_tokens",
        "Key", "KeyReader", "read_line", "complete", "COMMANDS",
        "Interrupted", "QuitRequested", "RetryRequested", "OutputRequested",
        "http_json", "ctx_meter", "fetch_context_stats", "save_session",
        "load_history", "LAST_CONTEXT", "SESSION_FILE",
    ]
    missing = [name for name in expected if not hasattr(cli_agent, name)]
    assert not missing, f"兼容层丢失这些名字：{missing}"


def test_cli_agent_stays_small():
    """行数棘轮：主入口不得再长回巨型文件（拆分前 2,423 行）。"""
    lines = len((BACKEND / "cli_agent.py").read_text(encoding="utf-8").splitlines())
    assert lines <= 1400, (
        f"cli_agent.py 已 {lines} 行（上限 1400）：新逻辑请放进 cli/ 对应分层"
        "（终端原语→term、格式化→format、行编辑→input、HTTP/文件→http）"
    )
