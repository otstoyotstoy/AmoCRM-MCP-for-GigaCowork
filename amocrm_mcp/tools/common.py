"""Общая логика списков: страницы, выгрузка всего, экспорт в файл."""
from typing import Any, Callable, Literal, Optional

from .. import exports
from ..client import amo
from ..config import cfg
from ..core import out

ExportFormat = Optional[Literal["csv", "xlsx"]]


def date_range(params: dict, field: str, frm: Any, to: Any) -> None:
    from ..timeutil import to_ts
    a, b = to_ts(frm), to_ts(to, end_of_day=True)
    if a is not None:
        params[f"filter[{field}][from]"] = a
    if b is not None:
        params[f"filter[{field}][to]"] = b


def order(params: dict, order_by: Optional[str], order_dir: Optional[str]) -> None:
    if order_by:
        params[f"order[{order_by}]"] = (order_dir or "desc").lower()


async def listing(path: str, key: str, params: dict, formatter: Callable[[dict], dict], *,
                  page: int = 1, limit: int = 50, fetch_all: bool = False, max_items: int = 500,
                  export_format: ExportFormat = None, export_name: str = "export",
                  predicate: Callable[[dict], bool] | None = None, extra: dict | None = None) -> str:
    if export_format:
        raw, truncated = await amo.list_all(path, key, params, max_items=cfg.max_fetch_items, predicate=predicate)
        rows = [formatter(x) for x in raw]
        res = exports.save(rows, export_format, export_name)
        if truncated:
            res["warning"] = f"Выгружено максимум {cfg.max_fetch_items} записей (MCP_MAX_FETCH_ITEMS). Сузьте фильтры."
        res["preview"] = rows[:5]
        return out(res)
    if fetch_all or predicate is not None:
        cap = min(max_items or 500, cfg.max_inline_items) if not fetch_all else min(max_items or 500, cfg.max_inline_items)
        if predicate is not None and not fetch_all:
            cap = min(limit, cfg.max_inline_items)
        raw, truncated = await amo.list_all(path, key, params, max_items=cap, predicate=predicate)
        res: dict[str, Any] = {"count": len(raw), "truncated": truncated, "items": [formatter(x) for x in raw]}
        if truncated:
            res["hint"] = (f"Показаны первые {len(raw)}. Чтобы получить все — export_format='xlsx', "
                           f"для подсчётов — инструменты отчётов.")
    else:
        raw, has_next = await amo.list_page(path, key, params, page=page, limit=limit)
        res = {"page": page, "count": len(raw), "has_more": has_next, "items": [formatter(x) for x in raw]}
        if has_next:
            res["next_page"] = page + 1
    if extra:
        res.update(extra)
    return out(res)


def created_ids(data: Any, key: str) -> list[int]:
    return [x.get("id") for x in ((data or {}).get("_embedded") or {}).get(key) or []]
