"""Сделки: поиск с фильтрами, выгрузка, создание, изменение, закрытие, массовые операции."""
from typing import Any, Literal, Optional, Union

from pydantic import BaseModel, Field

from .. import refs
from ..client import AmoError, amo
from ..config import cfg
from .. import safety
from ..core import chunks, out, tool
from .common import ExportFormat, date_range, listing, order

Ref = Optional[Union[int, str]]
CF = Optional[dict[str, Any]]


class LeadFilter(BaseModel):
    query: Optional[str] = Field(None, description="Полнотекстовый поиск (название, поля, контакты)")
    ids: Optional[list[int]] = None
    pipeline: Optional[list[Union[int, str]]] = Field(None, description="Воронки (ID или названия)")
    status: Optional[list[Union[int, str]]] = Field(None, description="Этапы (ID или названия); ограничиваются воронками из pipeline, если заданы")
    result: Optional[Literal["open", "won", "lost", "closed"]] = Field(None, description="open — в работе, won — успешно, lost — не реализовано, closed — won+lost")
    responsible: Optional[list[Union[int, str]]] = Field(None, description="Ответственные (ID, имя или email)")
    created_by: Optional[list[Union[int, str]]] = None
    price_from: Optional[int] = None
    price_to: Optional[int] = None
    created_from: Ref = None
    created_to: Ref = None
    updated_from: Ref = None
    updated_to: Ref = None
    closed_from: Ref = None
    closed_to: Ref = None
    closest_task_from: Ref = None
    closest_task_to: Ref = None
    tags: Optional[list[str]] = Field(None, description="Есть хотя бы один из тегов (фильтруется на стороне сервера MCP)")
    without_tags: Optional[list[str]] = Field(None, description="Нет ни одного из тегов")
    custom_field_equals: Optional[dict[str, Any]] = Field(None, description="{Название поля: значение} — точное совпадение (фильтруется на стороне сервера MCP)")
    include_deleted: bool = Field(False, description="Только удалённые сделки (корзина)")


async def lead_params(f: LeadFilter) -> tuple[dict, Any]:
    p: dict[str, Any] = {"with": "contacts,loss_reason"}
    if f.query:
        p["query"] = f.query
    if f.ids:
        p["filter[id][]"] = f.ids
    if f.responsible:
        p["filter[responsible_user_id][]"] = [await refs.user_id(x) for x in f.responsible]
    if f.created_by:
        p["filter[created_by][]"] = [await refs.user_id(x) for x in f.created_by]
    if f.price_from is not None:
        p["filter[price][from]"] = f.price_from
    if f.price_to is not None:
        p["filter[price][to]"] = f.price_to
    date_range(p, "created_at", f.created_from, f.created_to)
    date_range(p, "updated_at", f.updated_from, f.updated_to)
    date_range(p, "closed_at", f.closed_from, f.closed_to)
    date_range(p, "closest_task_at", f.closest_task_from, f.closest_task_to)
    if f.include_deleted:
        p["with"] += ",only_deleted"

    pipes = [await refs.pipeline_id(x) for x in f.pipeline] if f.pipeline else None
    pairs: list[tuple[int, int]] = []
    if f.status:
        for s in f.status:
            for pp in (pipes or [None]):
                pairs += await refs.status_ids(s, pp)
    if f.result:
        closed = (refs.WON, refs.LOST)
        match = {"won": lambda s: s == refs.WON, "lost": lambda s: s == refs.LOST,
                 "closed": lambda s: s in closed, "open": lambda s: s not in closed}[f.result]
        by_result = [(pl["id"], s["id"]) for pl in await refs.pipelines() if not pipes or pl["id"] in pipes
                     for s in (pl.get("_embedded") or {}).get("statuses") or [] if match(s["id"])]
        pairs = [x for x in pairs if x in by_result] if f.status else by_result
        if not pairs:
            raise AmoError(404, "Под сочетание status и result не подходит ни один этап")
    pairs = list(dict.fromkeys(pairs))
    status_set = None
    if pairs and len(pairs) <= 40:
        for i, (pid, sid) in enumerate(pairs):
            p[f"filter[statuses][{i}][pipeline_id]"] = pid
            p[f"filter[statuses][{i}][status_id]"] = sid
    elif pairs:  # слишком много этапов для URL — фильтруем по воронкам, этапы проверяем сами
        p["filter[pipeline_id][]"] = sorted({x[0] for x in pairs})
        status_set = set(pairs)
    elif pipes:
        p["filter[pipeline_id][]"] = pipes

    pred = None
    if status_set or f.tags or f.without_tags or f.custom_field_equals:
        want = {refs._norm(t) for t in f.tags or []}
        ban = {refs._norm(t) for t in f.without_tags or []}
        cf_eq = {refs._norm(k): v for k, v in (f.custom_field_equals or {}).items()}

        def pred(e: dict) -> bool:
            if status_set and (e.get("pipeline_id"), e.get("status_id")) not in status_set:
                return False
            tags ={refs._norm(t.get("name") or "") for t in (e.get("_embedded") or {}).get("tags") or []}
            if want and not (tags & want):
                return False
            if ban and tags & ban:
                return False
            if cf_eq:
                vals = {}
                for cf in e.get("custom_fields_values") or []:
                    vals[refs._norm(cf.get("field_name") or "")] = [refs._norm(v.get("value")) for v in cf.get("values") or []]
                    vals[str(cf.get("field_id"))] = vals[refs._norm(cf.get("field_name") or "")]
                for k, v in cf_eq.items():
                    if refs._norm(v) not in vals.get(k, []):
                        return False
            return True
    return p, pred


async def _fmt():
    c = await refs.ctx(need_leads=True)
    return lambda e: refs.lead(e, c)


@tool("leads", title="Поиск и выгрузка сделок")
async def search_leads(
    query: Optional[str] = None, ids: Optional[list[int]] = None,
    pipeline: Optional[list[Union[int, str]]] = None, status: Optional[list[Union[int, str]]] = None,
    result: Optional[Literal["open", "won", "lost", "closed"]] = None,
    responsible: Optional[list[Union[int, str]]] = None,
    price_from: Optional[int] = None, price_to: Optional[int] = None,
    created_from: Ref = None, created_to: Ref = None, updated_from: Ref = None, updated_to: Ref = None,
    closed_from: Ref = None, closed_to: Ref = None,
    tags: Optional[list[str]] = None, without_tags: Optional[list[str]] = None,
    custom_field_equals: CF = None, include_deleted: bool = False,
    order_by: Literal["created_at", "updated_at", "id"] = "updated_at", order_dir: Literal["asc", "desc"] = "desc",
    page: int = 1, limit: int = 50, fetch_all: bool = False, max_items: int = 500,
    export_format: ExportFormat = None,
) -> str:
    """Найти сделки по любым условиям: воронка, этап, результат (open/won/lost), ответственный, бюджет,
    даты создания/изменения/закрытия, теги, значения полей, текстовый поиск.
    Постранично (page/limit ≤250), fetch_all=true — все подряд до max_items, export_format — файл со всеми записями.
    Воронки, этапы и ответственных можно указывать названиями."""
    f = LeadFilter(query=query, ids=ids, pipeline=pipeline, status=status, result=result, responsible=responsible,
                   price_from=price_from, price_to=price_to, created_from=created_from, created_to=created_to,
                   updated_from=updated_from, updated_to=updated_to, closed_from=closed_from, closed_to=closed_to,
                   tags=tags, without_tags=without_tags, custom_field_equals=custom_field_equals,
                   include_deleted=include_deleted)
    p, pred = await lead_params(f)
    order(p, order_by, order_dir)
    return await listing("/leads", "leads", p, await _fmt(), page=page, limit=limit, fetch_all=fetch_all,
                         max_items=max_items, export_format=export_format, export_name="leads", predicate=pred)


@tool("leads", title="Карточка сделки")
async def get_lead(lead_id: int, with_notes: bool = False, with_tasks: bool = False) -> str:
    """Полная карточка сделки: поля, контакты (с телефонами), компания, теги; опционально последние примечания и задачи."""
    e = await amo.get(f"/leads/{lead_id}", {"with": "contacts,catalog_elements,loss_reason,source_id"})
    if not e:
        raise AmoError(404, f"Сделка {lead_id} не найдена")
    c = await refs.ctx(need_leads=True)
    r = refs.lead(e, c)
    emb = e.get("_embedded") or {}
    if emb.get("contacts"):
        ids = [x["id"] for x in emb["contacts"]]
        main = next((x["id"] for x in emb["contacts"] if x.get("is_main")), None)
        cs, _ = await amo.list_page("/contacts", "contacts", {"filter[id][]": ids}, limit=50)
        r["contacts"] = [{**refs.contact(x, c), **({"is_main": True} if x["id"] == main else {})} for x in cs]
    if emb.get("companies"):
        co = await amo.get(f"/companies/{emb['companies'][0]['id']}")
        if co:
            r["company"] = refs.company(co, c)
    if emb.get("catalog_elements"):
        r["catalog_elements"] = emb["catalog_elements"]
    if with_notes:
        ns, _ = await amo.list_page(f"/leads/{lead_id}/notes", "notes", {"order[updated_at]": "desc"}, limit=20)
        r["notes"] = [refs.note(n, c) for n in ns]
    if with_tasks:
        ts, _ = await amo.list_page("/tasks", "tasks", {"filter[entity_type]": "leads", "filter[entity_id][]": [lead_id]}, limit=50)
        r["tasks"] = [refs.task(t, c) for t in ts]
    return out(r)


class LeadIn(BaseModel):
    name: Optional[str] = Field(None, description="Название сделки")
    price: Optional[int] = Field(None, description="Бюджет")
    pipeline: Ref = Field(None, description="Воронка (ID или название), по умолчанию основная")
    status: Ref = Field(None, description="Этап (ID или название)")
    responsible: Ref = Field(None, description="Ответственный (ID, имя или email)")
    tags: Optional[list[str]] = None
    custom_fields: CF = Field(None, description="{Название/ID поля: значение}")
    contact_ids: Optional[list[int]] = Field(None, description="Привязать существующие контакты (первый — основной)")
    company_id: Optional[int] = None
    created_at: Ref = None


class LeadUpdate(BaseModel):
    id: int
    name: Optional[str] = None
    price: Optional[int] = None
    pipeline: Ref = Field(None, description="Перенести в воронку")
    status: Ref = Field(None, description="Перевести на этап (ID или название; 'won'/'lost' — закрыть)")
    responsible: Ref = None
    loss_reason: Ref = Field(None, description="Причина отказа (для status=lost)")
    tags_to_add: Optional[list[str]] = None
    tags_to_delete: Optional[list[str]] = None
    tags_replace: Optional[list[str]] = Field(None, description="Заменить все теги этим списком ([] — снять все)")
    custom_fields: CF = None
    closed_at: Ref = None


async def _lead_body(x: BaseModel, is_update: bool, current_pipeline: Optional[int] = None) -> dict:
    """Тело запроса. Для изменения этап ищется в ТЕКУЩЕЙ воронке сделки, если воронка не указана явно —
    чтобы сделку нельзя было случайно перенести в другую воронку с одноимённым этапом."""
    from ..timeutil import to_ts
    b: dict[str, Any] = {}
    d = x.model_dump()
    if is_update:
        b["id"] = d["id"]
    for k in ("name", "price"):
        if d.get(k) is not None:
            b[k] = d[k]
    if d.get("status") is not None:
        explicit = d.get("pipeline") is not None
        scope = d.get("pipeline") if explicit else current_pipeline
        try:
            pid, sid = await refs.one_status(d["status"], scope)
        except AmoError:
            if explicit or scope is None:
                raise
            raise AmoError(400, f"Этап «{d['status']}» не найден в текущей воронке сделки {d.get('id')}. "
                                "Если нужно перенести сделку в другую воронку — укажите pipeline явно.")
        b["status_id"] = sid
        if sid not in (refs.WON, refs.LOST) and (explicit or pid != current_pipeline):
            b["pipeline_id"] = pid
        elif explicit:
            b["pipeline_id"] = pid
    elif d.get("pipeline") is not None:
        b["pipeline_id"] = await refs.pipeline_id(d["pipeline"])
    if d.get("responsible") is not None:
        b["responsible_user_id"] = await refs.user_id(d["responsible"])
    if d.get("loss_reason") is not None:
        b["loss_reason_id"] = await refs.loss_reason_id(d["loss_reason"])
    cf = await refs.cf_to_api("leads", d.get("custom_fields"))
    if cf:
        b["custom_fields_values"] = cf
    for k in ("created_at", "closed_at"):
        if d.get(k) is not None:
            b[k] = to_ts(d[k])
    emb: dict[str, Any] = {}
    if d.get("tags"):
        emb["tags"] = refs.tags_payload(d["tags"])
    if d.get("tags_replace") is not None:
        emb["tags"] = refs.tags_payload(d["tags_replace"]) or []
    if d.get("tags_to_add"):
        b["tags_to_add"] = refs.tags_payload(d["tags_to_add"])
    if d.get("tags_to_delete"):
        b["tags_to_delete"] = refs.tags_payload(d["tags_to_delete"])
    if emb:
        b["_embedded"] = emb
    return b


async def link_after_create(entity: str, eid: int, contact_ids=None, company_id=None, lead_ids=None) -> None:
    links = []
    for i, cid in enumerate(contact_ids or []):
        links.append({"to_entity_id": cid, "to_entity_type": "contacts",
                      **({"metadata": {"main_contact": True}} if i == 0 and entity == "leads" else {})})
    if company_id:
        links.append({"to_entity_id": company_id, "to_entity_type": "companies"})
    for lid in lead_ids or []:
        links.append({"to_entity_id": lid, "to_entity_type": "leads"})
    if links:
        await amo.post(f"/{entity}/{eid}/link", links)


@tool("leads", write=True, title="Создать сделки")
async def create_leads(leads: list[LeadIn]) -> str:
    """Создать одну или несколько сделок (пачками по 50). Можно сразу привязать существующие контакты/компанию,
    задать этап, ответственного, теги и любые поля. Повторный одинаковый вызов в течение нескольких минут
    дублей не создаёт."""
    bodies = [await _lead_body(l, False) for l in leads]

    async def after(lid, idx, x=None):
        l = leads[idx]
        await link_after_create("leads", lid, l.contact_ids, l.company_id)

    res = await safety.apply_create(tool="create_leads", entity="leads", bodies=bodies,
                                    args={"leads": [l.model_dump(exclude_none=True) for l in leads]},
                                    url="/leads", result_key="leads", after=after)
    return out(res)


class ContactLite(BaseModel):
    name: Optional[str] = None
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    phone: Optional[Union[str, list[str]]] = None
    email: Optional[Union[str, list[str]]] = None
    responsible: Ref = None
    custom_fields: CF = None


class CompanyLite(BaseModel):
    name: str
    phone: Optional[Union[str, list[str]]] = None
    email: Optional[Union[str, list[str]]] = None
    custom_fields: CF = None


class ComplexLeadIn(LeadIn):
    contact: Optional[ContactLite] = Field(None, description="Новый контакт, создаётся вместе со сделкой")
    company: Optional[CompanyLite] = Field(None, description="Новая компания, создаётся вместе со сделкой")


async def person_body(entity: str, x: BaseModel) -> dict:
    d = x.model_dump()
    b: dict[str, Any] = {}
    for k in ("name", "first_name", "last_name"):
        if d.get(k):
            b[k] = d[k]
    if d.get("responsible") is not None:
        b["responsible_user_id"] = await refs.user_id(d["responsible"])
    cf = dict(d.get("custom_fields") or {})
    for code, key in (("PHONE", "phone"), ("EMAIL", "email"), ("WEB", "web"), ("ADDRESS", "address")):
        if d.get(key) is not None:
            cf[code] = d[key]
    vals = await refs.cf_to_api(entity, cf)
    if vals:
        b["custom_fields_values"] = vals
    return b


@tool("leads", write=True, title="Создать сделку с контактом и компанией")
async def create_leads_complex(leads: list[ComplexLeadIn]) -> str:
    """Создать сделки вместе с НОВЫМИ контактом и/или компанией одним запросом (например, заявка с сайта).
    Если контакт уже есть в CRM — используйте create_leads с contact_ids. Повторный одинаковый вызов
    дублей не создаёт."""
    body = []
    for l in leads:
        b = await _lead_body(l, False)
        emb = b.setdefault("_embedded", {})
        if l.contact:
            emb["contacts"] = [await person_body("contacts", l.contact)]
        if l.company:
            emb["companies"] = [await person_body("companies", l.company)]
        body.append(b)
    res = await safety.apply_create(tool="create_leads_complex", entity="leads", bodies=body,
                                    args={"leads": [l.model_dump(exclude_none=True) for l in leads]},
                                    url="/leads/complex", result_key="leads")
    return out(res)


@tool("leads", write=True, title="Изменить сделки")
async def update_leads(leads: list[LeadUpdate], confirm_token: Optional[str] = None) -> str:
    """Изменить одну или много сделок: название, бюджет, воронку/этап, ответственного, поля, теги
    (добавить/снять/заменить), причину отказа. Передавайте только то, что меняется.
    Этап ищется в текущей воронке сделки (чтобы перенести в другую — укажите pipeline).
    Если сделок больше порога, сначала вернётся предпросмотр «было → станет» и confirm_token.
    Всё записывается в журнал; отменить: undo_change(op_id)."""
    safety.check_limit(len(leads))
    before = await safety.snapshot("leads", [l.id for l in leads])
    missing = [l.id for l in leads if l.id not in before]
    if missing:
        raise AmoError(404, f"Сделки не найдены: {missing[:20]}. Ничего не изменено.")
    bodies = [await _lead_body(l, True, before[l.id].get("pipeline_id")) for l in leads]
    bodies = [b for b in bodies if len(b) > 1]
    if not bodies:
        raise AmoError(400, "Не указано, что менять")
    res = await safety.apply_patch(tool="update_leads", entity="leads", bodies=bodies, before=before,
                                   args={"leads": [l.model_dump(exclude_none=True) for l in leads]},
                                   confirm_token_value=confirm_token)
    return out(res)


@tool("leads", write=True, title="Закрыть сделки")
async def close_leads(lead_ids: list[int], outcome: Literal["won", "lost"],
                      loss_reason: Ref = None, comment: Optional[str] = None,
                      confirm_token: Optional[str] = None) -> str:
    """Закрыть сделки как успешные (won) или нереализованные (lost, можно с причиной отказа).
    Замена удалению: через API amoCRM сделки удалять нельзя. comment добавится примечанием.
    Обратимо: undo_change(op_id) вернёт прежние этапы."""
    sid = refs.WON if outcome == "won" else refs.LOST
    lr = await refs.loss_reason_id(loss_reason) if outcome == "lost" else None
    ids = list(dict.fromkeys(lead_ids))
    body = [{"id": i, "status_id": sid, **({"loss_reason_id": lr} if lr else {})} for i in ids]
    args = {"lead_ids": ids, "outcome": outcome, "loss_reason": loss_reason, "comment": comment}
    res = await safety.apply_patch(tool="close_leads", entity="leads", bodies=body, args=args,
                                   confirm_token_value=confirm_token)
    if comment and res.get("ids"):
        try:
            for part in chunks(res["ids"]):
                await amo.post("/leads/notes", [{"entity_id": i, "note_type": "common", "params": {"text": comment}}
                                                for i in part])
        except AmoError as e:
            res["note_error"] = f"Сделки закрыты, но примечание не добавлено: {e}"
    if res.get("ok") and "updated" in res:
        res["outcome"] = outcome
    return out(res)


class LeadChanges(BaseModel):
    pipeline: Ref = None
    status: Ref = None
    responsible: Ref = None
    price: Optional[int] = None
    loss_reason: Ref = None
    tags_to_add: Optional[list[str]] = None
    tags_to_delete: Optional[list[str]] = None
    custom_fields: CF = None


def _filter_is_empty(f: Optional[LeadFilter]) -> bool:
    if f is None:
        return True
    d = f.model_dump(exclude_none=True)
    d.pop("include_deleted", None)
    return not any(v not in ([], {}, "") for v in d.values())


@tool("leads", write=True, destructive=True, title="Массовое изменение сделок")
async def bulk_update_leads(changes: LeadChanges, filter: Optional[LeadFilter] = None,
                            lead_ids: Optional[list[int]] = None, add_note: Optional[str] = None,
                            confirm_token: Optional[str] = None) -> str:
    """Применить одни и те же изменения ко всем сделкам по фильтру (или списку lead_ids): перенести на этап,
    сменить ответственного, добавить/снять теги, заполнить поле.
    ВСЕГДА в два шага: первый вызов ничего не меняет и показывает, сколько и какие сделки затронет,
    и возвращает confirm_token; второй вызов с теми же аргументами и confirm_token применяет.
    Фильтр не может быть пустым (защита от изменения всех сделок). Отменить: undo_change(op_id)."""
    if not changes.model_dump(exclude_none=True):
        raise AmoError(400, "Не указано, что менять (changes пустой)")
    if lead_ids:
        ids = list(dict.fromkeys(lead_ids))
        safety.check_limit(len(ids), limit=cfg.max_bulk_items)
        before = await safety.snapshot("leads", ids)
        targets = [before[i] for i in ids if i in before]
        if len(targets) != len(ids):
            raise AmoError(404, f"Не найдены сделки: {[i for i in ids if i not in before][:20]}. Ничего не изменено.")
    else:
        if _filter_is_empty(filter):
            raise AmoError(400, "Фильтр пустой — это изменило бы ВСЕ сделки аккаунта. Укажите условия "
                                "(воронка, этап, ответственный, даты, теги…) или lead_ids.")
        p, pred = await lead_params(filter)
        targets, truncated = await amo.list_all("/leads", "leads", p, max_items=cfg.max_bulk_items, predicate=pred)
        if truncated:
            raise AmoError(400, f"Под фильтр попадает больше {cfg.max_bulk_items} сделок (MCP_MAX_BULK_ITEMS). "
                                "Сузьте фильтр и выполните по частям. Ничего не изменено.")
        if not targets:
            return out({"ok": True, "updated": 0, "note": "Под фильтр не попало ни одной сделки"})
        before = {t["id"]: safety._strip(t) for t in targets}
    bodies = []
    for t in targets:
        b = await _lead_body(LeadUpdate(id=t["id"], **changes.model_dump()), True, t.get("pipeline_id"))
        bodies.append(b)
    args = {"changes": changes.model_dump(exclude_none=True),
            "filter": filter.model_dump(exclude_none=True) if filter else None,
            "lead_ids": lead_ids, "add_note": add_note}
    res = await safety.apply_patch(tool="bulk_update_leads", entity="leads", bodies=bodies, before=before,
                                   args=args, confirm_token_value=confirm_token, limit=cfg.max_bulk_items,
                                   force_confirm=f"Массовое изменение {len(bodies)} сделок")
    if add_note and res.get("ids"):
        try:
            for part in chunks(res["ids"]):
                await amo.post("/leads/notes", [{"entity_id": i, "note_type": "common", "params": {"text": add_note}}
                                                for i in part])
        except AmoError as e:
            res["note_error"] = f"Изменения применены, но примечания не добавлены: {e}"
    return out(res)
