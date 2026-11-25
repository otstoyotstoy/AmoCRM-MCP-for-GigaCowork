"""Неразобранное, покупатели, списки (каталоги/товары)."""
import time
from typing import Any, Literal, Optional, Union

from pydantic import BaseModel, Field

from .. import refs
from ..client import AmoError, amo
from ..config import cfg
from .. import safety
from ..core import out, tool
from ..timeutil import fmt, to_ts
from .common import ExportFormat, date_range, listing
from .leads import person_body

Ref = Optional[Union[int, str]]
CF = Optional[dict[str, Any]]


# ------------------------------------------------------------------ неразобранное

def _unsorted(e: dict) -> dict:
    emb = e.get("_embedded") or {}
    return {"uid": e.get("uid"), "category": e.get("category"), "pipeline_id": e.get("pipeline_id"),
            "source_name": e.get("source_name"), "created_at": fmt(e.get("created_at")),
            "lead_ids": [x["id"] for x in emb.get("leads") or []],
            "contact_ids": [x["id"] for x in emb.get("contacts") or []],
            "metadata": e.get("metadata")}


@tool("unsorted", title="Неразобранное")
async def list_unsorted(category: Optional[list[Literal["sip", "mail", "forms", "chats"]]] = None,
                        pipeline: Ref = None, page: int = 1, limit: int = 50, fetch_all: bool = False) -> str:
    """Заявки в «Неразобранном»: звонки, письма, формы, чаты, ожидающие разбора."""
    p: dict[str, Any] = {"order[created_at]": "desc"}
    if category:
        p["filter[category][]"] = category
    if pipeline is not None:
        p["filter[pipeline_id]"] = await refs.pipeline_id(pipeline)
    return await listing("/leads/unsorted", "unsorted", p, _unsorted, page=page, limit=limit, fetch_all=fetch_all)


@tool("unsorted", write=True, destructive=True, title="Разобрать неразобранное")
async def process_unsorted(uids: list[str], action: Literal["accept", "decline"],
                           user: Ref = None, status: Ref = None, pipeline: Ref = None,
                           confirm_token: Optional[str] = None) -> str:
    """Принять заявки из «Неразобранного» (создаются сделка и контакт, можно сразу на этап status)
    или отклонить их (decline — заявка удаляется безвозвратно, поэтому всегда в два шага:
    сначала предпросмотр и confirm_token, затем повтор с токеном)."""
    uids = list(dict.fromkeys(uids))
    safety.check_limit(len(uids), "заявок")
    safety.ensure_audit_writable()
    snap = []
    for uid in uids:
        try:
            d = await amo.get(f"/leads/unsorted/{uid}")
        except AmoError:
            d = None
        if not d:
            raise AmoError(404, f"Заявка {uid} не найдена в «Неразобранном». Ничего не изменено.")
        snap.append(safety._strip(d))
    args = {"uids": uids, "action": action, "user": user, "status": status, "pipeline": pipeline}
    need = (f"Отклонение удалит {len(uids)} заявок безвозвратно" if action == "decline"
            else (f"Принятие {len(uids)} заявок" if len(uids) > cfg.confirm_threshold else None))
    if need and not safety.is_confirmed("process_unsorted", args, confirm_token):
        return out(safety.need_confirmation("process_unsorted", args, need,
                                            [_unsorted(x) for x in snap[:10]]))
    uid_user = await refs.user_id(user) if user is not None else None
    sid = (await refs.one_status(status, pipeline))[1] if (action == "accept" and status is not None) else None
    done, failed, err = [], [], None
    for uid in uids:
        try:
            if action == "accept":
                body: dict[str, Any] = {}
                if uid_user:
                    body["user_id"] = uid_user
                if sid:
                    body["status_id"] = sid
                await amo.post(f"/leads/unsorted/{uid}/accept", body or None)
            else:
                await amo.delete(f"/leads/unsorted/{uid}/decline", {"user_id": uid_user} if uid_user else None)
            done.append(uid)
        except AmoError as e:
            err = str(e)
            failed = uids[uids.index(uid):]
            break
    op_id = safety.audit(action, "process_unsorted", "unsorted", ids=done, before=snap, args=args,
                         result={"failed": failed}, error=err)
    res = {"ok": not failed, "processed": len(done), "uids": done, "action": action, "op_id": op_id}
    if failed:
        res.update({"failed_uids": failed, "error": err, "note": "Обработаны только заявки из uids."})
    return out(res)


class UnsortedFormIn(BaseModel):
    lead_name: str
    contact_name: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = None
    price: Optional[int] = None
    form_name: str = "MCP"
    form_page: str = "mcp"
    pipeline: Ref = None
    lead_custom_fields: CF = None


@tool("unsorted", write=True, title="Создать заявку в неразобранном")
async def create_unsorted_forms(forms: list[UnsortedFormIn]) -> str:
    """Создать заявки в «Неразобранном» (как с формы сайта) — менеджеры разберут их вручную."""
    body = []
    now = int(time.time())
    for f in forms:
        lead: dict[str, Any] = {"name": f.lead_name}
        if f.price:
            lead["price"] = f.price
        cf = await refs.cf_to_api("leads", f.lead_custom_fields)
        if cf:
            lead["custom_fields_values"] = cf
        item: dict[str, Any] = {
            "source_name": f.form_name, "source_uid": f"mcp-{now}-{len(body)}", "created_at": now,
            "metadata": {"form_id": "mcp", "form_name": f.form_name, "form_page": f.form_page,
                         "form_sent_at": now, "ip": "127.0.0.1", "referer": f.form_page},
            "_embedded": {"leads": [lead]},
        }
        if f.pipeline is not None:
            item["pipeline_id"] = await refs.pipeline_id(f.pipeline)
        if f.contact_name or f.phone or f.email:
            from .leads import ContactLite
            item["_embedded"]["contacts"] = [await person_body("contacts", ContactLite(
                name=f.contact_name or f.phone or f.email, phone=f.phone, email=f.email))]
        body.append(item)
    for b in body:  # source_uid зависит от содержимого — повтор не создаст дубль
        b["source_uid"] = "mcp-" + safety.confirm_token("unsorted_form", {k: v for k, v in b.items()
                                                                         if k not in ("created_at", "metadata", "source_uid")})
    res = await safety.apply_create(tool="create_unsorted_forms", entity="unsorted", bodies=body,
                                    args={"forms": [f.model_dump(exclude_none=True) for f in forms]},
                                    url="/leads/unsorted/forms", result_key="unsorted")
    return out(res)


# ------------------------------------------------------------------ покупатели

@tool("customers", title="Поиск покупателей")
async def search_customers(query: Optional[str] = None, ids: Optional[list[int]] = None,
                           responsible: Optional[list[Union[int, str]]] = None,
                           next_date_from: Ref = None, next_date_to: Ref = None,
                           page: int = 1, limit: int = 50, fetch_all: bool = False, max_items: int = 500,
                           export_format: ExportFormat = None) -> str:
    """Покупатели (режим «Покупатели» в amoCRM должен быть включён): поиск, ожидаемые покупки по дате."""
    p: dict[str, Any] = {"with": "contacts,companies"}
    if query:
        p["query"] = query
    if ids:
        p["filter[id][]"] = ids
    if responsible:
        p["filter[responsible_user_id][]"] = [await refs.user_id(x) for x in responsible]
    date_range(p, "next_date", next_date_from, next_date_to)
    c = await refs.ctx()
    return await listing("/customers", "customers", p, lambda e: refs.customer(e, c), page=page, limit=limit,
                         fetch_all=fetch_all, max_items=max_items, export_format=export_format, export_name="customers")


class CustomerIn(BaseModel):
    id: Optional[int] = Field(None, description="Есть id — обновить, нет — создать")
    name: Optional[str] = None
    next_price: Optional[int] = None
    next_date: Ref = None
    responsible: Ref = None
    status_id: Optional[int] = None
    periodicity: Optional[int] = Field(None, description="Периодичность покупок в днях")
    tags_to_add: Optional[list[str]] = None
    custom_fields: CF = None


@tool("customers", write=True, title="Создать/изменить покупателей")
async def save_customers(customers: list[CustomerIn], confirm_token: Optional[str] = None) -> str:
    """Создать новых или изменить существующих покупателей (с id). Теги добавляются к существующим.
    Изменения записываются в журнал."""
    new, upd = [], []
    for x in customers:
        b: dict[str, Any] = {}
        for k in ("id", "name", "next_price", "status_id", "periodicity"):
            v = getattr(x, k)
            if v is not None:
                b[k] = v
        if x.next_date is not None:
            b["next_date"] = to_ts(x.next_date)
        if x.responsible is not None:
            b["responsible_user_id"] = await refs.user_id(x.responsible)
        cf = await refs.cf_to_api("customers", x.custom_fields)
        if cf:
            b["custom_fields_values"] = cf
        if x.tags_to_add:
            b["_tags_to_add"] = x.tags_to_add
        (upd if x.id else new).append(b)
    res: dict[str, Any] = {"ok": True}
    if upd:
        before = await safety.snapshot("customers", [b["id"] for b in upd])
        for b in upd:  # теги ДОБАВЛЯЮТСЯ к существующим, а не заменяют их
            add = b.pop("_tags_to_add", None)
            if add:
                old = [t.get("name") for t in ((before.get(b["id"]) or {}).get("_embedded") or {}).get("tags") or []]
                b["_embedded"] = {"tags": refs.tags_payload(list(dict.fromkeys(old + add)))}
        r = await safety.apply_patch(tool="save_customers", entity="customers", bodies=upd, before=before,
                                     args={"update": upd}, confirm_token_value=confirm_token)
        if r.get("needs_confirmation"):
            return out(r)
        res["updated"] = r
    if new:
        for b in new:
            add = b.pop("_tags_to_add", None)
            if add:
                b["_embedded"] = {"tags": refs.tags_payload(add)}
        res["created"] = await safety.apply_create(tool="save_customers", entity="customers", bodies=new,
                                                   args={"create": new}, url="/customers", result_key="customers")
    return out(res)


# ------------------------------------------------------------------ списки / каталоги / товары

@tool("catalogs", title="Списки (каталоги)")
async def list_catalogs() -> str:
    """Списки аккаунта: товары, услуги, файлы и пользовательские списки."""
    items, _ = await amo.list_all("/catalogs", "catalogs")
    return out({"count": len(items), "items": [{"id": x["id"], "name": x.get("name"), "type": x.get("type"),
                                                "can_add_elements": x.get("can_add_elements")} for x in items]})


@tool("catalogs", title="Элементы списка")
async def search_catalog_elements(catalog_id: int, query: Optional[str] = None, ids: Optional[list[int]] = None,
                                  page: int = 1, limit: int = 50, fetch_all: bool = False, max_items: int = 500,
                                  export_format: ExportFormat = None) -> str:
    """Элементы списка (например, товары с ценой и артикулом)."""
    p: dict[str, Any] = {}
    if query:
        p["query"] = query
    if ids:
        p["filter[id][]"] = ids
    c = await refs.ctx()
    return await listing(f"/catalogs/{catalog_id}/elements", "elements", p, lambda e: refs.catalog_element(e, c),
                         page=page, limit=limit, fetch_all=fetch_all, max_items=max_items,
                         export_format=export_format, export_name=f"catalog_{catalog_id}")


class ElementIn(BaseModel):
    id: Optional[int] = Field(None, description="Есть id — обновить, нет — создать")
    name: Optional[str] = None
    custom_fields: CF = Field(None, description="Например {'Цена': 1500, 'Артикул': 'A-1'}")


@tool("catalogs", write=True, title="Создать/изменить элементы списка")
async def save_catalog_elements(catalog_id: int, elements: list[ElementIn],
                                confirm_token: Optional[str] = None) -> str:
    """Добавить новые или изменить существующие элементы списка (товары и т.п.). Изменения — в журнал."""
    new, upd = [], []
    for x in elements:
        b: dict[str, Any] = {}
        if x.id:
            b["id"] = x.id
        if x.name:
            b["name"] = x.name
        cf = await refs.cf_to_api(f"catalogs/{catalog_id}", x.custom_fields)
        if cf:
            b["custom_fields_values"] = cf
        (upd if x.id else new).append(b)
    res: dict[str, Any] = {"ok": True}
    if upd:
        r = await safety.apply_patch(tool="save_catalog_elements", entity=f"catalogs/{catalog_id}/elements",
                                     bodies=upd, args={"catalog_id": catalog_id, "update": upd},
                                     confirm_token_value=confirm_token,
                                     path=f"/catalogs/{catalog_id}/elements",
                                     snap_path=f"/catalogs/{catalog_id}/elements", snap_key="elements")
        if r.get("needs_confirmation"):
            return out(r)
        res["updated"] = r
    if new:
        res["created"] = await safety.apply_create(tool="save_catalog_elements", entity=f"catalogs/{catalog_id}/elements",
                                                   bodies=new, args={"catalog_id": catalog_id, "create": new},
                                                   url=f"/catalogs/{catalog_id}/elements", result_key="elements")
    return out(res)
