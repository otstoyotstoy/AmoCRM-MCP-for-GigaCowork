"""Защита данных клиента: журнал изменений со снимком «до», подтверждение крупных и опасных
операций, защита от повторного создания, пакетная запись с отчётом о частичных сбоях."""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import secrets
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable

from .client import AmoError, amo
from .config import cfg

# ---------------------------------------------------------------- хранилище

def _dir() -> Path:
    d = Path(cfg.audit_dir) if cfg.audit_dir else Path(__file__).resolve().parent.parent / "audit"
    d.mkdir(parents=True, exist_ok=True)
    return d


def ensure_audit_writable() -> None:
    """Перед любым изменением: если журнал недоступен — ничего не меняем (иначе изменение нельзя отменить)."""
    try:
        d = _dir()
        probe = d / ".write-test"
        probe.write_text("ok")
        probe.unlink()
    except OSError as e:
        raise AmoError(500, f"Журнал изменений недоступен для записи ({e}). Изменения НЕ выполнены. "
                            "Проверьте MCP_AUDIT_DIR и права на папку.")


def _canon(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":"))


def _salt() -> str:
    # одинаковая соль во всех процессах сервера, но не угадываемая снаружи
    base = (cfg.auth_tokens[0] if cfg.auth_tokens else "") + cfg.amo_token + cfg.base_url
    return hashlib.sha256(("amocrm-mcp:" + base).encode()).hexdigest()


# ---------------------------------------------------------------- подтверждение

def confirm_token(tool: str, args: dict) -> str:
    a = {k: v for k, v in args.items() if k not in ("confirm", "confirm_token")}
    return hashlib.sha256((_salt() + tool + _canon(a)).encode()).hexdigest()[:10]


def is_confirmed(tool: str, args: dict, token: str | None) -> bool:
    return bool(token) and secrets.compare_digest(str(token), confirm_token(tool, args))


def need_confirmation(tool: str, args: dict, reason: str, preview: Any) -> dict:
    return {
        "ok": False, "needs_confirmation": True, "applied": False,
        "reason": reason, "preview": preview,
        "confirm_token": confirm_token(tool, args),
        "next_step": "Покажите пользователю, что изменится, и после его согласия повторите вызов с теми же аргументами "
                     "и confirm_token. Без токена изменения не применяются.",
    }


def require_destructive(what: str) -> None:
    if not cfg.allow_destructive:
        raise AmoError(403, f"{what} отключено настройками сервера (MCP_ALLOW_DESTRUCTIVE=0). "
                            "Это защита от необратимой потери данных; включает администратор сервера.")


# ---------------------------------------------------------------- журнал изменений

def audit(op: str, tool: str, entity: str, *, args: Any = None, before: list | None = None,
          ids: Iterable | None = None, touched: dict | None = None, result: Any = None,
          error: str | None = None) -> str:
    op_id = datetime.now().strftime("%y%m%d") + "-" + secrets.token_hex(3)
    rec = {"op_id": op_id, "ts": int(time.time()), "time": datetime.now().isoformat(timespec="seconds"),
           "op": op, "tool": tool, "entity": entity, "ids": list(ids or []), "touched": touched or {},
           "args": args, "before": before or [], "result": result, "error": error}
    try:
        path = _dir() / f"audit-{datetime.now():%Y-%m}.jsonl"
        with open(path, "a", encoding="utf-8") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            fh.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
            fcntl.flock(fh, fcntl.LOCK_UN)
        os.chmod(path, 0o600)
    except OSError as e:  # изменение уже применено — не скрываем это ошибкой, но сообщаем в лог сервера
        import logging
        logging.getLogger("amocrm_mcp").error("Не удалось записать журнал %s: %s | %s", op_id, e,
                                              json.dumps(rec, ensure_ascii=False, default=str)[:2000])
        return op_id + "-not-saved"
    return op_id


def read_audit(limit: int = 50, op_id: str | None = None) -> list[dict]:
    files = sorted(_dir().glob("audit-*.jsonl"), reverse=True)
    out: list[dict] = []
    for f in files:
        for line in reversed(f.read_text(encoding="utf-8").splitlines()):
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if op_id and rec.get("op_id") != op_id:
                continue
            out.append(rec)
            if len(out) >= limit or op_id:
                return out
    return out


# ---------------------------------------------------------------- защита от дублей

def dedupe_check(tool: str, payload: Any) -> dict | None:
    if cfg.dedupe_minutes <= 0:
        return None
    key = hashlib.sha256((tool + _canon(payload)).encode()).hexdigest()
    path = _dir() / "dedupe.json"
    now = time.time()
    with open(path, "a+", encoding="utf-8") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        fh.seek(0)
        try:
            data = json.loads(fh.read() or "{}")
        except ValueError:
            data = {}
        data = {k: v for k, v in data.items() if now - v.get("ts", 0) < cfg.dedupe_minutes * 60}
        hit = data.get(key)
        fcntl.flock(fh, fcntl.LOCK_UN)
    return {"key": key, "hit": hit}


def dedupe_save(key: str, result: Any) -> None:
    if cfg.dedupe_minutes <= 0:
        return
    path = _dir() / "dedupe.json"
    now = time.time()
    with open(path, "a+", encoding="utf-8") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        fh.seek(0)
        try:
            data = json.loads(fh.read() or "{}")
        except ValueError:
            data = {}
        data = {k: v for k, v in data.items() if now - v.get("ts", 0) < cfg.dedupe_minutes * 60}
        data[key] = {"ts": now, "result": result}
        fh.seek(0)
        fh.truncate()
        fh.write(json.dumps(data, ensure_ascii=False, default=str))
        fcntl.flock(fh, fcntl.LOCK_UN)


# ---------------------------------------------------------------- снимки

SNAPSHOT = {  # сущность → (путь списка, ключ, with)
    "leads": ("/leads", "leads", "contacts,loss_reason"),
    "contacts": ("/contacts", "contacts", "leads,companies"),
    "companies": ("/companies", "companies", "leads,contacts"),
    "customers": ("/customers", "customers", ""),
    "tasks": ("/tasks", "tasks", ""),
}


def _strip(e: dict) -> dict:
    e = {k: v for k, v in e.items() if k != "_links"}
    emb = e.get("_embedded")
    if isinstance(emb, dict):
        e["_embedded"] = {k: [{kk: vv for kk, vv in x.items() if kk != "_links"} for x in v]
                          if isinstance(v, list) else v for k, v in emb.items()}
    return e


async def snapshot(entity: str, ids: list[int], path: str | None = None, key: str | None = None) -> dict[int, dict]:
    """Текущее состояние записей перед изменением."""
    if path is None:
        path, key, with_ = SNAPSHOT[entity]
    else:
        with_ = ""
    res: dict[int, dict] = {}
    uniq = list(dict.fromkeys(int(i) for i in ids))
    for i in range(0, len(uniq), 50):
        part = uniq[i:i + 50]
        items, _ = await amo.list_page(path, key, {"filter[id][]": part, "with": with_ or None}, limit=50)
        for e in items:
            res[e["id"]] = _strip(e)
    return res


def check_limit(n: int, what: str = "записей", limit: int | None = None) -> None:
    lim = limit or cfg.max_write_items
    if n > lim:
        raise AmoError(400, f"За один вызов можно изменить не более {lim} {what} "
                            f"(MCP_MAX_WRITE_ITEMS / MCP_MAX_BULK_ITEMS). Разбейте на части или сузьте фильтр.")
    if n == 0:
        raise AmoError(400, "Пустой список — менять нечего")


def touched_fields(bodies: list[dict]) -> dict:
    keys, cf = set(), set()
    for b in bodies:
        for k in b:
            if k != "id":
                keys.add(k)
        for f in b.get("custom_fields_values") or []:
            cf.add(f.get("field_id"))
        if "_embedded" in b and "tags" in (b["_embedded"] or {}):
            keys.add("tags")
        if "tags_to_add" in b or "tags_to_delete" in b:
            keys.add("tags")
    return {"keys": sorted(keys), "cf": sorted(x for x in cf if x is not None)}


# ---------------------------------------------------------------- применение изменений

def _summary(before: dict, body: dict) -> dict:
    """Короткое «было → станет» для предпросмотра."""
    ch: dict[str, Any] = {}
    for k, v in body.items():
        if k in ("id",):
            continue
        if k == "custom_fields_values":
            ch["custom_fields"] = {str(f["field_id"]): {"было": next(
                ([x.get("value") for x in c.get("values") or []] for c in before.get("custom_fields_values") or []
                 if c.get("field_id") == f["field_id"]), None),
                "станет": [x.get("value", x.get("enum_id")) for x in (f.get("values") or [])] if f.get("values") else None}
                for f in v}
        elif k in ("tags_to_add", "tags_to_delete"):
            ch[k] = [t.get("name") or t.get("id") for t in v]
        elif k == "_embedded":
            if "tags" in v:
                ch["tags"] = {"было": [t.get("name") for t in (before.get("_embedded") or {}).get("tags") or []],
                              "станет": [t.get("name") or t.get("id") for t in v["tags"]]}
        else:
            ch[k] = {"было": before.get(k), "станет": v}
    return {"id": body.get("id"), "name": before.get("name") or before.get("text"), "changes": ch}


async def apply_patch(*, tool: str, entity: str, bodies: list[dict], args: dict,
                      confirm_token_value: str | None, path: str | None = None,
                      snap_path: str | None = None, snap_key: str | None = None,
                      before: dict[int, dict] | None = None, force_confirm: str | None = None,
                      limit: int | None = None) -> dict:
    """PATCH с журналом, подтверждением крупных изменений и отчётом о частичных сбоях."""
    check_limit(len(bodies), limit=limit)
    ensure_audit_writable()
    ids = [b["id"] for b in bodies]
    if before is None:
        before = await snapshot(entity, ids, snap_path, snap_key)
    missing = [i for i in ids if i not in before]
    if missing:
        raise AmoError(404, f"Не найдены (или нет доступа): {missing[:20]}. Ничего не изменено.")
    reason = force_confirm or (f"Изменение затрагивает {len(bodies)} записей (порог подтверждения "
                               f"{cfg.confirm_threshold})" if len(bodies) > cfg.confirm_threshold else None)
    if reason and not is_confirmed(tool, args, confirm_token_value):
        return need_confirmation(tool, args, reason,
                                 {"count": len(bodies), "items": [_summary(before[b["id"]], b) for b in bodies[:10]]})
    url = path or f"/{entity}"
    done, failed, err = [], [], None
    for i in range(0, len(bodies), 50):
        part = bodies[i:i + 50]
        try:
            await amo.patch(url, part)
            done += [b["id"] for b in part]
        except AmoError as e:
            err = str(e)
            failed = [b["id"] for b in bodies[i:]]
            break
    op_id = audit("update", tool, entity, args=args, before=[before[i] for i in done + failed if i in before],
                  ids=done, touched=touched_fields(bodies), result={"done": len(done), "failed": failed}, error=err)
    res: dict[str, Any] = {"ok": not failed, "updated": len(done), "ids": done, "op_id": op_id}
    if failed:
        res.update({"failed_ids": failed, "error": err,
                    "note": "Применено частично: изменены только записи из ids. Остальные не тронуты. "
                            f"Отменить применённое: undo_change(op_id='{op_id}')."})
    else:
        res["undo"] = f"undo_change(op_id='{op_id}')"
    return res


async def apply_create(*, tool: str, entity: str, bodies: list[dict], args: dict, url: str,
                       result_key: str, after: Callable | None = None) -> dict:
    """POST с защитой от повторного создания и отчётом о частичных сбоях."""
    check_limit(len(bodies))
    ensure_audit_writable()
    dd = dedupe_check(tool, args)
    if dd and dd["hit"]:
        prev = dict(dd["hit"]["result"])
        prev.update({"duplicate_call": True,
                     "note": f"Такой же вызов уже выполнялся менее {cfg.dedupe_minutes} мин назад — повторно не создаю. "
                             "Если действительно нужны копии, измените данные (например, название)."})
        return prev
    created, err, failed_from = [], None, None
    for i in range(0, len(bodies), 50):
        part = bodies[i:i + 50]
        try:
            data = await amo.post(url, part)
        except AmoError as e:
            err, failed_from = str(e), i
            break
        new = data if isinstance(data, list) else ((data or {}).get("_embedded") or {}).get(result_key) or []
        ids = [x.get("id") or x.get("uid") for x in new]
        created += ids
        if after:
            for j, nid in enumerate(ids):
                try:
                    await after(nid, i + j, x=new[j])
                except AmoError as e:
                    err = f"Запись {nid} создана, но шаг после создания (привязка) не выполнен: {e}"
    op_id = audit("create", tool, entity, args=args, ids=created, result={"created": created}, error=err)
    res: dict[str, Any] = {"ok": err is None, "created": len(created), "ids": created, "op_id": op_id}
    if err:
        res["error"] = err
        if failed_from is not None:
            res["not_created_count"] = len(bodies) - failed_from
            res["note"] = (f"Создано {len(created)} из {len(bodies)}. Остальные НЕ созданы — повторите только их, "
                           "иначе получатся дубли.")
    if dd and not err:
        dedupe_save(dd["key"], res)
    return res
