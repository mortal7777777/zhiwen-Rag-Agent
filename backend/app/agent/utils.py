"""Agent 工具辅助纯函数：XML 工具调用解析、标记清洗、结果瘦身、历史重建、缓存键、前缀哈希、写后验证（自 langgraph_agent.py 拆分）。"""

from __future__ import annotations

import json
import logging
import re
import sys
from pathlib import Path

from langchain_core.messages import (
    AIMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)

from ..runtime_config import effective

logger = logging.getLogger(__name__)
# ==== 函数体（原文）====
def _parse_xml_tool_calls(text: str) -> list[dict]:
    """解析 XML 风格工具调用文本（deepseek 等模型的兜底格式）。

    支持两种常见形态：
      <tool_calls><invoke name="bash"><parameter name="command">ls</parameter></invoke></tool_calls>
      <tool_use name="bash"><parameter name="command">ls</parameter></tool_use>
    返回 langchain AIMessage.tool_calls 格式的列表，解析失败返回空列表。
    """
    import re as _re

    text = _normalize_tool_markers(text)
    calls: list[dict] = []
    # 匹配 <invoke name="X"> / <tool_use name='X'> / <tool_call> / <function> /
    # <antml:invoke> 等（容忍 name= 两侧空格与单双引号）
    pattern = _re.compile(
        r"<(?:invoke|tool_use|tool_call|function|tool|antml:invoke)\s+name\s*=\s*[\"']([^\"']+)[\"'][^>]*>"
        r"(.*?)</(?:invoke|tool_use|tool_call|function|tool|antml:invoke)>",
        _re.S | _re.I,
    )
    for m in pattern.finditer(text):
        name = m.group(1).strip()
        body = m.group(2)
        if not name:
            continue
        args: dict = {}
        # 参数：<parameter name="k">v</parameter>
        for pm in _re.finditer(
            r"<parameter\s+name\s*=\s*[\"']([^\"']+)[\"'][^>]*>(.*?)</parameter>",
            body,
            _re.S,
        ):
            k = pm.group(1).strip()
            v = pm.group(2).strip()
            if not k or not v:
                continue
            # 参数值统一尝试 JSON 解析（数字/布尔/数组/对象），失败按字符串；
            # 解析出 null 时跳过该参数（由 _mcp_default_args 补默认值，
            # 避免把 null 传给服务端触发 "expected X, received null"）
            try:
                parsed = json.loads(v)
                if parsed is not None:
                    args[k] = parsed
            except Exception:
                args[k] = v
        # 参数：JSON 内嵌（<arguments>{...}</arguments> 或裸 JSON）
        if not args:
            for am in _re.finditer(
                r"<arguments[^>]*>(.*?)</arguments>", body, _re.S
            ):
                raw = am.group(1).strip()
                try:
                    parsed = json.loads(raw)
                    if isinstance(parsed, dict):
                        args = parsed
                except Exception:
                    pass
        if not args:
            # 裸 JSON 兜底（body 本身就是 JSON）
            try:
                parsed = json.loads(body.strip())
                if isinstance(parsed, dict):
                    args = parsed
            except Exception:
                pass
        calls.append(
            {
                "name": name,
                "args": args,
                "id": f"xml_{len(calls)}",
                "type": "tool_call",
            }
        )
    return calls


def _pick_verify_command(settings, path: str) -> str | None:
    """为单个写入/编辑的文件选择验证命令。

    优先级：显式配置的 verify_command（全局）> 按扩展名自动检测。
    自动检测覆盖：.py（py_compile）/ .js/.mjs/.cjs（node --check）/
    .json / .yaml/.yml（语法解析），其余类型不验证。
    """
    explicit = str(effective(settings, "verify_command") or "").strip()
    if explicit:
        return explicit
    # 注意：effective(key) 不传 default，否则永远返回 default 读不到 settings 属性
    if not effective(settings, "verify_auto_detect"):
        return None
    ext = Path(path).suffix.lower()
    if ext == ".py":
        return f'{sys.executable} -m py_compile "{path}"'
    if ext in (".js", ".mjs", ".cjs"):
        return f'node --check "{path}"'
    if ext == ".json":
        # python -m json.tool：语法错误时非零退出，输出原样捕获，避免嵌套引号
        return f'{sys.executable} -m json.tool "{path}"'
    if ext in (".yaml", ".yml"):
        return f'{sys.executable} -c "import yaml,sys; yaml.safe_load(open(sys.argv[1], encoding=\'utf-8\'))" "{path}"'
    return None


def _run_verify(settings, paths: list[str]) -> list[dict]:
    """对写入/编辑的文件逐条运行验证，返回 [{path, command, exit_code, output}]。"""
    from ..tools_extra import _resolve_workspace, _run_command

    sandbox = str(effective(settings, "command_sandbox") or "subprocess").strip().lower()
    workspace = Path(_resolve_workspace(settings)).resolve()

    def _verifiable_path(path: str) -> str:
        # Docker 沙箱里没有宿主机盘符路径：工作目录内的文件映射到 /workspace
        if sandbox != "docker":
            return path
        try:
            rel = Path(path).resolve().relative_to(workspace)
            return "/workspace/" + rel.as_posix()
        except ValueError:
            return path

    results: list[dict] = []
    for p in paths:
        target = _verifiable_path(p)
        cmd = _pick_verify_command(settings, target)
        if not cmd:
            continue
        try:
            r = _run_command(settings, cmd)
            results.append(
                {
                    "path": p,
                    "command": cmd,
                    "exit_code": r.get("exit_code"),
                    "error": r.get("error"),
                    "output": str(r.get("output") or "")[:400],
                }
            )
        except Exception as exc:
            logger.warning("验证命令执行失败 %s：%s", cmd, exc)
            results.append(
                {"path": p, "command": cmd, "exit_code": -1, "error": str(exc), "output": ""}
            )
    return results


# 纯查询类工具：per-run 结果缓存（同轮重复调用直接命中，不重复执行）
# 副作用类（写/改/删/命令/建索引）绝不缓存，否则会掩盖真实状态变化
_CACHEABLE_TOOLS = frozenset(
    {
        "knowledge_base_search",
        "web_search",
        "list_dir",
        "read_file",
        "grep_search",
        "skill_lookup",
    }
)


def _tool_cache_key(name: str, args: dict | None) -> str | None:
    """工具结果缓存键：工具名 + 规范化参数序列化（键序无关）。

    返回 None 表示无法序列化（此时不缓存，直接执行）。
    """
    try:
        return name + "|" + json.dumps(
            args or {}, sort_keys=True, ensure_ascii=False
        )
    except Exception:
        return None


# 工具调用 XML 标签：正文中出现说明模型把工具调用写进了回答文本
# （sensenova 等模型偶尔如此），对用户展示前清理，避免 <tool_calls> 泄漏
_XML_TOOL_TAG_RE = re.compile(
    r"</?(?:tool_calls|tool_call|invoke|tool_use|function_calls|function|arguments|parameter)[^>]*>",
    re.I,
)

# 完整工具调用块（含内部参数值）：整体删除，避免仅删标签后参数内容泄漏。
# 覆盖 <tool_calls>…</tool_calls>、<invoke name=…>…</invoke>、
# <tool_use>…</tool_use>、<tool_call>…</tool_call>、<function>…</function>，
# 以及完整的 <parameter>…</parameter>/<arguments>…</arguments> 子元素
# （外层标签未闭合时子元素仍可能完整，逐元素删除防内容残留）。
_XML_TOOL_BLOCK_RE = re.compile(
    r"<(?:tool_calls|tool_call|tool_use|invoke|function|function_calls|parameter|arguments)\b[^>]*>.*?"
    r"</(?:tool_calls|tool_call|tool_use|invoke|function|function_calls|parameter|arguments)>",
    re.S | re.I,
)

# 工具轮检测标记：模型输出这些片段即视为 XML 风格工具调用（含变体）
_XML_TOOL_MARKERS = (
    "<tool_calls",
    "<invoke",
    "<tool_use",
    "<function_calls",
    "<user|tool_calls",
    "<|tool_calls",
    "<|DSML|tool_calls",
    "<function name=",
)


def _normalize_tool_markers(text: str) -> str:
    """归一化工具调用标记的变体写法（全角符号 / |DSML| 前缀等）。

    实测泄漏消息里的标签是全角竖线变体 <｜DSML｜tool_calls>（U+FF5C），
    不归一化则检测/解析/清理全部漏判，正文直接泄漏进最终回答；
    <|DSML|tool_calls>、<|user|tool_calls> 等前缀格式同样先转标准标签。
    """
    if not text:
        return text
    # 全角变体（｜＜＞ = U+FF5C/U+FF1C/U+FF1E）先转 ASCII
    text = text.replace("｜", "|").replace("＜", "<").replace("＞", ">")
    # 去掉 |DSML| / |user| 前缀（竖线数量不限），如
    # <|DSML|tool_calls> / <||DSML||tool_calls> / <|user|tool_calls> → <tool_calls>
    text = re.sub(r"<(/?)(?:\|+DSML\|+|\|+user\|+)", r"<\1", text)
    return text


def _strip_xml_tool_tags(text: str) -> str:
    """清理回答正文中的工具调用 XML 标记（仅影响展示，不改历史消息）。

    整块删除工具调用 XML（含内部参数值），避免只删标签后参数内容（如命令、
    query）泄漏进回答；未闭合/跨片残留的孤立标签再逐个清掉。
    不做 strip：流式分片逐片调用时保持空白原样，拼接不受影响；
    完整文本的收尾清理（strip）在 finalize 做。
    """
    if not text:
        return text
    # 全角/DSML 前缀等变体先归一成标准标签，再走整块删除
    cleaned = _normalize_tool_markers(text)
    # <user|tool_calls> / <|tool_calls> 变体（开/闭标签）：先归一成 <tool_calls>
    cleaned = re.sub(r"</?user\|", "<", cleaned)
    cleaned = re.sub(r"</\|tool_calls", "</tool_calls", cleaned)
    cleaned = re.sub(r"<\|tool_calls", "<tool_calls", cleaned)
    # 整块删除（含内部参数值）
    cleaned = _XML_TOOL_BLOCK_RE.sub("", cleaned)
    # 残留的孤立标签（未闭合/不完整）逐个清掉
    cleaned = _XML_TOOL_TAG_RE.sub("", cleaned)
    return cleaned


def _slim_tool_result(result: dict, content_limit: int = 2500) -> dict:
    """工具结果进历史前瘦身：大 content 字段保留首尾、中间省略。

    借鉴 Hermes 的 proactive_prune 思路：模型在本轮已看过完整输出，
    历史里只需要 summary + 关键片段供下一轮决策；小输出原样保留，
    不破坏 provider 已建立的缓存前缀。返回新 dict，不改原 result。
    """

    if not isinstance(result, dict):
        return result
    slim = dict(result)
    for key in ("content", "output", "entries", "matches"):
        val = slim.get(key)
        if isinstance(val, str) and len(val) > content_limit:
            head = val[: int(content_limit * 0.6)]
            tail = val[-int(content_limit * 0.3) :]
            slim[key] = (
                f"{head}\n…（中间省略 {len(val) - len(head) - len(tail)} 字符，"
                f"共 {len(val)} 字符，如需完整内容请重新调用工具）…\n{tail}"
            )
        elif isinstance(val, list) and len(val) > 60:
            # 超长列表（目录/匹配项）只保留前 60 条 + 计数
            slim[key] = val[:60] + [
                {"_omitted": f"… 共 {len(val)} 项，仅显示前 60 项"}
            ]
    return slim


def _tool_detail(result: dict, limit: int = 3500) -> str:
    """生成工具完整输出的展示文本（供 CLI /output 与 Web 工具卡展开查看）。

    只影响展示层：模型历史仍走 _slim_tool_result 的瘦身路径。
    优先取信息量最大的字段（error/output/content/diff/matches/entries），
    都没有时序列化整个结果。
    """
    if not isinstance(result, dict):
        return str(result)[:limit]
    for key in ("error", "output", "content", "diff", "matches", "entries", "result"):
        val = result.get(key)
        if val in (None, "", []):
            continue
        text = val if isinstance(val, str) else json.dumps(val, ensure_ascii=False)
        text = text[:limit]
        if len(val if isinstance(val, str) else "") > limit:
            text += f"\n…（共 {len(str(val))} 字符，已截断）"
        return text
    try:
        return json.dumps(result, ensure_ascii=False)[:limit]
    except Exception:
        return str(result)[:limit]


def _tools_prefix_hash(tools) -> str:
    """计算工具定义的稳定哈希（供缓存前缀监测）。

    bind_tools 发给模型的工具定义（名称/描述/参数 schema）构成缓存前缀的
    一部分，前缀字节变化会让 DeepSeek 自动缓存整体失效（命中率骤降）。
    会话内工具定义应保持稳定；变化时记录日志便于定位元凶（用户自写工具、
    技能注入、MCP 服务器变更等）。
    """
    import hashlib as _hashlib
    import json as _json

    parts = []
    for t in sorted(tools, key=lambda x: getattr(x, "name", "") or ""):
        name = getattr(t, "name", "") or ""
        desc = getattr(t, "description", "") or ""
        schema = ""
        args_schema = getattr(t, "args_schema", None)
        if args_schema is not None:
            try:
                schema = _json.dumps(
                    args_schema.model_json_schema(),
                    sort_keys=True,
                    ensure_ascii=False,
                )
            except Exception:
                schema = str(args_schema)
        parts.append(f"{name}\u0000{desc}\u0000{schema}")
    return _hashlib.md5("\n".join(parts).encode("utf-8")).hexdigest()[:12]


def _rows_to_history(rows: list[dict], history_messages: list | None = None) -> list:
    """把数据库历史行重建为消息链（含工具轮消息，缓存前缀保真）。

    - role='tool' 行 -> ToolMessage（tool_call_id/name 来自 tool_trace 元数据）；
    - role='assistant' 且 content 为 {"__tool_calls__": [...]} 标记 ->
      AIMessage(tool_calls=...)，即工具轮助手消息；
    - 其余 user/assistant 行按原文还原。
    trim/滚动压缩可能在边界拆散 AIMessage 与其 ToolMessage：
    拆散时丢弃无来源的 ToolMessage、清空无结果的 tool_calls，
    避免 provider 报 "tool 消息未匹配" 错误。
    """
    out = list(history_messages or [])
    for row in rows:
        role = row.get("role")
        content = str(row.get("content") or "")
        if role == "user":
            out.append(HumanMessage(content=content))
        elif role == "system":
            # D 块与轮内提示的持久化行：按原位置重建，保证前缀保真
            out.append(SystemMessage(content=content))
        elif role == "assistant":
            if content.lstrip().startswith('{"__tool_calls__"'):
                try:
                    marker = json.loads(content)
                    calls = marker.get("__tool_calls__") or []
                except Exception:
                    marker = {}
                    calls = []
                reasoning = marker.get("__reasoning__")
                out.append(
                    AIMessage(
                        content="",
                        tool_calls=calls,
                        # 无条件回传（含空串）：旧数据缺 __reasoning__ 时
                        # 补空串，避免工具轮消息无字段触发 API 400
                        additional_kwargs={
                            "reasoning_content": (
                                reasoning if reasoning is not None else ""
                            )
                        },
                    )
                )
            else:
                out.append(AIMessage(content=content))
        elif role == "tool":
            meta = {}
            try:
                tt = row.get("tool_trace")
                if isinstance(tt, str):
                    tt = json.loads(tt) if tt else []
                if isinstance(tt, list) and tt and isinstance(tt[0], dict):
                    meta = tt[0]
            except Exception:
                pass
            out.append(
                ToolMessage(
                    content=content,
                    tool_call_id=str(meta.get("tool_call_id") or ""),
                    name=str(meta.get("name") or ""),
                )
            )
    call_ids = {
        tc.get("id")
        for m in out
        if isinstance(m, AIMessage)
        for tc in (getattr(m, "tool_calls", None) or [])
    }
    out = [
        m for m in out
        if not (isinstance(m, ToolMessage) and m.tool_call_id not in call_ids)
    ]
    result_ids = {m.tool_call_id for m in out if isinstance(m, ToolMessage)}
    for m in out:
        if isinstance(m, AIMessage) and getattr(m, "tool_calls", None):
            m.tool_calls = [tc for tc in m.tool_calls if tc.get("id") in result_ids]
    return out


def _tool_has_required_args(tool) -> bool:
    """判断工具参数 schema 是否存在必填字段（空参数兜底用）。

    MCP 工具经 _schema_to_model 转换后所有字段均为可选（Playwright MCP 的
    schema 缺陷由 _mcp_default_args 在 invoke 内补默认值），因此空参数调用
    是合法的，不应被"参数缺失"拦截；只有 schema 明确存在必填字段（或无
    schema 信息）时才拦截空参数。
    """
    schema = getattr(tool, "args_schema", None)
    if schema is None:
        return True  # 无 schema 信息，保守拦截（保持原有行为）
    try:
        fields = getattr(schema, "model_fields", None)
        if fields is None:
            fields = getattr(schema, "__fields__", None)  # pydantic v1 兼容
        if not fields:
            return False  # schema 无字段：空参数合法
        for f in fields.values():
            is_required = getattr(f, "is_required", None)
            if is_required is not None and is_required():
                return True
            if getattr(f, "required", False):
                return True
        return False
    except Exception:
        return True  # 判断失败时保守拦截



_CACHEABLE_TOOLS = frozenset(
    {
        "knowledge_base_search",
        "web_search",
        "list_dir",
        "read_file",
        "grep_search",
        "skill_lookup",
    }
)



_XML_TOOL_TAG_RE = re.compile(
    r"</?(?:tool_calls|tool_call|invoke|tool_use|function_calls|function|arguments|parameter)[^>]*>",
    re.I,
)

# 完整工具调用块（含内部参数值）：整体删除，避免仅删标签后参数内容泄漏。
# 覆盖 <tool_calls>…</tool_calls>、<invoke name=…>…</invoke>、
# <tool_use>…</tool_use>、<tool_call>…</tool_call>、<function>…</function>，
# 以及完整的 <parameter>…</parameter>/<arguments>…</arguments> 子元素
# （外层标签未闭合时子元素仍可能完整，逐元素删除防内容残留）。

_XML_TOOL_BLOCK_RE = re.compile(
    r"<(?:tool_calls|tool_call|tool_use|invoke|function|function_calls|parameter|arguments)\b[^>]*>.*?"
    r"</(?:tool_calls|tool_call|tool_use|invoke|function|function_calls|parameter|arguments)>",
    re.S | re.I,
)

# 工具轮检测标记：模型输出这些片段即视为 XML 风格工具调用（含变体）

_XML_TOOL_MARKERS = (
    "<tool_calls",
    "<invoke",
    "<tool_use",
    "<function_calls",
    "<user|tool_calls",
    "<|tool_calls",
    "<|DSML|tool_calls",
    "<function name=",
)


