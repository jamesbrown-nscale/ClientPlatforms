"""The recycle bin: what's deleted is kept 30 days with everything that went with it, and comes back as it was."""
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

import tests.test_projects as base  # noqa: F401  (sets the test environment first)
from app import access, main, recycle, store as store_mod, tasks, team
from tests.test_files import doc_record, upload, url

SECRET = base.SECRET
H = {"X-Requested-With": "nscale-admin"}
BIN = "/projects/api/bin"


def who(email, tool, admin=False):
    return {**H, "x-cp-user": access.issue(email, access.display_name(email), tool, admin, secret=SECRET)}


AMY_P, AMY_T = who("amy@nscale.com", "projects"), who("amy@nscale.com", "tasks")
ADMIN_P, ADMIN_T, ADMIN_TEAM = who("boss@nscale.com", "projects", True), who("boss@nscale.com", "tasks", True), who("boss@nscale.com", "team", True)


@pytest.fixture
def c(tmp_path, monkeypatch):
    s = store_mod.Store(str(tmp_path / "t.db"))
    monkeypatch.setattr(main, "store", s)
    tasks.setup(s, main.STATIC)
    team.setup(s, main.STATIC)
    main.inbox.setup(s, main.STATIC)
    main.changes.setup(s, main.STATIC)
    monkeypatch.setitem(team._cache, "departments", None)
    with TestClient(main.app) as client:
        yield client


# Each tool's deletions are in its own bin, for that tool's admins.
BINS = {"projects": (BIN, ADMIN_P), "tasks": ("/tasks/api/bin", ADMIN_T), "team": ("/team/api/bin", ADMIN_TEAM)}


def binned(c, tool="projects"):
    path, h = BINS[tool]
    r = c.get(path, headers=h)
    assert r.status_code == 200, r.text
    return r.json()["items"]


def restore(c, kind, tool="projects"):
    path, h = BINS[tool]
    item = next(i for i in binned(c, tool) if i["kind"] == kind)
    return c.post(f"{path}/{item['id']}/restore", headers=h)


def test_only_admins(c):
    assert c.get(BIN).status_code == 401
    assert c.get(BIN, headers=AMY_P).status_code == 403
    assert c.get(BIN, headers=ADMIN_P).json() == {"items": [], "keepDays": 30}
    assert c.post(f"{BIN}/x/restore", headers={"x-cp-user": ADMIN_P["x-cp-user"]}).status_code == 403   # needs the page's header
    assert c.post(f"{BIN}/x/restore", headers=ADMIN_P).status_code == 404
    assert c.get("/tasks/api/bin", headers=AMY_T).status_code == 403   # Team Planning's bin: its admins


def test_document_comes_back_with_every_version(c):
    pid = base.new_project(c, headers=AMY_P, name="Laptop refresh")
    iid = doc_record(c, pid)
    assert upload(c, pid, iid, "costs.csv", b"Item,Cost\nLaptops,250\n", headers=AMY_P).status_code == 200
    assert upload(c, pid, iid, "costs.csv", b"Item,Cost\nLaptops,300\n", headers=AMY_P).status_code == 200
    assert c.delete(url(pid, f"/items/documents/{iid}"), headers=AMY_P).status_code == 200
    assert c.get(url(pid, f"/items/documents/{iid}"), headers=AMY_P).status_code in (404, 405)

    item = binned(c)[0]
    assert item["kind"] == "Document" and "Laptop refresh" in item["context"] and item["deletedBy"] == "amy@nscale.com"
    assert "Laptops" not in item["label"]   # what it was, not its contents
    assert restore(c, "Document").status_code == 200 and binned(c) == []
    with main.store.db() as db:
        versions = db.execute("SELECT version, data FROM item_files WHERE item_id = ? ORDER BY version", (iid,)).fetchall()
    assert [(v[0], bytes(v[1])) for v in versions] == [(1, b"Item,Cost\nLaptops,250\n"), (2, b"Item,Cost\nLaptops,300\n")]


def test_project_with_its_records_members_and_plans(c):
    pid = base.new_project(c, headers=AMY_P, name="Intune migration")
    c.post(url(pid, "/items/risks"), headers=AMY_P, json={"title": "Licences late", "likelihood": 3, "impact": 4})
    tid = c.post("/tasks/api/tasks", headers=AMY_T, json={"title": "Pilot", "projectId": pid}).json()["id"]
    assert c.delete(url(pid), headers=AMY_P).status_code == 200
    with main.store.db() as db:   # the plan lost its project
        assert db.execute("SELECT project_id FROM tasks WHERE id = ?", (tid,)).fetchone()[0] is None

    assert [i["label"] for i in binned(c)] == ["Intune migration"]
    assert restore(c, "Project").status_code == 200
    assert c.get(url(pid), headers=AMY_P).json()["name"] == "Intune migration"   # Amy is its owner again
    assert len(c.get(url(pid, "/items/risks"), headers=AMY_P).json()) == 1
    with main.store.db() as db:   # and the plan is back in it
        assert db.execute("SELECT project_id FROM tasks WHERE id = ?", (tid,)).fetchone()[0] == pid


def test_plan_job_sub_job_and_comment(c):
    t = c.post("/tasks/api/tasks", headers=AMY_T, json={"title": "Rebuild Autopilot"}).json()
    tid = t["id"]
    job = c.post(f"/tasks/api/tasks/{tid}/jobs", headers=AMY_T, json={"title": "Export hashes", "start": "2026-11-23", "duration": 3}).json()["jobs"][-1]
    jid = job["id"]
    for title in ("One", "Two", "Three"):
        c.post(f"/tasks/api/tasks/{tid}/jobs/{jid}/subs", headers=AMY_T, json={"title": title})
    subs = next(j for j in c.get(f"/tasks/api/tasks/{tid}", headers=AMY_T).json()["jobs"] if j["id"] == jid)["subs"]
    q = c.post(f"/tasks/api/tasks/{tid}/activity", headers=AMY_T, json={"kind": "question", "body": "Which tenant?"}).json()["activity"][-1]
    a = c.post(f"/tasks/api/tasks/{tid}/activity", headers=AMY_T, json={"kind": "answer", "answers": q["id"], "body": "Prod."}).json()["activity"][-1]

    # A sub job goes back where it was in the list.
    c.delete(f"/tasks/api/tasks/{tid}/jobs/{jid}/subs/{subs[1]['id']}", headers=AMY_T)
    assert restore(c, "Sub job", "tasks").status_code == 200
    back = next(j for j in c.get(f"/tasks/api/tasks/{tid}", headers=AMY_T).json()["jobs"] if j["id"] == jid)["subs"]
    assert [s["title"] for s in back] == ["One", "Two", "Three"]

    # Deleting the answer reopens the question; restoring it answers it again.
    c.delete(f"/tasks/api/tasks/{tid}/activity/{a['id']}", headers=AMY_T)
    act = lambda: {x["id"]: x for x in c.get(f"/tasks/api/tasks/{tid}", headers=AMY_T).json()["activity"]}  # noqa: E731
    assert not act()[q["id"]].get("answeredAt")
    assert restore(c, "Comment", "tasks").status_code == 200
    assert act()[q["id"]].get("answeredAt") and a["id"] in act()

    # A job whose plan has gone since can't come back until the plan does.
    c.delete(f"/tasks/api/tasks/{tid}/jobs/{jid}", headers=AMY_T)
    c.delete(f"/tasks/api/tasks/{tid}", headers=AMY_T)
    r = restore(c, "Job", "tasks")
    assert r.status_code == 409 and "Restore that first" in r.json()["detail"]
    assert {i["kind"] for i in binned(c, "tasks")} == {"Job", "Plan"}   # still there
    assert restore(c, "Plan", "tasks").status_code == 200 and restore(c, "Job", "tasks").status_code == 200
    jobs = c.get(f"/tasks/api/tasks/{tid}", headers=AMY_T).json()["jobs"]
    assert [j["title"] for j in jobs] == ["Export hashes"] and len(jobs[0]["subs"]) == 3


def test_team_member_with_reports_and_notes(c):
    add = lambda **kw: c.post("/team/api/members", headers=ADMIN_TEAM, json=kw)  # noqa: E731
    add(email="james.brown@nscale.com", title="Lead")
    add(email="amy@nscale.com", name="Amy", manager="james.brown@nscale.com")
    c.post("/team/api/members/amy@nscale.com/entries", headers=ADMIN_TEAM,
           json={"kind": "one_to_one", "date": "2026-11-18", "body": "Private: talked about pay."})
    assert c.delete("/team/api/members/james.brown@nscale.com", headers=ADMIN_TEAM).status_code == 200
    assert c.get("/team/api/members/amy@nscale.com", headers=ADMIN_TEAM).json()["manager"] == ""
    assert restore(c, "Team member", "team").status_code == 200
    assert c.get("/team/api/members/amy@nscale.com", headers=ADMIN_TEAM).json()["manager"] == "james.brown@nscale.com"

    eid = c.get("/team/api/members/amy@nscale.com/entries", headers=ADMIN_TEAM).json()["entries"]["one_to_one"][0]["id"]
    c.delete(f"/team/api/members/amy@nscale.com/entries/{eid}", headers=ADMIN_TEAM)
    assert binned(c) == []   # not in Projects' bin: Projects admins don't see the team's notes
    item = binned(c, "team")[0]
    assert item["label"] == "1-2-1 · 2026-11-18" and item["context"] == "Amy" and "pay" not in str(item)
    # Someone with the same email added since: it can't come back as it was.
    c.delete("/team/api/members/amy@nscale.com", headers=ADMIN_TEAM)
    add(email="amy@nscale.com", name="Amy (new)")
    r = restore(c, "Team member", "team")
    assert r.status_code == 409 and "same name or ID" in r.json()["detail"]


def test_delete_for_good_and_after_30_days(c):
    tid = c.post("/tasks/api/tasks", headers=AMY_T, json={"title": "Old plan"}).json()["id"]
    c.delete(f"/tasks/api/tasks/{tid}", headers=AMY_T)
    bid = binned(c, "tasks")[0]["id"]
    assert c.delete(f"/tasks/api/bin/{bid}", headers=ADMIN_T).status_code == 200 and binned(c, "tasks") == []
    assert c.delete(f"/tasks/api/bin/{bid}", headers=ADMIN_T).status_code == 404

    tid = c.post("/tasks/api/tasks", headers=AMY_T, json={"title": "Older plan"}).json()["id"]
    c.delete(f"/tasks/api/tasks/{tid}", headers=AMY_T)
    old = (datetime.now(timezone.utc) - timedelta(days=31)).strftime("%Y-%m-%dT%H:%M:%SZ")
    with main.store.db() as db:
        db.execute("UPDATE recycle_bin SET deleted_at = ?", (old,))
    assert binned(c, "tasks") == []


def test_module_is_the_same_everywhere():
    """recycle.py is one file, copied into each service that has a bin."""
    from pathlib import Path
    copies = list(Path(__file__).resolve().parents[2].glob("*/app/recycle.py"))
    if len(copies) < 2:
        pytest.skip("the other services aren't here (Docker build)")
    assert len({p.read_text() for p in copies}) == 1, [str(p) for p in copies]
    assert recycle.KEEP_DAYS == 30
