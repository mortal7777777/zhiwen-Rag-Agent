"""运行时配置：允许在设置页免重启切换模型/供应商/技能偏好。

设计：
- 覆盖值持久化在 app_meta.runtime_overrides（JSON）；
- 进程内缓存一份，API 保存后立即生效；
- 模型构造点统一用 effective() / provider 解析函数读取。

供应商模型配置（参考 cc-switch 的多供应商管理方式）：
- providers：{id, name, type(chat|vision), base_url, api_key, model, models, enabled}
- active_chat_provider / active_vision_provider：当前使用的供应商 id
- 旧的 deepseek_*/sensenova_* 键值仍保留兼容：保存时会自动同步到供应商列表。

优先级：DB 覆盖 > 环境变量/默认值。
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

# ============================================================
# 可调项声明表（单一来源，2026-10-08）
# ------------------------------------------------------------
# 这里是"设置页能改什么"的唯一事实来源：
#   * EDITABLE_KEYS / 数值区间 / 布尔键 / 枚举取值 全部由 _SPECS 派生；
#   * GET /api/settings/schema 直接把 _SPECS 的元数据吐给前端（可据此渲染表单）；
#   * 新增一个可调项 = 在 _SPECS（或 _PLAIN_KEYS）里加一行 + 在读点用
#     effective()/effective_or() 读——不再需要"三处同步"。
# 改动守则：本表的键集合有测试快照（tests/test_runtime_config_specs.py），
# 少一个键 = 该设置项静默不可改，测试会拦住。
# ============================================================


@dataclass(frozen=True)
class FieldSpec:
    """一个可调项的声明：类型 + 兜底值 + 合法区间/枚举 + 展示元数据。

    kind: int | float | bool | enum —— 决定保存时怎么清洗（越界夹取、
    非数值跳过、枚举非法跳过；坏值不拖垮整批保存）。
    default 仅用于展示与 schema（运行期读取一律走 effective/effective_or，
    避免出现"默认值"与"读点"两个真相）。
    """

    kind: str
    default: object = None
    lo: float | None = None
    hi: float | None = None
    choices: tuple[str, ...] = ()
    label: str = ""
    group: str = "other"


_SPECS: dict[str, FieldSpec] = {
    # ---- Agent 预算与循环 ----
    "agent_max_iterations": FieldSpec(
        "int", 20, 1, 200, label="工具调用上限（普通轮次）", group="agent"
    ),
    "agent_max_failures": FieldSpec(
        "int", 3, 1, 50, label="连续失败上限（普通轮次）", group="agent"
    ),
    "agent_task_max_iterations": FieldSpec(
        "int", 50, 1, 500, label="工具调用上限（项目任务）", group="agent"
    ),
    "agent_task_max_failures": FieldSpec(
        "int", 6, 1, 100, label="连续失败上限（项目任务）", group="agent"
    ),
    "agent_max_dispatch_rounds": FieldSpec(
        "int", 3, 1, 10, label="单轮最多派发轮数", group="agent"
    ),
    "agent_subagent_max_rounds": FieldSpec(
        "int", 2, 1, 20, label="research 子代理最多 LLM 轮数", group="agent"
    ),
    "agent_llm_stall_timeout_s": FieldSpec(
        "float", 150.0, 10.0, 1800.0, label="流式卡死看门狗（秒）", group="agent"
    ),
    "task_mode_detect": FieldSpec(
        "bool", True, label="任务态自动识别", group="agent"
    ),
    "agent_subagents_enabled": FieldSpec(
        "bool", True, label="子代理并行", group="agent"
    ),
    "plan_mode_allowed": FieldSpec(
        "bool", True, label="计划模式", group="agent"
    ),
    "reasoning_summary_enabled": FieldSpec(
        "bool", False, label="展示思考过程", group="agent"
    ),
    "chat_temperature": FieldSpec(
        "float", 0.5, 0.0, 2.0, label="回答温度", group="model"
    ),
    # ---- 检索与上下文 ----
    "kb_chat_min_score": FieldSpec(
        "float", 0.2, 0.0, 1.0, label="知识库相关度门槛", group="retrieval"
    ),
    "crag_fallback_enabled": FieldSpec(
        "bool", True, label="知识库不足时补联网", group="retrieval"
    ),
    "crag_min_score": FieldSpec(
        "float", 0.45, 0.0, 1.0, label="CRAG 触发阈值", group="retrieval"
    ),
    "history_max_messages": FieldSpec(
        "int", 400, 8, 500, label="历史窗口条数", group="context"
    ),
    "history_max_tokens": FieldSpec(
        "int", 64000, 1000, 200000, label="历史 token 预算", group="context"
    ),
    "vision_max_images": FieldSpec(
        "int", 6, 1, 10, label="单次最多识图数", group="context"
    ),
    "checkpoint_keep_per_thread": FieldSpec(
        "int", 20, 5, 200, label="每线程保留快照数", group="context"
    ),
    # ---- 记忆 ----
    "memory_auto_extract_min_chars": FieldSpec(
        "int", 400, 50, 5000, label="自动提取记忆的最小回答字数", group="memory"
    ),
    "memory_consolidate_threshold": FieldSpec(
        "int", 10, 2, 500, label="记忆整合触发条数", group="memory"
    ),
    "memory_consolidate_interval_hours": FieldSpec(
        "int", 24, 1, 720, label="记忆整合间隔（小时）", group="memory"
    ),
    "project_memory_max_chars": FieldSpec(
        "int", 2600, 500, 20000, label="项目记忆注入上限（字符）", group="memory"
    ),
    "trajectory_compress_enabled": FieldSpec(
        "bool", True, label="长任务轨迹摘要", group="memory"
    ),
    # ---- 技能与工具 ----
    "skills_enabled": FieldSpec(
        "bool", True, label="技能总开关", group="skills"
    ),
    "advanced_tools_enabled": FieldSpec(
        "bool", True, label="受控执行工具（文件/命令）", group="tools"
    ),
    "mcp_enabled": FieldSpec("bool", True, label="MCP 工具接入", group="tools"),
    "skill_sandbox_enabled": FieldSpec(
        "bool", False, label="技能沙箱执行", group="tools"
    ),
    "sandbox_workspace_readonly": FieldSpec(
        "bool", False, label="沙箱内工作目录只读", group="tools"
    ),
    "command_timeout": FieldSpec(
        "int", 60, 5, 300, label="命令执行超时（秒）", group="tools"
    ),
    "verify_max_retries": FieldSpec(
        "int", 1, 0, 5, label="写后验证重试次数", group="tools"
    ),
    "verify_auto_detect": FieldSpec(
        "bool", True, label="按文件类型自动选验证命令", group="tools"
    ),
    # ---- 审批 ----
    "tool_permission_mode": FieldSpec(
        "enum", "ask", choices=("ask", "allow"), label="敏感操作确认模式", group="permission"
    ),
    "permission_timeout": FieldSpec(
        "int", 0, 30, 3600, label="确认超时（秒，0=无限等待）", group="permission"
    ),
    "command_sandbox": FieldSpec(
        "enum", "subprocess", choices=("subprocess", "docker"), label="命令执行环境", group="permission"
    ),
    # ---- 联网搜索 ----
    "web_search_provider": FieldSpec(
        "enum",
        "duckduckgo",
        choices=("duckduckgo", "searxng", "tavily", "off"),
        label="搜索供应商",
        group="web",
    ),
    "web_search_max_results": FieldSpec(
        "int", 6, 1, 20, label="单次搜索结果条数", group="web"
    ),
    # ---- 视觉 ----
    "main_model_vision": FieldSpec(
        "bool", False, label="主模型原生支持视觉", group="vision"
    ),
}

# 可调项的历史全集（键集合有测试快照锁住；类型化键已迁到上面的 _SPECS，
# 由下面的 _PLAIN_KEYS 扣除，保证 EDITABLE_KEYS 与历史完全一致）
_EDITABLE_LEGACY = {
    # 旧式对话模型键值（兼容保留，新前端走 providers）
    "deepseek_api_key",
    "deepseek_base_url",
    "deepseek_model",
    "agent_title_model",
    "chat_temperature",
    "main_model_vision",
    # 知识库数据目录（可在知识库管理页修改，改动即时生效并持久化）
    "data_dir",
    # 旧式视觉模型键值（兼容保留）
    "sensenova_api_key",
    "sensenova_base_url",
    "sensenova_model",
    # 供应商列表与当前激活项（cc-switch 式多供应商管理）
    "providers",
    "active_chat_provider",
    "active_vision_provider",
    # 联网搜索
    "web_search_provider",
    "web_search_max_results",
    "tavily_api_key",
    "searxng_base_url",
    "searxng_engines",
    # 技能总开关
    "skills_enabled",
    # 高级工具（P0：受控执行 / MCP / 技能沙箱 / 思考过程展示）
    "advanced_tools_enabled",
    "agent_task_max_iterations",
    "agent_task_max_failures",
    "agent_max_iterations",
    "agent_max_failures",
    "agent_max_dispatch_rounds",
    "agent_llm_stall_timeout_s",
    "task_mode_detect",
    "agent_subagents_enabled",
    "agent_subagent_max_rounds",
    "plan_mode_allowed",
    # 检索与上下文/记忆调参（2026-09-29 上设置页）
    "kb_chat_min_score",
    "crag_fallback_enabled",
    "crag_min_score",
    "vision_max_images",
    "history_max_messages",
    "history_max_tokens",
    "memory_auto_extract_min_chars",
    "project_memory_max_chars",
    "memory_consolidate_threshold",
    "memory_consolidate_interval_hours",
    "checkpoint_keep_per_thread",
    "verify_command",
    "verify_auto_detect",
    "verify_max_retries",
    "tool_workspace",
    "command_allowlist",
    "command_timeout",
    "command_sandbox",
    "sandbox_image",
    "sandbox_workspace_readonly",
    "tool_permission_mode",
    "permission_timeout",
    "skill_sandbox_enabled",
    "reasoning_summary_enabled",
    "project_memory_file",
    "mcp_enabled",
    "trajectory_compress_enabled",
    # 嵌入/重排序模型提供方式（local=本地 BGE / api=OpenAI 兼容接口）
    "embedding_provider",
    "embedding_api_base_url",
    "embedding_api_key",
    "embedding_api_model",
    "reranker_provider",
    "reranker_api_base_url",
    "reranker_api_key",
    "reranker_api_model",
}

# 无需类型校验的普通键（字符串 / 密钥 / 路径 / 供应商列表）= 历史全集 − 类型化键
_PLAIN_KEYS = _EDITABLE_LEGACY - set(_SPECS)

# 可调项总集（键集合与历史一致；类型校验规则由 _SPECS 派生）
EDITABLE_KEYS = _PLAIN_KEYS | set(_SPECS)

# 只读展示键
READONLY_KEYS = {
    "embedding_model_dir",
    "reranker_cache_dir",
    "embedding_fp16",
    "embed_batch_size",
    "opensearch_url",
    "opensearch_index",
}

# 数值区间 / 布尔键 / 枚举取值：全部由 _SPECS 派生（新增可调项只改声明表）
_INT_RANGES = {
    key: (int(spec.lo), int(spec.hi))
    for key, spec in _SPECS.items()
    if spec.kind == "int"
}
_FLOAT_RANGES = {
    key: (float(spec.lo), float(spec.hi))
    for key, spec in _SPECS.items()
    if spec.kind == "float"
}
_BOOL_KEYS = {key for key, spec in _SPECS.items() if spec.kind == "bool"}
_ENUM_KEYS = {
    key: spec.choices for key, spec in _SPECS.items() if spec.kind == "enum"
}


def editable_schema() -> list[dict]:
    """可调项元数据（供设置页渲染表单；新增一项只需改 _SPECS）。

    只暴露"填写辅助"信息，不含任何敏感值——密钥类键在 _PLAIN_KEYS 里，
    这里不带 default，避免 schema 端点泄露。
    """
    rows: list[dict] = []
    for key, spec in _SPECS.items():
        rows.append(
            {
                "key": key,
                "kind": spec.kind,
                "default": spec.default,
                "lo": spec.lo,
                "hi": spec.hi,
                "choices": list(spec.choices),
                "label": spec.label,
                "group": spec.group,
            }
        )
    for key in sorted(_PLAIN_KEYS):
        rows.append(
            {
                "key": key,
                "kind": "str",
                "default": None,
                "lo": None,
                "hi": None,
                "choices": [],
                "label": "",
                "group": "other",
            }
        )
    return rows

# 旧键值 -> 供应商字段的映射（保存旧键时同步到 providers，保持两套机制一致）
_LEGACY_TO_PROVIDER = {
    "deepseek_api_key": ("chat", "api_key"),
    "deepseek_base_url": ("chat", "base_url"),
    "deepseek_model": ("chat", "model"),
    "sensenova_api_key": ("vision", "api_key"),
    "sensenova_base_url": ("vision", "base_url"),
    "sensenova_model": ("vision", "model"),
}

_lock = threading.Lock()
_overrides: dict = {}


def _apply_data_dir_override(settings, value) -> None:
    """把 data_dir 覆盖同步到全局 settings 单例。

    RAGService 直接读 settings.data_dir 属性（不走 effective()），
    所以改目录必须直接改单例属性才能即时生效。Settings 是 frozen
    dataclass，用 object.__setattr__ 原地修改（有意为之的运行时变更，
    所有持有该实例引用的 service 都会读到新值）。
    """
    if settings is None:
        return
    try:
        from pathlib import Path

        p = Path(str(value)).expanduser().resolve()
        p.mkdir(parents=True, exist_ok=True)  # 不存在则创建，保证可用
        object.__setattr__(settings, "data_dir", p)
    except Exception as exc:
        logger.warning("应用 data_dir 覆盖失败（%s）：%s", value, exc)


def load_overrides(db) -> dict:
    """从数据库加载覆盖值（启动时 / 保存后调用）。"""
    global _overrides
    from .db import repository as repo

    raw = repo.get_meta(db, "runtime_overrides")
    data = {}
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                data = parsed
        except Exception as exc:
            logger.warning("runtime_overrides 解析失败：%s", exc)
    with _lock:
        _overrides = data
    # 重启后恢复 data_dir：直接改 settings 单例（RAGService 读属性不走 effective）
    if data.get("data_dir"):
        from .config import get_settings  # 本模块此前漏了这个 import（仅 data_dir 路径触发）

        _apply_data_dir_override(get_settings(), data["data_dir"])
    return data


def get_overrides() -> dict:
    with _lock:
        return dict(_overrides)


def save_overrides(db, updates: dict, settings=None) -> dict:
    """校验并保存覆盖值，更新进程内缓存。"""
    from .db import repository as repo

    clean = {}
    for key, value in updates.items():
        if key not in EDITABLE_KEYS:
            continue
        if value is None:
            continue
        if key == "providers":
            # 非列表（坏载荷）跳过；显式空列表 = 删除全部供应商，必须允许写入
            # （此前 `if cleaned:` 会把空列表当作"未提供"，删完后刷新又回来）
            if isinstance(value, list):
                clean[key] = _validate_providers(value, settings=settings)
            continue
        if key == "data_dir":
            # 路径校验 + 即时同步到 settings 单例（RAGService 读属性不走 effective）
            _apply_data_dir_override(settings, value)
            clean[key] = str(
                getattr(settings, "data_dir", None)
                if settings is not None
                else Path(str(value)).expanduser().resolve()
            )
            continue
        if key in ("active_chat_provider", "active_vision_provider"):
            clean[key] = str(value).strip()
            continue
        if key.endswith("api_key"):
            value = str(value).strip()
            if not value:
                continue  # 空串表示不修改
        elif key in _ENUM_KEYS:
            value = str(value).strip()
            if value not in _ENUM_KEYS[key]:
                continue  # 非法枚举：跳过该键（不拖垮整批保存）
        elif key in _INT_RANGES:
            lo, hi = _INT_RANGES[key]
            try:
                value = max(lo, min(hi, int(value)))
            except (TypeError, ValueError):
                continue  # 非数值：跳过该键（不拖垮整批保存）
        elif key in _FLOAT_RANGES:
            lo, hi = _FLOAT_RANGES[key]
            try:
                value = max(lo, min(hi, float(value)))
            except (TypeError, ValueError):
                continue
        elif key in _BOOL_KEYS:
            value = bool(value)
        clean[key] = value

    with _lock:
        _overrides.update(clean)
        _sync_legacy_to_providers(set(clean))
        _fix_active_provider_ids()
        merged = dict(_overrides)
    repo.set_meta(db, "runtime_overrides", json.dumps(merged, ensure_ascii=False))
    return merged


# 自定义请求头的约束：名字按 RFC 7230 token 字符集；这些头由传输层管理，
# 用户自定义会破坏请求完整性（Host/长度/连接管理/鉴权走 api_key 字段）
_HEADER_NAME_RE = re.compile(r"[A-Za-z0-9!#$%&'*+\-.^_`|~]+")
_HEADER_NAME_BLACKLIST = {
    "host",
    "content-length",
    "transfer-encoding",
    "connection",
    "authorization",
}
_MAX_HEADERS = 20
_MAX_HEADER_NAME = 64
_MAX_HEADER_VALUE = 2048

# 名字含这些词的头值按密钥处理：GET /settings 与导出走 mask_key 脱敏，
# 保存时按 "****" 前缀还原（与 api_key 同一套约定）
_SENSITIVE_HEADER_RE = re.compile(r"key|token|secret|auth|cookie", re.IGNORECASE)


def is_sensitive_header(name: str) -> bool:
    """判断请求头名是否按敏感值处理（脱敏/还原）。"""
    return bool(_SENSITIVE_HEADER_RE.search(str(name or "")))


def _clean_headers(value) -> dict[str, str]:
    """清洗自定义请求头：黑名单 + 非法名/值丢弃（坏条目跳过，不拖垮整批）。"""
    if not isinstance(value, dict):
        return {}
    out: dict[str, str] = {}
    for name, val in value.items():
        name = str(name).strip()
        val = str(val).strip()
        if not name or len(name) > _MAX_HEADER_NAME:
            continue
        if name.lower() in _HEADER_NAME_BLACKLIST:
            continue
        if not _HEADER_NAME_RE.fullmatch(name):
            continue
        if len(val) > _MAX_HEADER_VALUE or "\r" in val or "\n" in val:
            continue
        out[name] = val
        if len(out) >= _MAX_HEADERS:
            break
    return out


def _validate_providers(value, settings=None) -> list[dict]:
    """清洗前端提交的供应商列表；**** 开头的 Key 表示未修改，取旧值。"""
    if not isinstance(value, list):
        return []
    prev = get_overrides().get("providers")
    prev_by_id = {p.get("id"): p for p in prev} if isinstance(prev, list) else {}
    cleaned: list[dict] = []
    seen: set[str] = set()
    for raw in value:
        if not isinstance(raw, dict):
            continue
        pid = str(raw.get("id") or "").strip()
        if not pid or pid in seen:
            continue
        seen.add(pid)
        ptype = raw.get("type") if raw.get("type") in ("chat", "vision") else "chat"
        api_key = str(raw.get("api_key") or "").strip()
        if api_key.startswith("****"):
            # 只允许按 id 命中的旧值。绝不回退 env/config 里的 deepseek_api_key——
            # 那会把环境里的真实 Key 塞给"任意 id + 任意 base_url"的新供应商（外泄）。
            api_key = str(prev_by_id.get(pid, {}).get("api_key") or "").strip()
        raw_models = raw.get("models") or []
        models = [str(m).strip() for m in raw_models if str(m).strip()]
        model = str(raw.get("model") or "").strip() or (models[0] if models else "")
        if model and model not in models:
            models.insert(0, model)  # 不变量：默认模型必须出现在候选列表里
        # 思考模式/强度（DeepSeek V4 等支持；disabled 时不发 reasoning_effort）
        effort = str(raw.get("thinking_effort") or "high").strip().lower()
        if effort not in ("low", "medium", "high", "max"):
            effort = "high"
        entry = {
            "id": pid,
            "name": str(raw.get("name") or "").strip()[:60] or pid,
            "type": ptype,
            "base_url": str(raw.get("base_url") or "").strip()[:300],
            "api_key": api_key,
            "model": model,
            "models": models or ([model] if model else []),
            "enabled": bool(raw.get("enabled", True)),
            "note": str(raw.get("note") or "").strip()[:200],
            "thinking_enabled": bool(raw.get("thinking_enabled", True)),
            "thinking_effort": effort,
        }
        # 显式声明的 API 格式（provider_api_format 会优先读它）；空值不落库，
        # 保持"按 base_url 推断"的默认行为
        fmt = str(raw.get("api_format") or "").strip().lower()
        if fmt in ("anthropic", "openai"):
            entry["api_format"] = fmt
        # 每供应商参数覆盖（空 = 用全局）；temperature 用 0.0 判断必须走 None 检查
        temp = raw.get("temperature")
        if temp is not None and str(temp).strip() != "":
            try:
                entry["temperature"] = max(0.0, min(2.0, float(temp)))
            except (TypeError, ValueError):
                pass
        mt = raw.get("max_tokens")
        if mt is not None and str(mt).strip() != "":
            try:
                n = int(mt)
                if n > 0:
                    entry["max_tokens"] = max(256, min(200000, n))
            except (TypeError, ValueError):
                pass
        headers = _clean_headers(raw.get("headers"))
        if headers:
            # 敏感头值的 "****" 掩码还原：与 api_key 同约定，只认同 id 的旧值；
            # 还原不到就丢弃该头（绝不能把 **** 字面量发给上游）
            old_headers = prev_by_id.get(pid, {}).get("headers")
            if isinstance(old_headers, dict):
                for name, val in list(headers.items()):
                    if not str(val).startswith("****"):
                        continue
                    if name in old_headers:
                        headers[name] = old_headers[name]
                    else:
                        headers.pop(name)
            entry["headers"] = headers
        cleaned.append(entry)
    return cleaned


def _sync_legacy_to_providers(updated_keys=None) -> None:
    """旧式 deepseek_*/sensenova_* 键值保存后，同步到对应 id 的供应商。

    仅当本次保存显式包含 legacy 键才执行（否则每次保存都拿 DB 里可能残留的
    旧值覆盖供应商），并按既定 id（deepseek/sensenova）匹配，而不是"第一个同类"。
    """
    providers = _overrides.get("providers")
    if not isinstance(providers, list):
        return
    changed = False
    for key, (ptype, field) in _LEGACY_TO_PROVIDER.items():
        if key not in _overrides:
            continue
        if updated_keys is not None and key not in updated_keys:
            continue
        target_id = "sensenova" if ptype == "vision" else "deepseek"
        for p in providers:
            if p.get("id") == target_id:
                p[field] = _overrides[key]
                changed = True
                break
    if changed:
        _overrides["providers"] = providers


def _fix_active_provider_ids() -> None:
    """保证激活项指向已启用且存在的供应商；否则自动选第一个。"""
    providers = _overrides.get("providers")
    if not isinstance(providers, list) or not providers:
        return
    for ptype, active_key in (
        ("chat", "active_chat_provider"),
        ("vision", "active_vision_provider"),
    ):
        ids = [p["id"] for p in providers if p.get("type") == ptype and p.get("enabled", True)]
        current = _overrides.get(active_key)
        if not ids:
            continue
        if current not in ids:
            _overrides[active_key] = ids[0]


def thinking_extra_body(cfg: dict) -> dict | None:
    """根据供应商配置生成思考模式/强度的 extra_body 参数。

    DeepSeek V4 系：`thinking: {type: enabled|disabled}` + `reasoning_effort`
    （low/medium/high/max，默认 high）；disabled 时不发送 reasoning_effort
    （官方文档：两者互斥）。非思考模型可忽略该参数。
    """
    if cfg is None:
        return None
    if cfg.get("thinking_enabled") is False:
        return {"thinking": {"type": "disabled"}}
    effort = str(cfg.get("thinking_effort") or "high").strip().lower()
    if effort not in ("low", "medium", "high", "max"):
        effort = "high"
    return {"thinking": {"type": "enabled"}, "reasoning_effort": effort}


def effective(settings, key: str, default=None):
    """读取生效值：DB 覆盖 > settings（环境变量/config 默认值）> None。

    **不要再传 default**：`effective(settings, key, x)` 会让环境变量与
    config.py 默认值全部失效（只有设置页的 DB 覆盖能生效），历史上已经
    造成 9 个键"部署配置通道断开"（含 /api/advanced 的受控执行开关读不到
    默认值 True）。需要兜底常量时用 effective_or()。
    守门：scripts/check_effective_default.py（CI + pre-commit）。
    """
    overrides = get_overrides()
    if key in overrides:
        return overrides[key]
    return getattr(settings, key, None)


def effective_or(settings, key: str, fallback):
    """读取生效值，读不到才用兜底常量（语义写在名字里，防误用）。

    顺序：DB 覆盖 > settings 属性（env/config）> fallback。
    适用于"settings 里可能没有该字段"（如 skills_enabled）或"必须给安全
    默认值"（如审批模式默认 ask，绝不能因读不到而放开确认）的读点。
    """
    overrides = get_overrides()
    if key in overrides:
        return overrides[key]
    value = getattr(settings, key, None)
    return fallback if value is None else value


# ---------------- 供应商 API 格式（OpenAI 兼容 / Anthropic 兼容） ----------------

def provider_api_format(cfg: dict | None) -> str:
    """判定供应商使用的 API 格式：'anthropic' | 'openai'。

    显式 `api_format` 字段优先；否则按 base_url 推断——路径含 /anthropic
    即为 Anthropic 兼容端点（如 DeepSeek 的 https://api.deepseek.com/anthropic，
    见 https://api-docs.deepseek.com/guides/anthropic_api）。
    """
    if not cfg:
        return "openai"
    fmt = str(cfg.get("api_format") or "").strip().lower()
    if fmt in ("anthropic", "openai"):
        return fmt
    return "anthropic" if "/anthropic" in str(cfg.get("base_url") or "") else "openai"


# 设置页常见粘贴错误：把文档里的完整端点当 base_url 填（如 OpenCode Zen 文档
# 的 https://opencode.ai/zen/go/v1/messages）。按格式剥掉端点后缀还原出 SDK
# 需要的 base：openai 保留 /v1（SDK 会拼 /chat/completions），anthropic 回到
# 根（SDK 会拼 /v1/messages）。
_OPENAI_ENDPOINT_SUFFIXES = ("/chat/completions", "/messages", "/responses")
_ANTHROPIC_ENDPOINT_SUFFIXES = ("/v1/messages", "/messages")

_DEFAULT_CHAT_BASE_URL = "https://api.deepseek.com"


def normalize_base_url(base_url: str | None, fmt: str) -> str:
    """把"完整端点地址"还原为 SDK 需要的 base_url（防御用户粘贴错误）。"""
    base = (base_url or _DEFAULT_CHAT_BASE_URL).rstrip("/")
    suffixes = (
        _ANTHROPIC_ENDPOINT_SUFFIXES if fmt == "anthropic" else _OPENAI_ENDPOINT_SUFFIXES
    )
    for suffix in suffixes:
        if base.endswith(suffix):
            base = base[: -len(suffix)].rstrip("/")
            break
    return base or _DEFAULT_CHAT_BASE_URL


def provider_extra_headers(cfg: dict | None, base_url: str) -> dict:
    """按供应商生成额外的请求头。

    OpenCode Zen/Go 网关要求客户端自报身份（https://opencode.ai/docs/go）：
    自定义 User-Agent + 会话级稳定 `x-opencode-session`，缺失后者直接 400
    （MissingSessionID）。会话 ID 按 (api_key, base_url) 稳定派生——进程重启
    不变，网关侧可跨轮复用路由与提示词缓存；其他供应商返回空 dict 不受影响。
    """
    if not cfg or "opencode.ai/zen" not in base_url:
        return {}
    seed = f"{cfg.get('api_key') or ''}|{base_url}"
    session_id = "zhiwen-" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]
    return {"x-opencode-session": session_id, "User-Agent": "zhiwen-rag/1.0"}


def thinking_param(cfg: dict | None) -> dict:
    """Anthropic 格式的 thinking 参数（ChatAnthropic 顶层参数）。

    DeepSeek 兼容端点忽略 budget_tokens（官方兼容表），给名义值即可；
    disabled 在两种格式下都表示关闭思考（辅助调用用）。
    """
    if cfg and cfg.get("thinking_enabled") is False:
        return {"type": "disabled"}
    return {"type": "enabled", "budget_tokens": 4096}


class _AnthropicSystemNormalizer:
    """Anthropic 兼容端点适配层：链中非连续 SystemMessage 降级为 HumanMessage。

    背景（2026-09-18 实测 bug）：langchain_anthropic 的 _format_messages 只允许
    一个 system 消息（出现第二个即抛 "Received multiple non-consecutive system
    messages"）。而本项目的纯追加消息链会把 merge 摘要、计划进度等 system 行
    追加在链中（问题/工具消息之后），导致 Anthropic 格式下带子代理的任务全崩。

    处理：请求构造时保留"首个 system 段"，其后出现的 SystemMessage 按
    **位置确定性**转为 HumanMessage（内容不变）——
    - 转换规则只取决于"是否在首个 system 段之后"，同一条消息每轮转换结果一致，
      发往 provider 的请求仍是逐轮稳定的纯追加链，前缀缓存不受影响；
    - 本地落库的消息链不做任何改动（finalize 仍按原样重建）；
    - OpenAI 格式不经过本适配层。
    """

    def __init__(self, inner):
        self._inner = inner

    @staticmethod
    def _normalize(messages):
        if not isinstance(messages, list):
            return messages
        from langchain_core.messages import HumanMessage, SystemMessage

        out = []
        seen_non_system = False
        converted = False
        for m in messages:
            if isinstance(m, SystemMessage):
                if seen_non_system:
                    out.append(HumanMessage(content=m.content))
                    converted = True
                    continue
            else:
                seen_non_system = True
            out.append(m)
        if converted:
            messages = out
        return _AnthropicSystemNormalizer._inject_thinking(messages)

    @staticmethod
    def _inject_thinking(messages):
        """把工具轮暂存的 Anthropic 思考块还原进 content。

        langchain-anthropic 不认 OpenAI 风格的 `reasoning_content`，只认 content 里的
        `{"type": "thinking"}` 块；启用思考时带 tool_use 的 assistant 消息必须把思考
        原样回传（含 signature），否则 DeepSeek /anthropic 端点报
        "The `content[].thinking` in the thinking mode must be passed back"（2026-09-26 实测）。

        纯函数 → 同一输入每轮转换结果一致，不破坏前缀缓存的"纯追加"不变量；
        只作用于发往 provider 的请求，不改动本地落库链路。
        """
        from langchain_core.messages import AIMessage

        out = []
        changed = False
        for m in messages:
            blocks = None
            if isinstance(m, AIMessage) and getattr(m, "tool_calls", None):
                blocks = (getattr(m, "additional_kwargs", {}) or {}).get(
                    "anthropic_thinking"
                )
            if not blocks:
                out.append(m)
                continue
            content = m.content
            if isinstance(content, list) and any(
                isinstance(b, dict) and b.get("type") == "thinking"
                for b in content
            ):
                out.append(m)  # 已有思考块：不重复注入
                continue
            if isinstance(content, str):
                tail = (
                    [{"type": "text", "text": content}] if content.strip() else []
                )
            elif isinstance(content, list):
                tail = list(content)
            else:
                tail = []
            out.append(
                m.model_copy(update={"content": [dict(b) for b in blocks] + tail})
            )
            changed = True
        return out if changed else messages

    def invoke(self, input, config=None, **kwargs):
        return self._inner.invoke(self._normalize(input), config=config, **kwargs)

    def stream(self, input, config=None, **kwargs):
        return self._inner.stream(self._normalize(input), config=config, **kwargs)

    async def ainvoke(self, input, config=None, **kwargs):
        return await self._inner.ainvoke(self._normalize(input), config=config, **kwargs)

    def astream(self, input, config=None, **kwargs):
        return self._inner.astream(self._normalize(input), config=config, **kwargs)

    def bind_tools(self, tools, **kwargs):
        return _AnthropicSystemNormalizer(self._inner.bind_tools(tools, **kwargs))

    def __getattr__(self, name):
        return getattr(self._inner, name)


def _fallback_sync_client(*, base_url: str | None = None, timeout=None, anthropic_proxy: str | None = None):
    """带系统代理回退的 httpx2 客户端。

    注意用 httpx2：本环境 anthropic SDK 校验 http_client 必须是 httpx2.Client
    （httpx 的 Client 会被拒收："this SDK uses httpx2"）。
    anthropic 显式配了代理时尊重显式配置（不走回退）。
    """
    from .network import make_httpx2_client

    if anthropic_proxy:
        import httpx2

        kwargs = {"proxy": anthropic_proxy}
        if timeout is not None:
            kwargs["timeout"] = timeout
        return httpx2.Client(**kwargs)
    kwargs = {}
    if base_url:
        kwargs["base_url"] = base_url
    if timeout is not None:
        kwargs["timeout"] = timeout
    return make_httpx2_client(**kwargs)


def _fallback_async_client(*, base_url: str | None = None, timeout=None, anthropic_proxy: str | None = None):
    """带系统代理回退的异步 httpx2 客户端。"""
    from .network import make_async_httpx2_client

    if anthropic_proxy:
        import httpx2

        kwargs = {"proxy": anthropic_proxy}
        if timeout is not None:
            kwargs["timeout"] = timeout
        return httpx2.AsyncClient(**kwargs)
    kwargs = {}
    if base_url:
        kwargs["base_url"] = base_url
    if timeout is not None:
        kwargs["timeout"] = timeout
    return make_async_httpx2_client(**kwargs)


_fallback_chat_anthropic_cls = None


def _fallback_chat_anthropic_class():
    """ChatAnthropic 子类：同步/异步客户端都走带代理回退的工厂（惰性构建一次）。

    2026-09-18 实测教训：langchain_anthropic 自建 httpx 客户端，构造时捕获
    系统代理（Clash 关闭后所有模型调用报 "Connection error." 且重试无效）；
    其余 5 处出站调用早已走 network.make_httpx_client，chat 模型是遗漏路径。
    """
    global _fallback_chat_anthropic_cls
    if _fallback_chat_anthropic_cls is None:
        from functools import cached_property

        from langchain_anthropic import ChatAnthropic

        class _FallbackChatAnthropic(ChatAnthropic):
            @cached_property
            def _client(self):  # type: ignore[override]
                import anthropic

                params = self._client_params
                http_params = {"base_url": params["base_url"]}
                if "timeout" in params:
                    http_params["timeout"] = params["timeout"]
                if self.anthropic_proxy:
                    http_params["anthropic_proxy"] = self.anthropic_proxy
                return anthropic.Client(
                    **{**params, "http_client": _fallback_sync_client(**http_params)}
                )

            @cached_property
            def _async_client(self):  # type: ignore[override]
                import anthropic

                params = self._client_params
                http_params = {"base_url": params["base_url"]}
                if "timeout" in params:
                    http_params["timeout"] = params["timeout"]
                if self.anthropic_proxy:
                    http_params["anthropic_proxy"] = self.anthropic_proxy
                return anthropic.AsyncClient(
                    **{**params, "http_client": _fallback_async_client(**http_params)}
                )

        _fallback_chat_anthropic_cls = _FallbackChatAnthropic
    return _fallback_chat_anthropic_cls


def build_chat_model(
    cfg: dict,
    *,
    model: str | None = None,
    temperature: float | None = None,
    timeout: float = 180,
    max_retries: int = 2,
    max_tokens: int | None = None,
    thinking: dict | None = None,
    extra_body: dict | None = None,
):
    """按供应商 API 格式构建对话模型。

    - openai 格式：ChatOpenAI（思考配置走 extra_body）；
    - anthropic 格式：ChatAnthropic（思考配置走顶层 thinking 参数；
      max_tokens 必填且**思考 token 计入**，默认 32000）。
      不能设小：思考模式下 output 会被 reasoning 吃光，正文一个字都出不来
      ——实测 8192 时整轮 answer_len=0（HTTP 200、无异常，纯被截断）。
      Claude Code 用同一模型单次输出实测峰值 16,517 tokens 未撞限，
      32000 留足余量。

    注意：Anthropic 兼容端点上 DeepSeek 仍是**自动前缀缓存**，cache_control
    断点被忽略（官方兼容表）；命中量体现在 usage 的 cache_read_input_tokens，
    与 OpenAI 格式的 prompt_cache_hit_tokens 等价。
    """
    fmt = provider_api_format(cfg)
    base_url = normalize_base_url(cfg.get("base_url"), fmt)
    # 供应商自定义请求头覆盖内置自动头（如 OpenCode 会话头），坏条目在构建点再兜一次底
    extra_headers = {
        **provider_extra_headers(cfg, base_url),
        **_clean_headers(cfg.get("headers")),
    }
    model_name = model or cfg.get("model") or "deepseek-v4-flash"
    temp = temperature if temperature is not None else 0.5
    if fmt == "anthropic":
        temp = min(temp, 1.0)  # Anthropic 协议温度上界是 1，超界直接 400
        kwargs: dict = dict(
            model=model_name,
            api_key=cfg.get("api_key") or "",
            base_url=base_url,
            temperature=temp,
            timeout=timeout,
            max_retries=max_retries,
            max_tokens=max_tokens or 32000,
        )
        if thinking is not None:
            kwargs["thinking"] = thinking
        if extra_headers:
            kwargs["default_headers"] = extra_headers
        # 用带回退客户端的子类（Clash 关闭后不再 "Connection error."）
        return _AnthropicSystemNormalizer(_fallback_chat_anthropic_class()(**kwargs))
    from langchain_openai import ChatOpenAI

    from .network import make_async_httpx_client, make_httpx_client

    kwargs = dict(
        api_key=cfg.get("api_key"),
        base_url=base_url,
        model=model_name,
        temperature=temp,
        request_timeout=timeout,
        max_retries=max_retries,
        extra_body=extra_body,
        http_client=make_httpx_client(),
        http_async_client=make_async_httpx_client(),
    )
    if extra_headers:
        kwargs["default_headers"] = extra_headers
    if max_tokens:
        kwargs["max_tokens"] = max_tokens
    return ChatOpenAI(**kwargs)


def mask_key(value: str | None) -> str | None:
    """脱敏展示 API Key：只留后 4 位。"""
    if not value:
        return None
    value = str(value)
    if len(value) <= 8:
        return "****"
    return "****" + value[-4:]


# ============================================================
# 供应商解析（cc-switch 式多供应商）
# ============================================================


def _seed_providers(settings) -> list[dict]:
    """首次使用（或未保存过 providers）时，从旧键值/环境变量播种供应商。"""
    providers: list[dict] = []
    chat_key = effective(settings, "deepseek_api_key")
    chat_model = effective(settings, "deepseek_model")
    if chat_key or chat_model:
        models = [m.strip() for m in chat_model.split(",") if m.strip()]
        providers.append(
            {
                "id": "deepseek",
                "name": "DeepSeek",
                "type": "chat",
                "base_url": effective(settings, "deepseek_base_url"),
                "api_key": chat_key,
                "model": models[0] if models else "",
                "models": models or ([chat_model] if chat_model else []),
                "enabled": True,
                "note": "对话模型（OpenAI 兼容）",
            }
        )
    vision_key = effective(settings, "sensenova_api_key")
    vision_model = effective(settings, "sensenova_model")
    if vision_key:
        providers.append(
            {
                "id": "sensenova",
                "name": "日日新 SenseNova（Token Plan）",
                "type": "vision",
                "base_url": effective(settings, "sensenova_base_url"),
                "api_key": vision_key,
                "model": vision_model,
                "models": [vision_model] if vision_model else [],
                "enabled": True,
                "note": "视觉/多模态模型",
            }
        )
    return providers


def get_providers(settings) -> list[dict]:
    """返回当前供应商列表（内部含明文 API Key，仅服务端使用）。"""
    overrides = get_overrides()
    providers = overrides.get("providers")
    if isinstance(providers, list) and providers:
        return providers
    return _seed_providers(settings)


def get_active_provider(settings, ptype: str) -> dict | None:
    """返回当前激活的供应商（chat|vision），找不到时回退旧键值。"""
    overrides = get_overrides()
    active_key = (
        "active_chat_provider" if ptype == "chat" else "active_vision_provider"
    )
    active_id = overrides.get(active_key)
    providers = get_providers(settings)
    if active_id:
        for p in providers:
            if p.get("type") == ptype and p.get("id") == active_id and p.get("enabled", True):
                return p
    for p in providers:
        if p.get("type") == ptype and p.get("enabled", True):
            return p
    # 旧键值回退（供应商列表为空时）
    if ptype == "chat" and effective(settings, "deepseek_api_key"):
        model = effective(settings, "deepseek_model")
        return {
            "id": "deepseek",
            "name": "DeepSeek",
            "type": "chat",
            "base_url": effective(settings, "deepseek_base_url"),
            "api_key": effective(settings, "deepseek_api_key"),
            "model": model,
            "models": [model] if model else [],
            "enabled": True,
        }
    if ptype == "vision" and effective(settings, "sensenova_api_key"):
        model = effective(settings, "sensenova_model")
        return {
            "id": "sensenova",
            "name": "日日新 SenseNova",
            "type": "vision",
            "base_url": effective(settings, "sensenova_base_url"),
            "api_key": effective(settings, "sensenova_api_key"),
            "model": model,
            "models": [model] if model else [],
            "enabled": True,
        }
    return None


def chat_provider_config(settings) -> dict | None:
    """对话模型供应商配置：{api_key, base_url, model}。"""
    return get_active_provider(settings, "chat")


def vision_provider_config(settings) -> dict | None:
    """视觉模型供应商配置：{api_key, base_url, model}。"""
    return get_active_provider(settings, "vision")
