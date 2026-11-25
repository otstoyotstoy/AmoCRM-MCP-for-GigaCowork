"""Справочники аккаунта (кэш), поиск ID по названиям, преобразование полей и компактное форматирование."""
from __future__ import annotations

import time
from typing import Any

from .client import AmoError, amo
from .timeutil import fmt, fmt_date, to_ts

TTL = 300
WON, LOST = 142, 143
_STATUS_ALIASES = {
    "won": WON, "успешно": WON, "успешно реализовано": WON, "выиграна": WON, "выиграно": WON,
    "lost": LOST, "закрыто и не реализовано": LOST, "не реализовано": LOST, "проиграна": LOST, "проиграно": LOST,
}
_DATE_TYPES = {"date", "date_time", "birthday"}
_ENUM_TYPES = {"select", "radiobutton", "category", "multiselect"}

_cache: dict[str, tuple[float, Any]] = {}


def invalidate(prefix: str = "") -> None:
    for k in list(_cache):
        if k.startswith(prefix):
            _cache.pop(k, None)


async def _cached(key: str, loader):
    hit = _cache.get(key)
    if hit and time.monotonic() - hit[0] < TTL:
        return hit[1]
    data = await loader()
    _cache[key] = (time.monotonic(), data)
    return data


def _norm(s: Any) -> str:
    return " ".join(str(s).strip().lower().replace("ё", "е").split())


# ---------------------------------------------------------------- загрузчики

async def account() -> dict:
    return await _cached("account", lambda: amo.get(
        "/account", {"with": "task_types,users_groups,datetime_settings,version,entity_names,amojo_id"}))


async def users() -> list[dict]:
    async def load():
        items, _ = await amo.list_all("/users", "users")
        return items
    return await _cached("users", load)


async def pipelines() -> list[dict]:
    async def load():
        data = await amo.get("/leads/pipelines")
        return ((data or {}).get("_embedded") or {}).get("pipelines") or []
    return await _cached("pipelines", load)


async def custom_fields(entity: str) -> list[dict]:
    async def load():
        items, _ = await amo.list_all(f"/{entity}/custom_fields", "custom_fields")
        return items
    return await _cached(f"cf:{entity}", load)


async def loss_reasons() -> list[dict]:
    async def load():
        items, _ = await amo.list_all("/leads/loss_reasons", "loss_reasons")
        return items
    return await _cached("loss_reasons", load)


async def task_types() -> list[dict]:
    acc = await account()
    return ((acc or {}).get("_embedded") or {}).get("task_types") or [
        {"id": 1, "name": "Звонок"}, {"id": 2, "name": "Встреча"}]


# ---------------------------------------------------------------- поиск ID по названию

def _pick(kind: str, value: Any, items: list[dict], names: list[str]) -> dict:
    v = _norm(value)
    exact = [it for it in items if any(_norm(it.get(n) or "") == v for n in names)]
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        raise AmoError(400, f"{kind} «{value}» неоднозначно: " + ", ".join(f"{i.get('name')} (id {i['id']})" for i in exact[:10]))
    part = [it for it in items if any(v in _norm(it.get(n) or "") for n in names)]
    if len(part) == 1:
        return part[0]
    if part:
        raise AmoError(400, f"{kind} «{value}» неоднозначно, уточните: " + ", ".join(f"{i.get('name')} (id {i['id']})" for i in part[:10]))
    raise AmoError(404, f"{kind} «{value}» не найден(а). Доступно: " + ", ".join(f"{i.get('name')} (id {i['id']})" for i in items[:30]))


def _as_int(v: Any) -> int | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, str) and v.strip().isdigit():
        return int(v.strip())
    return None


async def user_id(value: Any) -> int | None:
    if value in (None, ""):
        return None
    i = _as_int(value)
    if i is not None:
        return i
    return _pick("Пользователь", value, await users(), ["name", "email"])["id"]


async def pipeline_id(value: Any) -> int | None:
    if value in (None, ""):
        return None
    i = _as_int(value)
    if i is not None:
        return i
    return _pick("Воронка", value, await pipelines(), ["name"])["id"]


async def status_ids(value: Any, pipeline: Any = None) -> list[tuple[int, int]]:
    """Все пары (pipeline_id, status_id), подходящие под статус (ID или название)."""
    pls = await pipelines()
    pid = await pipeline_id(pipeline)
    scope = [p for p in pls if pid is None or p["id"] == pid]
    if pid is not None and not scope:
        raise AmoError(404, f"Воронка {pipeline} не найдена")
    sid = _as_int(value)
    if sid is None and _norm(value) in _STATUS_ALIASES:
        sid = _STATUS_ALIASES[_norm(value)]
    pairs: list[tuple[int, int]] = []
    for p in scope:
        for s in (p.get("_embedded") or {}).get("statuses") or []:
            if (sid is not None and s["id"] == sid) or (sid is None and _norm(s["name"]) == _norm(value)):
                pairs.append((p["id"], s["id"]))
    if not pairs and sid is None:
        for p in scope:
            for s in (p.get("_embedded") or {}).get("statuses") or []:
                if _norm(value) in _norm(s["name"]):
                    pairs.append((p["id"], s["id"]))
    if not pairs:
        avail = "; ".join(f"{p['name']} (id {p['id']}): " + ", ".join(
            f"{s['name']}={s['id']}" for s in (p.get('_embedded') or {}).get('statuses') or []) for p in scope[:5])
        raise AmoError(404, f"Этап «{value}» не найден. {avail}")
    return pairs


async def one_status(value: Any, pipeline: Any = None) -> tuple[int, int]:
    pairs = await status_ids(value, pipeline)
    sid = pairs[0][1]
    if len({p for p, _ in pairs}) > 1 and sid not in (WON, LOST):
        raise AmoError(400, f"Этап «{value}» есть в нескольких воронках — укажите pipeline")
    return pairs[0]


async def loss_reason_id(value: Any) -> int | None:
    if value in (None, ""):
        return None
    i = _as_int(value)
    if i is not None:
        return i
    return _pick("Причина отказа", value, await loss_reasons(), ["name"])["id"]


async def task_type_id(value: Any) -> int | None:
    if value in (None, ""):
        return None
    i = _as_int(value)
    if i is not None:
        return i
    return _pick("Тип задачи", value, await task_types(), ["name"])["id"]


def tags_payload(names: list[Any] | None) -> list[dict] | None:
    if not names:
        return None
    return [{"id": t} if isinstance(t, int) else {"name": str(t)} for t in names]


# ---------------------------------------------------------------- пользовательские поля

async def cf_to_api(entity: str, values: dict[str, Any] | None) -> list[dict] | None:
    """{"Название/ID/код поля": значение} → custom_fields_values для API."""
    if not values:
        return None
    defs = await custom_fields(entity)
    by_id = {f["id"]: f for f in defs}
    out = []
    for key, val in values.items():
        f = None
        k = _as_int(key)
        if k is not None:
            f = by_id.get(k)
            if f is None:
                raise AmoError(404, f"Поле с ID {k} не найдено у {entity}")
        else:
            nk = _norm(key)
            cands = [d for d in defs if _norm(d.get("name") or "") == nk or _norm(d.get("code") or "") == nk]
            if not cands:
                raise AmoError(404, f"Поле «{key}» не найдено у {entity}. Поля: " +
                               ", ".join(f"{d['name']} (id {d['id']}, {d['type']})" for d in defs[:60]))
            f = cands[0]
        out.append(_cf_value(f, val))
    return out


def _enum(f: dict, v: Any) -> dict:
    if isinstance(v, dict):
        return v
    i = _as_int(v)
    enums = f.get("enums") or []
    if i is not None and any(e["id"] == i for e in enums):
        return {"enum_id": i}
    for e in enums:
        if _norm(e.get("value") or "") == _norm(v):
            return {"enum_id": e["id"]}
    raise AmoError(400, f"Для поля «{f['name']}» нет варианта «{v}». Варианты: " +
                   ", ".join(f"{e['value']} (id {e['id']})" for e in enums[:50]))


def _cf_value(f: dict, val: Any) -> dict:
    base = {"field_id": f["id"]}
    if val is None or val == [] or val == "":
        return {**base, "values": None}
    t = f.get("type")
    vals = val if isinstance(val, list) else [val]
    if t == "multitext":
        res = []
        for v in vals:
            if isinstance(v, dict):
                res.append({"enum_code": "WORK", **v})
            else:
                res.append({"value": str(v), "enum_code": "WORK"})
        return {**base, "values": res}
    if t in _ENUM_TYPES:
        return {**base, "values": [_enum(f, v) for v in vals]}
    if t in _DATE_TYPES:
        return {**base, "values": [{"value": to_ts(v)} for v in vals]}
    if t == "checkbox":
        b = vals[0]
        if isinstance(b, str):
            b = _norm(b) in ("1", "true", "да", "yes", "on")
        return {**base, "values": [{"value": bool(b)}]}
    if t in ("numeric", "price", "monetary"):
        return {**base, "values": [{"value": v if isinstance(v, (int, float)) else float(str(v).replace(" ", "").replace(",", "."))} for v in vals]}
    return {**base, "values": [v if isinstance(v, dict) else {"value": v if t in ("legal_entity", "items", "linked_entity", "smart_address", "chained_list", "tracking_data", "file", "payer", "supplier") else str(v)} for v in vals]}


def cf_from_api(cfv: list[dict] | None, skip_codes: tuple[str, ...] = ()) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for f in cfv or []:
        if f.get("field_code") in skip_codes:
            continue
        t = f.get("field_type")
        vals = []
        for v in f.get("values") or []:
            x = v.get("value")
            if t in _DATE_TYPES and isinstance(x, (int, float)):
                x = fmt_date(x) if t != "date_time" else fmt(x)
            vals.append(x)
        out[f.get("field_name") or str(f.get("field_id"))] = vals[0] if len(vals) == 1 else vals
    return out


def cf_by_code(cfv: list[dict] | None, code: str) -> list[str]:
    for f in cfv or []:
        if f.get("field_code") == code:
            return [str(v.get("value")) for v in f.get("values") or []]
    return []


# ---------------------------------------------------------------- форматирование

class Ctx:
    """Снимок справочников для быстрого форматирования списков."""

    def __init__(self, usr, pls, lrs, tts):
        self.users = {u["id"]: u.get("name") for u in usr}
        self.pipelines = {p["id"]: p.get("name") for p in pls}
        self.statuses = {(p["id"], s["id"]): s.get("name")
                         for p in pls for s in (p.get("_embedded") or {}).get("statuses") or []}
        self.loss = {r["id"]: r.get("name") for r in lrs}
        self.task_types = {t["id"]: t.get("name") for t in tts}

    def user(self, i):
        return self.users.get(i, i) if i else None


async def ctx(need_leads: bool = False) -> Ctx:
    usr = await _safe(users)
    pls = await _safe(pipelines) if need_leads else []
    lrs = await _safe(loss_reasons) if need_leads else []
    tts = await _safe(task_types)
    return Ctx(usr, pls, lrs, tts)


async def _safe(fn):
    try:
        return await fn()
    except AmoError:
        return []


def _tags(e: dict) -> list[str]:
    return [t.get("name") for t in (e.get("_embedded") or {}).get("tags") or []]


def _ids(e: dict, key: str) -> list[int]:
    return [x["id"] for x in (e.get("_embedded") or {}).get(key) or []]


def lead(e: dict, c: Ctx) -> dict:
    r = {
        "id": e["id"], "name": e.get("name"), "price": e.get("price"),
        "pipeline_id": e.get("pipeline_id"), "pipeline": c.pipelines.get(e.get("pipeline_id")),
        "status_id": e.get("status_id"), "status": c.statuses.get((e.get("pipeline_id"), e.get("status_id"))),
        "responsible_user_id": e.get("responsible_user_id"), "responsible": c.user(e.get("responsible_user_id")),
        "created_at": fmt(e.get("created_at")), "updated_at": fmt(e.get("updated_at")),
    }
    if e.get("closed_at"):
        r["closed_at"] = fmt(e.get("closed_at"))
    if e.get("loss_reason_id"):
        r["loss_reason"] = c.loss.get(e["loss_reason_id"], e["loss_reason_id"])
    if e.get("closest_task_at"):
        r["closest_task_at"] = fmt(e["closest_task_at"])
    if e.get("is_deleted"):
        r["is_deleted"] = True
    tags = _tags(e)
    if tags:
        r["tags"] = tags
    for k in ("contacts", "companies"):
        ids = _ids(e, k)
        if ids:
            r[f"{k[:-1] if k == 'contacts' else 'company'}_ids"] = ids
    cfs = cf_from_api(e.get("custom_fields_values"))
    if cfs:
        r["custom_fields"] = cfs
    return r


def contact(e: dict, c: Ctx) -> dict:
    cfv = e.get("custom_fields_values")
    r = {
        "id": e["id"], "name": e.get("name"), "first_name": e.get("first_name") or None,
        "last_name": e.get("last_name") or None,
        "phones": cf_by_code(cfv, "PHONE"), "emails": cf_by_code(cfv, "EMAIL"),
        "responsible_user_id": e.get("responsible_user_id"), "responsible": c.user(e.get("responsible_user_id")),
        "created_at": fmt(e.get("created_at")), "updated_at": fmt(e.get("updated_at")),
    }
    r = {k: v for k, v in r.items() if v not in (None, [])}
    tags = _tags(e)
    if tags:
        r["tags"] = tags
    for k, name in (("leads", "lead_ids"), ("companies", "company_ids"), ("customers", "customer_ids")):
        ids = _ids(e, k)
        if ids:
            r[name] = ids
    cfs = cf_from_api(cfv, ("PHONE", "EMAIL"))
    if cfs:
        r["custom_fields"] = cfs
    return r


def company(e: dict, c: Ctx) -> dict:
    cfv = e.get("custom_fields_values")
    r = {
        "id": e["id"], "name": e.get("name"),
        "phones": cf_by_code(cfv, "PHONE"), "emails": cf_by_code(cfv, "EMAIL"),
        "web": cf_by_code(cfv, "WEB"), "address": cf_by_code(cfv, "ADDRESS"),
        "responsible_user_id": e.get("responsible_user_id"), "responsible": c.user(e.get("responsible_user_id")),
        "created_at": fmt(e.get("created_at")), "updated_at": fmt(e.get("updated_at")),
    }
    r = {k: v for k, v in r.items() if v not in (None, [])}
    tags = _tags(e)
    if tags:
        r["tags"] = tags
    for k, name in (("leads", "lead_ids"), ("contacts", "contact_ids"), ("customers", "customer_ids")):
        ids = _ids(e, k)
        if ids:
            r[name] = ids
    cfs = cf_from_api(cfv, ("PHONE", "EMAIL", "WEB", "ADDRESS"))
    if cfs:
        r["custom_fields"] = cfs
    return r


def task(e: dict, c: Ctx) -> dict:
    import time as _t
    r = {
        "id": e["id"], "text": e.get("text"),
        "task_type_id": e.get("task_type_id"), "task_type": c.task_types.get(e.get("task_type_id")),
        "responsible_user_id": e.get("responsible_user_id"), "responsible": c.user(e.get("responsible_user_id")),
        "entity_type": e.get("entity_type"), "entity_id": e.get("entity_id"),
        "complete_till": fmt(e.get("complete_till")), "is_completed": e.get("is_completed"),
        "created_at": fmt(e.get("created_at")),
    }
    if not e.get("is_completed") and e.get("complete_till") and e["complete_till"] < _t.time():
        r["overdue"] = True
    res = (e.get("result") or {}).get("text") if isinstance(e.get("result"), dict) else None
    if res:
        r["result"] = res
    return {k: v for k, v in r.items() if v is not None}


def note(e: dict, c: Ctx) -> dict:
    params = dict(e.get("params") or {})
    r = {
        "id": e["id"], "entity_id": e.get("entity_id"), "note_type": e.get("note_type"),
        "text": params.pop("text", None), "created_by": c.user(e.get("created_by")) or e.get("created_by"),
        "created_at": fmt(e.get("created_at")),
    }
    if params:
        r["params"] = params
    return {k: v for k, v in r.items() if v is not None}


def customer(e: dict, c: Ctx) -> dict:
    r = {
        "id": e["id"], "name": e.get("name"), "next_price": e.get("next_price"),
        "next_date": fmt_date(e.get("next_date")), "status_id": e.get("status_id"),
        "periodicity": e.get("periodicity"), "ltv": e.get("ltv"), "purchases_count": e.get("purchases_count"),
        "responsible": c.user(e.get("responsible_user_id")),
        "created_at": fmt(e.get("created_at")), "updated_at": fmt(e.get("updated_at")),
    }
    r = {k: v for k, v in r.items() if v is not None}
    tags = _tags(e)
    if tags:
        r["tags"] = tags
    cfs = cf_from_api(e.get("custom_fields_values"))
    if cfs:
        r["custom_fields"] = cfs
    return r


def catalog_element(e: dict, c: Ctx) -> dict:
    r = {"id": e["id"], "name": e.get("name"), "catalog_id": e.get("catalog_id"),
         "created_at": fmt(e.get("created_at")), "updated_at": fmt(e.get("updated_at"))}
    cfs = cf_from_api(e.get("custom_fields_values"))
    if cfs:
        r["custom_fields"] = cfs
    return r
