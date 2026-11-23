"""供应商模型列表候选端点（设置页「拉取模型」/ 供应商探测共用）。

下沉到基础设施层的唯一目的是让 `api.settings` 与 `provider_probe` 都向下
依赖它，消除 `api.settings ⇄ provider_probe` 的横跨分层循环依赖。
"""

from __future__ import annotations

# Anthropic 兼容子路径：剥离后可回到同一厂商的 OpenAI 兼容根端点
# （照搬 cc-switch services/model_fetch.rs 的 strip_compat_suffix 列表，
#  长后缀在前，避免 /anthropic 提前匹配掉 /api/anthropic）
_COMPAT_SUFFIXES = ("/api/anthropic", "/apps/anthropic", "/anthropic")


def models_candidates(cfg: dict) -> list[tuple[str, dict]]:
    """按序生成模型列表候选请求（URL + 鉴权头）。

    1. 格式对应端点：openai → {base}/models（Bearer）；
       anthropic → {base}/v1/models（x-api-key，Anthropic 官方形状）；
    2. 剥离 Anthropic 兼容子路径后的 OpenAI 兼容端点：{root}/models、
       {root}/v1/models——DeepSeek /anthropic 未实现模型列表，但根端点可用
       （cc-switch 的 DeepSeek 预设同样直接指向 https://api.deepseek.com/models）。
    """
    from .runtime_config import normalize_base_url, provider_api_format

    fmt = provider_api_format(cfg)
    base = normalize_base_url(cfg.get("base_url"), fmt)
    api_key = cfg.get("api_key") or ""
    auth = {"Authorization": f"Bearer {api_key}"}
    if fmt == "anthropic":
        candidates = [
            (
                f"{base}/v1/models",
                {"x-api-key": api_key, "anthropic-version": "2023-06-01"},
            )
        ]
    else:
        candidates = [(f"{base}/models", auth)]
    root = base
    for suffix in _COMPAT_SUFFIXES:
        if base.endswith(suffix):
            root = base[: -len(suffix)]
            break
    if root != base:
        candidates.append((f"{root}/models", auth))
        candidates.append((f"{root}/v1/models", auth))
    return candidates
