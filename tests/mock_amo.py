"""Имитация amoCRM API v4 в памяти — для тестов без реального аккаунта."""
import json
import re
from urllib.parse import parse_qsl

import httpx

WON, LOST = 142, 143


class MockAmo:
    def __init__(self):
        self.calls: list[tuple[str, str, dict, object]] = []
        self.users = [{"id": 1, "name": "Иван Петров", "email": "ivan@x.ru", "rights": {"is_active": True, "is_admin": True}},
                      {"id": 2, "name": "Мария Смирнова", "email": "maria@x.ru", "rights": {"is_active": True}}]
        self.pipelines = [
            {"id": 10, "name": "Продажи", "is_main": True, "_embedded": {"statuses": [
                {"id": 100, "name": "Неразобранное", "type": 1}, {"id": 101, "name": "Первичный контакт"},
                {"id": 102, "name": "Переговоры"}, {"id": WON, "name": "Успешно реализовано"},
                {"id": LOST, "name": "Закрыто и не реализовано"}]}},
            {"id": 20, "name": "Партнёры", "_embedded": {"statuses": [
                {"id": 201, "name": "Новый"}, {"id": 202, "name": "Переговоры"},
                {"id": WON, "name": "Успешно реализовано"}, {"id": LOST, "name": "Закрыто и не реализовано"}]}},
        ]
        self.cf = {
            "leads": [{"id": 500, "name": "Источник", "type": "select", "enums": [{"id": 1, "value": "Сайт"}, {"id": 2, "value": "Звонок"}]},
                      {"id": 501, "name": "Дата встречи", "type": "date"},
                      {"id": 502, "name": "Комментарий", "type": "textarea"}],
            "contacts": [{"id": 600, "name": "Телефон", "type": "multitext", "code": "PHONE"},
                         {"id": 601, "name": "Email", "type": "multitext", "code": "EMAIL"},
                         {"id": 602, "name": "Должность", "type": "text", "code": "POSITION"}],
            "companies": [{"id": 700, "name": "Телефон", "type": "multitext", "code": "PHONE"},
                          {"id": 701, "name": "Email", "type": "multitext", "code": "EMAIL"},
                          {"id": 702, "name": "Web", "type": "url", "code": "WEB"},
                          {"id": 703, "name": "Адрес", "type": "textarea", "code": "ADDRESS"},
                          {"id": 704, "name": "ИНН", "type": "text"}],
        }
        self.leads = {}
        for i in range(1, 621):  # 620 сделок → несколько страниц
            pid = 10 if i % 3 else 20
            sid = [101, 102, WON, LOST][i % 4] if pid == 10 else [201, 202, WON, LOST][i % 4]
            self.leads[i] = {"id": i, "name": f"Сделка {i}", "price": i * 1000, "pipeline_id": pid, "status_id": sid,
                             "responsible_user_id": 1 if i % 2 else 2, "created_at": 1767225600 + i * 3600,
                             "updated_at": 1767225600 + i * 7200, "closed_at": None, "loss_reason_id": None,
                             "custom_fields_values": [{"field_id": 500, "field_name": "Источник", "field_type": "select",
                                                       "values": [{"value": "Сайт" if i % 5 else "Звонок", "enum_id": 1}]}],
                             "_embedded": {"tags": [{"id": 1, "name": "vip"}] if i % 10 == 0 else [], "contacts": [{"id": 1, "is_main": True}] if i == 1 else []}}
        self.contacts = {1: {"id": 1, "name": "Анна Кузнецова", "first_name": "Анна", "last_name": "Кузнецова",
                             "responsible_user_id": 1, "created_at": 1767225600, "updated_at": 1767225600,
                             "custom_fields_values": [{"field_id": 600, "field_code": "PHONE", "field_name": "Телефон", "values": [{"value": "+79990001122", "enum_code": "WORK"}]}],
                             "_embedded": {"tags": [], "leads": [{"id": 1}]}}}
        self.tasks = {1: {"id": 1, "text": "Позвонить", "task_type_id": 1, "responsible_user_id": 2, "entity_type": "leads",
                          "entity_id": 1, "complete_till": 1000, "is_completed": False, "created_at": 900}}
        self.companies = {50: {"id": 50, "name": "ООО Ромашка", "responsible_user_id": 1, "created_at": 1, "updated_at": 1,
                               "custom_fields_values": [], "_embedded": {"tags": []}}}
        self.unsorted = {"u1": {"uid": "u1", "category": "forms", "pipeline_id": 10, "created_at": 1,
                                "_embedded": {"leads": [{"id": 9001}], "contacts": []}}}
        self.notes = {700: {"id": 700, "entity_id": 1, "note_type": "common", "params": {"text": "старый"}}}
        self.links = {("leads", 1): [{"to_entity_id": 1, "to_entity_type": "contacts", "metadata": {"main_contact": True}}]}
        self.deleted = []
        self.fail = {}          # {(method, path): "timeout" | "500" | int(n-й вызов падает)}
        self.counts = {}
        self.clock = 1767225600 + 10**6
        self.next_id = 10000

    def _nid(self):
        self.next_id += 1
        return self.next_id

    @staticmethod
    def _page(items, q, key, path):
        page, limit = int(q.get("page", 1)), int(q.get("limit", 50))
        chunk = items[(page - 1) * limit: page * limit]
        if not chunk:
            return httpx.Response(204)
        links = {"self": {"href": path}}
        if page * limit < len(items):
            links["next"] = {"href": path + f"?page={page + 1}"}
        return httpx.Response(200, json={"_page": page, "_links": links, "_embedded": {key: chunk}})

    def _tag_name(self, tid):
        for L in self.leads.values():
            for t in L["_embedded"]["tags"]:
                if t.get("id") == tid:
                    return t.get("name")
        return f"tag{tid}"

    def _filter_leads(self, q, qm):
        items = list(self.leads.values())
        if "filter[id][]" in qm:
            ids = {int(x) for x in qm["filter[id][]"]}
            items = [x for x in items if x["id"] in ids]
        if "filter[responsible_user_id][]" in qm:
            ids = {int(x) for x in qm["filter[responsible_user_id][]"]}
            items = [x for x in items if x["responsible_user_id"] in ids]
        if "filter[pipeline_id][]" in qm:
            ids = {int(x) for x in qm["filter[pipeline_id][]"]}
            items = [x for x in items if x["pipeline_id"] in ids]
        pairs = set()
        i = 0
        while f"filter[statuses][{i}][pipeline_id]" in q:
            pairs.add((int(q[f"filter[statuses][{i}][pipeline_id]"]), int(q[f"filter[statuses][{i}][status_id]"])))
            i += 1
        if pairs:
            items = [x for x in items if (x["pipeline_id"], x["status_id"]) in pairs]
        if "filter[price][from]" in q:
            items = [x for x in items if x["price"] >= int(q["filter[price][from]"])]
        if "filter[price][to]" in q:
            items = [x for x in items if x["price"] <= int(q["filter[price][to]"])]
        if "filter[created_at][from]" in q:
            items = [x for x in items if x["created_at"] >= int(q["filter[created_at][from]"])]
        if "query" in q:
            items = [x for x in items if q["query"].lower() in x["name"].lower()]
        if "order[id]" in q or "order[updated_at]" in q:
            items.sort(key=lambda x: x["updated_at"], reverse=q.get("order[updated_at]", q.get("order[id]")) == "desc")
        return items

    def _tick(self):
        self.clock += 10
        return self.clock

    def handler(self, req: httpx.Request) -> httpx.Response:
        assert req.headers["authorization"] == "Bearer test-token"
        path = req.url.path
        key = (req.method, path.removeprefix("/api/v4"))
        self.counts[key] = self.counts.get(key, 0) + 1
        f = self.fail.get(key)
        if f == "timeout":
            raise httpx.ReadTimeout("mock timeout", request=req)
        if f == "connect":
            raise httpx.ConnectError("mock connect", request=req)
        if f == "502":
            return httpx.Response(502, text="bad gateway")
        if isinstance(f, int) and self.counts[key] == f:
            return httpx.Response(500, json={"title": "Internal", "detail": "mock failure"})
        pairs = parse_qsl(req.url.query.decode(), keep_blank_values=True)
        q = dict(pairs)
        qm = {}
        for k, v in pairs:
            qm.setdefault(k, []).append(v)
        body = json.loads(req.content) if req.content else None
        self.calls.append((req.method, path, q, body))
        m = req.method
        p = path.removeprefix("/api/v4")

        if p == "/account":
            return httpx.Response(200, json={"id": 1, "name": "Тест", "subdomain": "test", "currency": "RUB",
                                             "_embedded": {"task_types": [{"id": 1, "name": "Звонок"}, {"id": 2, "name": "Встреча"}]}})
        if p == "/users":
            return self._page(self.users, q, "users", path)
        if p == "/leads/pipelines" and m == "GET":
            return httpx.Response(200, json={"_embedded": {"pipelines": self.pipelines}})
        if p == "/leads/pipelines" and m == "POST":
            return httpx.Response(200, json={"_embedded": {"pipelines": [{"id": 30, **body[0]}]}})
        if (mm := re.fullmatch(r"/(\w+)/custom_fields", p)) and m == "GET":
            return self._page(self.cf.get(mm.group(1), []), q, "custom_fields", path)
        if p == "/leads/loss_reasons":
            return self._page([{"id": 7, "name": "Дорого"}, {"id": 8, "name": "Нет ответа"}], q, "loss_reasons", path)
        if p == "/leads" and m == "GET":
            return self._page(self._filter_leads(q, qm), q, "leads", path)
        if p == "/leads" and m == "POST":
            out = []
            for b in body:
                i = self._nid()
                self.leads[i] = {"id": i, "price": 0, "pipeline_id": 10, "status_id": 101, "responsible_user_id": 1,
                                 "created_at": 1, "updated_at": 1, **b, "_embedded": {"tags": (b.get("_embedded") or {}).get("tags", [])}}
                out.append({"id": i})
            return httpx.Response(200, json={"_embedded": {"leads": out}})
        if p == "/leads" and m == "PATCH":
            for b in body:
                if b["id"] not in self.leads:
                    return httpx.Response(400, json={"title": "Bad Request", "validation-errors": [{"request_id": "0", "errors": [{"code": "NotFound", "path": "id", "detail": "Lead not found"}]}]})
                L = self.leads[b["id"]]
                L["updated_at"] = self._tick()
                for k, v in b.items():
                    if k == "tags_to_add":
                        L["_embedded"]["tags"] += v
                    elif k == "tags_to_delete":
                        names = {t.get("name") for t in v}
                        L["_embedded"]["tags"] = [t for t in L["_embedded"]["tags"] if t.get("name") not in names]
                    elif k == "_embedded":
                        if "tags" in v:
                            L["_embedded"]["tags"] = [{"id": t.get("id"), "name": t.get("name") or self._tag_name(t.get("id"))} for t in v["tags"]]
                    elif k == "custom_fields_values":
                        for cf in v:
                            L["custom_fields_values"] = [c for c in L.get("custom_fields_values", []) if c["field_id"] != cf["field_id"]]
                            if cf.get("values"):
                                L["custom_fields_values"].append({"field_id": cf["field_id"], "field_name": str(cf["field_id"]),
                                                                  "values": [{"value": x.get("value"), "enum_id": x.get("enum_id")} for x in cf["values"]]})
                    else:
                        L[k] = v
            return httpx.Response(200, json={"_embedded": {"leads": [{"id": b["id"]} for b in body]}})
        if p == "/leads/complex":
            return httpx.Response(200, json=[{"id": self._nid(), "contact_id": self._nid(), "company_id": None, "merged": False} for _ in body])
        if mm := re.fullmatch(r"/leads/(\d+)", p):
            L = self.leads.get(int(mm.group(1)))
            return httpx.Response(200, json=L) if L else httpx.Response(204)
        if p == "/contacts" and m == "GET":
            items = list(self.contacts.values())
            if "filter[id][]" in qm:
                ids = {int(x) for x in qm["filter[id][]"]}
                items = [x for x in items if x["id"] in ids]
            if "query" in q:
                items = [x for x in items if q["query"] in json.dumps(x, ensure_ascii=False)]
            return self._page(items, q, "contacts", path)
        if p == "/contacts" and m == "POST":
            out = []
            for b in body:
                i = self._nid()
                self.contacts[i] = {"id": i, **b}
                out.append({"id": i})
            return httpx.Response(200, json={"_embedded": {"contacts": out}})
        if mm := re.fullmatch(r"/contacts/(\d+)", p):
            C = self.contacts.get(int(mm.group(1)))
            return httpx.Response(200, json=C) if C else httpx.Response(204)
        if (mm := re.fullmatch(r"/(\w+)/(\d+)/(link|unlink)", p)):
            k = (mm.group(1), int(mm.group(2)))
            cur = self.links.setdefault(k, [])
            for b in body:
                cur[:] = [c for c in cur if not (c["to_entity_id"] == b["to_entity_id"] and c["to_entity_type"] == b["to_entity_type"])]
                if mm.group(3) == "link":
                    cur.append(b)
        if re.fullmatch(r"/\w+/\d+/(link|unlink)", p):
            return httpx.Response(200, json={"_embedded": {"links": body}}) if p.endswith("/link") else httpx.Response(204)
        if p == "/tasks" and m == "GET":
            items = list(self.tasks.values())
            if "filter[id][]" in qm:
                ids = {int(x) for x in qm["filter[id][]"]}
                items = [t for t in items if t["id"] in ids]
            if q.get("filter[is_completed]") == "0":
                items = [t for t in items if not t["is_completed"]]
            return self._page(items, q, "tasks", path)
        if p == "/tasks" and m == "POST":
            out = []
            for b in body:
                i = self._nid()
                self.tasks[i] = {"id": i, "is_completed": False, **b}
                out.append({"id": i})
            return httpx.Response(200, json={"_embedded": {"tasks": out}})
        if p == "/tasks" and m == "PATCH":
            for b in body:
                self.tasks.setdefault(b["id"], {"id": b["id"]}).update(b)
            return httpx.Response(200, json={"_embedded": {"tasks": [{"id": b["id"]} for b in body]}})
        if re.fullmatch(r"/\w+/notes", p) and m == "POST":
            return httpx.Response(200, json={"_embedded": {"notes": [{"id": self._nid()} for _ in body]}})
        if re.fullmatch(r"/\w+/(\d+/)?notes", p) and m == "GET":
            return httpx.Response(204)
        if p == "/events":
            return httpx.Response(200, json={"_embedded": {"events": [{"id": "e1", "type": "lead_status_changed", "entity_type": "lead", "entity_id": 1, "created_by": 1, "created_at": 1767225600,
                                                                       "value_before": [{"lead_status": {"id": 101, "pipeline_id": 10}}], "value_after": [{"lead_status": {"id": 102, "pipeline_id": 10}}]}]}})
        if p == "/webhooks":
            return httpx.Response(200, json={"_embedded": {"webhooks": []}})
        if p == "/companies" and m == "GET":
            items = list(self.companies.values())
            if "filter[id][]" in qm:
                ids = {int(x) for x in qm["filter[id][]"]}
                items = [x for x in items if x["id"] in ids]
            return self._page(items, q, "companies", path)
        if p in ("/contacts", "/companies") and m == "PATCH":
            store = self.contacts if p == "/contacts" else self.companies
            for b in body:
                E = store[b["id"]]
                E["updated_at"] = self._tick()
                for k, v in b.items():
                    if k == "custom_fields_values":
                        for cf in v:
                            old = next((c for c in E.get("custom_fields_values") or [] if c["field_id"] == cf["field_id"]), None)
                            code = (old or {}).get("field_code") or {600: "PHONE", 601: "EMAIL"}.get(cf["field_id"])
                            E["custom_fields_values"] = [c for c in E.get("custom_fields_values") or [] if c["field_id"] != cf["field_id"]]
                            if cf.get("values"):
                                E["custom_fields_values"].append({"field_id": cf["field_id"], "field_code": code, "field_name": code,
                                                                  "values": cf["values"]})
                    elif k not in ("id", "tags_to_add", "tags_to_delete", "_embedded"):
                        E[k] = v
            return httpx.Response(200, json={"_embedded": {p[1:]: [{"id": b["id"]} for b in body]}})
        if p == "/tasks" and m == "GET" and "filter[id][]" in qm:
            ids = {int(x) for x in qm["filter[id][]"]}
            return self._page([t for t in self.tasks.values() if t["id"] in ids], q, "tasks", path)
        if (mm := re.fullmatch(r"/leads/unsorted/(\w+)", p)) and m == "GET":
            u = self.unsorted.get(mm.group(1))
            return httpx.Response(200, json=u) if u else httpx.Response(404, json={"title": "Not Found"})
        if (mm := re.fullmatch(r"/leads/unsorted/(\w+)/decline", p)) and m == "DELETE":
            self.unsorted.pop(mm.group(1), None)
            self.deleted.append(("unsorted", mm.group(1)))
            return httpx.Response(200, json={"uid": mm.group(1)})
        if (mm := re.fullmatch(r"/(\w+)/notes/(\d+)", p)):
            n = self.notes.get(int(mm.group(2)))
            if m == "GET":
                return httpx.Response(200, json=n) if n else httpx.Response(204)
            n["params"] = body["params"]
            return httpx.Response(200, json={"id": n["id"]})
        if (mm := re.fullmatch(r"/(\w+)/(\d+)/links", p)):
            return httpx.Response(200, json={"_embedded": {"links": self.links.get((mm.group(1), int(mm.group(2))), [])}})
        if (mm := re.fullmatch(r"/leads/pipelines/(\d+)", p)) and m == "DELETE":
            self.deleted.append(("pipeline", int(mm.group(1))))
            return httpx.Response(204)
        if (mm := re.fullmatch(r"/leads/pipelines/(\d+)/statuses/(\d+)", p)) and m == "DELETE":
            self.deleted.append(("status", int(mm.group(2))))
            return httpx.Response(204)
        if (mm := re.fullmatch(r"/(\w+)/custom_fields/(\d+)", p)) and m == "DELETE":
            self.deleted.append(("field", int(mm.group(2))))
            return httpx.Response(204)
        if (mm := re.fullmatch(r"/(\w+)/custom_fields", p)) and m == "PATCH":
            self.last_cf_patch = body
            return httpx.Response(200, json={"_embedded": {"custom_fields": body}})
        return httpx.Response(404, json={"title": "Not Found", "detail": f"mock: {m} {p}"})
