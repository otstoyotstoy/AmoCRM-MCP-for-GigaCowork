"""Сквозной тест: настоящий HTTP-сервер MCP + MCP-клиент + имитация amoCRM."""
import json
import os
import socket
import tempfile
import threading
import time

os.environ.update({
    "AMOCRM_DOMAIN": "test", "AMOCRM_TOKEN": "test-token", "MCP_AUTH_TOKEN": "secret-mcp",
    "AMOCRM_RPS": "1000", "PUBLIC_BASE_URL": "http://localhost", "EXPORT_DIR": tempfile.mkdtemp(),
    "MCP_AUDIT_DIR": tempfile.mkdtemp(),
})

import httpx  # noqa: E402
import pytest  # noqa: E402
import uvicorn  # noqa: E402
from mcp import ClientSession  # noqa: E402
from mcp.client.streamable_http import streamablehttp_client  # noqa: E402

from amocrm_mcp.client import amo  # noqa: E402
from amocrm_mcp.server import build_app  # noqa: E402
from tests.mock_amo import MockAmo  # noqa: E402

MOCK = MockAmo()
amo._client = httpx.AsyncClient(base_url="https://test.amocrm.ru", transport=httpx.MockTransport(MOCK.handler),
                                headers={"Authorization": "Bearer test-token"})


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


PORT = _free_port()
URL = f"http://127.0.0.1:{PORT}"


@pytest.fixture(scope="session", autouse=True)
def server():
    srv = uvicorn.Server(uvicorn.Config(build_app(), host="127.0.0.1", port=PORT, log_level="error"))
    th = threading.Thread(target=srv.run, daemon=True)
    th.start()
    for _ in range(100):
        try:
            httpx.get(URL + "/health")
            break
        except httpx.HTTPError:
            time.sleep(0.05)
    yield
    srv.should_exit = True


async def call(name, args=None, headers=None):
    async with streamablehttp_client(URL + "/mcp", headers=headers or {"Authorization": "Bearer secret-mcp"}) as (r, w, _):
        async with ClientSession(r, w) as s:
            await s.initialize()
            res = await s.call_tool(name, args or {})
            text = res.content[0].text if res.content else ""
            return res.isError, (json.loads(text) if not res.isError else text)


def test_auth_required():
    assert httpx.post(URL + "/mcp", json={}).status_code == 401
    assert httpx.post(URL + "/mcp?key=wrong", json={}).status_code == 401
    assert httpx.get(URL + "/health").status_code == 200


@pytest.mark.asyncio
async def test_auth_query_key_and_list_tools():
    async with streamablehttp_client(URL + "/mcp?key=secret-mcp") as (r, w, _):
        async with ClientSession(r, w) as s:
            await s.initialize()
            tools = (await s.list_tools()).tools
            assert len(tools) >= 45
            assert all("title" not in json.dumps(t.inputSchema) for t in tools)


@pytest.mark.asyncio
async def test_reference():
    err, d = await call("list_pipelines")
    assert not err and d["count"] == 2
    err, d = await call("list_custom_fields", {"entity_type": "leads"})
    assert d["items"][0]["enums"][0]["value"] == "Сайт"


@pytest.mark.asyncio
async def test_search_pagination_and_names():
    err, d = await call("search_leads", {"pipeline": ["Продажи"], "status": ["Переговоры"], "responsible": ["Иван"]})
    assert not err, d
    assert d["items"] and all(x["status"] == "Переговоры" and x["responsible"] == "Иван Петров" for x in d["items"])
    err, d = await call("search_leads", {"fetch_all": True, "max_items": 400})
    assert d["count"] == 400 and d["truncated"] is True


@pytest.mark.asyncio
async def test_result_filter_and_tags():
    err, d = await call("search_leads", {"result": "won", "fetch_all": True})
    assert not err and d["count"] > 0 and all(x["status_id"] == 142 for x in d["items"])
    err, d = await call("search_leads", {"tags": ["VIP"], "limit": 5})
    assert all("vip" in x["tags"] for x in d["items"]) and d["count"] == 5


@pytest.mark.asyncio
async def test_empty_result_204():
    err, d = await call("search_leads", {"query": "нет-такой-сделки"})
    assert not err and d["count"] == 0 and d["items"] == []


@pytest.mark.asyncio
async def test_export_xlsx():
    err, d = await call("search_leads", {"export_format": "xlsx"})
    assert not err and d["rows"] == 620
    url = d["download_url"].replace("http://localhost", URL)
    r = httpx.get(url)
    assert r.status_code == 200 and r.content[:2] == b"PK"
    assert httpx.get(URL + "/files/..%2F..%2Fetc%2Fpasswd").status_code == 404
    assert httpx.get(URL + "/files/nope__x.csv").status_code == 404


@pytest.mark.asyncio
async def test_create_update_close():
    err, d = await call("create_leads", {"leads": [{"name": "Новая", "price": 5000, "pipeline": "Партнёры", "status": "Новый",
                                                   "responsible": "maria@x.ru", "tags": ["mcp"],
                                                   "custom_fields": {"Источник": "Звонок", "Дата встречи": "2026-12-01"},
                                                   "contact_ids": [1]}]})
    assert not err, d
    lid = d["ids"][0]
    post = [c for c in MOCK.calls if c[0] == "POST" and c[1] == "/api/v4/leads"][-1][3][0]
    assert post["pipeline_id"] == 20 and post["status_id"] == 201 and post["responsible_user_id"] == 2
    assert {"field_id": 500, "values": [{"enum_id": 2}]} in post["custom_fields_values"]
    link = [c for c in MOCK.calls if c[1] == f"/api/v4/leads/{lid}/link"][-1][3]
    assert link[0]["to_entity_id"] == 1
    err, d = await call("update_leads", {"leads": [{"id": lid, "status": "Переговоры", "pipeline": "Партнёры", "tags_to_add": ["горячий"]}]})
    assert not err and MOCK.leads[lid]["status_id"] == 202
    err, d = await call("close_leads", {"lead_ids": [lid], "outcome": "lost", "loss_reason": "Дорого", "comment": "клиент ушёл"})
    assert not err and MOCK.leads[lid]["status_id"] == 143 and MOCK.leads[lid]["loss_reason_id"] == 7


@pytest.mark.asyncio
async def test_bulk_requires_token_then_applies_and_undo():
    args = {"filter": {"pipeline": ["Продажи"], "status": ["Первичный контакт"]},
            "changes": {"responsible": "Иван Петров", "tags_to_add": ["перераспределено"]}}
    target = [l["id"] for l in MOCK.leads.values() if l["pipeline_id"] == 10 and l["status_id"] == 101]
    patches_before = sum(1 for c in MOCK.calls if c[0] == "PATCH")
    err, d = await call("bulk_update_leads", args)
    assert not err and d["needs_confirmation"] and d["applied"] is False and d["preview"]["count"] == len(target)
    assert sum(1 for c in MOCK.calls if c[0] == "PATCH") == patches_before  # ничего не изменено
    err, bad = await call("bulk_update_leads", {**args, "confirm_token": "wrong"})
    assert bad["needs_confirmation"]
    err, d2 = await call("bulk_update_leads", {**args, "confirm_token": d["confirm_token"]})
    assert d2["ok"] and d2["updated"] == len(target) and d2["op_id"]
    assert all(MOCK.leads[i]["responsible_user_id"] == 1 for i in target)


@pytest.mark.asyncio
async def test_errors_are_readable():
    err, txt = await call("update_leads", {"leads": [{"id": 999999, "name": "x"}]})
    assert err and "не найдены" in txt and "Ничего не изменено" in txt
    MOCK.fail[("PATCH", "/leads")] = MOCK.counts.get(("PATCH", "/leads"), 0) + 1
    try:
        err, d = await call("update_leads", {"leads": [{"id": 2, "name": "y"}]})
    finally:
        MOCK.fail.clear()
    assert not err and d["ok"] is False and "mock failure" in d["error"] and d["failed_ids"] == [2]
    err, txt = await call("search_leads", {"status": ["Несуществующий этап"]})
    assert err and "не найден" in txt
    err, txt = await call("create_leads", {"leads": [{"name": "x", "custom_fields": {"Источник": "Телепатия"}}]})
    assert err and "Сайт" in txt  # подсказка вариантов


@pytest.mark.asyncio
async def test_contacts_tasks_events_raw():
    err, d = await call("search_contacts", {"query": "+79990001122"})
    assert d["items"][0]["phones"] == ["+79990001122"]
    err, d = await call("create_contacts", {"contacts": [{"name": "Пётр", "phone": "+79990001122"}], "skip_duplicates_by_phone": True})
    assert d["created"] == 0 and d["already_existed"][0]["id"] == 1
    err, d = await call("search_tasks", {"state": "overdue"})
    assert d["items"][0]["overdue"] is True
    err, d = await call("create_tasks", {"tasks": [{"text": "Перезвонить", "complete_till": "tomorrow", "entity_type": "leads", "entity_id": 1, "task_type": "Встреча"}]})
    assert not err and d["created"] == 1
    err, d = await call("get_events", {"entity_type": "leads", "entity_ids": [1]})
    assert d["items"][0]["after"][0]["status"] == "Переговоры"
    err, d = await call("amocrm_api_request", {"method": "GET", "path": "/api/v4/webhooks"})
    assert not err and d["ok"]
    err, txt = await call("amocrm_api_request", {"method": "GET", "path": "https://evil.com/x"})
    assert err


@pytest.mark.asyncio
async def test_report():
    err, d = await call("leads_report", {"group_by": ["pipeline", "responsible"]})
    assert not err and d["totals"]["count"] >= 620 and d["totals"]["win_rate_pct"] is not None


@pytest.mark.asyncio
async def test_basic_auth():
    import base64
    good = base64.b64encode(b"amocrm:secret-mcp").decode()
    bad = base64.b64encode(b"user@mail.ru:wrong").decode()
    assert httpx.post(URL + "/mcp", json={}, headers={"Authorization": "Basic " + bad}).status_code == 401
    err, d = await call("list_pipelines", headers={"Authorization": "Basic " + good})
    assert not err and d["count"] == 2


# ============================================================ безопасность

async def ok(name, args):
    err, d = await call(name, args)
    assert not err, d
    return d


async def confirmed(name, args):
    d = await ok(name, args)
    if d.get("needs_confirmation"):
        d = await ok(name, {**args, "confirm_token": d["confirm_token"]})
    return d


@pytest.mark.asyncio
async def test_bulk_empty_filter_refused():
    for f in [None, {}, {"include_deleted": False}, {"tags": []}]:
        args = {"changes": {"responsible": "Мария"}}
        if f is not None:
            args["filter"] = f
        err, txt = await call("bulk_update_leads", args)
        assert err and ("пустой" in txt or "Нужен" in txt or "filter" in txt), txt
    err, txt = await call("bulk_update_leads", {"filter": {"pipeline": ["Продажи"]}, "changes": {}})
    assert err and "что менять" in txt


@pytest.mark.asyncio
async def test_update_threshold_preview_shows_before_after():
    ids = list(MOCK.leads)[100:115]
    old = {i: MOCK.leads[i]["price"] for i in ids}
    d = await ok("update_leads", {"leads": [{"id": i, "price": 1} for i in ids]})
    assert d["needs_confirmation"] and d["preview"]["count"] == 15
    assert d["preview"]["items"][0]["changes"]["price"]["было"] == old[ids[0]]
    assert all(MOCK.leads[i]["price"] == old[i] for i in ids)  # не применено


@pytest.mark.asyncio
async def test_undo_restores_previous_values():
    lid = 7
    before = dict(MOCK.leads[lid])
    d = await ok("update_leads", {"leads": [{"id": lid, "price": 999, "name": "Изменено ИИ"}]})
    assert MOCK.leads[lid]["price"] == 999
    u = await ok("undo_change", {"op_id": d["op_id"]})
    assert u["needs_confirmation"]
    u2 = await ok("undo_change", {"op_id": d["op_id"], "confirm_token": u["confirm_token"]})
    assert u2["ok"] and MOCK.leads[lid]["price"] == before["price"] and MOCK.leads[lid]["name"] == before["name"]
    log = await ok("get_change_log", {"limit": 5})
    assert any(x["tool"] == "undo_change" for x in log["items"])


@pytest.mark.asyncio
async def test_status_resolved_in_current_pipeline():
    # сделка 3 в «Партнёры» (pipeline 20); этап «Переговоры» есть в обеих воронках — должна остаться в 20
    lid = next(i for i, l in MOCK.leads.items() if l["pipeline_id"] == 20 and l["status_id"] == 201)
    await ok("update_leads", {"leads": [{"id": lid, "status": "Переговоры"}]})
    assert MOCK.leads[lid]["pipeline_id"] == 20 and MOCK.leads[lid]["status_id"] == 202
    # этап, которого нет в текущей воронке, — ошибка, а не тихий перенос
    err, txt = await call("update_leads", {"leads": [{"id": lid, "status": "Первичный контакт"}]})
    assert err and "текущей воронке" in txt and MOCK.leads[lid]["pipeline_id"] == 20


@pytest.mark.asyncio
async def test_create_dedupe_and_no_retry_on_timeout():
    n0 = len(MOCK.leads)
    a = {"leads": [{"name": "Уникальная сделка X", "price": 1}]}
    d1 = await ok("create_leads", a)
    d2 = await ok("create_leads", a)
    assert d1["created"] == 1 and d2.get("duplicate_call") and len(MOCK.leads) == n0 + 1
    MOCK.fail[("POST", "/leads")] = "timeout"
    try:
        c0 = MOCK.counts.get(("POST", "/leads"), 0)
        d3 = await ok("create_leads", {"leads": [{"name": "Таймаут"}]})
        assert d3["ok"] is False and "мог быть выполнен" in d3["error"]
        assert MOCK.counts[("POST", "/leads")] == c0 + 1  # ровно одна попытка, без повтора
    finally:
        MOCK.fail.clear()


@pytest.mark.asyncio
async def test_partial_failure_reported():
    ids = list(MOCK.leads)[200:260]  # 60 сделок = 2 пачки
    MOCK.fail[("PATCH", "/leads")] = MOCK.counts.get(("PATCH", "/leads"), 0) + 2  # вторая пачка падает
    try:
        d = await confirmed("update_leads", {"leads": [{"id": i, "price": 5} for i in ids]})
    finally:
        MOCK.fail.clear()
    assert d["ok"] is False and d["updated"] == 50 and len(d["failed_ids"]) == 10 and d["op_id"]
    assert all(MOCK.leads[i]["price"] == 5 for i in ids[:50]) and all(MOCK.leads[i]["price"] != 5 for i in ids[50:])


@pytest.mark.asyncio
async def test_phones_are_added_not_replaced():
    await ok("update_contacts", {"contacts": [{"id": 1, "phone": "+7 999 111-22-33"}]})
    phones = [v["value"] for c in MOCK.contacts[1]["custom_fields_values"] if c.get("field_code") == "PHONE"
              for v in c["values"]]
    assert "+79990001122" in phones and "+7 999 111-22-33" in phones
    await ok("update_contacts", {"contacts": [{"id": 1, "phone": "8 (999) 000-11-22"}]})  # тот же номер — не дублируется
    phones = [v["value"] for c in MOCK.contacts[1]["custom_fields_values"] if c.get("field_code") == "PHONE" for v in c["values"]]
    assert len(phones) == 2


@pytest.mark.asyncio
async def test_destructive_admin_blocked_by_default():
    for args in [{"action": "delete", "pipeline": "Партнёры"},
                 ]:
        err, txt = await call("manage_pipeline", args)
        assert err and "MCP_ALLOW_DESTRUCTIVE" in txt
    err, txt = await call("manage_status", {"action": "delete", "pipeline": "Продажи", "status": "Переговоры"})
    assert err and "MCP_ALLOW_DESTRUCTIVE" in txt
    err, txt = await call("manage_custom_field", {"action": "delete", "entity_type": "leads", "field": "Источник"})
    assert err and "MCP_ALLOW_DESTRUCTIVE" in txt
    err, txt = await call("manage_status", {"action": "delete", "pipeline": "Продажи", "status": "won"})
    assert err and "Системные" in txt
    assert not MOCK.deleted


@pytest.mark.asyncio
async def test_enum_update_keeps_existing_options():
    await ok("manage_custom_field", {"action": "update", "entity_type": "leads", "field": "Источник",
                                     "enums": [{"value": "Реклама"}]})
    vals = [e["value"] for e in MOCK.last_cf_patch[0]["enums"]]
    assert "Сайт" in vals and "Звонок" in vals and "Реклама" in vals


@pytest.mark.asyncio
async def test_raw_api_read_only_by_default():
    d = await ok("amocrm_api_request", {"path": "/api/v4/webhooks"})
    assert d["ok"]
    err, txt = await call("amocrm_api_request", {"method": "DELETE", "path": "/api/v4/leads/pipelines/10"})
    assert err  # параметра method нет — запись невозможна
    assert ("pipeline", 10) not in MOCK.deleted


@pytest.mark.asyncio
async def test_unsorted_decline_needs_confirmation():
    d = await ok("process_unsorted", {"uids": ["u1"], "action": "decline"})
    assert d["needs_confirmation"] and "u1" in MOCK.unsorted
    d2 = await ok("process_unsorted", {"uids": ["u1"], "action": "decline", "confirm_token": d["confirm_token"]})
    assert d2["ok"] and "u1" not in MOCK.unsorted and d2["op_id"]


@pytest.mark.asyncio
async def test_unlink_logged_and_undoable():
    d = await ok("unlink_entities", {"entity_type": "leads", "entity_id": 1,
                                     "links": [{"to_entity_type": "contacts", "to_entity_id": 1}]})
    assert MOCK.links[("leads", 1)] == []
    u = await confirmed("undo_change", {"op_id": d["op_id"]})
    assert u["ok"] and MOCK.links[("leads", 1)][0]["metadata"] == {"main_contact": True}


@pytest.mark.asyncio
async def test_missing_ids_change_nothing():
    n = sum(1 for c in MOCK.calls if c[0] == "PATCH")
    err, txt = await call("update_leads", {"leads": [{"id": 1, "price": 3}, {"id": 99999999, "price": 3}]})
    assert err and "не найдены" in txt.lower()
    assert sum(1 for c in MOCK.calls if c[0] == "PATCH") == n


@pytest.mark.asyncio
async def test_write_limit():
    err, txt = await call("update_leads", {"leads": [{"id": i, "price": 1} for i in range(1, 600)]})
    assert err and "не более" in txt


def test_health_deep():
    r = httpx.get(URL + "/health?deep=1").json()
    assert r["audit"] == "ok" and r["allow_destructive"] is False and r["allow_raw_write"] is False


@pytest.mark.asyncio
async def test_destructive_when_enabled_still_guarded():
    from amocrm_mcp.config import cfg
    cfg.allow_destructive = True
    try:
        err, txt = await call("manage_pipeline", {"action": "delete", "pipeline": "Продажи"})
        assert err and "есть сделки" in txt
        MOCK.pipelines.append({"id": 30, "name": "Пустая", "_embedded": {"statuses": [{"id": 301, "name": "Новый"}]}})
        from amocrm_mcp import refs
        refs.invalidate("pipelines")
        d = await ok("manage_pipeline", {"action": "delete", "pipeline": "Пустая"})
        assert d["needs_confirmation"] and ("pipeline", 30) not in MOCK.deleted
        d2 = await ok("manage_pipeline", {"action": "delete", "pipeline": "Пустая", "confirm_token": d["confirm_token"]})
        assert d2["ok"] and ("pipeline", 30) in MOCK.deleted
        f = await ok("manage_custom_field", {"action": "delete", "entity_type": "leads", "field": "Источник"})
        assert f["needs_confirmation"] and f["preview"]["records_with_values"] > 0
        f2 = await ok("manage_custom_field", {"action": "delete", "entity_type": "leads", "field": "Источник",
                                              "confirm_token": f["confirm_token"]})
        assert f2["values_backed_up"] > 0 and ("field", 500) in MOCK.deleted
        log = await ok("get_change_log", {"op_id": f2["op_id"]})
        assert len(log["result"]["values_backup"]) == f2["values_backed_up"]
    finally:
        cfg.allow_destructive = False
        MOCK.pipelines[:] = [p for p in MOCK.pipelines if p["id"] != 30]


@pytest.mark.asyncio
async def test_audit_unwritable_blocks_changes(tmp_path):
    from amocrm_mcp.config import cfg
    old = cfg.audit_dir
    blocker = tmp_path / "file"
    blocker.write_text("x")
    cfg.audit_dir = str(blocker / "sub")  # невозможный путь
    try:
        n = sum(1 for c in MOCK.calls if c[0] in ("PATCH", "POST"))
        err, txt = await call("update_leads", {"leads": [{"id": 3, "price": 42}]})
        assert err and "Журнал изменений недоступен" in txt
        assert sum(1 for c in MOCK.calls if c[0] in ("PATCH", "POST")) == n
    finally:
        cfg.audit_dir = old


@pytest.mark.asyncio
async def test_read_only_tools_unaffected_by_safety():
    d = await ok("search_leads", {"pipeline": ["Продажи"], "limit": 5})
    assert d["count"] == 5
    d = await ok("leads_report", {"group_by": ["status"]})
    assert d["totals"]["count"] > 0
