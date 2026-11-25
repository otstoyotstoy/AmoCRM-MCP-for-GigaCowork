"""Настройки сервера из переменных окружения."""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _bool(name: str, default: bool = False) -> bool:
    v = os.environ.get(name)
    if v is None or v == "":
        return default
    return v.strip().lower() in ("1", "true", "yes", "on", "да")


def _int(name: str, default: int) -> int:
    v = os.environ.get(name)
    return int(v) if v not in (None, "") else default


def _list(name: str) -> list[str]:
    return [x.strip() for x in os.environ.get(name, "").split(",") if x.strip()]


ALL_GROUPS = (
    "reference", "admin", "leads", "contacts", "companies", "links", "tasks",
    "notes", "events", "unsorted", "customers", "catalogs", "calls", "reports", "raw", "journal",
)


@dataclass
class Config:
    # amoCRM
    amo_domain: str = field(default_factory=lambda: os.environ.get("AMOCRM_DOMAIN", "").strip())
    amo_zone: str = field(default_factory=lambda: os.environ.get("AMOCRM_ZONE", "ru").strip() or "ru")
    amo_base_url: str = field(default_factory=lambda: os.environ.get("AMOCRM_BASE_URL", "").strip().rstrip("/"))
    amo_token: str = field(default_factory=lambda: os.environ.get("AMOCRM_TOKEN", "").strip())
    timezone: str = field(default_factory=lambda: os.environ.get("AMOCRM_TIMEZONE", "Europe/Moscow"))
    rps: int = field(default_factory=lambda: _int("AMOCRM_RPS", 6))
    timeout: int = field(default_factory=lambda: _int("AMOCRM_TIMEOUT", 30))

    # поведение
    read_only: bool = field(default_factory=lambda: _bool("MCP_READ_ONLY"))
    toolsets: list[str] = field(default_factory=lambda: _list("MCP_TOOLSETS"))
    max_inline_items: int = field(default_factory=lambda: _int("MCP_MAX_INLINE_ITEMS", 500))
    max_fetch_items: int = field(default_factory=lambda: _int("MCP_MAX_FETCH_ITEMS", 50000))

    # безопасность
    allow_destructive: bool = field(default_factory=lambda: _bool("MCP_ALLOW_DESTRUCTIVE"))
    allow_raw_write: bool = field(default_factory=lambda: _bool("MCP_ALLOW_RAW_WRITE"))
    confirm_threshold: int = field(default_factory=lambda: _int("MCP_CONFIRM_THRESHOLD", 10))
    max_write_items: int = field(default_factory=lambda: _int("MCP_MAX_WRITE_ITEMS", 500))
    max_bulk_items: int = field(default_factory=lambda: _int("MCP_MAX_BULK_ITEMS", 2000))
    audit_dir: str = field(default_factory=lambda: os.environ.get("MCP_AUDIT_DIR", ""))
    dedupe_minutes: int = field(default_factory=lambda: _int("MCP_DEDUPE_MINUTES", 10))

    # HTTP
    json_response: bool = field(default_factory=lambda: _bool("MCP_JSON_RESPONSE"))
    transport: str = field(default_factory=lambda: os.environ.get("MCP_TRANSPORT", "streamable-http"))
    host: str = field(default_factory=lambda: os.environ.get("MCP_HOST", "0.0.0.0"))
    port: int = field(default_factory=lambda: _int("MCP_PORT", 8000))
    auth_tokens: list[str] = field(default_factory=lambda: _list("MCP_AUTH_TOKEN"))
    allow_no_auth: bool = field(default_factory=lambda: _bool("MCP_ALLOW_NO_AUTH"))
    public_base_url: str = field(default_factory=lambda: os.environ.get("PUBLIC_BASE_URL", "").strip().rstrip("/"))
    export_dir: str = field(default_factory=lambda: os.environ.get("EXPORT_DIR", "/tmp/amocrm-exports"))
    export_ttl_hours: int = field(default_factory=lambda: _int("EXPORT_TTL_HOURS", 24))

    @property
    def base_url(self) -> str:
        if self.amo_base_url:
            return self.amo_base_url if self.amo_base_url.startswith("http") else f"https://{self.amo_base_url}"
        d = self.amo_domain
        if not d:
            return ""
        d = d.replace("https://", "").replace("http://", "").strip("/")
        if "." not in d:
            d = f"{d}.kommo.com" if self.amo_zone == "kommo" else f"{d}.amocrm.{self.amo_zone}"
        return f"https://{d}"

    def group_enabled(self, group: str) -> bool:
        return not self.toolsets or "all" in self.toolsets or group in self.toolsets


cfg = Config()
