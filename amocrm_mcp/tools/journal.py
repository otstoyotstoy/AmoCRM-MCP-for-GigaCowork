"""Журнал изменений и отмена операций."""
from typing import Any, Optional

from .. import safety
from ..client import AmoError, amo
from ..core import out, tool

_SCALAR = ("name", "price", "status_id", "pipeline_id", "responsible_user_id", "loss_reason_id", "closed_at",
           "first_name", "last_name", "text", "complete_till", "task_type_id", "entity_id", "entity_type",
           "duration", "next_price", "next_date", "periodicity")
_PATCHABLE = {"leads": "/leads", "contacts": "/contacts", "companies": "/companies", "tasks": "/tasks",
              "customers": "/customers"}


@tool("journal", title="Журнал изменений")
async def get_change_log(limit: int = 20, op_id: Optional[str] = None, tool_name: Optional[str] = None) -> str:
    """Что меняли через этот сервер: когда, каким инструментом, какие записи, были ли ошибки.
    op_id — подробности одной операции (включая значения «до»). По op_id можно отменить: undo_change."""
    recs = safety.read_audit(limit=1 if op_id else max(1, min(limit, 200)) * (3 if tool_name else 1), op_id=op_id)
    if tool_name:
        recs = [r for r in recs if r.get("tool") == tool_name][:limit]
    if op_id:
        if not recs:
            raise AmoError(404, f"Операция {op_id} не найдена в журнале")
        r = dict(recs[0])
        before = r.get("before") or []
        r["before"] = before[:20]
        if len(before) > 20:
            r["before_truncated"] = len(before)
        return out(r)
    return out({"count": len(recs), "items": [{
        "op_id": r.get("op_id"), "time": r.get("time"), "tool": r.get("tool"), "op": r.get("op"),
        "entity": r.get("entity"), "count": len(r.get("ids") or []), "ids": (r.get("ids") or [])[:10],
        **({"error": r["error"]} if r.get("error") else {})} for r in recs]})


def _cf_restore(before: dict, fid: int) -> dict:
    old = next((c for c in before.get("custom_fields_values") or [] if c.get("field_id") == fid), None)
    if not old or not old.get("values"):
        return {"field_id": fid, "values": None}
    vals = []
    for v in old["values"]:
        if v.get("enum_id") and old.get("field_type") not in ("multitext",):
            vals.append({"enum_id": v["enum_id"]})
        elif v.get("enum_code"):
            vals.append({"value": v.get("value"), "enum_code": v["enum_code"]})
        else:
            vals.append({"value": v.get("value")})
    return {"field_id": fid, "values": vals}


def _restore_body(entity: str, before: dict, touched: dict) -> dict:
    keys = set(touched.get("keys") or [])
    b: dict[str, Any] = {"id": before["id"]}
    for k in _SCALAR:
        if k in keys and before.get(k) is not None:
            b[k] = before[k]
    if "status_id" in keys and "pipeline_id" not in b and before.get("pipeline_id"):
        b["pipeline_id"] = before["pipeline_id"]
    if keys & {"is_completed", "result"}:
        b["is_completed"] = bool(before.get("is_completed"))
    if touched.get("cf"):
        b["custom_fields_values"] = [_cf_restore(before, fid) for fid in touched["cf"]]
    if "tags" in keys:
        b["_embedded"] = {"tags": [{"id": t["id"]} for t in (before.get("_embedded") or {}).get("tags") or []
                                   if t.get("id")]}
    return b


@tool("journal", write=True, title="Отменить операцию")
async def undo_change(op_id: str, confirm_token: Optional[str] = None) -> str:
    """Отменить операцию из журнала (op_id приходит в ответе каждого изменения): вернуть прежние значения
    сделок/контактов/компаний/задач/покупателей, отменить привязку или отвязку. Всегда в два шага:
    сначала предпросмотр (и предупреждение, если записи с тех пор меняли), затем повтор с confirm_token.
    Созданные записи через API удалить нельзя — для них будет подсказка (например, закрыть сделку)."""
    recs = safety.read_audit(op_id=op_id)
    if not recs:
        raise AmoError(404, f"Операция {op_id} не найдена в журнале")
    r = recs[0]
    op, entity, args = r.get("op"), r.get("entity"), {"op_id": op_id}

    if op == "create":
        hint = {"leads": "закройте их: close_leads(outcome='lost', loss_reason=…)",
                "tasks": "отметьте выполненными: complete_tasks"}.get(entity, "удалите вручную в интерфейсе amoCRM")
        return out({"ok": False, "reversible": False, "ids": r.get("ids"),
                    "note": f"Созданные записи API amoCRM удалить не позволяет — {hint}."})
    if op in ("link", "unlink"):
        links = (r.get("args") or {}).get("links") or []
        if op == "unlink":
            links = r.get("before") or links
        target = "unlink" if op == "link" else "link"
        eid = (r.get("ids") or [None])[0]
        if not safety.is_confirmed("undo_change", args, confirm_token):
            return out(safety.need_confirmation("undo_change", args,
                                                f"Отмена операции {op_id}: {target} для {entity} {eid}", links))
        safety.ensure_audit_writable()
        await amo.post(f"/{entity}/{eid}/{target}", links)
        new_op = safety.audit(target, "undo_change", entity, ids=[eid], args={"undo_of": op_id, "links": links})
        return out({"ok": True, "undone": op_id, "op_id": new_op})
    if op != "update" or entity not in _PATCHABLE:
        return out({"ok": False, "reversible": False,
                    "note": f"Операцию типа «{op}» для «{entity}» автоматически отменить нельзя. "
                            "Значения «до» доступны в get_change_log(op_id=…)."})

    befores = [b for b in r.get("before") or [] if b.get("id") in set(r.get("ids") or [])]
    if not befores:
        return out({"ok": False, "note": "В операции нет применённых изменений — отменять нечего."})
    bodies = [_restore_body(entity, b, r.get("touched") or {}) for b in befores]
    current = await safety.snapshot(entity, [b["id"] for b in befores])
    drift = [i for i, c in current.items() if (c.get("updated_at") or 0) > (r.get("ts") or 0) + 5]
    gone = [b["id"] for b in befores if b["id"] not in current]
    if gone:
        bodies = [b for b in bodies if b["id"] not in gone]
    reason = f"Отмена операции {op_id} ({r.get('tool')}, {len(bodies)} записей)"
    if drift:
        reason += f". ВНИМАНИЕ: {len(drift)} записей меняли после этой операции — их более поздние правки " \
                  f"в тех же полях будут перезаписаны (ids: {drift[:20]})"
    res = await safety.apply_patch(tool="undo_change", entity=entity, bodies=bodies, before=current,
                                   args=args, confirm_token_value=confirm_token, force_confirm=reason,
                                   path=_PATCHABLE[entity], limit=max(len(bodies), 1))
    if gone:
        res["skipped_missing"] = gone
    res["undo_of"] = op_id
    return out(res)
