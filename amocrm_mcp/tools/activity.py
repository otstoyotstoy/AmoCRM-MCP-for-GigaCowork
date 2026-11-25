"""Примечания, связи между сущностями, история событий, звонки."""
import uuid
from collections import defaultdict
from typing import Any, Literal, Optional, Union

from pydantic import BaseModel, Field

from .. import refs
from ..client import AmoError, amo
from .. import safety
from ..core import out, tool
from ..timeutil import to_ts
from .common import date_range, listing

Ref = Optional[Union[int, str]]
EntityT = Literal["leads", "contacts", "companies", "customers"]


# ------------------------------------------------------------------ примечания

@tool("notes", title="Примечания")
async def get_notes(entity_type: EntityT, entity_id: Optional[int] = None,
                    note_types: Optional[list[str]] = Field(None, description="common, call_in, call_out, service_message, sms_in, sms_out, attachment …"),
                    updated_from: Ref = None, updated_to: Ref = None,
                    page: int = 1, limit: int = 50, fetch_all: bool = False, max_items: int = 500) -> str:
    """Примечания (комментарии, звонки, системные) одной сущности или всех сущностей типа за период."""
    p: dict[str, Any] = {"order[updated_at]": "desc"}
    if note_types:
        p["filter[note_type][]"] = note_types
    date_range(p, "updated_at", updated_from, updated_to)
    path = f"/{entity_type}/{entity_id}/notes" if entity_id else f"/{entity_type}/notes"
    c = await refs.ctx()
    return await listing(path, "notes", p, lambda e: refs.note(e, c), page=page, limit=limit,
                         fetch_all=fetch_all, max_items=max_items)


class NoteIn(BaseModel):
    entity_type: EntityT = "leads"
    entity_id: int
    text: Optional[str] = None
    note_type: Literal["common", "call_in", "call_out", "service_message", "sms_in", "sms_out"] = "common"
    params: Optional[dict[str, Any]] = Field(None, description="Доп. параметры: для звонков uniq, duration, source, link, phone; для service_message service")


@tool("notes", write=True, title="Добавить примечания")
async def add_notes(notes: list[NoteIn]) -> str:
    """Добавить примечания к сделкам/контактам/компаниям (по одному или пачкой)."""
    groups: dict[str, list] = defaultdict(list)
    for n in notes:
        params = dict(n.params or {})
        if n.text is not None:
            params["text"] = n.text
        if n.note_type in ("call_in", "call_out"):
            params.setdefault("uniq", str(uuid.uuid4()))
            params.setdefault("duration", 0)
            params.setdefault("source", "MCP")
            params.setdefault("phone", "")
        if n.note_type == "service_message":
            params.setdefault("service", "MCP")
        groups[n.entity_type].append({"entity_id": n.entity_id, "note_type": n.note_type, "params": params})
    total = {"ok": True, "created": 0, "ids": [], "op_ids": []}
    for et, body in groups.items():
        res = await safety.apply_create(tool="add_notes", entity=f"{et}_notes", bodies=body,
                                        args={"entity_type": et, "notes": body}, url=f"/{et}/notes",
                                        result_key="notes")
        total["created"] += res.get("created", 0)
        total["ids"] += res.get("ids", [])
        total["op_ids"].append(res.get("op_id"))
        for k in ("error", "note", "duplicate_call"):
            if res.get(k):
                total[k] = res[k]
                total["ok"] = total["ok"] and k == "duplicate_call"
    return out(total)


@tool("notes", write=True, title="Изменить примечание")
async def update_note(entity_type: EntityT, note_id: int, text: str) -> str:
    """Изменить текст обычного примечания (common). Прежний текст сохраняется в журнале."""
    safety.ensure_audit_writable()
    old = await amo.get(f"/{entity_type}/notes/{note_id}")
    if not old:
        raise AmoError(404, f"Примечание {note_id} не найдено")
    if old.get("note_type") != "common":
        raise AmoError(400, "Изменять можно только обычные примечания (common), не звонки и не системные")
    await amo.patch(f"/{entity_type}/notes/{note_id}", {"note_type": "common", "params": {"text": text}})
    op_id = safety.audit("update", "update_note", f"{entity_type}_notes", ids=[note_id],
                         before=[safety._strip(old)], args={"text": text})
    return out({"ok": True, "op_id": op_id, "previous_text": (old.get("params") or {}).get("text")})


# ------------------------------------------------------------------ связи

class LinkIn(BaseModel):
    to_entity_type: Literal["leads", "contacts", "companies", "customers", "catalog_elements"]
    to_entity_id: int
    is_main: Optional[bool] = Field(None, description="Основной контакт сделки/компании")
    catalog_id: Optional[int] = Field(None, description="Для catalog_elements — ID списка/каталога")
    quantity: Optional[float] = Field(None, description="Для товаров — количество")


def _link_body(links: list[LinkIn]) -> list[dict]:
    res = []
    for l in links:
        md = {k: v for k, v in {"main_contact": l.is_main, "catalog_id": l.catalog_id, "quantity": l.quantity}.items() if v is not None}
        res.append({"to_entity_id": l.to_entity_id, "to_entity_type": l.to_entity_type, **({"metadata": md} if md else {})})
    return res


@tool("links", title="Связи сущности")
async def get_links(entity_type: EntityT, entity_id: int,
                    to_entity_type: Optional[Literal["leads", "contacts", "companies", "customers", "catalog_elements"]] = None) -> str:
    """С чем связана сущность: контакты, компании, сделки, товары (элементы списков)."""
    p = {}
    data = await amo.get(f"/{entity_type}/{entity_id}/links", p)
    items = ((data or {}).get("_embedded") or {}).get("links") or []
    if to_entity_type:
        items = [x for x in items if x.get("to_entity_type") == to_entity_type]
    return out({"count": len(items), "items": [{k: v for k, v in x.items() if k != "_links"} for x in items]})


@tool("links", write=True, title="Связать сущности")
async def link_entities(entity_type: EntityT, entity_id: int, links: list[LinkIn]) -> str:
    """Привязать к сделке/контакту/компании другие сущности: контакт к сделке, компанию к контакту, товар к сделке."""
    safety.ensure_audit_writable()
    body = _link_body(links)
    await amo.post(f"/{entity_type}/{entity_id}/link", body)
    op_id = safety.audit("link", "link_entities", entity_type, ids=[entity_id], args={"links": body})
    return out({"ok": True, "linked": len(links), "op_id": op_id, "undo": f"undo_change(op_id='{op_id}')"})


@tool("links", write=True, destructive=True, title="Отвязать сущности")
async def unlink_entities(entity_type: EntityT, entity_id: int, links: list[LinkIn]) -> str:
    """Отвязать сущности (например, убрать контакт из сделки). Сами записи не удаляются."""
    safety.ensure_audit_writable()
    current = await amo.get(f"/{entity_type}/{entity_id}/links")
    cur = ((current or {}).get("_embedded") or {}).get("links") or []
    body = _link_body(links)
    keep_meta = []
    for b in body:
        m = next((c for c in cur if c.get("to_entity_id") == b["to_entity_id"]
                  and c.get("to_entity_type") == b["to_entity_type"]), None)
        if not m:
            raise AmoError(404, f"Связи с {b['to_entity_type']} {b['to_entity_id']} нет — отвязывать нечего. Ничего не изменено.")
        keep_meta.append({"to_entity_id": m["to_entity_id"], "to_entity_type": m["to_entity_type"],
                          **({"metadata": m["metadata"]} if m.get("metadata") else {})})
    await amo.post(f"/{entity_type}/{entity_id}/unlink", body)
    op_id = safety.audit("unlink", "unlink_entities", entity_type, ids=[entity_id], before=keep_meta,
                         args={"links": body})
    return out({"ok": True, "unlinked": len(links), "op_id": op_id, "undo": f"undo_change(op_id='{op_id}')",
                "note": "Сами записи не удалены, только связь."})


# ------------------------------------------------------------------ события

_EV_ENTITY = {"leads": "lead", "contacts": "contact", "companies": "company", "customers": "customer", "tasks": "task"}


@tool("events", title="История событий")
async def get_events(entity_type: Optional[Literal["leads", "contacts", "companies", "customers", "tasks"]] = None,
                     entity_ids: Optional[list[int]] = None,
                     types: Optional[list[str]] = Field(None, description="lead_added, lead_status_changed, entity_responsible_changed, entity_tag_added, incoming_call, outgoing_call, incoming_chat_message, outgoing_chat_message, task_completed, custom_field_<ID>_value_changed …"),
                     created_by: Optional[list[Union[int, str]]] = None,
                     created_from: Ref = None, created_to: Ref = None,
                     page: int = 1, limit: int = 100, fetch_all: bool = False, max_items: int = 500) -> str:
    """Журнал событий: смены этапов, ответственных, полей, звонки, сообщения, теги — кто и когда.
    Полезно для анализа движения сделок и активности менеджеров."""
    p: dict[str, Any] = {}
    if entity_type:
        p["filter[entity][]"] = [_EV_ENTITY[entity_type]]
    if entity_ids:
        p["filter[entity_id][]"] = entity_ids
    if types:
        p["filter[type][]"] = types
    if created_by:
        p["filter[created_by][]"] = [await refs.user_id(x) for x in created_by]
    date_range(p, "created_at", created_from, created_to)
    c = await refs.ctx(need_leads=True)

    def fmt(e):
        def val(v):
            if not v:
                return v
            res = []
            for x in v:
                if "lead_status" in x:
                    s = x["lead_status"]
                    res.append({"pipeline": c.pipelines.get(s.get("pipeline_id")),
                                "status": c.statuses.get((s.get("pipeline_id"), s.get("id")), s.get("id"))})
                elif "responsible_user" in x:
                    res.append({"responsible": c.user(x["responsible_user"].get("id"))})
                else:
                    res.append(x)
            return res
        from ..timeutil import fmt as f
        return {"id": e.get("id"), "type": e.get("type"), "entity_type": e.get("entity_type"),
                "entity_id": e.get("entity_id"), "created_by": c.user(e.get("created_by")) or e.get("created_by"),
                "created_at": f(e.get("created_at")), "before": val(e.get("value_before")), "after": val(e.get("value_after"))}

    return await listing("/events", "events", p, fmt, page=page, limit=min(limit, 100), fetch_all=fetch_all,
                         max_items=max_items)


# ------------------------------------------------------------------ звонки

class CallIn(BaseModel):
    direction: Literal["inbound", "outbound"]
    phone: str
    duration_sec: int = 0
    source: str = "MCP"
    link: Optional[str] = Field(None, description="Ссылка на запись разговора")
    call_status: Optional[int] = Field(None, description="1 оставил сообщение, 2 перезвонить, 3 нет на месте, 4 разговор состоялся, 5 неверный номер, 6 не дозвонился, 7 занято")
    call_result: Optional[str] = None
    responsible: Ref = None
    created_at: Ref = None


@tool("calls", write=True, title="Добавить звонки")
async def add_calls(calls: list[CallIn]) -> str:
    """Записать звонки в amoCRM: система сама найдёт контакт по номеру и прикрепит звонок к нему/его сделке."""
    body = []
    for x in calls:
        b = {"direction": x.direction, "phone": x.phone, "duration": x.duration_sec, "source": x.source,
             "uniq": None}
        if x.link:
            b["link"] = x.link
        if x.call_status:
            b["call_status"] = x.call_status
        if x.call_result:
            b["call_result"] = x.call_result
        if x.responsible is not None:
            b["responsible_user_id"] = await refs.user_id(x.responsible)
        if x.created_at is not None:
            b["created_at"] = to_ts(x.created_at)
        body.append(b)
    import hashlib
    for b in body:  # детерминированный uniq: повтор того же звонка не создаст дубль и в самой amoCRM
        b["uniq"] = str(uuid.UUID(hashlib.md5(safety._canon({k: v for k, v in b.items() if k != "uniq"}).encode()).hexdigest()))
    res = await safety.apply_create(tool="add_calls", entity="calls", bodies=body, args={"calls": body},
                                    url="/calls", result_key="calls")
    return out(res)
