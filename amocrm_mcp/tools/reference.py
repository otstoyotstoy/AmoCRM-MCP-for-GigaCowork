"""Справочники и настройка аккаунта: воронки, этапы, поля, теги, пользователи."""
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

from .. import refs
from ..client import AmoError, amo
from .. import safety
from ..core import out, tool

Entity = Literal["leads", "contacts", "companies", "customers"]


@tool("reference", title="Информация об аккаунте")
async def get_account_info() -> str:
    """Аккаунт amoCRM: название, ID, валюта, часовой пояс, типы задач, группы пользователей."""
    a = await refs.account()
    emb = a.get("_embedded") or {}
    return out({
        "id": a.get("id"), "name": a.get("name"), "subdomain": a.get("subdomain"),
        "currency": a.get("currency"), "country": a.get("country"),
        "customers_mode": a.get("customers_mode"), "is_unsorted_on": a.get("is_unsorted_on"),
        "datetime_settings": a.get("datetime_settings"),
        "task_types": [{"id": t["id"], "name": t.get("name")} for t in emb.get("task_types") or []],
        "users_groups": [{"id": g["id"], "name": g.get("name")} for g in emb.get("users_groups") or []],
    })


@tool("reference", title="Пользователи")
async def list_users(include_inactive: bool = False) -> str:
    """Сотрудники аккаунта: id, имя, email, активность, группа. Нужны для назначения ответственных."""
    us = await refs.users()
    items = []
    for u in us:
        r = u.get("rights") or {}
        if not include_inactive and r.get("is_active") is False:
            continue
        items.append({"id": u["id"], "name": u.get("name"), "email": u.get("email"),
                      "is_active": r.get("is_active"), "is_admin": r.get("is_admin"),
                      "group_id": r.get("group_id")})
    return out({"count": len(items), "items": items})


@tool("reference", title="Воронки и этапы")
async def list_pipelines() -> str:
    """Все воронки сделок с этапами (id, название, тип). 142 — «Успешно реализовано», 143 — «Закрыто и не реализовано»."""
    pls = await refs.pipelines()
    items = [{
        "id": p["id"], "name": p.get("name"), "is_main": p.get("is_main"),
        "is_archive": p.get("is_archive"), "is_unsorted_on": p.get("is_unsorted_on"),
        "statuses": [{"id": s["id"], "name": s.get("name"), "sort": s.get("sort"), "type": s.get("type"),
                      "color": s.get("color")} for s in (p.get("_embedded") or {}).get("statuses") or []],
    } for p in pls]
    return out({"count": len(items), "items": items})


@tool("reference", title="Пользовательские поля")
async def list_custom_fields(entity_type: Entity = "leads", catalog_id: Optional[int] = None) -> str:
    """Поля сущности (или списка/каталога при catalog_id): id, название, тип, код, варианты для списков."""
    path = f"catalogs/{catalog_id}" if catalog_id else entity_type
    defs = await refs.custom_fields(path)
    items = []
    for f in defs:
        r = {"id": f["id"], "name": f.get("name"), "type": f.get("type"), "code": f.get("code"),
             "is_required": f.get("required_statuses") or None, "is_api_only": f.get("is_api_only") or None}
        if f.get("enums"):
            r["enums"] = [{"id": e["id"], "value": e.get("value")} for e in f["enums"]]
        items.append({k: v for k, v in r.items() if v is not None})
    return out({"count": len(items), "items": items})


@tool("reference", title="Причины отказа")
async def list_loss_reasons() -> str:
    """Причины отказа для закрытия сделок как «не реализовано»."""
    lr = await refs.loss_reasons()
    return out({"count": len(lr), "items": [{"id": r["id"], "name": r.get("name")} for r in lr]})


@tool("reference", title="Теги")
async def list_tags(entity_type: Entity = "leads", query: Optional[str] = None, page: int = 1, limit: int = 250) -> str:
    """Теги сущности, можно искать по части названия."""
    items, has_next = await amo.list_page(f"/{entity_type}/tags", "tags", {"query": query}, page=page, limit=limit)
    return out({"page": page, "has_more": has_next, "count": len(items),
                "items": [{"id": t["id"], "name": t.get("name"), "color": t.get("color")} for t in items]})


# ------------------------------------------------------------------ администрирование
#
# Удаления здесь — единственные по-настоящему необратимые операции сервера. Поэтому:
#   • выключены, пока администратор сервера не задаст MCP_ALLOW_DESTRUCTIVE=1;
#   • даже включённые — только в два шага (предпросмотр → confirm_token);
#   • воронку/этап с сделками удалить нельзя; перед удалением поля все его значения сохраняются в журнал.

class StatusIn(BaseModel):
    name: str
    sort: Optional[int] = Field(None, description="Порядок; больше — правее")
    color: Optional[str] = Field(None, description="Цвет из палитры amoCRM, например #fffeb2")


async def _pipeline(pid: int) -> dict:
    for p in await refs.pipelines():
        if p["id"] == pid:
            return p
    raise AmoError(404, f"Воронка {pid} не найдена")


async def _has_leads(params: dict) -> bool:
    for extra in ({}, {"with": "only_deleted"}):
        items, _ = await amo.list_page("/leads", "leads", {**params, **extra}, limit=1)
        if items:
            return True
    return False


@tool("admin", write=True, destructive=True, title="Управление воронками")
async def manage_pipeline(action: Literal["create", "update", "delete"], pipeline: Optional[Any] = None,
                          name: Optional[str] = None, sort: Optional[int] = None, is_main: Optional[bool] = None,
                          is_unsorted_on: Optional[bool] = None, statuses: Optional[list[StatusIn]] = None,
                          confirm_token: Optional[str] = None) -> str:
    """Создать, переименовать/изменить или удалить воронку. pipeline — ID или название (для update/delete).
    При create можно сразу передать statuses. Удаление: только пустой воронки, только если разрешено
    администратором сервера, и только в два шага (предпросмотр → confirm_token)."""
    args = {"action": action, "pipeline": pipeline, "name": name, "sort": sort, "is_main": is_main,
            "is_unsorted_on": is_unsorted_on, "statuses": [x.model_dump() for x in statuses or []]}
    safety.ensure_audit_writable()
    if action == "create":
        if not name:
            raise AmoError(400, "Для create нужно name")
        body: dict[str, Any] = {"name": name, "is_main": bool(is_main), "is_unsorted_on": bool(is_unsorted_on),
                                "sort": sort or 100}
        if statuses:
            body["_embedded"] = {"statuses": [s.model_dump(exclude_none=True) for s in statuses]}
        data = await amo.post("/leads/pipelines", [body])
        ids = [x.get("id") for x in ((data or {}).get("_embedded") or {}).get("pipelines") or []]
        op_id = safety.audit("create", "manage_pipeline", "pipelines", ids=ids, args=args)
        refs.invalidate("pipelines")
        return out({"ok": True, "created_ids": ids, "op_id": op_id})
    pid = await refs.pipeline_id(pipeline)
    if not pid:
        raise AmoError(400, "Укажите pipeline")
    before = await _pipeline(pid)
    if action == "update":
        body = {k: v for k, v in {"name": name, "sort": sort, "is_main": is_main,
                                  "is_unsorted_on": is_unsorted_on}.items() if v is not None}
        if not body:
            raise AmoError(400, "Не указано, что менять")
        await amo.patch(f"/leads/pipelines/{pid}", body)
        op_id = safety.audit("update", "manage_pipeline", "pipelines", ids=[pid], before=[before], args=args)
        refs.invalidate("pipelines")
        return out({"ok": True, "updated": pid, "op_id": op_id})
    safety.require_destructive("Удаление воронок")
    if await _has_leads({"filter[pipeline_id][]": [pid]}):
        raise AmoError(409, f"В воронке «{before.get('name')}» есть сделки (в том числе закрытые или в корзине). "
                            "Удалять её нельзя: сначала перенесите сделки в другую воронку.")
    if not safety.is_confirmed("manage_pipeline", args, confirm_token):
        return out(safety.need_confirmation("manage_pipeline", args, f"Удаление воронки «{before.get('name')}»",
                                            {"pipeline": before.get("name"), "id": pid,
                                             "statuses": [s.get("name") for s in (before.get("_embedded") or {}).get("statuses") or []]}))
    await amo.delete(f"/leads/pipelines/{pid}")
    op_id = safety.audit("delete", "manage_pipeline", "pipelines", ids=[pid], before=[before], args=args)
    refs.invalidate("pipelines")
    return out({"ok": True, "deleted_pipeline_id": pid, "op_id": op_id})


@tool("admin", write=True, destructive=True, title="Управление этапами")
async def manage_status(action: Literal["create", "update", "delete"], pipeline: Any,
                        status: Optional[Any] = None, name: Optional[str] = None,
                        sort: Optional[int] = None, color: Optional[str] = None,
                        confirm_token: Optional[str] = None) -> str:
    """Добавить, изменить или удалить этап воронки. pipeline и status — ID или название.
    Системные этапы (142/143, «Неразобранное») не удаляются. Удаление — только пустого этапа, только если
    разрешено администратором сервера, и только в два шага (предпросмотр → confirm_token)."""
    args = {"action": action, "pipeline": pipeline, "status": status, "name": name, "sort": sort, "color": color}
    safety.ensure_audit_writable()
    pid = await refs.pipeline_id(pipeline)
    if action == "create":
        if not name:
            raise AmoError(400, "Для create нужно name")
        data = await amo.post(f"/leads/pipelines/{pid}/statuses",
                              [{k: v for k, v in {"name": name, "sort": sort, "color": color}.items() if v is not None}])
        ids = [x.get("id") for x in ((data or {}).get("_embedded") or {}).get("statuses") or []]
        op_id = safety.audit("create", "manage_status", "statuses", ids=ids, args=args)
        refs.invalidate("pipelines")
        return out({"ok": True, "created_ids": ids, "op_id": op_id})
    _, sid = await refs.one_status(status, pid)
    pl = await _pipeline(pid)
    st = next((x for x in (pl.get("_embedded") or {}).get("statuses") or [] if x["id"] == sid), {})
    if action == "update":
        body = {k: v for k, v in {"name": name, "sort": sort, "color": color}.items() if v is not None}
        if not body:
            raise AmoError(400, "Не указано, что менять")
        await amo.patch(f"/leads/pipelines/{pid}/statuses/{sid}", body)
        op_id = safety.audit("update", "manage_status", "statuses", ids=[sid], before=[{**st, "pipeline_id": pid}], args=args)
        refs.invalidate("pipelines")
        return out({"ok": True, "updated": sid, "op_id": op_id})
    if sid in (refs.WON, refs.LOST) or st.get("type") == 1:
        raise AmoError(400, "Системные этапы («Успешно», «Не реализовано», «Неразобранное») удалить нельзя")
    safety.require_destructive("Удаление этапов")
    if await _has_leads({"filter[statuses][0][pipeline_id]": pid, "filter[statuses][0][status_id]": sid}):
        raise AmoError(409, f"На этапе «{st.get('name')}» есть сделки. Сначала перенесите их на другой этап.")
    if not safety.is_confirmed("manage_status", args, confirm_token):
        return out(safety.need_confirmation("manage_status", args, f"Удаление этапа «{st.get('name')}»",
                                            {"pipeline": pl.get("name"), "status": st.get("name"), "id": sid}))
    await amo.delete(f"/leads/pipelines/{pid}/statuses/{sid}")
    op_id = safety.audit("delete", "manage_status", "statuses", ids=[sid], before=[{**st, "pipeline_id": pid}], args=args)
    refs.invalidate("pipelines")
    return out({"ok": True, "deleted_status_id": sid, "op_id": op_id})


class EnumIn(BaseModel):
    value: str
    sort: Optional[int] = None
    id: Optional[int] = Field(None, description="ID существующего варианта (при update, чтобы сохранить его)")


@tool("admin", write=True, destructive=True, title="Управление полями")
async def manage_custom_field(action: Literal["create", "update", "delete"], entity_type: Entity = "leads",
                              field: Optional[Any] = None, name: Optional[str] = None,
                              type: Optional[str] = Field(None, description="text, numeric, checkbox, select, multiselect, date, date_time, url, textarea, radiobutton, birthday, price, streetaddress, monetary"),
                              enums: Optional[list[EnumIn]] = None, remove_missing_enums: bool = False,
                              sort: Optional[int] = None, is_api_only: Optional[bool] = None,
                              group_id: Optional[str] = None, confirm_token: Optional[str] = None) -> str:
    """Создать, изменить или удалить пользовательское поле. field — ID или название (для update/delete).
    Для списков передайте enums: новые варианты ДОБАВЛЯЮТСЯ, существующие сохраняются
    (удалить отсутствующие — remove_missing_enums=true, это стирает их значения у записей).
    Удаление поля стирает его значения у всех записей: только если разрешено администратором сервера,
    в два шага, а все значения перед удалением сохраняются в журнал."""
    args = {"action": action, "entity_type": entity_type, "field": field, "name": name, "type": type,
            "enums": [e.model_dump() for e in enums or []], "remove_missing_enums": remove_missing_enums,
            "sort": sort, "is_api_only": is_api_only, "group_id": group_id}
    safety.ensure_audit_writable()
    if action == "create":
        if not name or not type:
            raise AmoError(400, "Для create нужны name и type")
        body: dict[str, Any] = {"name": name, "type": type}
        if enums:
            body["enums"] = [e.model_dump(exclude_none=True) for e in enums]
        for k, v in {"sort": sort, "is_api_only": is_api_only, "group_id": group_id}.items():
            if v is not None:
                body[k] = v
        data = await amo.post(f"/{entity_type}/custom_fields", [body])
        ids = [x.get("id") for x in ((data or {}).get("_embedded") or {}).get("custom_fields") or []]
        op_id = safety.audit("create", "manage_custom_field", f"{entity_type}_fields", ids=ids, args=args)
        refs.invalidate(f"cf:{entity_type}")
        return out({"ok": True, "created_ids": ids, "op_id": op_id})

    defs = await refs.custom_fields(entity_type)
    if isinstance(field, int) or (isinstance(field, str) and field.isdigit()):
        f = next((d for d in defs if d["id"] == int(field)), None)
        if not f:
            raise AmoError(404, f"Поле {field} не найдено")
    else:
        f = refs._pick("Поле", field, defs, ["name", "code"])
    fid = f["id"]
    if f.get("code") in ("PHONE", "EMAIL", "WEB", "ADDRESS", "POSITION", "IM") or f.get("is_predefined"):
        if action == "delete" or name:
            raise AmoError(400, f"«{f.get('name')}» — системное поле amoCRM, его нельзя удалять или переименовывать")

    if action == "update":
        body = {"id": fid}
        if name:
            body["name"] = name
        removed: list[dict] = []
        if enums is not None:
            old = f.get("enums") or []
            new = [e.model_dump(exclude_none=True) for e in enums]
            for e in new:  # привязываем к существующим вариантам по значению, чтобы не плодить копии
                if "id" not in e:
                    m = next((o for o in old if refs._norm(o.get("value")) == refs._norm(e["value"])), None)
                    if m:
                        e["id"] = m["id"]
            new_ids = {e.get("id") for e in new if e.get("id")}
            removed = [o for o in old if o["id"] not in new_ids]
            if removed and not remove_missing_enums:
                new = [{"id": o["id"], "value": o.get("value"), "sort": o.get("sort")} for o in removed] + new
                removed = []
            body["enums"] = new
        for k, v in {"sort": sort, "is_api_only": is_api_only, "group_id": group_id}.items():
            if v is not None:
                body[k] = v
        if len(body) == 1:
            raise AmoError(400, "Не указано, что менять")
        if removed:
            safety.require_destructive("Удаление вариантов списка")
            if not safety.is_confirmed("manage_custom_field", args, confirm_token):
                return out(safety.need_confirmation(
                    "manage_custom_field", args,
                    "Будут удалены варианты списка — их значения пропадут у всех записей",
                    {"field": f.get("name"), "remove": [o.get("value") for o in removed]}))
        await amo.patch(f"/{entity_type}/custom_fields", [body])
        op_id = safety.audit("update", "manage_custom_field", f"{entity_type}_fields", ids=[fid],
                             before=[f], args=args)
        refs.invalidate(f"cf:{entity_type}")
        return out({"ok": True, "updated": fid, "op_id": op_id,
                    **({"kept_existing_enums": True} if enums is not None and not remove_missing_enums else {})})

    # delete
    safety.require_destructive("Удаление полей")
    items, truncated = await amo.list_all(f"/{entity_type}", entity_type)
    if truncated:
        raise AmoError(400, "Записей слишком много, чтобы сохранить резервную копию значений поля "
                            "(MCP_MAX_FETCH_ITEMS). Удаление отменено.")
    backup = []
    for e in items:
        for c in e.get("custom_fields_values") or []:
            if c.get("field_id") == fid:
                backup.append({"id": e["id"], "values": c.get("values")})
    if not safety.is_confirmed("manage_custom_field", args, confirm_token):
        return out(safety.need_confirmation(
            "manage_custom_field", args, f"Удаление поля «{f.get('name')}»",
            {"field": f.get("name"), "type": f.get("type"), "records_with_values": len(backup),
             "note": "Значения будут сохранены в журнал до удаления"}))
    op_id = safety.audit("delete_field_backup", "manage_custom_field", f"{entity_type}_fields", ids=[fid],
                         before=[f], args=args, result={"values_backup": backup})
    await amo.delete(f"/{entity_type}/custom_fields/{fid}")
    refs.invalidate(f"cf:{entity_type}")
    return out({"ok": True, "deleted_field_id": fid, "values_backed_up": len(backup), "op_id": op_id})


@tool("admin", write=True, title="Создать теги")
async def create_tags(names: list[str], entity_type: Entity = "leads", color: Optional[str] = None) -> str:
    """Создать теги для сущности (обычно не требуется — теги создаются автоматически при назначении)."""
    safety.ensure_audit_writable()
    body = [{"name": n, **({"color": color} if color else {})} for n in names]
    data = await amo.post(f"/{entity_type}/tags", body)
    ids = [x.get("id") for x in ((data or {}).get("_embedded") or {}).get("tags") or []]
    op_id = safety.audit("create", "create_tags", f"{entity_type}_tags", ids=ids, args={"names": names})
    return out({"ok": True, "created_ids": ids, "op_id": op_id})
