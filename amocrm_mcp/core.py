"""Экземпляр MCP-сервера, регистрация инструментов и общие помощники ответов."""
from __future__ import annotations

import functools
import json
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from .client import AmoError
from .config import cfg

INSTRUCTIONS = """Сервер полного доступа к amoCRM (API v4) с защитой данных.

Как работать:
• Воронки, этапы, пользователей, поля, теги, причины отказа и типы задач можно указывать НАЗВАНИЯМИ — сервер сам найдёт ID. Если название неоднозначно, придёт ошибка со списком вариантов. Справочники: list_pipelines, list_users, list_custom_fields, list_loss_reasons, list_tags, get_account_info.
• Пользовательские поля передаются словарём {"Название или ID поля": значение}. Для списков — текст варианта, для дат — 'YYYY-MM-DD', для мультисписков — массив. null очищает поле.
• Даты: 'YYYY-MM-DD', 'YYYY-MM-DD HH:MM', 'DD.MM.YYYY', unix timestamp, 'today', 'tomorrow', 'yesterday', '+3d', '-2h'. Часовой пояс аккаунта.
• Большие выборки: в search_* укажите fetch_all=true (до max_items записей) или export_format='xlsx'/'csv' — придёт ссылка на файл. Для сводок — leads_report / tasks_report.

Правила безопасности (обязательно соблюдать):
• Если ответ содержит needs_confirmation=true — НИЧЕГО ЕЩЁ НЕ ИЗМЕНЕНО. Покажите пользователю preview (что изменится, «было → станет») и спросите согласия. Только после явного «да» повторите тот же вызов с теми же аргументами и confirm_token. Никогда не подставляйте confirm_token без согласия пользователя.
• Массовые изменения (bulk_update_leads), изменения больше нескольких записей, отклонение заявок, удаление настроек и откат всегда проходят через такое подтверждение.
• Перед массовым изменением уточняйте у пользователя фильтр; пустой фильтр запрещён.
• Каждое изменение возвращает op_id. Ошибку можно отменить: undo_change(op_id). История: get_change_log.
• Если ответ ok=false с failed_ids — изменения применены ЧАСТИЧНО: сообщите пользователю, что применено, а что нет; не повторяйте всю операцию целиком.
• Если при создании пришла ошибка таймаута — запрос мог выполниться: сначала проверьте поиском, создалось ли, и только потом повторяйте.
• duplicate_call=true означает, что такой же вызов уже выполнен — повторно ничего не создано.
• Сделки, контакты и компании через API НЕ удаляются. Вместо удаления: закрыть сделку (close_leads), снять теги, отвязать, сменить ответственного.
• Телефоны и email контактов по умолчанию ДОБАВЛЯЮТСЯ; заменять — только если пользователь прямо попросил (replace_phones / replace_emails).
• При смене этапа без указания воронки этап ищется в текущей воронке сделки. Для переноса в другую воронку указывайте pipeline явно.
"""

mcp = FastMCP(
    "amoCRM",
    instructions=INSTRUCTIONS,
    host=cfg.host,
    port=cfg.port,
    stateless_http=True,
    json_response=cfg.json_response,
    log_level="WARNING",
)

REGISTERED: list[str] = []


def tool(group: str, *, write: bool = False, destructive: bool = False, title: str | None = None):
    """Регистрирует инструмент, если включена его группа (MCP_TOOLSETS) и он разрешён режимом MCP_READ_ONLY."""

    def deco(fn):
        if not cfg.group_enabled(group) or (write and cfg.read_only):
            return fn

        @functools.wraps(fn)
        async def wrapper(*args, **kwargs):
            try:
                return await fn(*args, **kwargs)
            except AmoError as e:
                raise ValueError(str(e)) from None

        mcp.tool(annotations=ToolAnnotations(
            title=title, readOnlyHint=not write, destructiveHint=destructive,
            idempotentHint=not write, openWorldHint=True,
        ))(wrapper)
        REGISTERED.append(fn.__name__)
        return fn

    return deco


def _compact(node):
    """Ужимает JSON-схему: убирает title, сворачивает anyOf[X, null] → X. Валидация не меняется."""
    if isinstance(node, list):
        return [_compact(x) for x in node]
    if not isinstance(node, dict):
        return node
    node = {k: _compact(v) for k, v in node.items() if k != "title"}
    any_of = node.get("anyOf")
    if isinstance(any_of, list):
        rest = [x for x in any_of if x != {"type": "null"}]
        if all(set(x) == {"type"} and isinstance(x["type"], str) for x in rest) and rest:
            node.pop("anyOf")
            types = [x["type"] for x in rest]
            node["type"] = types[0] if len(types) == 1 else types
        elif len(rest) == 1:
            node.pop("anyOf")
            node = {**rest[0], **node}
        else:
            node["anyOf"] = rest
    if node.get("default", 0) is None:
        node.pop("default")
    return node


def compact_schemas() -> None:
    for t in mcp._tool_manager.list_tools():
        t.parameters = _compact(t.parameters)


MAX_CHARS = 180_000


def out(data: Any) -> str:
    """Компактный JSON для модели; слишком большие списки обрезаются с подсказкой."""
    s = json.dumps(data, ensure_ascii=False, separators=(",", ":"), default=str)
    if len(s) <= MAX_CHARS or not isinstance(data, dict) or not isinstance(data.get("items"), list):
        return s
    items = data["items"]
    keep = len(items)
    while keep > 1:
        keep = int(keep * 0.7)
        trial = {**data, "items": items[:keep], "returned": keep, "truncated": True,
                 "hint": "Ответ слишком большой — показана часть. Для полного списка используйте export_format='xlsx' или сузьте фильтры."}
        s = json.dumps(trial, ensure_ascii=False, separators=(",", ":"), default=str)
        if len(s) <= MAX_CHARS:
            return s
    return s[:MAX_CHARS]


def chunks(seq: list, n: int = 50):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]
