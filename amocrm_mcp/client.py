"""HTTP-клиент amoCRM API v4: ограничение частоты, повторы, понятные ошибки."""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Callable

import httpx

from .config import cfg

log = logging.getLogger("amocrm_mcp")

MAX_PAGE = 250


class AmoError(Exception):
    def __init__(self, status: int, message: str):
        self.status = status
        super().__init__(message)


_HUMAN = {
    400: "Некорректный запрос",
    401: "Токен amoCRM недействителен или истёк — выпустите новый долгосрочный токен",
    402: "Аккаунт amoCRM не оплачен",
    403: "Нет прав на это действие (или аккаунт заблокирован / IP запрещён)",
    404: "Не найдено",
    429: "Превышен лимит запросов amoCRM",
}


def _format_error(resp: httpx.Response) -> str:
    status = resp.status_code
    base = _HUMAN.get(status, f"Ошибка amoCRM {status}")
    try:
        data = resp.json()
    except Exception:
        text = resp.text.strip()[:300]
        return f"{base} (HTTP {status}){': ' + text if text else ''}"
    parts = [f"{base} (HTTP {status})"]
    if isinstance(data, dict):
        for k in ("title", "detail", "hint"):
            if data.get(k) and str(data.get(k)) not in parts[0]:
                parts.append(str(data[k]))
        ve = data.get("validation-errors") or data.get("validation_errors")
        if ve:
            details = []
            for block in ve:
                for e in block.get("errors", []) if isinstance(block, dict) else []:
                    details.append(f"{e.get('path', '?')}: {e.get('detail') or e.get('code')}")
            if details:
                parts.append("Ошибки валидации: " + "; ".join(details[:20]))
    return " — ".join(parts)


class AmoClient:
    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None
        self._lock = asyncio.Lock()
        self._last = 0.0

    def _http(self) -> httpx.AsyncClient:
        if not cfg.base_url or not cfg.amo_token:
            raise AmoError(0, "Сервер не настроен: задайте AMOCRM_DOMAIN и AMOCRM_TOKEN")
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=cfg.base_url,
                headers={
                    "Authorization": f"Bearer {cfg.amo_token}",
                    "Accept": "application/json",
                    "User-Agent": "amocrm-mcp/2.0",
                },
                timeout=httpx.Timeout(cfg.timeout, connect=10),
            )
        return self._client

    async def _throttle(self) -> None:
        interval = 1.0 / max(cfg.rps, 1)
        async with self._lock:
            wait = self._last + interval - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            self._last = time.monotonic()

    async def request(self, method: str, path: str, *, params: dict | None = None,
                      json: Any = None, retries: int = 4) -> Any:
        if not path.startswith("/"):
            path = "/" + path
        if not path.startswith("/api/"):
            path = "/api/v4" + path
        client = self._http()
        clean = {k: v for k, v in (params or {}).items() if v is not None and v != "" and v != []}
        # POST (создание) не идемпотентен: повторяем только если запрос точно не был обработан
        # (не удалось соединиться или amoCRM ответил 429). Иначе повтор может создать дубль.
        safe_retry = method.upper() != "POST"
        for attempt in range(retries + 1):
            await self._throttle()
            try:
                resp = await client.request(method, path, params=clean, json=json)
            except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout) as e:
                if attempt < retries:
                    await asyncio.sleep(1.5 * (attempt + 1))
                    continue
                raise AmoError(0, f"Не удалось соединиться с amoCRM ({method} {path}): {e}. Изменения не выполнены.")
            except httpx.TimeoutException:
                if safe_retry and attempt < retries:
                    await asyncio.sleep(1.5 * (attempt + 1))
                    continue
                hint = (" Запрос мог быть выполнен — проверьте результат поиском, прежде чем повторять."
                        if not safe_retry else "")
                raise AmoError(0, f"amoCRM не ответил за {cfg.timeout} с ({method} {path}).{hint}")
            except httpx.HTTPError as e:
                if safe_retry and attempt < retries:
                    await asyncio.sleep(1.5 * (attempt + 1))
                    continue
                hint = " Запрос мог быть выполнен — проверьте результат, прежде чем повторять." if not safe_retry else ""
                raise AmoError(0, f"Сетевая ошибка при обращении к amoCRM: {e}.{hint}")
            retryable = (429,) if not safe_retry else (429, 502, 503, 504)
            if resp.status_code in retryable and attempt < retries:
                ra = resp.headers.get("Retry-After")
                delay = float(ra) if ra and ra.replace(".", "").isdigit() else 1.0 * (2 ** attempt)
                log.warning("amoCRM %s, повтор через %.1f с", resp.status_code, delay)
                await asyncio.sleep(min(delay, 20))
                continue
            if resp.status_code == 204:
                return None
            if 200 <= resp.status_code < 300:
                if not resp.content:
                    return None
                try:
                    return resp.json()
                except ValueError:
                    return {"raw": resp.text}
            raise AmoError(resp.status_code, _format_error(resp))
        raise AmoError(0, "Не удалось выполнить запрос к amoCRM")

    async def get(self, path: str, params: dict | None = None) -> Any:
        return await self.request("GET", path, params=params)

    async def post(self, path: str, body: Any) -> Any:
        return await self.request("POST", path, json=body)

    async def patch(self, path: str, body: Any) -> Any:
        return await self.request("PATCH", path, json=body)

    async def delete(self, path: str, body: Any = None) -> Any:
        return await self.request("DELETE", path, json=body)

    async def list_page(self, path: str, key: str, params: dict | None = None,
                        page: int = 1, limit: int = 50) -> tuple[list[dict], bool]:
        """Одна страница списка. Возвращает (элементы, есть_ли_следующая)."""
        p = dict(params or {})
        p["page"] = page
        p["limit"] = max(1, min(limit, MAX_PAGE))
        data = await self.get(path, p)
        if not data:
            return [], False
        items = (data.get("_embedded") or {}).get(key) or []
        has_next = bool((data.get("_links") or {}).get("next")) and len(items) >= p["limit"]
        return items, has_next

    async def list_all(self, path: str, key: str, params: dict | None = None,
                       max_items: int | None = None, limit: int = MAX_PAGE,
                       predicate: Callable[[dict], bool] | None = None) -> tuple[list[dict], bool]:
        """Все страницы списка (до max_items). Возвращает (элементы, обрезано_ли)."""
        cap = max_items or cfg.max_fetch_items
        out: list[dict] = []
        page = 1
        while True:
            items, has_next = await self.list_page(path, key, params, page=page, limit=limit)
            for it in items:
                if predicate is None or predicate(it):
                    out.append(it)
                    if len(out) >= cap:
                        return out, has_next or it is not items[-1]
            if not has_next:
                return out, False
            page += 1


amo = AmoClient()
