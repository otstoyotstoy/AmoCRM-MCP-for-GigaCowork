"""Запуск через WSGI (Passenger на виртуальном хостинге: Рег.ру, Timeweb, Beget…).

Работает только транспорт /mcp в режиме JSON-ответов (без постоянных соединений).
SSE на WSGI-хостинге недоступен. Для боевой работы лучше VPS + uvicorn/Docker.
"""
import asyncio
import os
import threading

os.environ.setdefault("MCP_JSON_RESPONSE", "1")

from a2wsgi import ASGIMiddleware  # noqa: E402

from .config import cfg  # noqa: E402
from .server import build_app, mcp  # noqa: E402

if not cfg.auth_tokens and not cfg.allow_no_auth:
    raise RuntimeError("MCP_AUTH_TOKEN не задан — сервер не будет запущен без защиты")

_loop = asyncio.new_event_loop()
threading.Thread(target=_loop.run_forever, name="mcp-loop", daemon=True).start()

_asgi = build_app(with_sse=False)
_ready = threading.Event()


async def _keep_session_manager():
    async with mcp.session_manager.run():
        _ready.set()
        await asyncio.Event().wait()


asyncio.run_coroutine_threadsafe(_keep_session_manager(), _loop)
if not _ready.wait(15):
    raise RuntimeError("MCP session manager не запустился")

_inner = ASGIMiddleware(_asgi, loop=_loop)
_HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailers",
        "transfer-encoding", "upgrade"}


def application(environ, start_response):
    """WSGI запрещает hop-by-hop заголовки — убираем их из ответа."""
    def _start(status, headers, exc_info=None):
        return start_response(status, [(k, v) for k, v in headers if k.lower() not in _HOP], exc_info)
    return _inner(environ, _start)
