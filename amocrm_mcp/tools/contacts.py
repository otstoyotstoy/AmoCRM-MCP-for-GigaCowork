"""Контакты и компании: поиск, выгрузка, карточка, создание, изменение."""
from typing import Any, Literal, Optional, Union

from pydantic import BaseModel, Field

from .. import refs
from ..client import AmoError, amo
from .. import safety
from ..core import out, tool
from .common import ExportFormat, date_range, listing, order
from .leads import link_after_create, person_body

Ref = Optional[Union[int, str]]
CF = Optional[dict[str, Any]]
Many = Optional[Union[str, list[str]]]


async def _params(query, ids, responsible, created_from, created_to, updated_from, updated_to, with_):
    p: dict[str, Any] = {"with": with_}
    if query:
        p["query"] = query
    if ids:
        p["filter[id][]"] = ids
    if responsible:
        p["filter[responsible_user_id][]"] = [await refs.user_id(x) for x in responsible]
    date_range(p, "created_at", created_from, created_to)
    date_range(p, "updated_at", updated_from, updated_to)
    return p


def _tag_pred(tags):
    if not tags:
        return None
    want = {refs._norm(t) for t in tags}
    return lambda e: bool({refs._norm(t.get("name") or "") for t in (e.get("_embedded") or {}).get("tags") or []} & want)


# ------------------------------------------------------------------ контакты

@tool("contacts", title="Поиск и выгрузка контактов")
async def search_contacts(
    query: Optional[str] = Field(None, description="Поиск по имени, телефону, email и полям"),
    ids: Optional[list[int]] = None, responsible: Optional[list[Union[int, str]]] = None,
    tags: Optional[list[str]] = None,
    created_from: Ref = None, created_to: Ref = None, updated_from: Ref = None, updated_to: Ref = None,
    with_leads: bool = False,
    order_by: Literal["created_at", "updated_at", "id"] = "updated_at", order_dir: Literal["asc", "desc"] = "desc",
    page: int = 1, limit: int = 50, fetch_all: bool = False, max_items: int = 500, export_format: ExportFormat = None,
) -> str:
    """Найти контакты (по телефону — в любом формате, по email, имени), отфильтровать по ответственному,
    тегам, датам. Постранично, все сразу (fetch_all) или файлом (export_format)."""
    p = await _params(query, ids, responsible, created_from, created_to, updated_from, updated_to,
                      "leads,companies" if with_leads else "companies")
    order(p, order_by, order_dir)
    c = await refs.ctx()
    return await listing("/contacts", "contacts", p, lambda e: refs.contact(e, c), page=page, limit=limit,
                         fetch_all=fetch_all, max_items=max_items, export_format=export_format,
                         export_name="contacts", predicate=_tag_pred(tags))


@tool("contacts", title="Карточка контакта")
async def get_contact(contact_id: int) -> str:
    """Контакт целиком: телефоны, email, поля, теги, сделки (со статусами) и компании."""
    e = await amo.get(f"/contacts/{contact_id}", {"with": "leads,customers,catalog_elements"})
    if not e:
        raise AmoError(404, f"Контакт {contact_id} не найден")
    c = await refs.ctx(need_leads=True)
    r = refs.contact(e, c)
    lids = r.get("lead_ids") or []
    if lids:
        got, _ = await amo.list_page("/leads", "leads", {"filter[id][]": lids[:250]}, limit=250)
        r["leads"] = [{k: v for k, v in refs.lead(x, c).items() if k in ("id", "name", "price", "pipeline", "status", "responsible", "updated_at")} for x in got]
    return out(r)


class ContactIn(BaseModel):
    name: Optional[str] = Field(None, description="Полное имя (или используйте first_name/last_name)")
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    phone: Many = Field(None, description="Телефон или список телефонов")
    email: Many = None
    responsible: Ref = None
    tags: Optional[list[str]] = None
    custom_fields: CF = None
    company_id: Optional[int] = Field(None, description="Привязать к компании")
    lead_ids: Optional[list[int]] = Field(None, description="Привязать к сделкам")


class ContactUpdate(BaseModel):
    id: int
    name: Optional[str] = None
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    phone: Many = Field(None, description="Телефон(ы). По умолчанию ДОБАВЛЯЮТСЯ к существующим")
    email: Many = Field(None, description="Email. По умолчанию ДОБАВЛЯЮТСЯ к существующим")
    replace_phones: bool = Field(False, description="true — заменить все телефоны переданными (старые удалятся)")
    replace_emails: bool = Field(False, description="true — заменить все email переданными")
    responsible: Ref = None
    tags_to_add: Optional[list[str]] = None
    tags_to_delete: Optional[list[str]] = None
    custom_fields: CF = None


def _digits(v: Any) -> str:
    d = "".join(ch for ch in str(v) if ch.isdigit())
    return d[-10:] if len(d) >= 10 else str(v).strip().lower()


def _merge_multitext(body: dict, before: dict, code: str, replace: bool) -> None:
    """Добавить новые телефоны/email к существующим, если не просили заменить."""
    if replace:
        return
    new_f = None
    for f in body.get("custom_fields_values") or []:
        old = next((c for c in before.get("custom_fields_values") or [] if c.get("field_id") == f.get("field_id")), None)
        if old and old.get("field_code") == code:
            new_f, old_f = f, old
            break
    else:
        return
    if not new_f.get("values"):
        return
    keep = [{"value": v.get("value"), "enum_code": v.get("enum_code") or "WORK"} for v in old_f.get("values") or []]
    seen = {_digits(v["value"]) if code == "PHONE" else str(v["value"]).lower() for v in keep}
    for v in new_f["values"]:
        k = _digits(v.get("value")) if code == "PHONE" else str(v.get("value")).lower()
        if k not in seen:
            keep.append(v)
            seen.add(k)
    new_f["values"] = keep


async def _upd_body(entity: str, x: BaseModel, before: dict) -> dict:
    b = await person_body(entity, x)
    b["id"] = x.id
    d = x.model_dump()
    _merge_multitext(b, before, "PHONE", d.get("replace_phones", False))
    _merge_multitext(b, before, "EMAIL", d.get("replace_emails", False))
    if d.get("tags_to_add"):
        b["tags_to_add"] = refs.tags_payload(d["tags_to_add"])
    if d.get("tags_to_delete"):
        b["tags_to_delete"] = refs.tags_payload(d["tags_to_delete"])
    return b


async def _find_existing_contact(x) -> Optional[int]:
    """Ищет контакт с ТЕМ ЖЕ телефоном или email (проверка точного совпадения, а не только поиска)."""
    phones = [x.phone] if isinstance(x.phone, str) else (x.phone or [])
    emails = [x.email] if isinstance(x.email, str) else (x.email or [])
    for key, code in [(p, "PHONE") for p in phones] + [(e, "EMAIL") for e in emails]:
        found, _ = await amo.list_page("/contacts", "contacts", {"query": key}, limit=10)
        want = _digits(key) if code == "PHONE" else str(key).strip().lower()
        for c in found:
            vals = refs.cf_by_code(c.get("custom_fields_values"), code)
            if any((_digits(v) if code == "PHONE" else v.strip().lower()) == want for v in vals):
                return c["id"]
    return None


@tool("contacts", write=True, title="Создать контакты")
async def create_contacts(contacts: list[ContactIn], skip_duplicates_by_phone: bool = True) -> str:
    """Создать контакты. По умолчанию НЕ создаёт контакт, если в CRM уже есть контакт с таким же телефоном
    или email (вернёт его ID в already_existed). Отключить проверку: skip_duplicates_by_phone=false."""
    existing, todo = [], []
    for x in contacts:
        if skip_duplicates_by_phone:
            cid = await _find_existing_contact(x)
            if cid:
                existing.append({"input": x.name or x.first_name or x.phone or x.email, "id": cid})
                continue
        todo.append(x)
    if not todo:
        return out({"ok": True, "created": 0, "ids": [], "already_existed": existing})
    body = []
    for x in todo:
        b = await person_body("contacts", x)
        if x.tags:
            b["_embedded"] = {"tags": refs.tags_payload(x.tags)}
        body.append(b)

    async def after(cid, idx, x=None):
        await link_after_create("contacts", cid, company_id=todo[idx].company_id, lead_ids=todo[idx].lead_ids)

    res = await safety.apply_create(tool="create_contacts", entity="contacts", bodies=body,
                                    args={"contacts": [c.model_dump(exclude_none=True) for c in todo]},
                                    url="/contacts", result_key="contacts", after=after)
    res["already_existed"] = existing
    return out(res)


async def _update_people(entity: str, tool_name: str, items: list, confirm_token: Optional[str]) -> dict:
    safety.check_limit(len(items))
    before = await safety.snapshot(entity, [x.id for x in items])
    missing = [x.id for x in items if x.id not in before]
    if missing:
        raise AmoError(404, f"Не найдены: {missing[:20]}. Ничего не изменено.")
    bodies = [b for b in [await _upd_body(entity, x, before[x.id]) for x in items] if len(b) > 1]
    if not bodies:
        raise AmoError(400, "Не указано, что менять")
    return await safety.apply_patch(tool=tool_name, entity=entity, bodies=bodies, before=before,
                                    args={entity: [x.model_dump(exclude_none=True) for x in items]},
                                    confirm_token_value=confirm_token)


@tool("contacts", write=True, title="Изменить контакты")
async def update_contacts(contacts: list[ContactUpdate], confirm_token: Optional[str] = None) -> str:
    """Изменить контакты: имя, телефоны, email, ответственного, поля, теги. Только изменяемые поля.
    Новые телефоны/email ДОБАВЛЯЮТСЯ к существующим (заменить — replace_phones/replace_emails=true).
    Отменить: undo_change(op_id)."""
    return out(await _update_people("contacts", "update_contacts", contacts, confirm_token))


# ------------------------------------------------------------------ компании

@tool("companies", title="Поиск и выгрузка компаний")
async def search_companies(
    query: Optional[str] = None, ids: Optional[list[int]] = None,
    responsible: Optional[list[Union[int, str]]] = None, tags: Optional[list[str]] = None,
    created_from: Ref = None, created_to: Ref = None, updated_from: Ref = None, updated_to: Ref = None,
    order_by: Literal["created_at", "updated_at", "id"] = "updated_at", order_dir: Literal["asc", "desc"] = "desc",
    page: int = 1, limit: int = 50, fetch_all: bool = False, max_items: int = 500, export_format: ExportFormat = None,
) -> str:
    """Найти компании по названию/телефону/полям, ответственному, тегам, датам. Постранично, все или файлом."""
    p = await _params(query, ids, responsible, created_from, created_to, updated_from, updated_to, "contacts,leads")
    order(p, order_by, order_dir)
    c = await refs.ctx()
    return await listing("/companies", "companies", p, lambda e: refs.company(e, c), page=page, limit=limit,
                         fetch_all=fetch_all, max_items=max_items, export_format=export_format,
                         export_name="companies", predicate=_tag_pred(tags))


@tool("companies", title="Карточка компании")
async def get_company(company_id: int) -> str:
    """Компания целиком: реквизиты/поля, контакты и сделки."""
    e = await amo.get(f"/companies/{company_id}", {"with": "contacts,leads,customers,catalog_elements"})
    if not e:
        raise AmoError(404, f"Компания {company_id} не найдена")
    c = await refs.ctx(need_leads=True)
    r = refs.company(e, c)
    if r.get("contact_ids"):
        got, _ = await amo.list_page("/contacts", "contacts", {"filter[id][]": r["contact_ids"][:250]}, limit=250)
        r["contacts"] = [refs.contact(x, c) for x in got]
    if r.get("lead_ids"):
        got, _ = await amo.list_page("/leads", "leads", {"filter[id][]": r["lead_ids"][:250]}, limit=250)
        r["leads"] = [{k: v for k, v in refs.lead(x, c).items() if k in ("id", "name", "price", "pipeline", "status", "responsible")} for x in got]
    return out(r)


class CompanyIn(BaseModel):
    name: str
    phone: Many = None
    email: Many = None
    web: Many = None
    address: Many = None
    responsible: Ref = None
    tags: Optional[list[str]] = None
    custom_fields: CF = Field(None, description="Например ИНН, КПП, реквизиты — по названию поля")
    contact_ids: Optional[list[int]] = None
    lead_ids: Optional[list[int]] = None


class CompanyUpdate(BaseModel):
    id: int
    name: Optional[str] = None
    phone: Many = Field(None, description="По умолчанию ДОБАВЛЯЮТСЯ к существующим")
    email: Many = Field(None, description="По умолчанию ДОБАВЛЯЮТСЯ к существующим")
    replace_phones: bool = False
    replace_emails: bool = False
    web: Many = None
    address: Many = None
    responsible: Ref = None
    tags_to_add: Optional[list[str]] = None
    tags_to_delete: Optional[list[str]] = None
    custom_fields: CF = None


@tool("companies", write=True, title="Создать компании")
async def create_companies(companies: list[CompanyIn]) -> str:
    """Создать компании с телефонами, сайтом, адресом, полями; сразу привязать контакты и сделки.
    Повторный одинаковый вызов дублей не создаёт."""
    body = []
    for x in companies:
        b = await person_body("companies", x)
        if x.tags:
            b["_embedded"] = {"tags": refs.tags_payload(x.tags)}
        body.append(b)

    async def after(cid, idx, x=None):
        c = companies[idx]
        await link_after_create("companies", cid, contact_ids=c.contact_ids, lead_ids=c.lead_ids)

    res = await safety.apply_create(tool="create_companies", entity="companies", bodies=body,
                                    args={"companies": [c.model_dump(exclude_none=True) for c in companies]},
                                    url="/companies", result_key="companies", after=after)
    return out(res)


@tool("companies", write=True, title="Изменить компании")
async def update_companies(companies: list[CompanyUpdate], confirm_token: Optional[str] = None) -> str:
    """Изменить компании: название, телефоны/email (по умолчанию добавляются к существующим), сайт, адрес,
    ответственного, поля, теги. Отменить: undo_change(op_id)."""
    return out(await _update_people("companies", "update_companies", companies, confirm_token))
