"""Точка входа: HTTP (Streamable HTTP + SSE) с авторизацией или stdio."""
import base64
import hmac
import json
import logging
import sys
from urllib.parse import parse_qs

from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse

from . import exports
from .config import cfg
from .core import REGISTERED, compact_schemas, mcp
from .tools import activity, contacts, extra, journal, leads, reference, reports, tasks  # noqa: F401  (регистрация)

compact_schemas()

log = logging.getLogger("amocrm_mcp")
OPEN_PATHS = ("/health",)


@mcp.custom_route("/health", methods=["GET"])
async def health(request: Request):
    res = {"ok": True, "tools": len(REGISTERED), "amocrm": cfg.base_url or None, "read_only": cfg.read_only,
           "allow_destructive": cfg.allow_destructive, "allow_raw_write": cfg.allow_raw_write}
    if request.query_params.get("deep"):
        from . import safety
        from .client import amo
        try:
            safety.ensure_audit_writable()
            res["audit"] = "ok"
        except Exception as e:  # noqa: BLE001
            res.update({"ok": False, "audit": str(e)})
        try:
            acc = await amo.get("/account")
            res["amocrm_account"] = (acc or {}).get("subdomain")
        except Exception as e:  # noqa: BLE001
            res.update({"ok": False, "amocrm_error": str(e)})
    return JSONResponse(res, status_code=200 if res["ok"] else 503)


@mcp.custom_route("/files/{name}", methods=["GET"])
async def download(request: Request):
    p = exports.resolve(request.path_params["name"])
    if not p:
        return JSONResponse({"error": "Файл не найден или срок хранения истёк"}, status_code=404)
    return FileResponse(p, filename=p.name.split("__", 1)[1])


class AuthMiddleware:
    """Принимает токен из Authorization: Bearer, X-API-Key или ?key= / ?token= в URL."""

    def __init__(self, app, tokens: list[str]):
        self.app = app
        self.tokens = [t.encode() for t in tokens]

    def _ok(self, scope) -> bool:
        headers = {k.lower(): v for k, v in scope.get("headers") or []}
        cands = []
        auth = headers.get(b"authorization", b"")
        if auth[:7].lower() == b"bearer ":
            cands.append(auth[7:].strip())
        if auth[:6].lower() == b"basic ":  # логин любой, пароль = MCP_AUTH_TOKEN
            try:
                cands.append(base64.b64decode(auth[6:].strip()).split(b":", 1)[-1])
            except Exception:
                pass
        if headers.get(b"x-api-key"):
            cands.append(headers[b"x-api-key"].strip())
        qs = parse_qs((scope.get("query_string") or b"").decode())
        cands += [v.encode() for k in ("key", "token") for v in qs.get(k, [])]
        return any(hmac.compare_digest(c, t) for c in cands for t in self.tokens)

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not self.tokens:
            return await self.app(scope, receive, send)
        path = scope.get("path", "")
        if path in OPEN_PATHS or path.startswith("/files/") or scope.get("method") == "OPTIONS" or self._ok(scope):
            return await self.app(scope, receive, send)
        hdrs = {k.decode().lower(): v.decode(errors="replace") for k, v in scope.get("headers") or []}
        scheme = (hdrs.get("authorization", "").split(" ", 1)[0] or "нет")
        qs_keys = sorted(parse_qs((scope.get("query_string") or b"").decode()).keys())
        log.warning("401 %s %s | auth=%s | query=%s | headers=%s | ua=%s", scope.get("method"), path, scheme,
                    qs_keys, sorted(k for k in hdrs if k not in ("cookie",)), hdrs.get("user-agent", "")[:80])
        body = json.dumps({"error": "unauthorized", "message": "Нужен токен MCP: Authorization: Bearer <токен> или Basic (пароль = токен)"}).encode()
        await send({"type": "http.response.start", "status": 401,
                    "headers": [(b"content-type", b"application/json"),
                                (b"www-authenticate", b'Bearer, Basic realm="amocrm-mcp"')]})
        await send({"type": "http.response.body", "body": body})


def build_app(with_sse: bool = True):
    app = mcp.streamable_http_app()          # /mcp  (Streamable HTTP, основной транспорт)
    if with_sse:
        sse = mcp.sse_app()                  # /sse + /messages/  (для старых клиентов)
        have = {getattr(r, "path", None) for r in app.router.routes}
        for r in sse.routes:
            if getattr(r, "path", None) not in have:
                app.router.routes.append(r)
    return AuthMiddleware(app, cfg.auth_tokens)


def main():
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    transport = (sys.argv[1] if len(sys.argv) > 1 else cfg.transport).lower()
    if not cfg.base_url or not cfg.amo_token:
        log.warning("AMOCRM_DOMAIN / AMOCRM_TOKEN не заданы — инструменты будут возвращать ошибку настройки")
    if transport == "stdio":
        mcp.run("stdio")
        return
    if not cfg.auth_tokens and not cfg.allow_no_auth:
        sys.exit("MCP_AUTH_TOKEN не задан. Без него любой, кто знает адрес, получит доступ к CRM. "
                 "Задайте токен (openssl rand -hex 32) или явно MCP_ALLOW_NO_AUTH=1 для локальных тестов.")
    import uvicorn
    log.info("amoCRM MCP: %d инструментов, http://%s:%s/mcp", len(REGISTERED), cfg.host, cfg.port)
    uvicorn.run(build_app(), host=cfg.host, port=cfg.port, proxy_headers=True, forwarded_allow_ips="*",
                log_level="warning")


if __name__ == "__main__":
    main()
