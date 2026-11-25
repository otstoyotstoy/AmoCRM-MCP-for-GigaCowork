"""Даты: человеко-читаемый ввод → unix timestamp и обратно (в часовом поясе аккаунта)."""
from __future__ import annotations

from datetime import datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo

from .config import cfg

_FORMATS = ("%d.%m.%Y %H:%M:%S", "%d.%m.%Y %H:%M", "%d.%m.%Y", "%Y-%m-%d %H:%M", "%Y-%m-%d")


def tz() -> ZoneInfo:
    return ZoneInfo(cfg.timezone)


def now() -> datetime:
    return datetime.now(tz())


def to_ts(value, end_of_day: bool = False) -> int | None:
    """Принимает unix timestamp, 'YYYY-MM-DD', 'YYYY-MM-DD HH:MM', ISO 8601, 'DD.MM.YYYY',
    а также 'today'/'сегодня', 'tomorrow'/'завтра', 'yesterday'/'вчера', '+3d', '-2h'."""
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise ValueError("Дата не может быть true/false")
    if isinstance(value, (int, float)):
        return int(value)
    s = str(value).strip()
    if s.lstrip("-").isdigit():
        return int(s)
    low = s.lower()
    rel_days = {"today": 0, "сегодня": 0, "tomorrow": 1, "завтра": 1, "yesterday": -1, "вчера": -1}
    if low in rel_days:
        d = (now() + timedelta(days=rel_days[low])).date()
        t = dtime(23, 59, 59) if end_of_day else dtime(0, 0)
        return int(datetime.combine(d, t, tz()).timestamp())
    if low in ("now", "сейчас"):
        return int(now().timestamp())
    if low[:1] in "+-" and low[-1:] in "mhdw" and low[1:-1].isdigit():
        n = int(low[1:-1]) * (1 if low[0] == "+" else -1)
        unit = {"m": "minutes", "h": "hours", "d": "days", "w": "weeks"}[low[-1]]
        return int((now() + timedelta(**{unit: n})).timestamp())
    date_only = len(s) == 10
    dt = None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        for f in _FORMATS:
            try:
                dt = datetime.strptime(s, f)
                break
            except ValueError:
                continue
    if dt is None:
        raise ValueError(f"Не понял дату «{value}». Формат: YYYY-MM-DD, YYYY-MM-DD HH:MM, DD.MM.YYYY или unix timestamp")
    if date_only and end_of_day:
        dt = dt.replace(hour=23, minute=59, second=59)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz())
    return int(dt.timestamp())


def fmt(ts) -> str | None:
    if ts in (None, 0, ""):
        return None
    try:
        return datetime.fromtimestamp(int(ts), tz()).strftime("%Y-%m-%d %H:%M")
    except (ValueError, OSError, TypeError):
        return str(ts)


def fmt_date(ts) -> str | None:
    if ts in (None, 0, ""):
        return None
    return datetime.fromtimestamp(int(ts), tz()).strftime("%Y-%m-%d")
