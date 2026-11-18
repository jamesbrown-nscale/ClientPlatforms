"""Projects: who can see and change what, the section rules, meeting actions, dashboard and drafts."""
import importlib
import os
import tempfile

SECRET = "p" * 40
os.environ.update(BASE_PATH="/projects", AUTH_MODE="local", AUTH_SECRET=SECRET, DEV_USER="",
                  DATABASE_PATH=os.path.join(tempfile.mkdtemp(), "p.db"))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import access, main, modules, store as store_mod  # noqa: E402

H = {"X-Requested-With": "nscale-admin"}


def who(email, admin=False):
    return {**H, "x-cp-user": access.issue(email, access.display_name(email), "projects", admin, secret=SECRET)}


AMY, BOB, CAT, ADMIN = who("amy@nscale.com"), who("bob@nscale.com"), who("cat@nscale.com"), who("boss@nscale.com", True)


@pytest.fixture
def c(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "store", store_mod.Store(str(tmp_path / "t.db")))
    with TestClient(main.app) as client:
        yield client


def new_project(c, headers=AMY, name="Laptop refresh"):
    r = c.post("/projects/api/projects", headers=headers, json={"name": name, "description": "250 laptops"})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def url(pid, rest=""):
    return f"/projects/api/projects/{pid}{rest}"


def test_sign_in_is_checked(c):
    assert c.get("/projects/api/projects").status_code == 401
    assert c.get("/projects/api/projects", headers={"x-cp-user": access.issue("a@x.com", "A", "restart", secret=SECRET)}).status_code == 401
    assert c.post("/projects/api/projects", headers={"x-cp-user": AMY["x-cp-user"]}, json={"name": "x"}).status_code == 403  # no header
    assert c.get("/projects/").status_code == 403
    assert c.get("/projects/", headers=AMY).status_code == 200
    meta = c.get("/projects/api/meta", headers=AMY).json()
    assert [m["key"] for m in meta["modules"]][:3] == ["statusReports", "milestones", "risks"]
    assert "runbooks" not in [m["key"] for m in meta["modules"]]


def test_members_decide_who_sees_what(c):
    pid = new_project(c)
    assert [p["id"] for p in c.get("/projects/api/projects", headers=AMY).json()["projects"]] == [pid]
    assert c.get("/projects/api/projects", headers=BOB).json()["projects"] == []
    assert c.get(url(pid), headers=BOB).status_code == 404   # doesn't even say it exists
    assert c.get(url(pid), headers=ADMIN).json()["role"] == "Owner"   # admins see everything

    assert c.post(url(pid, "/members"), headers=AMY, json={"email": "Bob@nscale.com", "role": "Viewer"}).status_code == 200
    assert c.post(url(pid, "/members"), headers=AMY, json={"email": "cat@nscale.com", "role": "Editor"}).status_code == 200
    assert c.post(url(pid, "/members"), headers=AMY, json={"email": "nope", "role": "Editor"}).status_code == 422
    assert c.get(url(pid), headers=BOB).json()["role"] == "Viewer"
    # Viewers read; editors write records; only owners change the project or its members.
    assert c.post(url(pid, "/items/risks"), headers=BOB, json={"title": "x"}).status_code == 403
    assert c.post(url(pid, "/items/risks"), headers=CAT, json={"title": "Supply delay", "description": "Laptops late", "likelihood": "4", "impact": "5"}).status_code == 200
    assert c.patch(url(pid), headers=CAT, json={"name": "Renamed"}).status_code == 403
    assert c.post(url(pid, "/members"), headers=CAT, json={"email": "dan@nscale.com"}).status_code == 403
    assert c.delete(url(pid), headers=CAT).status_code == 403
    # There's always an owner.
    assert c.patch(url(pid, "/members/amy@nscale.com"), headers=AMY, json={"role": "Editor"}).status_code == 400
    assert c.delete(url(pid, "/members/amy@nscale.com"), headers=AMY).status_code == 400
    # Anyone can leave.
    assert c.delete(url(pid, "/members/bob@nscale.com"), headers=BOB).status_code == 200
    assert c.get(url(pid), headers=BOB).status_code == 404
    assert c.delete(url(pid), headers=AMY).status_code == 200
    assert c.get("/projects/api/projects", headers=AMY).json()["projects"] == []


def test_section_rules(c):
    pid = new_project(c)
    r = c.post(url(pid, "/items/risks"), headers=AMY, json={"title": "Supply delay", "description": "Laptops late", "likelihood": "4", "impact": "5"})
    item = r.json()
    assert item["ref"] == "RSK-001" and item["data"]["score"] == 20 and item["data"]["status"]
    assert c.post(url(pid, "/items/risks"), headers=AMY, json={"title": "Two", "description": "d", "likelihood": "9"}).status_code == 422
    assert c.post(url(pid, "/items/risks"), headers=AMY, json={"likelihood": "2"}).status_code == 422   # title required
    assert c.post(url(pid, "/items/risks"), headers=AMY, json={"title": "Two", "description": "d", "likelihood": "2", "impact": "2"}).json()["ref"] == "RSK-002"
    b = c.post(url(pid, "/items/budget"), headers=AMY, json={"category": "Hardware", "description": "Laptops",
                                                            "quantity": "250", "unitCost": "£1,000", "actual": "200000"}).json()
    assert b["data"]["planned"] == 250000 and b["data"]["variance"] == 50000
    assert c.post(url(pid, "/items/documents"), headers=AMY, json={"title": "Design", "link": "javascript:alert(1)"}).status_code == 422
    assert c.post(url(pid, "/items/nope"), headers=AMY, json={}).status_code == 404
    # Updates log, newest first; quick action.
    r = c.post(url(pid, "/items/issues"), headers=AMY, json={"title": "Imaging fails", "updates": [
        {"date": "2026-11-01", "notes": "Raised with vendor"}, {"date": "2026-11-10", "notes": "Patch due"}]}).json()
    assert [u["notes"] for u in r["data"]["updates"]] == ["Patch due", "Raised with vendor"]
    a = c.post(url(pid, "/items/actions"), headers=AMY, json={"description": "Order docks"}).json()
    assert c.post(url(pid, f"/items/actions/{a['id']}/quick"), headers=AMY).json()["data"]["status"] == "Completed"
    # Edit keeps the reference; delete removes it.
    up = c.put(url(pid, f"/items/actions/{a['id']}"), headers=AMY, json={**a["data"], "description": "Order 250 docks"}).json()
    assert up["ref"] == a["ref"] and up["data"]["description"] == "Order 250 docks"
    assert c.delete(url(pid, f"/items/actions/{a['id']}"), headers=AMY).status_code == 200
    assert c.get(url(pid, "/items/actions"), headers=AMY).json()["items"] == []


def test_meeting_actions_go_to_the_action_log(c):
    pid = new_project(c)
    m = c.post(url(pid, "/items/meetings"), headers=AMY, json={
        "title": "Weekly stand-up", "date": "2026-11-18", "attendees": "Amy, Bob",
        "actions": [{"description": "Book the pilot users", "assignedTo": "Bob", "dueDate": "2026-11-25"}]}).json()
    assert m["data"]["attendees"] == ["Amy", "Bob"]
    acts = c.get(url(pid, "/items/actions"), headers=AMY).json()["items"]
    assert len(acts) == 1 and acts[0]["data"]["owner"] == "Bob" and acts[0]["source"]["meeting"] == m["id"]
    # Completing it in the Action Log completes it in the meeting.
    c.post(url(pid, f"/items/actions/{acts[0]['id']}/quick"), headers=AMY)
    m2 = c.get(url(pid, "/items/meetings"), headers=AMY).json()["items"][0]
    assert m2["data"]["actions"][0]["status"] == "Completed"
    # Taking it out of the meeting takes it out of the log.
    c.put(url(pid, f"/items/meetings/{m['id']}"), headers=AMY, json={**m2["data"], "actions": []})
    assert c.get(url(pid, "/items/actions"), headers=AMY).json()["items"] == []


def test_templates_dashboard_and_status_draft(c):
    pid = new_project(c)
    plan = c.post(url(pid, "/items/milestones/template"), headers=AMY, json={"start": modules.add_days(modules.today(), -10)}).json()["items"]
    assert len(plan) == 13 and plan[0]["ref"] == "MS-001"
    closure = c.post(url(pid, "/items/closure/template"), headers=AMY, json={}).json()["items"]
    assert len(closure) == 16 and closure[0]["data"]["category"] == "Handover to BAU"
    assert c.post(url(pid, "/items/risks/template"), headers=AMY, json={}).status_code == 404
    c.post(url(pid, "/items/risks"), headers=AMY, json={"title": "Supply delay", "description": "Laptops late", "likelihood": "5", "impact": "5"})
    c.post(url(pid, "/items/actions"), headers=AMY, json={"description": "Late thing", "dueDate": "2020-01-01"})
    c.patch(url(pid), headers=AMY, json={"approvedBudget": "1000", "currency": "EUR"})
    c.post(url(pid, "/items/budget"), headers=AMY, json={"category": "Hardware", "description": "x", "actual": "1500"})
    s = c.get(url(pid, "/summary"), headers=AMY).json()
    assert s["status"]["phase"] == "Discovery" and s["risks"]["critical"] == 1 and s["actions"]["overdue"] == 1
    assert s["budget"] == {"lines": 1, "approved": 1000.0, "planned": 0.0, "actual": 1500.0, "currency": "EUR"}
    d = c.get(url(pid, "/status-draft"), headers=AMY).json()
    assert d["risksRag"] == "Red" and d["budgetRag"] == "Red" and d["scheduleRag"] == "Amber" and d["overallRag"] == "Red"
    assert "RSK-001" in d["keyRisks"]
    card = c.get("/projects/api/projects", headers=AMY).json()["projects"][0]
    assert card["summary"]["phase"] == "Discovery" and card["summary"]["openRisks"] == 1
    sr = c.post(url(pid, "/items/statusReports"), headers=AMY, json={**d, "summary": "On track apart from budget."}).json()
    assert sr["ref"] == "SR-001"
    assert c.get(url(pid, "/summary"), headers=AMY).json()["status"]["overallRag"] == "Red"
    assert "Amy" in " ".join(c.get(url(pid, "/people"), headers=AMY).json()["people"])


def test_project_details(c):
    pid = new_project(c)
    assert c.patch(url(pid), headers=AMY, json={"status": "nope"}).status_code == 422
    assert c.patch(url(pid), headers=AMY, json={"name": " "}).status_code == 422
    assert c.patch(url(pid), headers=AMY, json={"approvedBudget": "abc"}).status_code == 422
    p = c.patch(url(pid), headers=AMY, json={"status": "on hold", "approvedBudget": "£12,500"}).json()
    assert p["status"] == "on hold" and p["approvedBudget"] == 12500
    assert c.post("/projects/api/projects", headers=AMY, json={"name": ""}).status_code == 422


def test_page_and_base_path(c):
    assert c.get("/projects", follow_redirects=False).headers["location"] == "/projects/"
    assert c.get("/healthz").json() == {"ok": True}


def test_dev_user_is_an_admin(monkeypatch, tmp_path):
    monkeypatch.setenv("DEV_USER", "dev@nscale.com")
    for m in (access, main.auth):
        importlib.reload(m)
    importlib.reload(main)
    monkeypatch.setattr(main, "store", store_mod.Store(str(tmp_path / "d.db")))
    with TestClient(main.app) as c:
        assert c.get("/projects/api/me").json()["admin"] is True
    monkeypatch.setenv("DEV_USER", "")
    for m in (access, main.auth):
        importlib.reload(m)
    importlib.reload(main)
