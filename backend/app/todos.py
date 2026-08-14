"""TodoWrite 式任务清单：按会话持久化，跨轮跟踪长任务进度。

设计（对齐 Claude Code 的 TodoWrite 思路）：
- 每个会话一张任务清单，存 MySQL app_meta（键 todos_{conversation_id}）；
- 复杂问题规划后自动播种：plan_steps -> 任务项（source=plan，未完成）；
- Agent 可调用 todo_update 工具增删改查（list/add/complete/remove/set），
  跨工具调用轮次保持进度；set 支持整体修订清单（计划变更、步骤重排）；
- 计划是硬约束：tools 节点按执行进度自动同步已完成步骤（source=plan 的项），
  agent 未完成剩余工具型步骤前不允许收尾；纯推理/总结类步骤在收尾时自动补完成；
- 前端把计划卡片渲染成可勾选清单，点击勾选即调用 API 更新。
"""

from __future__ import annotations

import json
import logging
import time
import uuid

from langchain_core.tools import StructuredTool
from sqlalchemy.orm import Session

from .db import repository as repo

logger = logging.getLogger(__name__)

META_PREFIX = "todos_"


def _key(conversation_id: int) -> str:
    return f"{META_PREFIX}{conversation_id}"


def load_todos(db: Session | None, conversation_id: int | None) -> list[dict]:
    """读取会话任务清单；无会话或失败时返回空列表。"""
    if db is None or not conversation_id:
        return []
    try:
        # 工具在线程池中并行执行时，请求级 Session 非线程安全：
        # 优先用独立短会话读取（todo_update 也是独立会话写库，天然读到最新），
        # 避免并发把 MySQL 连接搞坏（Packet sequence number wrong）
        local = None
        try:
            from .db.database import SessionLocal, db_ready

            if db_ready and SessionLocal is not None:
                local = SessionLocal()
        except Exception:
            pass
        session = local if local is not None else db
        # 请求级 Session 可能缓存了旧 AppMeta 行（todo_update 用独立会话写库），
        # 读前强制过期，保证拿到最新清单
        if local is None:
            try:
                session.expire_all()
            except Exception:
                pass
        try:
            raw = repo.get_meta(session, _key(conversation_id))
        finally:
            if local is not None:
                try:
                    local.close()
                except Exception:
                    pass
        if not raw:
            return []
        data = json.loads(raw)
        return data if isinstance(data, list) else []
    except Exception as exc:
        logger.warning("读取任务清单失败：%s", exc)
        return []


def save_todos(db: Session | None, conversation_id: int | None, items: list[dict]) -> None:
    if db is None or not conversation_id:
        return
    try:
        repo.set_meta(db, _key(conversation_id), json.dumps(items, ensure_ascii=False))
    except Exception as exc:
        logger.warning("保存任务清单失败：%s", exc)


def seed_todos_from_plan(
    db: Session | None,
    conversation_id: int | None,
    plan_steps: list[str],
) -> list[dict]:
    """首次规划时播种任务清单；已有清单则不覆盖（支持跨轮追加）。"""
    items = load_todos(db, conversation_id)
    if items or not plan_steps:
        return items
    now = time.time()
    items = [
        {
            "id": f"todo_{uuid.uuid4().hex[:8]}",
            "text": str(step).strip()[:200],
            "done": False,
            "source": "plan",
            "step_index": idx,
            "created_at": now,
            "updated_at": now,
        }
        for idx, step in enumerate(plan_steps)
        if str(step).strip()
    ]
    save_todos(db, conversation_id, items)
    return items


def refresh_todos_for_plan(
    db: Session | None,
    conversation_id: int | None,
    plan_steps: list[str],
) -> list[dict]:
    """每轮 prepare 时把任务清单对齐到当前计划，避免上一轮清单串台。

    - 无新计划：上一轮清单若已全部完成则清空（不再回显旧任务）；
      仍有未完成项则保留（支持跨轮续做同一任务）。
    - 有新计划：与现有 plan 项一致则沿用；不一致则用新计划替换 plan 项，
      保留 manual 项（模型/用户在过程中手动追加的工作）。
    """
    items = load_todos(db, conversation_id)
    if not plan_steps:
        if items and all(i.get("done") for i in items):
            save_todos(db, conversation_id, [])
            return []
        return items

    has_source = any(i.get("source") == "plan" for i in items)
    plan_items = [i for i in items if i.get("source") == "plan"] if has_source else items
    new_texts = [str(s).strip() for s in plan_steps if str(s).strip()]
    if plan_items and [str(i.get("text", "")).strip() for i in plan_items] == new_texts:
        return items  # 同一计划：继续沿用现有进度

    now = time.time()
    new_plan = [
        {
            "id": f"todo_{uuid.uuid4().hex[:8]}",
            "text": str(step).strip()[:200],
            "done": False,
            "source": "plan",
            "step_index": idx,
            "created_at": now,
            "updated_at": now,
        }
        for idx, step in enumerate(new_texts)
    ]
    manual = [] if not has_source else [i for i in items if i.get("source") != "plan"]
    merged = new_plan + manual
    save_todos(db, conversation_id, merged)
    return merged


def todos_to_text(items: list[dict] | None) -> str:
    """把任务清单格式化成给模型看的文本（带序号与勾选状态）。"""
    items = items or []
    if not items:
        return ""
    return "\n".join(
        f"{i}. {'[x]' if t.get('done') else '[ ]'} {str(t.get('text', '')).strip()}"
        for i, t in enumerate(items, 1)
    )


def plan_progress(items: list[dict] | None) -> tuple[int, int, list[dict]]:
    """计算计划进度：返回 (已完成数, 计划步骤总数, 剩余计划步骤)。

    只统计 source=plan 的任务项（用户/模型手动新增的 manual 项不计入计划进度）；
    旧数据完全没有 source 字段时，整张清单按计划步骤处理（顺序兼容）；
    清单全部是 manual 项时表示计划已被修订/清空，进度为 0/0。
    """
    items = items or []
    sources = {i.get("source") for i in items}
    if "plan" in sources:
        plan_items = [i for i in items if i.get("source") == "plan"]
    elif all(not i.get("source") for i in items):
        plan_items = items  # 旧数据：无 source 字段
    else:
        plan_items = []
    done = sum(1 for i in plan_items if i.get("done"))
    remaining = [i for i in plan_items if not i.get("done")]
    return done, len(plan_items), remaining


def sync_todos_from_plan(
    db: Session | None,
    conversation_id: int | None,
    plan_steps: list[str],
    done_count: int,
    hints: list[str] | None = None,
) -> list[dict]:
    """按计划执行进度自动标记已完成步骤（硬约束同步，不依赖模型自觉）。

    done_count：前 N 个计划步骤视为已完成。source=plan 且 step_index < done_count
    的未完成项会被标记为 done；hints 提供每步的工具提示（非空=工具型步骤），
    仅工具型步骤会被自动标记，纯推理/总结步骤留给收尾补完成；
    旧数据无 source 时按列表顺序前 N 项处理。
    """
    items = load_todos(db, conversation_id)
    if not items or not plan_steps:
        return items
    done_count = max(0, min(int(done_count or 0), len(plan_steps)))
    sources = {i.get("source") for i in items}
    if "plan" in sources:
        use_plan_source = True
    elif all(not i.get("source") for i in items):
        use_plan_source = False  # 旧数据：无 source 字段，按顺序处理
    else:
        use_plan_source = None  # 无计划项（全部 manual）：不自动标记
    changed = False
    for idx, item in enumerate(items):
        if item.get("done"):
            continue
        if use_plan_source is True:
            raw_idx = item.get("step_index")
            step_idx = int(raw_idx) if raw_idx is not None else -1
            if hints is not None:
                hint = hints[step_idx] if 0 <= step_idx < len(hints) else ""
            else:
                hint = ""
            match = (
                item.get("source") == "plan"
                and step_idx < done_count
                and (hints is None or bool(hint))
            )
        elif use_plan_source is False:
            match = idx < done_count
        else:
            match = False
        if match:
            item["done"] = True
            item["completed_at"] = time.time()
            changed = True
    if changed:
        save_todos(db, conversation_id, items)
    return items


def complete_steps_by_text(
    db: Session | None,
    conversation_id: int | None,
    texts: list[str],
) -> list[dict]:
    """按文本匹配标记任务为完成（用于收尾时补完成纯推理/总结类步骤）。"""
    texts = {str(t).strip() for t in (texts or []) if str(t).strip()}
    items = load_todos(db, conversation_id)
    if not items or not texts:
        return items
    changed = False
    for item in items:
        if not item.get("done") and str(item.get("text", "")).strip() in texts:
            item["done"] = True
            item["completed_at"] = time.time()
            changed = True
    if changed:
        save_todos(db, conversation_id, items)
    return items


def make_todo_tool(db: Session | None, conversation_id: int | None):
    """任务清单工具：list / add / complete / remove / set，跨轮跟踪任务进度。"""

    def _invoke(
        operation: str,
        text: str = "",
        task_id: str = "",
        tasks: list | None = None,
    ) -> dict:
        # 工具在线程池中并行执行：请求级 Session 非线程安全，
        # 每个操作使用独立短会话，避免并发把 MySQL 连接搞坏
        local = None
        try:
            from .db.database import SessionLocal, db_ready

            if db_ready and SessionLocal is not None:
                local = SessionLocal()
            items = load_todos(local, conversation_id)
            op = (operation or "list").strip().lower()
            if op == "list":
                return {
                    "summary": f"当前任务清单 {len(items)} 项",
                    "todos": items,
                }
            if op == "add":
                if not text.strip():
                    return {"error": "参数缺失：add 需要 text", "summary": "新增失败：缺少任务内容"}
                now = time.time()
                items.append(
                    {
                        "id": f"todo_{uuid.uuid4().hex[:8]}",
                        "text": text.strip()[:200],
                        "done": False,
                        "source": "manual",
                        "created_at": now,
                        "updated_at": now,
                    }
                )
                save_todos(local, conversation_id, items)
                return {"summary": f"已新增任务：{text.strip()[:40]}", "todos": items}
            if op == "set":
                if tasks is None:
                    return {
                        "error": "参数缺失：set 需要 tasks 数组",
                        "summary": "修订失败：缺少 tasks 数组",
                    }
                seen: set[str] = set()
                normalized: list[dict] = []
                now = time.time()
                for raw in tasks:
                    if isinstance(raw, str):
                        raw = {"text": raw}
                    if not isinstance(raw, dict):
                        continue
                    txt = str(raw.get("text") or "").strip()[:200]
                    if not txt or txt in seen:
                        continue
                    seen.add(txt)
                    old = next((x for x in items if x.get("text") == txt), None)
                    old = old or {}
                    normalized.append(
                        {
                            "id": old.get("id") or f"todo_{uuid.uuid4().hex[:8]}",
                            "text": txt,
                            "done": bool(raw.get("done")),
                            "source": old.get("source") or "manual",
                            "step_index": old.get("step_index"),
                            "created_at": old.get("created_at") or now,
                            "updated_at": now,
                        }
                    )
                save_todos(local, conversation_id, normalized)
                return {
                    "summary": f"已整体修订任务清单（{len(normalized)} 项）",
                    "todos": normalized,
                }
            if op in ("complete", "done"):
                target_text = text.strip()
                # 支持数字序号：task_id 为纯数字时按清单顺序匹配第 N 项（1-based），
                # 兼容模型把"步骤 1"写成 task_id="1" 的常见情况
                for item in items:
                    if item.get("id") == task_id or item.get("text") == target_text:
                        item["done"] = True
                        item["completed_at"] = time.time()
                        save_todos(local, conversation_id, items)
                        return {"summary": f"已完成任务：{item['text'][:40]}", "todos": items}
                if str(task_id).strip().isdigit():
                    idx = int(str(task_id).strip()) - 1
                    if 0 <= idx < len(items):
                        item = items[idx]
                        item["done"] = True
                        item["completed_at"] = time.time()
                        save_todos(local, conversation_id, items)
                        return {"summary": f"已完成任务：{item['text'][:40]}", "todos": items}
                # 模糊匹配：唯一包含关系也视为命中，减少模型"任务不存在"的空转
                fuzzy = [
                    i
                    for i in items
                    if target_text
                    and (
                        target_text in str(i.get("text", ""))
                        or str(i.get("text", "")) in target_text
                    )
                ]
                if len(fuzzy) == 1:
                    item = fuzzy[0]
                    item["done"] = True
                    item["completed_at"] = time.time()
                    save_todos(local, conversation_id, items)
                    return {"summary": f"已完成任务：{item['text'][:40]}", "todos": items}
                return {"error": f"未找到任务：{task_id or text}", "summary": "标记失败：任务不存在"}
            if op in ("remove", "delete"):
                before = len(items)
                if str(task_id).strip().isdigit():
                    idx = int(str(task_id).strip()) - 1
                    if 0 <= idx < len(items):
                        removed = items.pop(idx)
                        save_todos(local, conversation_id, items)
                        return {
                            "summary": f"已删除 1 项任务：{removed.get('text', '')[:40]}（剩余 {len(items)}）",
                            "todos": items,
                        }
                items = [i for i in items if i.get("id") != task_id and i.get("text") != text.strip()]
                if len(items) == before:
                    return {"error": f"未找到任务：{task_id or text}", "summary": "删除失败：任务不存在"}
                save_todos(local, conversation_id, items)
                return {"summary": f"已删除 1 项任务（剩余 {len(items)}）", "todos": items}
            return {"error": f"不支持的操作：{operation}", "summary": f"不支持 {operation}"}
        finally:
            if local is not None:
                try:
                    local.close()
                except Exception:
                    pass

    return StructuredTool.from_function(
        func=_invoke,
        name="todo_update",
        description=(
            "维护当前会话的任务清单（TodoWrite 式，计划硬约束）：list 查看、"
            "add 新增、complete 标记完成、remove 删除、set 整体修订（tasks 传完整数组）。"
            "长任务每完成一步就 complete 对应任务；计划未完成前不要输出最终回答；"
            "某步确实无需执行时 remove 并说明原因，不要假装完成。"
        ),
        args_schema=None,
    )
