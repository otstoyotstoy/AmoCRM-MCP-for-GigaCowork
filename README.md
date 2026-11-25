# AmoCRM MCP for GigaCowork

> **Полная инструкция по установке и подключению — в [INSTALL.md](INSTALL.md).**

MCP-сервер полного доступа к amoCRM (API v4) для ИИ-платформ (GigaCowork, Claude, Cursor и любых MCP-клиентов): поиск и выгрузка сделок любого объёма, создание и изменение сделок, контактов, компаний, задач, примечаний, массовые операции, отчёты, настройка воронок и полей, плюс универсальный запрос к любому методу API.

- **Транспорт:** Streamable HTTP (`/mcp`), SSE (`/sse`) для старых клиентов, stdio для локального запуска.
- **Авторизация к серверу:** логин/пароль (логин любой, пароль = ключ), `Authorization: Bearer <ключ>`, `X-API-Key: <ключ>` или `?key=<ключ>` в URL.
- **Удобно для ИИ:** воронки, этапы, ответственных, поля, теги и причины отказа можно указывать **названиями**. Даты понимаются в формате `2026-11-20`, `20.11.2026`, `tomorrow`, `+3d`. Ошибки amoCRM приходят понятным текстом с подсказками.
- **Большие объёмы:** автоматический проход по страницам, выгрузка в XLSX/CSV по ссылке, отчёты считаются по всем записям без загрузки их в ответ модели.
- **Надёжность:** соблюдает лимит amoCRM (≤7 запросов/с), повторяет запрос при 429/5xx, корректно обрабатывает пустые ответы (204).

## Быстрый старт (Docker)

```bash
REPO_URL=https://github.com/<аккаунт>/AmoCRM-MCP-for-GigaCowork.git   # адрес этого репозитория (Code → HTTPS)
git clone "$REPO_URL" amocrm-mcp && cd amocrm-mcp
cp .env.example .env
nano .env                # AMOCRM_DOMAIN, AMOCRM_TOKEN, MCP_AUTH_TOKEN, PUBLIC_BASE_URL, MCP_DOMAIN
openssl rand -hex 32     # → значение для MCP_AUTH_TOKEN

# Вариант А: свой домен + автоматический HTTPS (Caddy, Let's Encrypt)
docker compose --profile https up -d --build

# Вариант Б: на сервере уже есть nginx — поднимаем только приложение на 127.0.0.1:8000
docker compose up -d --build     # пример конфига nginx: deploy/nginx.conf
```

Проверка: `curl https://mcp.example.ru/health` → `{"ok":true,"tools":50,...}`.

### Виртуальный хостинг (быстрый тест)

Подробно — в [INSTALL.md, раздел 7](INSTALL.md#7-вариант-в-виртуальный-хостинг-ispmanager-регру-и-др). Коротко: создать поддомен с SSL, включить для него Python 3.12, затем в терминале хостинга:
```bash
git clone "$REPO_URL" ~/amocrm-mcp
cd ~/amocrm-mcp && bash deploy/install_shared_hosting.sh mcp.company.ru
```

<details><summary>Ручная установка на хостинг</summary>

### Тестовый запуск на виртуальном хостинге (Passenger WSGI: Рег.ру, Beget, Timeweb…)

Только для тестов: работает лишь `/mcp` (без SSE), ресурсы хостинга ограничены.
Проверено на Рег.ру (ispmanager 6):
1. Создайте поддомен (например `mcp.site.ru`) с SSL Let's Encrypt. В его настройках → «Дополнительные возможности» включите **CGI-скрипты** и **Python**, версия **3.12**.
2. Код и настройки держите **вне** папки сайта, например `~/amocrm-mcp`:
   `/opt/python/python-3.12/bin/python3 -m venv ~/amocrm-mcp/venv && ~/amocrm-mcp/venv/bin/pip install -r requirements.txt`, затем `.env` (из `.env.example`, `chmod 600`).
3. В папке поддомена — только загрузчик `passenger_wsgi.py` (без `.htaccess` и `index.html`):
   ```python
   import runpy
   application = runpy.run_path("/var/www/<логин>/data/amocrm-mcp/passenger_wsgi.py")["application"]
   ```
4. Перезапуск после изменений: `touch <папка поддомена>/.restart-app`.
5. Проверка: `https://mcp.site.ru/health`, подключение: `https://mcp.site.ru/mcp`.

</details>

Без Docker: `python -m venv venv && venv/bin/pip install -r requirements.txt && venv/bin/python -m amocrm_mcp.server` (юнит systemd: `deploy/amocrm-mcp.service`).

## Подключение к ИИ-платформе

| Параметр | Значение |
|---|---|
| Тип | Удалённый MCP-сервер (Streamable HTTP) |
| URL | `https://mcp.example.ru/mcp` |
| Заголовок | `Authorization: Bearer <MCP_AUTH_TOKEN>` |
| Если заголовки задать нельзя | `https://mcp.example.ru/mcp?key=<MCP_AUTH_TOKEN>` |
| Если платформа просит логин и пароль (GigaCowork) | логин `amocrm` (любой), пароль = `MCP_AUTH_TOKEN` |
| Если платформа поддерживает только SSE | `https://mcp.example.ru/sse` (с тем же токеном) |

Локально (Claude Desktop и т.п.):
```json
{"mcpServers": {"amocrm": {"command": "python", "args": ["/path/amocrm-mcp/server.py", "stdio"],
  "env": {"AMOCRM_DOMAIN": "mycompany", "AMOCRM_TOKEN": "..."}}}}
```

## Токен amoCRM

amoCRM → Настройки → Интеграции → «Создать интеграцию» → «Внешняя интеграция» → дать все права (доступ к данным аккаунта) → сохранить → вкладка «Ключи и доступы» → «Сгенерировать долгосрочный токен». Срок выбирается при выпуске; когда токен истечёт, сервер вернёт понятную ошибку 401.

Права в CRM совпадают с правами пользователя, который выпустил токен. Для полного доступа токен должен выпустить администратор.

## Настройки

| Переменная | Описание |
|---|---|
| `AMOCRM_DOMAIN` | `mycompany` (→ .amocrm.ru), либо `mycompany.amocrm.com` / `mycompany.kommo.com` |
| `AMOCRM_TOKEN` | Долгосрочный токен |
| `AMOCRM_TIMEZONE` | Часовой пояс для дат (по умолчанию Europe/Moscow) |
| `MCP_AUTH_TOKEN` | Токен(ы) доступа к серверу, через запятую. Обязателен для HTTP |
| `PUBLIC_BASE_URL` | Внешний адрес сервера, нужен для ссылок на выгрузки |
| `MCP_READ_ONLY` | `1` — только чтение (все инструменты изменения скрыты) |
| `MCP_TOOLSETS` | Включить только нужные группы: `reference,admin,leads,contacts,companies,links,tasks,notes,events,unsorted,customers,catalogs,calls,reports,raw` |
| `AMOCRM_RPS` | Запросов в секунду к amoCRM (по умолчанию 6) |
| `MCP_MAX_FETCH_ITEMS` | Максимум записей в выгрузке/отчёте (по умолчанию 50 000) |

## Инструменты (50)

**Справочники:** `get_account_info`, `list_users`, `list_pipelines`, `list_custom_fields`, `list_loss_reasons`, `list_tags`

**Сделки:** `search_leads` (фильтры по воронке, этапу, результату, ответственному, бюджету, датам, тегам, значениям полей; страницы / всё / файл) · `get_lead` · `create_leads` · `create_leads_complex` (сделка + новый контакт + компания) · `update_leads` · `close_leads` · `bulk_update_leads` (массовые изменения по фильтру с предпросмотром)

**Контакты и компании:** `search_contacts`, `get_contact`, `create_contacts` (с защитой от дублей), `update_contacts`, `search_companies`, `get_company`, `create_companies`, `update_companies`

**Задачи:** `search_tasks` (открытые / просроченные / выполненные), `create_tasks`, `update_tasks`, `complete_tasks`

**Активность:** `get_notes`, `add_notes`, `update_note`, `get_events` (история изменений), `add_calls`

**Связи:** `get_links`, `link_entities`, `unlink_entities`

**Неразобранное:** `list_unsorted`, `process_unsorted` (принять / отклонить), `create_unsorted_forms`

**Покупатели и списки (товары):** `search_customers`, `save_customers`, `list_catalogs`, `search_catalog_elements`, `save_catalog_elements`

**Отчёты:** `leads_report` (количество, суммы, конверсия по этапам / менеджерам / месяцам / тегам / причинам отказа), `tasks_report` (просрочки по менеджерам)

**Настройка CRM:** `manage_pipeline`, `manage_status`, `manage_custom_field`, `create_tags`

**Журнал и откат:** `get_change_log` (кто, что и когда менял через ИИ), `undo_change` (вернуть прежние значения по op_id)

**Всё остальное:** `amocrm_api_request` — любой GET-запрос к API v4 (вебхуки, источники, роли, чаты…); запись через него включает только администратор сервера

## Защита данных клиента

Сервер спроектирован так, чтобы ИИ не мог случайно удалить или испортить данные:

| Защита | Как работает |
|---|---|
| Журнал изменений | Каждое изменение записывается с op_id, аргументами и **значениями «до»**; без доступного журнала сервер ничего не меняет |
| Отмена | `undo_change(op_id)` возвращает прежние значения (сделки, контакты, компании, задачи, покупатели, связи) |
| Подтверждение | Изменение больше 10 записей (настраивается), массовые изменения, отклонение заявок, удаления настроек и откат — только в два шага: предпросмотр «было → станет» → повтор с `confirm_token` |
| Пустой фильтр | Массовое изменение без условий запрещено (не затронет «все сделки») |
| Лимиты | Не более 500 записей за вызов, 2000 в массовом изменении (настраивается) |
| Необратимые операции | Удаление воронок, этапов, полей, вариантов списков и DELETE через произвольный запрос **выключены** (`MCP_ALLOW_DESTRUCTIVE=0`). Даже включённые: воронку/этап со сделками удалить нельзя, значения поля сохраняются в журнал перед удалением |
| Без дублей | Создание не повторяется при таймауте/сбое; одинаковый повторный вызов в течение 10 минут ничего не создаёт |
| Частичные сбои | Если пачка упала посередине — точный отчёт: что применено (ids), что нет (failed_ids) |
| Телефоны и email | Новые **добавляются** к существующим; замена — только по явному флагу |
| Этапы | При смене этапа без воронки этап ищется в текущей воронке сделки — сделка не «уезжает» в чужую воронку |
| Произвольный API | По умолчанию только чтение (`MCP_ALLOW_RAW_WRITE=0`) |
| Режим «только чтение» | `MCP_READ_ONLY=1` убирает все инструменты изменения |

### Ограничения amoCRM API

- Сделки, контакты и компании **нельзя удалить через API**, только в интерфейсе. Вместо удаления: `close_leads`, снять теги, отвязать, сменить ответственного.
- Удаление поддерживается для воронок, этапов, полей и заявок в «Неразобранном».
- Фильтры по тегам и значениям полей amoCRM на сервере не поддерживает, поэтому MCP-сервер применяет их сам после загрузки записей. Сочетайте их с другими фильтрами (воронка, даты), чтобы загрузка шла быстрее.

## Тесты

```bash
pip install -r requirements.txt pytest pytest-asyncio
pytest -q     # сквозные тесты: HTTP-сервер + MCP-клиент + имитация amoCRM
```
