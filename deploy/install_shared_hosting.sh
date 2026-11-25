#!/usr/bin/env bash
# Установка amoCRM MCP на виртуальный хостинг с Passenger (ispmanager: Рег.ру, Beget, Timeweb и др.)
#
# Перед запуском в панели хостинга:
#   1) создайте сайт-поддомен (например mcp.example.ru) с SSL Let's Encrypt;
#   2) в настройках сайта → «Дополнительные возможности» включите «CGI-скрипты» и «Python»,
#      выберите версию Python (3.10 или новее) — ТУ ЖЕ, что будет использована здесь.
#
# Запуск (из папки проекта, лежащей ВНЕ папок сайтов, например ~/amocrm-mcp):
#   bash deploy/install_shared_hosting.sh mcp.example.ru [/путь/к/python3]
set -euo pipefail

SITE="${1:?Укажите домен поддомена, например: bash deploy/install_shared_hosting.sh mcp.example.ru}"
APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
SITE_DIR="${SITE_DIR:-$HOME/www/$SITE}"
PY="${2:-}"

case "$APP_DIR" in
  "$HOME/www/"*) echo "Папка проекта не должна лежать внутри ~/www (иначе .env будет доступен из интернета)."; exit 1;;
esac

if [ -z "$PY" ]; then
  for v in 3.12 3.13 3.11 3.10; do
    for c in "/opt/python/python-$v/bin/python3" "$(command -v "python$v" 2>/dev/null || true)"; do
      if [ -n "$c" ] && [ -x "$c" ]; then PY="$c"; break 2; fi
    done
  done
fi
[ -n "$PY" ] || { echo "Не найден Python 3.10+. Укажите путь вторым аргументом."; exit 1; }
PYVER="$("$PY" -c 'import sys; print(f"{sys.version_info[0]}.{sys.version_info[1]}")')"
echo "→ Python: $PY ($PYVER). В панели хостинга для сайта должна быть выбрана та же версия!"

echo "→ Виртуальное окружение и зависимости (1–3 минуты)…"
"$PY" -m venv "$APP_DIR/venv"
"$APP_DIR/venv/bin/pip" install -q --upgrade pip
"$APP_DIR/venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"

cd "$APP_DIR"
if [ ! -f .env ]; then
  echo "→ Создаю .env со случайным ключом доступа"
  KEY="$(openssl rand -hex 24 2>/dev/null || "$PY" -c 'import secrets; print(secrets.token_hex(24))')"
  sed -e "s|^MCP_AUTH_TOKEN=.*|MCP_AUTH_TOKEN=$KEY|" \
      -e "s|^PUBLIC_BASE_URL=.*|PUBLIC_BASE_URL=https://$SITE|" \
      -e "s|^MCP_DOMAIN=.*|MCP_DOMAIN=$SITE|" .env.example > .env
  echo "EXPORT_DIR=$APP_DIR/exports" >> .env
  echo "MCP_AUDIT_DIR=$APP_DIR/audit" >> .env
  unset KEY
else
  echo "→ .env уже существует — не трогаю"
fi
chmod 600 .env
mkdir -p exports audit && chmod 700 audit

echo "→ Проверка запуска приложения"
"$PY" -c "import runpy; runpy.run_path('$APP_DIR/passenger_wsgi.py')['application']; print('   OK')"

echo "→ Загрузчик в папке сайта: $SITE_DIR"
mkdir -p "$SITE_DIR"
for f in index.html index.php .htaccess; do
  [ -f "$SITE_DIR/$f" ] && mv "$SITE_DIR/$f" "$SITE_DIR/$f.bak-$(date +%s)" && echo "   $f → переименован в .bak"
done
cat > "$SITE_DIR/passenger_wsgi.py" <<EOF
import runpy
application = runpy.run_path("$APP_DIR/passenger_wsgi.py")["application"]
EOF
touch "$SITE_DIR/.restart-app"
mkdir -p "$SITE_DIR/tmp" && touch "$SITE_DIR/tmp/restart.txt"

sleep 3
CODE="$(curl -s -m 60 -o /dev/null -w '%{http_code}' "https://$SITE/health" || true)"
echo
echo "Проверка https://$SITE/health → HTTP $CODE (ожидается 200)"
echo
echo "Дальше:"
echo "  1) Впишите AMOCRM_DOMAIN и AMOCRM_TOKEN в $APP_DIR/.env"
echo "  2) Перезапуск: touch $SITE_DIR/.restart-app"
echo "  3) Ключ доступа для ИИ-платформы: grep MCP_AUTH_TOKEN $APP_DIR/.env"
echo "  4) Адрес для подключения: https://$SITE/mcp"
