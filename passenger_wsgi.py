"""Точка входа для Passenger (виртуальный хостинг). Настройки — в файле .env рядом.

На многих хостингах (Рег.ру и др.) Passenger запускает системный Python той же версии,
что и venv: venv/bin/python — лишь ссылка на него, поэтому подменять интерпретатор
через os.execl бесполезно. Вместо этого подключаем библиотеки venv напрямую.
"""
import glob
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
for sp in glob.glob(os.path.join(HERE, "venv", "lib", "python3*", "site-packages")):
    if sp not in sys.path:
        sys.path.insert(1, sp)

# Простая загрузка .env (KEY=VALUE)
env_path = os.path.join(HERE, ".env")
if os.path.exists(env_path):
    for line in open(env_path, encoding="utf-8"):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
os.environ.setdefault("EXPORT_DIR", os.path.join(HERE, "exports"))

from amocrm_mcp.wsgi import application  # noqa: E402,F401
