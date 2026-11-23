"""写后回读与 mutation 记账。

在 tools 节点里，一个 mutating 工具成功返回后，可选地直接调用它声明的
read-back 工具，把"库里现在的真实状态"压缩进该次 ToolMessage；同时生成一条
mutation ledger 记录，供 finalize 做答复核对。

约束：
- 只在并行写全部完成后由 outcome loop 顺序调用，不改工具数组、不产生模型
  可见的 tool_call；
- 回读失败绝不影响原 mutation；
- 只保留契约白名单字段，块大小有上限（默认 900 字符）。
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Callable

from .tool_contracts import ToolContract

logger = logging.getLogger(__name__)

_VISIBLE_KEYS = {
    "reservation_id",
    "order_id",
    "item_id",
    "product_id",
    "payment_id",
    "flight_number",
    "date",
    "cabin",
    "status",
    "name",
    "amount",
    "total",
    "price",
    "refund",
    "balance",
    "reason",
    "user_id",
    "address1",
    "address2",
    "city",
    "state",
    "zip",
    "country",
    "first_name",
    "last_name",
    "dob",
}
_MAX_LIST_ITEMS = 10
_VERIFIED_LIMIT = 900


def _deep_get(obj: Any, path: str) -> Any:
    """按点路径取值；遇到 list 时把剩余路径映射到每个元素并拍平。"""
    if not path:
        return None
    parts = path.split(".")
    cur = obj
    for idx, part in enumerate(parts):
        if isinstance(cur, dict):
            cur = cur.get(part)
        elif isinstance(cur, (list, tuple)):
            rest = ".".join(parts[idx:])
            out = [_deep_get(item, rest) for item in cur]
            return [x for x in out if x not in (None, "", [], {})]
        else:
            return None
        if cur is None:
            return None
    return cur


def _compact_value(value: Any, depth: int = 0) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value[:160]
    if depth >= 3:
        return str(value)[:120]
    if isinstance(value, dict):
        out = {}
        for key, val in value.items():
            if key in _VISIBLE_KEYS:
                out[key] = _compact_value(val, depth + 1)
        return out
    if isinstance(value, (list, tuple)):
        return [_compact_value(v, depth + 1) for v in list(value)[:_MAX_LIST_ITEMS]]
    return str(value)[:120]


def extract_verified(contract: ToolContract, payload: dict) -> dict:
    """从回读结果中提取白名单字段；没有声明字段时至少带 status。"""
    fields = list(contract.verified_fields) or ["status"]
    out: dict[str, Any] = {"ok": True}
    for field in fields:
        value = _deep_get(payload, field)
        if value not in (None, "", [], {}):
            out[field] = _compact_value(value)
    status = _deep_get(payload, "status")
    if status not in (None, "") and "status" not in out:
        out["status"] = _compact_value(status)
    return out


def readback_spec(
    contract: ToolContract, args: dict, result: dict
) -> tuple[str, dict, str] | None:
    """返回 (readback_tool, readback_args, target)，不可回读时 None。"""
    if not contract.readback_tool:
        return None
    target = args.get(contract.target_arg) if contract.target_arg else None
    if target in (None, "") and contract.target_from_result:
        target = _deep_get(result, contract.target_from_result)
    if isinstance(target, list):
        target = target[0] if target else None
    if target in (None, ""):
        return None
    read_arg = contract.readback_arg or contract.target_arg
    if not read_arg:
        return None
    if contract.readback_from_result:
        value = _deep_get(result, contract.target_from_result or "")
    else:
        value = args.get(read_arg)
    if isinstance(value, list):
        value = value[0] if value else None
    if value in (None, ""):
        value = target
    if value in (None, ""):
        return None
    return contract.readback_tool, {read_arg: value}, str(target)


def _add_claim(
    claims: list[dict],
    seen: set[tuple[str, str]],
    kind: str,
    value: Any,
    label: str,
    tool: str,
    target: str,
) -> None:
    if value is None or isinstance(value, bool):
        return
    text = str(value).strip()
    if not text:
        return
    key = (kind, text.casefold())
    if key in seen:
        return
    seen.add(key)
    claims.append(
        {
            "kind": kind,
            "value": text,
            "label": label,
            "tool": tool,
            "target": target,
        }
    )


def _amount_text(value: Any) -> str | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number == 0:
        return None
    return f"{abs(number):.2f}"


def _claims_from_payload(
    contract: ToolContract,
    args: dict,
    result: dict,
    payload: dict,
    tool: str,
    target: str,
) -> list[dict]:
    claims: list[dict] = []
    seen: set[tuple[str, str]] = set()

    # 目标 id
    target_value = args.get(contract.target_arg) if contract.target_arg else None
    if target_value in (None, "") and contract.target_from_result:
        target_value = _deep_get(result, contract.target_from_result)
    _add_claim(
        claims, seen, "id", target_value, contract.target_arg or "target", tool, target
    )

    # 显式要求答复里出现的 id 参数
    for key in contract.answer_id_args:
        _add_claim(claims, seen, "id", args.get(key), key, tool, target)

    # 金额型参数（如 send_certificate 的 amount）
    for key, value in args.items():
        if "amount" in key.casefold():
            _add_claim(
                claims, seen, "amount", _amount_text(value), "amount", tool, target
            )

    # 回读结果里的关键字段
    for field in contract.answer_result_fields:
        value = _deep_get(payload, field)
        if value in (None, "", [], {}):
            continue
        if field in ("payment_history",) and isinstance(value, list):
            for entry in value[:_MAX_LIST_ITEMS]:
                if not isinstance(entry, dict):
                    continue
                pid = entry.get("payment_id") or entry.get("id")
                amount = _amount_text(entry.get("amount"))
                if pid and amount:
                    # 合并成"完整 ID + 金额"一条：答复核对时两者必须同时出现，
                    # 避免模型只写后四位或只写金额导致 NL 断言不通过。
                    _add_claim(
                        claims,
                        seen,
                        "payment",
                        f"{pid} ${amount}",
                        "payment",
                        tool,
                        target,
                    )
                elif pid:
                    _add_claim(
                        claims, seen, "id", pid, "payment", tool, target
                    )
                if amount:
                    _add_claim(
                        claims, seen, "amount", amount, "amount", tool, target
                    )
            continue
        if field in ("items", "flights") and isinstance(value, list):
            for entry in value[:_MAX_LIST_ITEMS]:
                if not isinstance(entry, dict):
                    continue
                _add_claim(
                    claims,
                    seen,
                    "id",
                    entry.get("item_id") or entry.get("flight_number"),
                    "item" if field == "items" else "flight",
                    tool,
                    target,
                )
            continue
        if isinstance(value, list):
            for item in value[:_MAX_LIST_ITEMS]:
                _add_claim(claims, seen, "id", item, field, tool, target)
        else:
            _add_claim(claims, seen, "id", value, field, tool, target)

    # 状态：真实 status 值 + 期望关键词
    status = _deep_get(payload, "status")
    if status not in (None, ""):
        _add_claim(claims, seen, "status", status, "status", tool, target)
        status_text = str(status).casefold()
        for keyword in contract.status_keywords:
            if keyword.casefold() in status_text:
                _add_claim(
                    claims, seen, "status", keyword, "status", tool, target
                )

    # 兜底金额：payment_history 之外的 total/refund/amount
    if not any(c["kind"] == "amount" for c in claims):
        if contract.amount_mode == "refund_sum":
            refund = _deep_get(payload, "refund")
            _add_claim(
                claims, seen, "amount", _amount_text(refund), "refund", tool, target
            )
        else:
            for key in ("total", "amount", "price", "balance"):
                value = _deep_get(payload, key)
                amount = _amount_text(value)
                if amount:
                    _add_claim(
                        claims, seen, "amount", amount, key, tool, target
                    )
                    break

    return claims


def augment_mutation(
    *,
    name: str,
    args: dict,
    result: dict,
    tools_by_name: dict[str, Any],
    contract: ToolContract | None,
    is_sensitive: Callable[[str, dict], bool] | None = None,
    enabled: bool = True,
) -> dict | None:
    """执行写后回读并返回 ledger entry；非 mutating/失败调用返回 None。"""
    if not enabled or contract is None or not contract.mutating:
        return None
    if not isinstance(result, dict) or result.get("error"):
        return None

    spec = readback_spec(contract, args, result)
    target = spec[2] if spec else ""
    if not target and contract.target_from_result:
        value = _deep_get(result, contract.target_from_result)
        target = str(value) if value not in (None, "") else ""

    verified: dict | None = None
    payload = result
    if spec is not None:
        readback_name, readback_args, _ = spec
        readback_tool = tools_by_name.get(readback_name)
        if readback_tool is None:
            verified = {"ok": False, "reason": "readback_tool_missing"}
        elif is_sensitive is not None and is_sensitive(
            readback_name, readback_args
        ):
            verified = {"ok": False, "reason": "readback_sensitive"}
        else:
            started = time.perf_counter()
            try:
                read_result = readback_tool.invoke(readback_args)
                if not isinstance(read_result, dict):
                    read_result = {"result": read_result}
                if read_result.get("error"):
                    verified = {
                        "ok": False,
                        "error": str(read_result.get("error"))[:200],
                    }
                else:
                    verified = extract_verified(contract, read_result)
                    payload = read_result
            except Exception as exc:  # noqa: BLE001 - 回读失败不能影响 mutation
                verified = {
                    "ok": False,
                    "error": f"{type(exc).__name__}: {exc}"[:200],
                }
            verified["readback_ms"] = round(
                (time.perf_counter() - started) * 1000
            )

    claims = _claims_from_payload(contract, args, result, payload, name, target)
    return {
        "tool": name,
        "target": target,
        "ok": True,
        "verified": verified,
        "claims": claims,
    }


def compact_verified(blob: dict, limit: int = _VERIFIED_LIMIT) -> dict:
    """把 verified 块压到 limit 字符内：优先保留 ok/status，再按体积丢字段。"""
    if not isinstance(blob, dict):
        return {"ok": False, "preview": str(blob)[:limit]}
    try:
        text = json.dumps(blob, ensure_ascii=False)
    except Exception:
        return {"ok": False, "preview": str(blob)[:limit]}
    if len(text) <= limit:
        return blob
    kept = dict(blob)
    priority = {"ok": 0, "status": 1}
    for key in sorted(
        list(kept),
        key=lambda k: (priority.get(k, 2), -len(str(kept.get(k)))),
        reverse=False,
    ):
        if key in ("ok", "status"):
            continue
        kept.pop(key, None)
        try:
            if len(json.dumps(kept, ensure_ascii=False)) <= limit:
                return kept
        except Exception:
            break
    kept["_omitted"] = "verified 块过大，已省略部分字段"
    preview = json.dumps(kept, ensure_ascii=False)
    if len(preview) > limit:
        kept = {
            "ok": blob.get("ok"),
            "preview": preview[: max(0, limit - 40)],
            "_omitted": True,
        }
    return kept
