"""Tasks: who sees and changes what, assigning, questions and answers, updates, mentions, the badge."""
import pytest
from fastapi.testclient import TestClient

import tests.test_projects as base  # noqa: F401  (sets the test environment first)
from app import access, main, store as store_mod, tasks

SECRET = base.SECRET
H = {"X-Requested-With": "nscale-admin"}


def who(email, tool="tasks", admin=False):
    return {**H, "x-cp-user": access.issue(email, access.display_name(email), tool, admin, secret=SECRET)}


AMY, BOB, CAT, ADMIN = who("amy.chen@nscale.com"), who("bob.smith@nscale.com"), who("cat@nscale.com"), who("boss@nscale.com", admin=True)
AMY_P = who("amy.chen@nscale.com", "projects")


@pytest.fixture
def c(tmp_path, monkeypatch):
    s = store_mod.Store(str(tmp_path / "t.db"))
    monkeypatch.setattr(main, "store", s)
    tasks.setup(s, main.STATIC)
    main.team.setup(s, main.STATIC)
    main.inbox.setup(s, main.STATIC)
    with TestClient(main.app) as client:
        yield client


def new(c, headers=AMY, **kw):
    r = c.post("/tasks/api/tasks", headers=headers, json={"title": "Rebuild Autopilot profile", **kw})
    assert r.status_code == 200, r.text
    return r.json()


def view(c, headers, v="mine", **q):
    return c.get("/tasks/api/tasks", headers=headers, params={"view": v, **q}).json()


def test_sign_in_is_checked(c):
    assert c.get("/tasks/api/tasks").status_code == 401
    assert c.get("/tasks/api/tasks", headers=who("amy.chen@nscale.com", "projects")).status_code == 401   # a Projects token
    assert c.post("/tasks/api/tasks", headers={"x-cp-user": AMY["x-cp-user"]}, json={"title": "x"}).status_code == 403
    assert c.get("/tasks/").status_code == 403 and c.get("/tasks/", headers=AMY).status_code == 200
    assert c.get("/tasks", follow_redirects=False).headers["location"] == "/tasks/"
    assert c.get("/projects/", headers=AMY_P).status_code == 200   # Projects still where it was


def test_assign_views_and_changes(c):
    t = new(c, assignee="Bob.Smith@nscale.com", due="2020-01-01", priority="High", description="ESP times out")
    assert t["ref"] == "PL-001" and t["assignee"] == "bob.smith@nscale.com" and t["assigneeName"] == "Bob Smith"
    assert c.post("/tasks/api/tasks", headers=AMY, json={"title": " "}).status_code == 422
    assert c.post("/tasks/api/tasks", headers=AMY, json={"title": "x", "assignee": "bob"}).status_code == 422
    assert c.post("/tasks/api/tasks", headers=AMY, json={"title": "x", "status": "Nope"}).status_code == 422
    assert [x["id"] for x in view(c, BOB)["tasks"]] == [t["id"]]
    assert view(c, AMY)["tasks"] == [] and [x["id"] for x in view(c, AMY, "asked")["tasks"]] == [t["id"]]
    assert c.get("/tasks/api/counts", headers=BOB).json() == {"mine": 1, "waiting": 0, "overdue": 1, "badge": 1}
    # Changes are recorded in the activity.
    r = c.patch(f"/tasks/api/tasks/{t['id']}", headers=BOB, json={"status": "In progress", "due": "2030-01-01"}).json()
    kinds = [(a["kind"], (a["data"] or {}).get("field")) for a in r["activity"]]
    assert ("change", "assignee") in kinds and ("change", "status") in kinds and ("change", "due") in kinds
    r = c.patch(f"/tasks/api/tasks/{t['id']}", headers=BOB, json={"status": "Done"}).json()
    assert r["progress"] == 100 and r["doneAt"]
    assert view(c, BOB)["tasks"] == [] and [x["id"] for x in view(c, BOB, "done")["tasks"]] == [t["id"]]
    # Only the creator (or an admin) deletes it.
    assert c.delete(f"/tasks/api/tasks/{t['id']}", headers=BOB).status_code == 403
    assert c.delete(f"/tasks/api/tasks/{t['id']}", headers=AMY).status_code == 200
    assert c.get(f"/tasks/api/tasks/{t['id']}", headers=AMY).status_code == 404


def test_questions_answers_updates_and_mentions(c):
    t = new(c, assignee="bob.smith@nscale.com")
    tid = t["id"]
    # Bob (the assignee) asks: it waits on Amy, who created it.
    r = c.post(f"/tasks/api/tasks/{tid}/activity", headers=BOB, json={"kind": "question", "body": "Keep the old profile?"}).json()
    q = [a for a in r["activity"] if a["kind"] == "question"][0]
    assert q["waitingOn"] == "amy.chen@nscale.com" and not q["answeredAt"]
    assert [x["id"] for x in view(c, AMY, "waiting")["tasks"]] == [tid]
    assert c.get("/tasks/api/counts", headers=AMY).json()["waiting"] == 1
    assert view(c, AMY, "asked")["tasks"][0]["openQuestions"] == 1
    # Amy answers; the question closes.
    r = c.post(f"/tasks/api/tasks/{tid}/activity", headers=AMY, json={"kind": "answer", "answers": q["id"], "body": "Move them all."}).json()
    assert [a for a in r["activity"] if a["id"] == q["id"]][0]["answeredBy"] == "Amy Chen"
    assert view(c, AMY, "waiting")["tasks"] == []
    assert c.post(f"/tasks/api/tasks/{tid}/activity", headers=AMY, json={"kind": "answer", "answers": "nope", "body": "x"}).status_code == 404
    # Deleting the answer reopens the question.
    ans = [a for a in r["activity"] if a["kind"] == "answer"][0]
    assert c.delete(f"/tasks/api/tasks/{tid}/activity/{ans['id']}", headers=BOB).status_code == 403   # not Bob's
    r = c.delete(f"/tasks/api/tasks/{tid}/activity/{ans['id']}", headers=AMY).json()
    assert not [a for a in r["activity"] if a["id"] == q["id"]][0]["answeredAt"]
    # Updates set progress.
    r = c.post(f"/tasks/api/tasks/{tid}/activity", headers=BOB, json={"kind": "update", "body": "2 of 3 enrolled", "progress": 60}).json()
    assert r["progress"] == 60 and r["status"] == "In progress"   # an update with progress starts the task
    assert c.post(f"/tasks/api/tasks/{tid}/activity", headers=BOB, json={"kind": "comment", "body": "  "}).status_code == 422
    assert c.post(f"/tasks/api/tasks/{tid}/activity", headers=BOB, json={"kind": "shout", "body": "x"}).status_code == 422
    # @mentions of known people.
    r = c.post(f"/tasks/api/tasks/{tid}/activity", headers=BOB, json={"kind": "comment", "body": "@amy.chen can you check?"}).json()
    assert r["activity"][-1]["mentions"] == ["amy.chen@nscale.com"]
    assert [x["id"] for x in view(c, AMY, "mentioned")["tasks"]] == [tid]
    assert view(c, AMY, "all")["tasks"][0]["comments"] == 3   # question, update, comment (the answer was deleted)


def test_project_tasks_follow_the_project(c):
    pid = c.post("/projects/api/projects", headers=AMY_P, json={"name": "Laptop refresh"}).json()["id"]
    # Only owners and editors can put tasks in a project.
    assert c.post("/tasks/api/tasks", headers=BOB, json={"title": "x", "projectId": pid}).status_code == 403
    assert [p["name"] for p in c.get("/tasks/api/projects", headers=AMY).json()["projects"]] == ["Laptop refresh"]
    t = new(c, projectId=pid)
    assert t["project"] == {"id": pid, "name": "Laptop refresh"}
    loose = new(c, title="Not in a project")
    # Bob isn't a member: he sees the loose task, not the project one.
    assert [x["id"] for x in view(c, BOB, "all")["tasks"]] == [loose["id"]]
    assert c.get(f"/tasks/api/tasks/{t['id']}", headers=BOB).status_code == 404
    # A viewer sees it and can comment, but not change it.
    c.post(f"/projects/api/projects/{pid}/members", headers=AMY_P, json={"email": "bob.smith@nscale.com", "role": "Viewer"})
    assert c.get(f"/tasks/api/tasks/{t['id']}", headers=BOB).json()["canChange"] is False
    assert c.patch(f"/tasks/api/tasks/{t['id']}", headers=BOB, json={"status": "Done"}).status_code == 403
    assert c.post(f"/tasks/api/tasks/{t['id']}/activity", headers=BOB, json={"body": "Looks good"}).status_code == 200
    # Assigning it to someone outside the project lets them see it.
    c.patch(f"/tasks/api/tasks/{t['id']}", headers=AMY, json={"assignee": "cat@nscale.com"})
    assert c.get(f"/tasks/api/tasks/{t['id']}", headers=CAT).json()["canChange"] is True
    # Filter by project; admins see everything.
    assert [x["id"] for x in view(c, AMY, "all", project=pid)["tasks"]] == [t["id"]]
    assert len(view(c, ADMIN, "all")["tasks"]) == 2
    # Moving it out of the project is recorded.
    r = c.patch(f"/tasks/api/tasks/{t['id']}", headers=AMY, json={"projectId": None}).json()
    assert r["project"] is None and r["activity"][-1]["data"]["field"] == "project"


def test_mentions_matching():
    people = [{"email": "amy.chen@nscale.com", "name": "Amy Chen"}, {"email": "bob@nscale.com", "name": "Bob"}]
    assert tasks.mentions_in("thanks @amy and @bob@nscale.com.", people) == ["amy.chen@nscale.com", "bob@nscale.com"]
    assert tasks.mentions_in("email me at x@y.com", people) == []


def test_picked_mentions(c):
    t = new(c, assignee="bob.smith@nscale.com")
    r = c.post(f"/tasks/api/tasks/{t['id']}/activity", headers=BOB, json={"body": "@zed.new can you look?", "mentions": ["zed.new@nscale.com"]}).json()
    assert r["activity"][-1]["mentions"] == ["zed.new@nscale.com"]
    assert tasks.picked_mentions("hi @amy.chen!", ["amy.chen@nscale.com", "bob@x.com"]) == ["amy.chen@nscale.com"]


def inbox_titles(c, email):
    h = {**H, "x-cp-user": access.issue(email, access.display_name(email), "inbox", False, secret=SECRET)}
    return [n["title"] for n in c.get("/inbox/api/notifications", headers=h).json()["notifications"]]


def test_jobs_schedule_and_calendar(c):
    t = new(c, title="Office move")
    J = f"/tasks/api/tasks/{t['id']}/jobs"
    assert c.post(J, headers=AMY, json={"title": " "}).status_code == 422
    assert c.post(J, headers=AMY, json={"title": "x", "start": "2026-02-30"}).status_code == 422
    assert c.post(J, headers=AMY, json={"title": "x", "duration": 0}).status_code == 422
    r = c.post(J, headers=AMY, json={"title": "Survey site", "description": "Measure rooms", "note": "Keys at reception",
                                     "start": "2026-11-30", "duration": 3, "assignee": "bob.smith@nscale.com"}).json()
    j1 = r["jobs"][0]
    assert (j1["start"], j1["end"], j1["duration"], j1["note"], j1["status"]) == ("2026-11-30", "2026-12-02", 3, "Keys at reception", "To do")
    assert r["activity"][-1]["data"] == {"field": "job", "action": "added", "to": "Survey site"}
    assert inbox_titles(c, "bob.smith@nscale.com")[0] == "Amy Chen gave you a job on PL-001 Office move: Survey site"
    r = c.post(J, headers=AMY, json={"title": "Pack"}).json()   # not scheduled yet
    j2 = r["jobs"][1]
    assert j2["start"] == "" and j2["end"] == "" and j2["duration"] == 1
    # A job assigned to you puts the plan in "Mine" and on your badge.
    assert [x["id"] for x in view(c, BOB)["tasks"]] == [t["id"]]
    row = view(c, AMY, "all")["tasks"][0]
    assert row["jobs"] == {"total": 2, "done": 0, "start": "2026-11-30", "end": "2026-12-02", "mine": 0}
    assert c.get("/tasks/api/counts", headers=BOB).json()["mine"] == 1
    # Reschedule, finish, reorder, delete.
    r = c.patch(f"{J}/{j2['id']}", headers=AMY, json={"start": "2026-12-03", "duration": 2}).json()
    assert r["activity"][-1]["data"]["action"] == "scheduled"
    r = c.patch(f"{J}/{j1['id']}", headers=BOB, json={"status": "Done"}).json()
    assert r["activity"][-1]["data"]["action"] == "done"
    r = c.post(f"{J}/order", headers=AMY, json={"ids": [j2["id"], j1["id"]]}).json()
    assert [j["title"] for j in r["jobs"]] == ["Pack", "Survey site"]
    # The calendar: jobs overlapping the range, plans due in it.
    c.patch(f"/tasks/api/tasks/{t['id']}", headers=AMY, json={"due": "2026-12-10"})
    cal = c.get("/tasks/api/calendar", headers=AMY, params={"start": "2026-12-01", "end": "2026-12-31"}).json()
    assert [j["title"] for j in cal["jobs"]] == ["Survey site", "Pack"] and cal["jobs"][0]["pipeline"]["ref"] == "PL-001"
    assert [d["due"] for d in cal["due"]] == ["2026-12-10"] and cal["unscheduled"] == 0
    assert c.get("/tasks/api/calendar", headers=AMY, params={"start": "2026-12-03", "end": "2026-12-31", "mine": True}).json()["jobs"] == []
    assert [j["title"] for j in c.get("/tasks/api/calendar", headers=BOB, params={"start": "2026-12-03", "end": "2026-12-31", "mine": True}).json()["jobs"]] == []
    assert c.get("/tasks/api/calendar", headers=AMY, params={"start": "2026-12-31", "end": "2026-12-01"}).status_code == 422
    r = c.delete(f"{J}/{j2['id']}", headers=AMY).json()
    assert [j["title"] for j in r["jobs"]] == ["Survey site"] and r["activity"][-1]["data"]["action"] == "removed"
    # Deleting the plan takes its jobs with it.
    c.delete(f"/tasks/api/tasks/{t['id']}", headers=AMY)
    assert c.get("/tasks/api/calendar", headers=ADMIN, params={"start": "2026-11-01", "end": "2026-12-31"}).json()["jobs"] == []


def test_jobs_in_a_project(c):
    pid = c.post("/projects/api/projects", headers=AMY_P, json={"name": "Move"}).json()["id"]
    t = new(c, projectId=pid)
    J = f"/tasks/api/tasks/{t['id']}/jobs"
    assert c.post(J, headers=BOB, json={"title": "x"}).status_code == 404   # can't see it
    r = c.post(J, headers=AMY, json={"title": "Cabling", "assignee": "cat@nscale.com", "start": "2026-12-01"}).json()
    # A job's assignee can see the plan and change it; outsiders still can't.
    assert c.get(f"/tasks/api/tasks/{t['id']}", headers=CAT).status_code == 200
    assert c.patch(f"{J}/{r['jobs'][0]['id']}", headers=CAT, json={"status": "In progress"}).status_code == 200
    c.post(f"/projects/api/projects/{pid}/members", headers=AMY_P, json={"email": "bob.smith@nscale.com", "role": "Viewer"})
    assert c.post(J, headers=BOB, json={"title": "x"}).status_code == 403


def test_timeline(c):
    a = new(c, title="Discovery", due="2026-12-01")
    b = new(c, title="Build")
    c.post(f"/tasks/api/tasks/{b['id']}/jobs", headers=AMY, json={"title": "Code", "start": "2026-11-20", "duration": 4})
    c.post(f"/tasks/api/tasks/{a['id']}/jobs", headers=AMY, json={"title": "Interviews", "start": "2026-11-25", "duration": 2, "assignee": "bob.smith@nscale.com"})
    c.post(f"/tasks/api/tasks/{a['id']}/jobs", headers=AMY, json={"title": "Write up"})
    done = new(c, title="Old")
    c.patch(f"/tasks/api/tasks/{done['id']}", headers=AMY, json={"status": "Done"})
    plans = c.get("/tasks/api/timeline", headers=AMY).json()["plans"]
    assert [p["title"] for p in plans] == ["Build", "Discovery"]   # by first start; done plans left out
    d = plans[1]
    assert (d["start"], d["end"], d["due"], d["canChange"]) == ("2026-11-25", "2026-11-26", "2026-12-01", True)
    assert [j["title"] for j in d["jobs"]] == ["Interviews", "Write up"]
    mine = c.get("/tasks/api/timeline", headers=BOB, params={"mine": True}).json()["plans"]
    assert [(p["title"], [j["title"] for j in p["jobs"]]) for p in mine] == [("Discovery", ["Interviews"])]
