"""Team: who sees and changes what (especially 1-2-1s, feedback and manager notes), responsibilities, work."""
import pytest
from fastapi.testclient import TestClient

import tests.test_projects as base  # noqa: F401  (sets the test environment first)
from app import access, main, store as store_mod, tasks, team

SECRET = base.SECRET
H = {"X-Requested-With": "nscale-admin"}


def who(email, tool="team", admin=False):
    return {**H, "x-cp-user": access.issue(email, access.display_name(email), tool, admin, secret=SECRET)}


ADMIN = who("boss@nscale.com", admin=True)
MGR = who("james.brown@nscale.com")          # Amy's manager
AMY = who("amy.chen@nscale.com")             # reports to James
BOB = who("bob.smith@nscale.com")            # a colleague: not her manager
BASE = "/team/api/members"


@pytest.fixture
def c(tmp_path, monkeypatch):
    s = store_mod.Store(str(tmp_path / "t.db"))
    monkeypatch.setattr(main, "store", s)
    tasks.setup(s, main.STATIC)
    team.setup(s, main.STATIC)
    main.inbox.setup(s, main.STATIC)
    with TestClient(main.app) as client:
        # Admin sets up James; James adds Amy (who then reports to him); admin adds Bob.
        assert client.post(BASE, headers=ADMIN, json={"email": "james.brown@nscale.com", "title": "Client Platforms Lead"}).status_code == 200
        assert client.post(BASE, headers=MGR, json={"email": "Amy.Chen@nscale.com", "name": "Amy Chen", "title": "Endpoint Engineer",
                                                     "team": "Client Platforms"}).status_code == 200
        client.post(BASE, headers=ADMIN, json={"email": "bob.smith@nscale.com", "title": "Engineer", "manager": "james.brown@nscale.com"})
        yield client


def entries(c, headers, email="amy.chen@nscale.com"):
    return c.get(f"{BASE}/{email}/entries", headers=headers)


def test_sign_in_is_checked(c):
    assert c.get(BASE).status_code == 401
    assert c.get(BASE, headers=who("amy.chen@nscale.com", "tasks")).status_code == 401   # a Tasks token
    assert c.post(BASE, headers={"x-cp-user": AMY["x-cp-user"]}, json={"email": "x@nscale.com"}).status_code == 403
    assert c.get("/team/").status_code == 403 and c.get("/team/", headers=AMY).status_code == 200


def test_who_can_add_and_change_people(c):
    amy = c.get(f"{BASE}/amy.chen@nscale.com", headers=BOB).json()
    assert amy["manager"] == "james.brown@nscale.com" and amy["managerName"] == "James Brown"
    assert amy["rights"] == {"edit": False, "personal": False, "managerNotes": False, "isMe": False, "isManager": False, "delete": False}
    assert c.get(f"{BASE}/james.brown@nscale.com", headers=BOB).json()["reports"][0]["email"] in ("amy.chen@nscale.com", "bob.smith@nscale.com")
    # A non-admin can only add their own reports.
    assert c.post(BASE, headers=AMY, json={"email": "new@nscale.com", "manager": "bob.smith@nscale.com"}).status_code == 403
    assert c.post(BASE, headers=AMY, json={"email": "new@nscale.com"}).json()["manager"] == "amy.chen@nscale.com"
    assert c.post(BASE, headers=ADMIN, json={"email": "new@nscale.com"}).status_code == 409
    assert c.post(BASE, headers=ADMIN, json={"email": "nope"}).status_code == 422
    # Her manager edits her; a colleague can't; only an admin changes her manager or removes her.
    assert c.patch(f"{BASE}/amy.chen@nscale.com", headers=MGR, json={"title": "Senior Endpoint Engineer"}).json()["title"] == "Senior Endpoint Engineer"
    assert c.patch(f"{BASE}/amy.chen@nscale.com", headers=BOB, json={"title": "x"}).status_code == 403
    assert c.patch(f"{BASE}/amy.chen@nscale.com", headers=AMY, json={"title": "CEO"}).status_code == 403
    assert c.patch(f"{BASE}/amy.chen@nscale.com", headers=MGR, json={"manager": "bob.smith@nscale.com"}).status_code == 403
    assert c.patch(f"{BASE}/amy.chen@nscale.com", headers=ADMIN, json={"manager": "amy.chen@nscale.com"}).status_code == 422
    assert c.delete(f"{BASE}/amy.chen@nscale.com", headers=MGR).status_code == 403
    assert c.delete(f"{BASE}/new@nscale.com", headers=ADMIN).status_code == 200
    # Responsibilities: her manager sets them; everyone can see them.
    r = c.put(f"{BASE}/amy.chen@nscale.com/responsibilities", headers=MGR, json={"items": [
        {"title": "Intune", "detail": "Policies, apps and Autopilot"}, {"title": "  "}, {"title": "Jamf"}]}).json()
    assert [x["title"] for x in r["responsibilities"]] == ["Intune", "Jamf"]
    assert c.put(f"{BASE}/amy.chen@nscale.com/responsibilities", headers=BOB, json={"items": []}).status_code == 403
    assert len(c.get(f"{BASE}/amy.chen@nscale.com", headers=BOB).json()["responsibilities"]) == 2
    lst = {m["email"]: m for m in c.get(BASE, headers=BOB).json()["members"]}
    assert lst["amy.chen@nscale.com"]["responsibilities"] == 2 and lst["amy.chen@nscale.com"]["lastOneToOne"] is None


def test_one_to_ones_feedback_and_manager_notes(c):
    add = lambda h, **kw: c.post(f"{BASE}/amy.chen@nscale.com/entries", headers=h, json=kw)  # noqa: E731
    r = add(MGR, kind="one_to_one", date="2026-11-18", body="Talked about the SL7 rollout.",
            data={"wellbeing": 4, "next": "2026-12-02", "actions": [{"text": "Book Intune training", "owner": "Amy"}, {"text": " "}]})
    assert r.status_code == 200
    one = r.json()["entries"]["one_to_one"][0]
    assert one["data"]["wellbeing"] == 4 and [a["text"] for a in one["data"]["actions"]] == ["Book Intune training"]
    assert add(MGR, kind="well", body="Led the pilot really well.").status_code == 200
    assert add(MGR, kind="improve", body="Write things down before changing prod.").status_code == 200
    assert add(MGR, kind="manager_note", body="Ready for a senior role in 2027.").status_code == 200
    assert add(MGR, kind="one_to_one", data={"wellbeing": 9}).status_code == 422
    assert add(MGR, kind="gossip", body="x").status_code == 422
    assert add(MGR, kind="well", body="   ").status_code == 422
    # Amy reads her own 1-2-1s and feedback, never the manager notes, and can't add or change anything.
    mine = entries(c, AMY).json()
    assert set(mine["entries"]) == {"one_to_one", "well", "improve"} and mine["canWrite"] is False
    assert "Ready for a senior role" not in str(mine)
    assert add(AMY, kind="well", body="I'm great").status_code == 403
    assert c.put(f"{BASE}/amy.chen@nscale.com/entries/{one['id']}", headers=AMY, json={"body": "x"}).status_code == 403
    # A colleague sees none of it.
    assert entries(c, BOB).status_code == 403
    # Her manager and admins see everything, notes included, and can edit and delete.
    full = entries(c, MGR).json()
    assert set(full["entries"]) == {"one_to_one", "well", "improve", "manager_note"} and full["canWrite"]
    assert set(entries(c, ADMIN).json()["entries"]) == set(team.KINDS)
    r = c.put(f"{BASE}/amy.chen@nscale.com/entries/{one['id']}", headers=MGR,
              json={"data": {**one["data"], "actions": [{**one["data"]["actions"][0], "done": True}]}}).json()
    assert r["entries"]["one_to_one"][0]["data"]["actions"][0]["done"] is True
    assert r["entries"]["one_to_one"][0]["body"] == "Talked about the SL7 rollout."   # untouched
    note = full["entries"]["manager_note"][0]["id"]
    assert c.delete(f"{BASE}/amy.chen@nscale.com/entries/{note}", headers=AMY).status_code == 403
    assert c.delete(f"{BASE}/amy.chen@nscale.com/entries/{note}", headers=MGR).json()["entries"]["manager_note"] == []
    # The list shows the last 1-2-1 to those who may see it.
    assert {m["email"]: m["lastOneToOne"] for m in c.get(BASE, headers=MGR).json()["members"]}["amy.chen@nscale.com"] == "2026-11-18"
    assert {m["email"]: m["lastOneToOne"] for m in c.get(BASE, headers=BOB).json()["members"]}["amy.chen@nscale.com"] is None


def test_work_shows_only_what_the_viewer_can_see(c):
    P = lambda e: who(e, "projects")  # noqa: E731
    T = lambda e: who(e, "tasks")  # noqa: E731
    pid = c.post("/projects/api/projects", headers=P("amy.chen@nscale.com"), json={"name": "Laptop refresh"}).json()["id"]
    c.post(f"/projects/api/projects/{pid}/items/risks", headers=P("amy.chen@nscale.com"),
           json={"title": "Supply slips", "description": "d", "likelihood": "4", "impact": "4"})
    secret = c.post("/projects/api/projects", headers=P("amy.chen@nscale.com"), json={"name": "Secret reorg"}).json()["id"]
    t = c.post("/tasks/api/tasks", headers=T("amy.chen@nscale.com"), json={"title": "Order docks", "assignee": "amy.chen@nscale.com", "projectId": pid}).json()
    c.post(f"/tasks/api/tasks/{t['id']}/activity", headers=T("amy.chen@nscale.com"), json={"kind": "update", "body": "PO raised", "progress": 50})
    c.post("/tasks/api/tasks", headers=T("amy.chen@nscale.com"), json={"title": "Loose task", "assignee": "amy.chen@nscale.com"})
    # James is in Laptop refresh only.
    c.post(f"/projects/api/projects/{pid}/members", headers=P("amy.chen@nscale.com"), json={"email": "james.brown@nscale.com", "role": "Viewer"})
    w = c.get(f"{BASE}/amy.chen@nscale.com/work", headers=MGR).json()
    assert [p["name"] for p in w["projects"]] == ["Laptop refresh"]
    assert {x["title"] for x in w["openTasks"]} == {"Order docks", "Loose task"}
    texts = " | ".join(a["text"] for a in w["activity"])
    assert "risk RSK-001 Supply slips" in texts and "posted an update on PL-001 Order docks (50%)" in texts
    assert "Secret reorg" not in str(w)
    # Amy sees all of hers; an admin sees everything.
    assert {p["name"] for p in c.get(f"{BASE}/amy.chen@nscale.com/work", headers=AMY).json()["projects"]} == {"Laptop refresh", "Secret reorg"}
    assert len(c.get(f"{BASE}/amy.chen@nscale.com/work", headers=ADMIN).json()["projects"]) == 2
    # Bob (no projects) sees only the loose task.
    wb = c.get(f"{BASE}/amy.chen@nscale.com/work", headers=BOB).json()
    assert wb["projects"] == [] and [x["title"] for x in wb["openTasks"]] == ["Loose task"]


def test_formatted_notes_are_kept_safe(c):
    html, text = team.clean_notes('<p><b><u>Wins and what went well</u></b></p><p>Pilot <i>done</i><script>alert(1)</script></p>'
                                  '<p><a href="javascript:x" onclick="y">link</a><img src=x onerror=z></p><ul><li>one</li></ul><p><br></p>')
    assert html == '<p><b><u>Wins and what went well</u></b></p><p>Pilot <i>done</i></p><p>link</p><ul><li>one</li></ul>'
    assert text == "Wins and what went well\nPilot done\nlink\n• one"
    assert team.clean_notes("<b>unclosed")[0] == "<b>unclosed</b>"
    r = c.post(f"{BASE}/amy.chen@nscale.com/entries", headers=MGR, json={"kind": "one_to_one", "date": "2026-11-18",
               "data": {"html": "<p><b><u>Priorities</u></b></p><p>SL7 pilot</p><script>bad()</script>", "actions": []}}).json()
    one = r["entries"]["one_to_one"][0]
    assert one["data"]["html"] == "<p><b><u>Priorities</u></b></p><p>SL7 pilot</p>" and one["body"] == "Priorities\nSL7 pilot"
    assert "text" not in one["data"]
    # Ticking an action off keeps the formatting.
    r = c.put(f"{BASE}/amy.chen@nscale.com/entries/{one['id']}", headers=MGR, json={"data": {**one["data"], "actions": [{"text": "Book it"}]}}).json()
    assert r["entries"]["one_to_one"][0]["data"]["html"] == one["data"]["html"]
    assert c.post(f"{BASE}/amy.chen@nscale.com/entries", headers=MGR, json={"kind": "one_to_one", "data": {"html": "<p><br></p>"}}).status_code == 422
