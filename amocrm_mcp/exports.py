"""Выгрузка больших списков в CSV/XLSX со ссылкой на скачивание."""
from __future__ import annotations

import csv
import os
import re
import secrets
import time
from pathlib import Path
from typing import Any

from .config import cfg

_SAFE = re.compile(r"[^A-Za-z0-9_.-]+")


def _flatten(row: dict) -> dict:
    out: dict[str, Any] = {}
    for k, v in row.items():
        if k == "custom_fields" and isinstance(v, dict):
            for ck, cv in v.items():
                out[ck] = "; ".join(map(str, cv)) if isinstance(cv, list) else cv
        elif isinstance(v, list):
            out[k] = "; ".join(map(str, v))
        elif isinstance(v, dict):
            out[k] = "; ".join(f"{a}={b}" for a, b in v.items())
        else:
            out[k] = v
    return out


def _cleanup(d: Path) -> None:
    limit = time.time() - cfg.export_ttl_hours * 3600
    for p in d.glob("*"):
        try:
            if p.stat().st_mtime < limit:
                p.unlink()
        except OSError:
            pass


def save(rows: list[dict], fmt: str, basename: str) -> dict:
    d = Path(cfg.export_dir)
    d.mkdir(parents=True, exist_ok=True)
    _cleanup(d)
    flat = [_flatten(r) for r in rows]
    cols: list[str] = []
    for r in flat:
        for k in r:
            if k not in cols:
                cols.append(k)
    fmt = "xlsx" if fmt == "xlsx" else "csv"
    name = f"{secrets.token_urlsafe(18)}__{_SAFE.sub('_', basename)}_{time.strftime('%Y%m%d_%H%M')}.{fmt}"
    path = d / name
    if fmt == "csv":
        with open(path, "w", newline="", encoding="utf-8-sig") as fh:
            w = csv.DictWriter(fh, fieldnames=cols, delimiter=";", extrasaction="ignore")
            w.writeheader()
            w.writerows(flat)
    else:
        from openpyxl import Workbook
        from openpyxl.utils import get_column_letter
        wb = Workbook(write_only=False)
        ws = wb.active
        ws.title = basename[:30] or "data"
        ws.append(cols)
        for r in flat:
            ws.append([r.get(c) for c in cols])
        ws.freeze_panes = "A2"
        for i, c in enumerate(cols, 1):
            ws.column_dimensions[get_column_letter(i)].width = min(max(len(str(c)) + 2, 12), 50)
        wb.save(path)
    os.chmod(path, 0o600)
    res: dict[str, Any] = {"file": name.split("__", 1)[1], "rows": len(flat), "columns": len(cols),
                           "expires_in_hours": cfg.export_ttl_hours}
    if cfg.public_base_url:
        res["download_url"] = f"{cfg.public_base_url}/files/{name}"
    else:
        res["path_on_server"] = str(path)
        res["note"] = "PUBLIC_BASE_URL не задан — ссылку на скачивание сформировать нельзя"
    return res


def resolve(name: str) -> Path | None:
    if "/" in name or "\\" in name or ".." in name or "__" not in name:
        return None
    p = Path(cfg.export_dir) / name
    return p if p.is_file() else None
