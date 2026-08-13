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

import json
import logging
import threading

logger = logging.getLogger(__name__)

# 允许用户在设置页修改的键
EDITABLE_KEYS = {
    # 旧式对话模型键值（兼容保留，新前端走 providers）
    "deepseek_api_key",
    "deepseek_base_url",
    "deepseek_model",
    "agent_title_model",
    "chat_temperature",
    "main_model_vision",
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
    # 技能总开关
    "skills_enabled",
    # 高级工具（P0：受控执行 / MCP / 技能沙箱 / 思考摘要）
    "advanced_tools_enabled",
    "agent_subagents_enabled",
    "agent_subagent_max_rounds",
    "verify_command",
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
    "checkpoint_enabled",
    "trajectory_compress_enabled",
}

# 只读展示键
READONLY_KEYS = {
    "embedding_model_dir",
    "reranker_cache_dir",
    "embedding_fp16",
    "embed_batch_size",
    "opensearch_url",
    "opensearch_index",
    "history_max_messages",
    "history_max_tokens",
}

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
            cleaned = _validate_providers(value, settings=settings)
            if cleaned:
                clean[key] = cleaned
            continue
        if key in ("active_chat_provider", "active_vision_provider"):
            clean[key] = str(value).strip()
            continue
        if key.endswith("api_key") or key == "tavily_api_key":
            value = str(value).strip()
            if not value:
                continue  # 空串表示不修改
        elif key == "chat_temperature":
            value = max(0.0, min(2.0, float(value)))
        elif key == "web_search_max_results":
            value = max(1, min(20, int(value)))
        elif key in ("main_model_vision", "skills_enabled"):
            value = bool(value)
        elif key == "tool_permission_mode":
            value = str(value).strip()
            if value not in ("ask", "allow"):
                continue
        elif key == "permission_timeout":
            value = max(30, min(3600, int(value)))
        clean[key] = value

    with _lock:
        _overrides.update(clean)
        _sync_legacy_to_providers()
        _fix_active_provider_ids()
        merged = dict(_overrides)
    repo.set_meta(db, "runtime_overrides", json.dumps(merged, ensure_ascii=False))
    return merged


def _legacy_keys(ptype: str) -> tuple[str, str, str]:
    """返回 (api_key 键, base_url 键, model 键)。"""
    if ptype == "vision":
        return "sensenova_api_key", "sensenova_base_url", "sensenova_model"
    return "deepseek_api_key", "deepseek_base_url", "deepseek_model"


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
            old = prev_by_id.get(pid, {}).get("api_key") or ""
            if not old:
                old = effective(settings, _legacy_keys(ptype)[0]) if settings is not None else ""
            api_key = str(old or "").strip()
        raw_models = raw.get("models") or []
        models = [str(m).strip() for m in raw_models if str(m).strip()]
        model = str(raw.get("model") or "").strip() or (models[0] if models else "")
        cleaned.append(
            {
                "id": pid,
                "name": str(raw.get("name") or "").strip()[:60] or pid,
                "type": ptype,
                "base_url": str(raw.get("base_url") or "").strip()[:300],
                "api_key": api_key,
                "model": model,
                "models": models or ([model] if model else []),
                "enabled": bool(raw.get("enabled", True)),
                "note": str(raw.get("note") or "").strip()[:200],
            }
        )
    return cleaned


def _sync_legacy_to_providers() -> None:
    """旧式 deepseek_*/sensenova_* 键值保存后，同步到对应供应商。"""
    providers = _overrides.get("providers")
    if not isinstance(providers, list):
        return
    changed = False
    for key, (ptype, field) in _LEGACY_TO_PROVIDER.items():
        if key not in _overrides:
            continue
        for p in providers:
            if p.get("type") == ptype:
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


def effective(settings, key: str, default=None):
    """读取生效值：DB 覆盖优先，其次默认值。"""
    overrides = get_overrides()
    if key in overrides:
        return overrides[key]
    if default is not None:
        return default
    return getattr(settings, key, None)


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
