"""Задачи: поиск (в т.ч. просроченные), создание, изменение, выполнение."""
import time
from typing import Literal, Optional, Union

from pydantic import BaseModel, Field

from .. import refs
from ..client import AmoError
from .. import safety
from ..core import out, tool
from ..timeutil import to_ts
from .common import ExportFormat, date_range, listing, order

Ref = Optional[Union[int, str]]
EntityT = Literal["leads", "contacts", "companies", "customers"]


@tool("tasks", title="Поиск задач")
async def search_tasks(
    responsible: Optional[list[Union[int, str]]] = None,
    state: Literal["open", "completed", "overdue", "all"] = "open",
    task_type: Optional[list[Union[int, str]]] = Field(None, description="Типы задач (ID или название: Звонок, Встреча, …)"),
    entity_type: Optional[EntityT] = None, entity_ids: Optional[list[int]] = None,
    due_from: Ref = Field(None, description="Срок выполнения от"), due_to: Ref = Field(None, description="Срок до"),
    updated_from: Ref = None, updated_to: Ref = None, ids: Optional[list[int]] = None,
    order_by: Literal["complete_till", "created_at", "id"] = "complete_till", order_dir: Literal["asc", "desc"] = "asc",
    page: int = 1, limit: int = 50, fetch_all: bool = False, max_items: int = 500, export_format: ExportFormat = None,
) -> str:
    """Задачи по ответственным, статусу (open/completed/overdue/all), типу, привязке к сделке/контакту, сроку.
    overdue — открытые с истёкшим сроком. Срок фильтруется на стороне сервера MCP."""
    p: dict = {}
    if responsible:
        p["filter[responsible_user_id][]"] = [await refs.user_id(x) for x in responsible]
    if state in ("open", "overdue"):
        p["filter[is_completed]"] = 0
    elif state == "completed":
        p["filter[is_completed]"] = 1
    if task_type:
        p["filter[task_type][]"] = [await refs.task_type_id(x) for x in task_type]
    if entity_type:
        p["filter[entity_type]"] = entity_type
    if entity_ids:
        if not entity_type:
            raise AmoError(400, "Для entity_ids укажите entity_type")
        p["filter[entity_id][]"] = entity_ids
    if ids:
        p["filter[id][]"] = ids
    date_range(p, "updated_at", updated_from, updated_to)
    order(p, order_by, order_dir)
    lo, hi = to_ts(due_from), to_ts(due_to, end_of_day=True)
    if state == "overdue":
        hi = min(hi or 10**12, int(time.time()))
    pred = None
    if lo is not None or hi is not None:
        def pred(e):
            ct = e.get("complete_till") or 0
            return (lo is None or ct >= lo) and (hi is None or ct <= hi)
    c = await refs.ctx()
    return await listing("/tasks", "tasks", p, lambda e: refs.task(e, c), page=page, limit=limit,
                         fetch_all=fetch_all, max_items=max_items, export_format=export_format,
                         export_name="tasks", predicate=pred)


class TaskIn(BaseModel):
    text: str
    complete_till: Union[int, str] = Field(..., description="Срок: '2026-11-20 15:00', 'tomorrow', '+2h', unix")
    entity_type: Optional[EntityT] = None
    entity_id: Optional[int] = None
    responsible: Ref = Field(None, description="По умолчанию — владелец токена")
    task_type: Ref = Field(None, description="Звонок / Встреча / ID; по умолчанию Связаться")
    duration_minutes: Optional[int] = None


class TaskUpdate(BaseModel):
    id: int
    text: Optional[str] = None
    complete_till: Ref = None
    responsible: Ref = None
    task_type: Ref = None
    entity_type: Optional[EntityT] = None
    entity_id: Optional[int] = None
    is_completed: Optional[bool] = None
    result_text: Optional[str] = None


async def _task_body(x: BaseModel) -> dict:
    d = x.model_dump()
    b: dict = {}
    if d.get("id"):
        b["id"] = d["id"]
    if d.get("text") is not None:
        b["text"] = d["text"]
    if d.get("complete_till") is not None:
        b["complete_till"] = to_ts(d["complete_till"])
    if d.get("responsible") is not None:
        b["responsible_user_id"] = await refs.user_id(d["responsible"])
    if d.get("task_type") is not None:
        b["task_type_id"] = await refs.task_type_id(d["task_type"])
    if d.get("entity_id"):
        b["entity_id"] = d["entity_id"]
        b["entity_type"] = d.get("entity_type") or "leads"
    if d.get("duration_minutes"):
        b["duration"] = d["duration_minutes"] * 60
    if d.get("is_completed") is not None:
        b["is_completed"] = d["is_completed"]
    if d.get("result_text"):
        b["result"] = {"text": d["result_text"]}
    return b


@tool("tasks", write=True, title="Создать задачи")
async def create_tasks(tasks: list[TaskIn]) -> str:
    """Поставить задачи (звонок, встреча и т.д.) по сделкам/контактам/компаниям или без привязки.
    Повторный одинаковый вызов дублей не создаёт."""
    bodies = [await _task_body(t) for t in tasks]
    res = await safety.apply_create(tool="create_tasks", entity="tasks", bodies=bodies,
                                    args={"tasks": [t.model_dump(exclude_none=True) for t in tasks]},
                                    url="/tasks", result_key="tasks")
    return out(res)


@tool("tasks", write=True, title="Изменить задачи")
async def update_tasks(tasks: list[TaskUpdate], confirm_token: Optional[str] = None) -> str:
    """Изменить задачи: текст, срок (перенос), ответственного, тип, привязку, выполнить с результатом.
    Отменить: undo_change(op_id)."""
    bodies = [b for b in [await _task_body(t) for t in tasks] if len(b) > 1]
    if not bodies:
        raise AmoError(400, "Не указано, что менять")
    res = await safety.apply_patch(tool="update_tasks", entity="tasks", bodies=bodies,
                                   args={"tasks": [t.model_dump(exclude_none=True) for t in tasks]},
                                   confirm_token_value=confirm_token)
    return out(res)


@tool("tasks", write=True, title="Выполнить задачи")
async def complete_tasks(task_ids: list[int], result_text: str = "Выполнено",
                         confirm_token: Optional[str] = None) -> str:
    """Отметить задачи выполненными с текстом результата (amoCRM требует результат).
    Отменить (вернуть в работу): undo_change(op_id)."""
    ids = list(dict.fromkeys(task_ids))
    bodies = [{"id": i, "is_completed": True, "result": {"text": result_text}} for i in ids]
    res = await safety.apply_patch(tool="complete_tasks", entity="tasks", bodies=bodies,
                                   args={"task_ids": ids, "result_text": result_text},
                                   confirm_token_value=confirm_token)
    return out(res)
