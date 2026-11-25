"""Отчёты и сводки по сделкам и задачам, универсальный запрос к API."""
import time
from collections import defaultdict
from datetime import datetime
from typing import Any, Literal, Optional, Union

from pydantic import Field

from .. import refs
from ..client import AmoError, amo
from ..config import cfg
from .. import safety
from ..core import out, tool
from ..timeutil import tz
from .leads import LeadFilter, lead_params

GroupKey = Literal["pipeline", "status", "responsible", "month", "week", "day", "tag", "loss_reason"]


@tool("reports", title="Отчёт по сделкам")
async def leads_report(filter: Optional[LeadFilter] = None,
                       group_by: list[GroupKey] = ["status"],
                       date_field: Literal["created_at", "closed_at", "updated_at"] = "created_at",
                       max_items: int = 50000) -> str:
    """Сводка по сделкам, подходящим под фильтр: количество, сумма и средний бюджет в разрезе
    воронки/этапа/ответственного/периода (month/week/day по date_field)/тега/причины отказа,
    плюс общие итоги и конверсия (успешные / закрытые). Считает по ВСЕМ сделкам, без выгрузки в ответ."""
    f = filter or LeadFilter()
    p, pred = await lead_params(f)
    items, truncated = await amo.list_all("/leads", "leads", p, max_items=min(max_items, cfg.max_fetch_items), predicate=pred)
    c = await refs.ctx(need_leads=True)

    def keys(e) -> list[tuple]:
        parts: list[list[Any]] = []
        for g in group_by:
            if g == "pipeline":
                parts.append([c.pipelines.get(e.get("pipeline_id"), e.get("pipeline_id"))])
            elif g == "status":
                parts.append([f"{c.pipelines.get(e.get('pipeline_id'), '')} / {c.statuses.get((e.get('pipeline_id'), e.get('status_id')), e.get('status_id'))}"])
            elif g == "responsible":
                parts.append([c.user(e.get("responsible_user_id")) or "—"])
            elif g in ("month", "week", "day"):
                ts = e.get(date_field)
                if not ts:
                    parts.append(["—"])
                    continue
                d = datetime.fromtimestamp(ts, tz())
                parts.append([d.strftime("%Y-%m") if g == "month" else
                              f"{d.isocalendar().year}-W{d.isocalendar().week:02d}" if g == "week" else d.strftime("%Y-%m-%d")])
            elif g == "tag":
                tg = [t.get("name") for t in (e.get("_embedded") or {}).get("tags") or []]
                parts.append(tg or ["без тега"])
            elif g == "loss_reason":
                parts.append([c.loss.get(e.get("loss_reason_id"), "—") if e.get("loss_reason_id") else "—"])
        res = [()]
        for vals in parts:
            res = [r + (v,) for r in res for v in vals]
        return res

    agg: dict[tuple, dict] = defaultdict(lambda: {"count": 0, "sum": 0, "won": 0, "lost": 0})
    tot = {"count": 0, "sum": 0, "won": 0, "won_sum": 0, "lost": 0}
    for e in items:
        price = e.get("price") or 0
        won, lost = e.get("status_id") == refs.WON, e.get("status_id") == refs.LOST
        tot["count"] += 1
        tot["sum"] += price
        tot["won"] += won
        tot["won_sum"] += price if won else 0
        tot["lost"] += lost
        for k in keys(e):
            a = agg[k]
            a["count"] += 1
            a["sum"] += price
            a["won"] += won
            a["lost"] += lost
    rows = []
    for k, a in sorted(agg.items(), key=lambda kv: (-kv[1]["count"])):
        r = {g: v for g, v in zip(group_by, k)}
        r.update({"count": a["count"], "sum": a["sum"], "avg": round(a["sum"] / a["count"]) if a["count"] else 0})
        if a["won"] or a["lost"]:
            r.update({"won": a["won"], "lost": a["lost"]})
        rows.append(r)
    closed = tot["won"] + tot["lost"]
    return out({
        "totals": {**tot, "avg": round(tot["sum"] / tot["count"]) if tot["count"] else 0,
                   "win_rate_pct": round(100 * tot["won"] / closed, 1) if closed else None},
        "group_by": group_by, "groups": len(rows), "items": rows[:1000],
        "truncated": truncated or None,
        **({"warning": f"Учтены только первые {len(items)} сделок"} if truncated else {}),
    })


@tool("reports", title="Отчёт по задачам")
async def tasks_report(responsible: Optional[list[Union[int, str]]] = None,
                       completed_from: Optional[Union[int, str]] = Field(None, description="Начало периода для подсчёта выполненных (по дате изменения)"),
                       completed_to: Optional[Union[int, str]] = None) -> str:
    """Дисциплина по задачам в разрезе ответственных: открытые, просроченные, на сегодня,
    выполненные за период."""
    from ..timeutil import to_ts as T
    p: dict[str, Any] = {"filter[is_completed]": 0}
    if responsible:
        p["filter[responsible_user_id][]"] = [await refs.user_id(x) for x in responsible]
    open_tasks, tr1 = await amo.list_all("/tasks", "tasks", p)
    c = await refs.ctx()
    now = time.time()
    end_today = T("today", end_of_day=True)
    rows: dict[Any, dict] = defaultdict(lambda: {"open": 0, "overdue": 0, "due_today": 0, "completed": 0})
    for t in open_tasks:
        r = rows[c.user(t.get("responsible_user_id"))]
        r["open"] += 1
        ct = t.get("complete_till") or 0
        if ct < now:
            r["overdue"] += 1
        elif ct <= end_today:
            r["due_today"] += 1
    if completed_from or completed_to:
        p2 = {k: v for k, v in p.items() if k != "filter[is_completed]"}
        p2["filter[is_completed]"] = 1
        a, b = T(completed_from), T(completed_to, end_of_day=True)
        if a:
            p2["filter[updated_at][from]"] = a
        if b:
            p2["filter[updated_at][to]"] = b
        done, _ = await amo.list_all("/tasks", "tasks", p2)
        for t in done:
            rows[c.user(t.get("responsible_user_id"))]["completed"] += 1
    items = [{"responsible": k, **v} for k, v in sorted(rows.items(), key=lambda kv: -kv[1]["overdue"])]
    return out({"count": len(items), "items": items, "truncated": tr1 or None})


# ------------------------------------------------------------------ универсальный запрос

def _check_path(path: str) -> str:
    if "://" in path or ".." in path or not path.strip():
        raise AmoError(400, "path должен быть относительным путём API, например /api/v4/leads или /leads")
    path = "/" + path.lstrip("/")
    if not path.startswith("/api/"):
        path = "/api/v4" + path
    if not path.startswith(("/api/v4/", "/api/v2/")):
        raise AmoError(400, "Разрешены только пути /api/v4/… (и /api/v2/…)")
    return path


async def _raw(method: str, path: str, params: Optional[dict[str, Any]], body: Any) -> str:
    data = await amo.request(method.upper(), _check_path(path), params=params, json=body)
    return out({"ok": True, "status": "no content" if data is None else "ok", "data": data})


if not cfg.allow_raw_write:
    @tool("raw", title="Произвольный запрос к API (только чтение)")
    async def amocrm_api_request(path: str, params: Optional[dict[str, Any]] = Field(None, description="Query-параметры, например {'filter[id][]': [1,2]}")) -> str:
        """Любой GET-запрос к amoCRM API v4, для которого нет отдельного инструмента: вебхуки (/api/v4/webhooks),
        источники (/sources), роли (/roles), группы полей, чаты, файлы, транзакции и т.д.
        Изменение данных через этот инструмент отключено администратором сервера (используйте профильные инструменты)."""
        return await _raw("GET", path, params, None)
else:
    @tool("raw", write=True, destructive=True, title="Произвольный запрос к API")
    async def amocrm_api_request(method: Literal["GET", "POST", "PATCH", "DELETE"], path: str,
                                 params: Optional[dict[str, Any]] = Field(None, description="Query-параметры"),
                                 body: Optional[Any] = Field(None, description="JSON-тело для POST/PATCH/DELETE"),
                                 confirm_token: Optional[str] = None) -> str:
        """Любой метод amoCRM API v4, для которого нет отдельного инструмента. Обходит проверки профильных
        инструментов, поэтому: изменения журналируются, POST/PATCH/DELETE требуют подтверждения
        (предпросмотр → confirm_token), DELETE — ещё и разрешения MCP_ALLOW_DESTRUCTIVE=1.
        Документация: https://www.amocrm.ru/developers/content/crm_platform/api-reference"""
        m = method.upper()
        if m == "GET":
            return await _raw("GET", path, params, None)
        p = _check_path(path)
        safety.ensure_audit_writable()
        if m == "DELETE":
            safety.require_destructive("DELETE через произвольный запрос")
        args = {"method": m, "path": p, "params": params, "body": body}
        if not safety.is_confirmed("amocrm_api_request", args, confirm_token):
            return out(safety.need_confirmation("amocrm_api_request", args,
                                                f"Произвольный {m}-запрос в обход проверок", args))
        try:
            data = await amo.request(m, p, params=params, json=body)
        except AmoError as e:
            safety.audit("raw", "amocrm_api_request", p, args=args, error=str(e))
            raise
        op_id = safety.audit("raw", "amocrm_api_request", p, args=args, result=data)
        return out({"ok": True, "op_id": op_id, "data": data})
