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
    with_done = c.get("/tasks/api/timeline", headers=AMY, params={"done": True}).json()["plans"]
    assert "Old" in [p["title"] for p in with_done]   # All Tasks asks for finished plans too
    d = plans[1]
    assert (d["start"], d["end"], d["due"], d["canChange"]) == ("2026-11-25", "2026-11-26", "2026-12-01", True)
    assert [j["title"] for j in d["jobs"]] == ["Interviews", "Write up"]
    mine = c.get("/tasks/api/timeline", headers=BOB, params={"mine": True}).json()["plans"]
    assert [(p["title"], [j["title"] for j in p["jobs"]]) for p in mine] == [("Discovery", ["Interviews"])]


def test_tag_people_in_job_notes(c):
    t = new(c, title="Rollout")
    J = f"/tasks/api/tasks/{t['id']}/jobs"
    r = c.post(J, headers=AMY, json={"title": "Inventory", "note": "@val.jones has the list", "mentions": ["val.jones@nscale.com"]}).json()
    jid = r["jobs"][0]["id"]
    assert inbox_titles(c, "val.jones@nscale.com") == ["Amy Chen mentioned you on PL-001 Rollout: Inventory"]
    # Saving again doesn't tell her twice; a newly tagged person is told.
    c.patch(f"{J}/{jid}", headers=AMY, json={"note": "@val.jones has the list, @bob.smith checks it",
                                              "mentions": ["val.jones@nscale.com", "bob.smith@nscale.com"]})
    assert len(inbox_titles(c, "val.jones@nscale.com")) == 1
    assert inbox_titles(c, "bob.smith@nscale.com") == ["Amy Chen mentioned you on PL-001 Rollout: Inventory"]


def test_job_progress(c, tmp_path):
    t = new(c, title="Office move")
    J = f"/tasks/api/tasks/{t['id']}/jobs"
    j = c.post(J, headers=AMY, json={"title": "Survey site", "status": "In progress", "progress": 40}).json()["jobs"][0]
    assert (j["status"], j["progress"]) == ("In progress", 40)
    assert c.patch(f"{J}/{j['id']}", headers=AMY, json={"progress": 101}).status_code == 422
    assert c.patch(f"{J}/{j['id']}", headers=AMY, json={"progress": 75}).json()["jobs"][0]["progress"] == 75
    assert c.patch(f"{J}/{j['id']}", headers=AMY, json={"status": "Done"}).json()["jobs"][0]["progress"] == 100
    # A database from before progress was kept gets the column when the app starts.
    import sqlite3
    from app import store as store_mod
    old = tmp_path / "old.db"
    with sqlite3.connect(old) as db:
        db.execute("CREATE TABLE pipeline_jobs (id TEXT PRIMARY KEY, task_id TEXT, position INTEGER, title TEXT, description TEXT, note TEXT, "
                   "start TEXT, duration INTEGER, assignee TEXT, assignee_name TEXT, status TEXT, created_by TEXT, created_at TEXT, updated_at TEXT)")
    store_mod.Store(str(old))
    with sqlite3.connect(old) as db:
        assert "progress" in {r[1] for r in db.execute("PRAGMA table_info(pipeline_jobs)")}


def test_several_people_on_a_job(c, tmp_path):
    t = new(c, title="Office move")
    J = f"/tasks/api/tasks/{t['id']}/jobs"
    r = c.post(J, headers=AMY, json={"title": "Move desks", "start": "2026-11-30", "duration": 2,
                                     "people": [{"email": "Bob.Smith@nscale.com", "name": "Bob Smith"}, {"email": "cat@nscale.com"},
                                                {"email": "bob.smith@nscale.com"}]}).json()
    j = r["jobs"][0]
    assert [p["email"] for p in j["people"]] == ["bob.smith@nscale.com", "cat@nscale.com"]   # duplicates dropped
    assert (j["assignee"], j["assigneeName"]) == ("bob.smith@nscale.com", "Bob Smith")         # the first, for older pages
    assert inbox_titles(c, "bob.smith@nscale.com")[0] == inbox_titles(c, "cat@nscale.com")[0] == \
        "Amy Chen gave you a job on PL-001 Office move: Move desks"
    # Each of them sees the plan, can change it, and has the job under Mine and on the timeline.
    for h in (BOB, CAT):
        assert c.get(f"/tasks/api/tasks/{t['id']}", headers=h).status_code == 200
        mine = c.get("/tasks/api/timeline?mine=true", headers=h).json()["plans"]
        assert [jj["title"] for p in mine for jj in p["jobs"]] == ["Move desks"]
    # Adding someone tells only them; taking someone off is fine; a bad email is refused.
    c.patch(f"{J}/{j['id']}", headers=AMY, json={"people": [{"email": "cat@nscale.com"}, {"email": "dan@nscale.com"}]})
    assert inbox_titles(c, "dan@nscale.com") == ["Amy Chen gave you a job on PL-001 Office move: Move desks"]
    assert len(inbox_titles(c, "cat@nscale.com")) == 1
    assert c.get("/tasks/api/timeline?mine=true", headers=BOB).json()["plans"] == []
    assert c.patch(f"{J}/{j['id']}", headers=AMY, json={"people": [{"email": "not an email"}]}).status_code == 422
    # Older pages still send one assignee.
    j = c.patch(f"{J}/{j['id']}", headers=AMY, json={"assignee": "bob.smith@nscale.com"}).json()["jobs"][0]
    assert [p["email"] for p in j["people"]] == ["bob.smith@nscale.com"]
    j = c.patch(f"{J}/{j['id']}", headers=AMY, json={"people": []}).json()["jobs"][0]
    assert (j["people"], j["assignee"]) == ([], "")

    # A database from before: each job's one assignee becomes its list of people.
    import sqlite3
    from app import store as store_mod
    old = tmp_path / "old.db"
    with sqlite3.connect(old) as db:
        db.execute("CREATE TABLE pipeline_jobs (id TEXT PRIMARY KEY, task_id TEXT, position INTEGER, title TEXT, description TEXT, note TEXT, "
                   "start TEXT, duration INTEGER, assignee TEXT, assignee_name TEXT, status TEXT, created_by TEXT, created_at TEXT, updated_at TEXT)")
        db.execute("INSERT INTO pipeline_jobs (id, task_id, title, assignee, assignee_name, status) VALUES ('j1', 't1', 'x', 'val@nscale.com', 'Val', 'To do')")
        db.execute("INSERT INTO pipeline_jobs (id, task_id, title, assignee, assignee_name, status) VALUES ('j2', 't1', 'y', '', '', 'To do')")
    store_mod.Store(str(old))
    with sqlite3.connect(old) as db:
        assert dict(db.execute("SELECT id, people FROM pipeline_jobs").fetchall()) == \
            {"j1": '[{"email":"val@nscale.com","name":"Val"}]', "j2": "[]"}


def test_weekends_dont_count(c):
    from app.tasks import end_of
    assert end_of("2026-11-26", 5) == "2026-12-02"      # Thu + 5 working days ends the Wed after
    assert end_of("2026-11-23", 5) == "2026-11-27"      # Mon–Fri
    assert end_of("2026-11-27", 1) == "2026-11-27"
    assert end_of("2026-11-23", 10) == "2026-12-04"
    assert end_of("2026-11-28", 2) == "2026-12-01"      # a Saturday start counts from Monday
    t = new(c, title="Rollout")
    j = c.post(f"/tasks/api/tasks/{t['id']}/jobs", headers=AMY, json={"title": "Inventory", "start": "2026-11-29", "duration": 3}).json()["jobs"][0]
    assert (j["start"], j["end"]) == ("2026-11-30", "2026-12-02")   # Sunday moves to Monday


def test_sub_jobs(c, tmp_path):
    """A job's checklist: add, tick, give to someone, rename, reorder, remove. Its % follows the ticks."""
    t = new(c, title="Windows App Standardisation")
    J = f"/tasks/api/tasks/{t['id']}/jobs"
    j = c.post(J, headers=AMY, json={"title": "App Standardisation"}).json()["jobs"][0]
    S = f"{J}/{j['id']}/subs"
    assert c.post(S, headers=AMY, json={"title": "  "}).status_code == 422
    assert c.post(S, headers=AMY, json={"title": "Device inventory", "email": "not-an-email"}).status_code == 422
    c.post(S, headers=AMY, json={"title": "Device inventory", "email": "Bob.Smith@nscale.com"})
    c.post(S, headers=AMY, json={"title": "Engage security"})
    c.post(S, headers=AMY, json={"title": "User engagement"})
    job = c.post(S, headers=AMY, json={"title": "Sign-off"}).json()["jobs"][0]
    subs = job["subs"]
    assert [s["title"] for s in subs] == ["Device inventory", "Engage security", "User engagement", "Sign-off"]
    assert subs[0]["email"] == "bob.smith@nscale.com" and subs[0]["name"] and not subs[0]["done"]
    assert (job["subsDone"], job["progress"], job["status"]) == (0, 0, "To do")
    assert "Amy Chen gave you part of a job on" in " ".join(inbox_titles(c, "bob.smith@nscale.com"))
    # Ticking the first one starts the job; the % follows the ticks.
    job = c.patch(f"{S}/{subs[0]['id']}", headers=AMY, json={"done": True}).json()["jobs"][0]
    assert (job["subsDone"], job["progress"], job["status"]) == (1, 25, "In progress")
    job = c.patch(f"{S}/{subs[1]['id']}", headers=AMY, json={"done": True, "title": "Engage Security", "email": "cat@nscale.com"}).json()["jobs"][0]
    assert job["progress"] == 50 and job["subs"][1]["title"] == "Engage Security" and job["subs"][1]["email"] == "cat@nscale.com"
    job = c.patch(f"{S}/{subs[0]['id']}", headers=AMY, json={"email": ""}).json()["jobs"][0]
    assert job["subs"][0]["email"] == "" and job["subs"][0]["name"] == ""
    # Reorder, remove; a finished job is 100% whatever's ticked.
    job = c.post(f"{S}/order", headers=AMY, json={"ids": [subs[3]["id"], subs[0]["id"]]}).json()["jobs"][0]
    assert [s["title"] for s in job["subs"]] == ["Sign-off", "Device inventory", "Engage Security", "User engagement"]
    job = c.delete(f"{S}/{subs[3]['id']}", headers=AMY).json()["jobs"][0]
    assert len(job["subs"]) == 3 and job["progress"] == 67
    assert c.delete(f"{S}/{subs[3]['id']}", headers=AMY).status_code == 404
    assert c.patch(f"{J}/{j['id']}", headers=AMY, json={"status": "Done"}).json()["jobs"][0]["progress"] == 100


def test_recurring_jobs(c):
    """A job can repeat: weekly, every 2 weeks, monthly, quarterly or yearly, each repeat as long as the first."""
    from datetime import date, timedelta
    t = new(c, title="Patching")
    J = f"/tasks/api/tasks/{t['id']}/jobs"
    nxt = date.today() + timedelta(days=(7 - date.today().weekday()))   # next Monday
    j = c.post(J, headers=AMY, json={"title": "Patch Tuesday checks", "start": nxt.isoformat(), "duration": 2,
                                     "recur": "weekly", "people": [{"email": "bob.smith@nscale.com"}]}).json()["jobs"][0]
    assert j["recur"] == "weekly" and j["repeats"][0] == {"start": (nxt + timedelta(days=7)).isoformat(),
                                                           "end": (nxt + timedelta(days=8)).isoformat()}
    assert len(j["repeats"]) > 100   # two years ahead
    assert c.patch(f"{J}/{j['id']}", headers=AMY, json={"recur": "daily"}).status_code == 422
    until = (nxt + timedelta(days=30)).isoformat()
    j = c.patch(f"{J}/{j['id']}", headers=AMY, json={"recur": "biweekly", "recurUntil": until}).json()["jobs"][0]
    assert [r["start"] for r in j["repeats"]] == [(nxt + timedelta(days=14)).isoformat(), (nxt + timedelta(days=28)).isoformat()]
    # The timeline gets them too; done, it stops repeating; off, no repeats.
    plan = next(p for p in c.get("/tasks/api/timeline", headers=AMY).json()["plans"] if p["id"] == t["id"])
    assert len(plan["jobs"][0]["repeats"]) == 2
    assert c.patch(f"{J}/{j['id']}", headers=AMY, json={"status": "Done"}).json()["jobs"][0]["repeats"] == []
    j = c.patch(f"{J}/{j['id']}", headers=AMY, json={"status": "To do", "recur": ""}).json()["jobs"][0]
    assert j["recur"] == "" and j["repeats"] == []


def test_monthly_repeats_keep_the_day():
    from app.tasks import repeats
    # The 31st: Feb's last day, then the 31st again; weekends move to the Monday.
    r = [x["start"] for x in repeats("2027-01-29", 1, "monthly", "2027-06-30")]
    assert r == ["2027-03-01", "2027-03-29", "2027-04-29", "2027-05-31", "2027-06-29"]   # 28 Feb 2027 is a Sunday
    assert [x["start"] for x in repeats("2027-01-15", 1, "quarterly", "2028-01-31")] == ["2027-04-15", "2027-07-15", "2027-10-15", "2028-01-17"]
    assert repeats("2027-01-15", 1, "yearly", "2027-12-31") == [] and repeats("", 1, "weekly") == [] and repeats("2027-01-15", 1, "nope") == []


def test_leave(c):
    """Leave: someone's days away, for the timeline; not a job."""
    L = "/tasks/api/leave"
    assert c.post(L, headers=AMY, json={"email": "bob.smith@nscale.com", "start": "2027-01-10", "end": "2027-01-08"}).status_code == 422
    assert c.post(L, headers=AMY, json={"email": "nope", "start": "2027-01-04", "end": "2027-01-08"}).status_code == 422
    assert c.post(L, headers=AMY, json={"start": "2027-01-04", "end": "2027-01-08"}).status_code == 422
    lv = c.post(L, headers=AMY, json={"email": "Bob.Smith@nscale.com", "start": "2027-01-04", "end": "2027-01-08", "note": " Skiing  "}).json()
    assert (lv["email"], lv["name"], lv["note"]) == ("bob.smith@nscale.com", "Bob Smith", "Skiing")
    tl = c.get("/tasks/api/timeline", headers=AMY).json()
    assert [x["id"] for x in tl["leave"]] == [lv["id"]] and all(not p["jobs"] or True for p in tl["plans"])
    lv = c.patch(f"{L}/{lv['id']}", headers=AMY, json={"end": "2027-01-12"}).json()
    assert lv["end"] == "2027-01-12" and lv["start"] == "2027-01-04"
    assert c.patch(f"{L}/{lv['id']}", headers=AMY, json={"end": "2026-12-01"}).status_code == 422
    assert c.delete(f"{L}/{lv['id']}", headers=AMY).status_code == 200
    assert c.delete(f"{L}/{lv['id']}", headers=AMY).status_code == 404
    assert c.get("/tasks/api/timeline", headers=AMY).json()["leave"] == []


def test_move_job_to_another_plan(c):
    a, b = new(c, title="Mac Devices"), new(c, title="Mac / Jamf Remediation")
    ja = f"/tasks/api/tasks/{a['id']}/jobs"
    c.post(ja, headers=AMY, json={"title": "Keep me"})
    j = c.post(ja, headers=AMY, json={"title": "Mac App Standardisation", "start": "2027-02-01", "duration": 3,
                                      "people": [{"email": "bob.smith@nscale.com"}]}).json()["jobs"][1]
    c.post(f"{ja}/{j['id']}/subs", headers=AMY, json={"title": "Inventory"})
    assert c.post(f"{ja}/{j['id']}/move", headers=AMY, json={"plan": "nope"}).status_code == 404
    moved = c.post(f"{ja}/{j['id']}/move", headers=AMY, json={"plan": b["id"]}).json()
    assert moved["id"] == b["id"]
    mj = next(x for x in moved["jobs"] if x["id"] == j["id"])
    assert (mj["start"], mj["duration"], mj["people"][0]["email"], len(mj["subs"])) == ("2027-02-01", 3, "bob.smith@nscale.com", 1)
    left = c.get(f"/tasks/api/tasks/{a['id']}", headers=AMY).json()
    assert [x["title"] for x in left["jobs"]] == ["Keep me"]
    assert any((x.get("data") or {}).get("action") == "moved_out" for x in left["activity"])
    assert c.post(f"/tasks/api/tasks/{b['id']}/jobs/{j['id']}/move", headers=AMY, json={"plan": b["id"]}).json()["id"] == b["id"]   # same plan: nothing


def test_job_priority(c):
    """A job's priority is its place in the plan: 1, 2, 3 … Reordering renumbers them."""
    t = new(c, title="Windows Devices")
    J = f"/tasks/api/tasks/{t['id']}/jobs"
    for title in ("Windows App Standardisation", "Removal of Un-Approved Applications", "Create and Build Windows apps"):
        jobs = c.post(J, headers=AMY, json={"title": title}).json()["jobs"]
    assert [(x["priority"], x["title"][:6]) for x in jobs] == [(1, "Window"), (2, "Remova"), (3, "Create")]
    ids = [x["id"] for x in jobs]
    jobs = c.post(f"{J}/order", headers=AMY, json={"ids": [ids[2], ids[0], ids[1]]}).json()["jobs"]
    assert [(x["priority"], x["title"][:6]) for x in jobs] == [(1, "Create"), (2, "Window"), (3, "Remova")]
    plan = next(p for p in c.get("/tasks/api/timeline", headers=AMY).json()["plans"] if p["id"] == t["id"])
    assert [x["priority"] for x in plan["jobs"]] == [1, 2, 3]


def test_half_days():
    from app.tasks import end_of
    assert end_of("2027-01-04", 0.5) == "2027-01-04"   # Monday: half a day ends that day
    assert end_of("2027-01-04", 1.5) == "2027-01-05"
    assert end_of("2027-01-08", 1.5) == "2027-01-11"   # Friday + half of Monday


def test_half_day_jobs(c):
    t = new(c, title="Half days")
    J = f"/tasks/api/tasks/{t['id']}/jobs"
    j = c.post(J, headers=AMY, json={"title": "Quick fix", "start": "2027-01-04", "duration": 0.5}).json()["jobs"][0]
    assert (j["duration"], j["end"]) == (0.5, "2027-01-04")
    assert c.patch(f"{J}/{j['id']}", headers=AMY, json={"duration": 0.25}).status_code == 422
    assert c.patch(f"{J}/{j['id']}", headers=AMY, json={"duration": 0.75}).status_code == 422
    j = c.patch(f"{J}/{j['id']}", headers=AMY, json={"duration": 2}).json()["jobs"][0]
    assert j["duration"] == 2 and isinstance(j["duration"], int)


def test_reusable_jobs(c):
    """A job marked Reusable is offered (to people who can see its plan) as a starting point for new jobs."""
    t = new(c, title="Business as usual")
    J = f"/tasks/api/tasks/{t['id']}/jobs"
    j = c.post(J, headers=AMY, json={"title": "Client Platform Tickets", "duration": 1, "description": "The Jira queue",
                                     "reusable": True}).json()["jobs"][0]
    assert j["reusable"] is True
    c.post(J, headers=AMY, json={"title": "One-off"})
    items = c.get("/tasks/api/reusable-jobs", headers=AMY).json()["items"]
    assert [(i["title"], i["description"], i["planId"], i["planRef"]) for i in items] == \
        [("Client Platform Tickets", "The Jira queue", t["id"], t["ref"])]
    # A copy is an ordinary job: not reusable itself unless ticked.
    copy = c.post(J, headers=AMY, json={"title": items[0]["title"], "description": items[0]["description"], "duration": 1}).json()["jobs"]
    assert [x["reusable"] for x in copy] == [True, False, False]
    assert c.patch(f"{J}/{j['id']}", headers=AMY, json={"reusable": False}).json()["jobs"][0]["reusable"] is False
    assert c.get("/tasks/api/reusable-jobs", headers=AMY).json()["items"] == []
