"""Personal notifications: who is told what, read and unread, and 1-2-1 actions."""
import pytest
from fastapi.testclient import TestClient

import tests.test_projects as base  # noqa: F401  (sets the test environment first)
from app import access, inbox, main, store as store_mod, tasks, team

SECRET = base.SECRET
H = {"X-Requested-With": "nscale-admin"}


def who(email, tool, admin=False):
    return {**H, "x-cp-user": access.issue(email, access.display_name(email), tool, admin, secret=SECRET)}


JAMES, AMY, BOB = "james.brown@nscale.com", "amy.chen@nscale.com", "bob.smith@nscale.com"
T = {e: who(e, "tasks") for e in (JAMES, AMY, BOB)}
I = {e: who(e, "inbox") for e in (JAMES, AMY, BOB)}
TEAM_ADMIN = who("boss@nscale.com", "team", admin=True)


@pytest.fixture
def c(tmp_path, monkeypatch):
    s = store_mod.Store(str(tmp_path / "t.db"))
    monkeypatch.setattr(main, "store", s)
    for m in (tasks, team, inbox):
        m.setup(s, main.STATIC)
    with TestClient(main.app) as client:
        yield client


def inbox_of(c, email):
    return c.get("/inbox/api/notifications", headers=I[email]).json()


def titles(c, email):
    return [n["title"] for n in inbox_of(c, email)["notifications"]]


def test_sign_in_is_checked(c):
    assert c.get("/inbox/api/count").status_code == 401
    assert c.get("/inbox/api/count", headers=T[AMY]).status_code == 401   # a Tasks token
    assert c.get("/inbox/api/count", headers=I[AMY]).json() == {"unread": 0, "actions": 0}
    assert c.get("/inbox/").status_code == 403 and c.get("/inbox/", headers=I[AMY]).status_code == 200


def test_task_notifications(c):
    t = c.post("/tasks/api/tasks", headers=T[JAMES], json={"title": "Rebuild profile", "assignee": AMY, "due": "2026-12-01"}).json()
    assert titles(c, AMY) == ["James Brown assigned you TSK-001 Rebuild profile"]
    assert inbox_of(c, AMY)["notifications"][0]["body"].startswith("Due 1 Dec")
    assert titles(c, JAMES) == []   # never told about your own doing
    tid = t["id"]
    # Amy asks; it waits on James (the creator). Her update tells James, not her.
    c.post(f"/tasks/api/tasks/{tid}/activity", headers=T[AMY], json={"kind": "question", "body": "Old profile too?"})
    assert titles(c, JAMES)[0] == "Amy Chen asked you a question on TSK-001 Rebuild profile"
    c.post(f"/tasks/api/tasks/{tid}/activity", headers=T[AMY], json={"kind": "update", "body": "Half done", "progress": 50})
    assert titles(c, JAMES)[0] == "Amy Chen posted an update (50%) on TSK-001 Rebuild profile"
    q = [a for a in c.get(f"/tasks/api/tasks/{tid}", headers=T[JAMES]).json()["activity"] if a["kind"] == "question"][0]
    c.post(f"/tasks/api/tasks/{tid}/activity", headers=T[JAMES], json={"kind": "answer", "answers": q["id"], "body": "Move all."})
    assert titles(c, AMY)[0] == "James Brown answered your question on TSK-001 Rebuild profile"
    # A mention tells the person once, as a mention.
    c.post(f"/tasks/api/tasks/{tid}/activity", headers=T[JAMES], json={"body": "@bob.smith can you help @amy.chen?"})
    assert titles(c, BOB) == []   # Bob isn't known to Tasks yet, so @bob.smith matches nobody
    c.post("/tasks/api/tasks", headers=T[BOB], json={"title": "Bob's own"})
    c.post(f"/tasks/api/tasks/{tid}/activity", headers=T[JAMES], json={"body": "@bob.smith over to you"})
    assert titles(c, BOB) == ["James Brown mentioned you on TSK-001 Rebuild profile"]
    assert titles(c, AMY)[0] == "James Brown commented on TSK-001 Rebuild profile"
    # Reassigning tells the new assignee; done tells the creator.
    c.patch(f"/tasks/api/tasks/{tid}", headers=T[JAMES], json={"assignee": BOB})
    assert titles(c, BOB)[0] == "James Brown assigned you TSK-001 Rebuild profile"
    c.patch(f"/tasks/api/tasks/{tid}", headers=T[BOB], json={"status": "Done"})
    assert titles(c, JAMES)[0] == "Bob Smith marked done: TSK-001 Rebuild profile"


def test_read_unread_and_dismiss(c):
    c.post("/tasks/api/tasks", headers=T[JAMES], json={"title": "One", "assignee": AMY})
    c.post("/tasks/api/tasks", headers=T[JAMES], json={"title": "Two", "assignee": AMY})
    box = inbox_of(c, AMY)
    assert box["unread"] == 2 and c.get("/inbox/api/count", headers=I[AMY]).json()["unread"] == 2
    first = box["notifications"][0]["id"]
    assert c.post(f"/inbox/api/notifications/{first}/read", headers=I[AMY], json={"read": True}).json()["unread"] == 1
    assert len(c.get("/inbox/api/notifications?unread=true", headers=I[AMY]).json()["notifications"]) == 1
    assert c.post(f"/inbox/api/notifications/{first}/read", headers=I[BOB], json={}).status_code == 404   # not Bob's
    assert c.post("/inbox/api/read-all", headers=I[AMY]).json()["unread"] == 0
    assert c.delete(f"/inbox/api/notifications/{first}", headers=I[AMY]).json()["unread"] == 0
    assert len(inbox_of(c, AMY)["notifications"]) == 1


def test_one_to_ones_feedback_and_actions(c):
    tm = lambda e: who(e, "team")  # noqa: E731
    c.post("/team/api/members", headers=TEAM_ADMIN, json={"email": JAMES, "name": "James Brown"})
    c.post("/team/api/members", headers=tm(JAMES), json={"email": AMY, "name": "Amy Chen"})
    c.post("/team/api/members", headers=TEAM_ADMIN, json={"email": BOB, "name": "Bob Smith"})
    r = c.post(f"/team/api/members/{AMY}/entries", headers=tm(JAMES), json={"kind": "one_to_one", "date": "2026-11-18", "body": "Pilot going well",
               "data": {"actions": [{"text": "Book MD-102 training", "owner": "Amy Chen", "due": "2026-12-15"},
                                    {"text": "Ask Bob about Jamf", "owner": BOB}, {"text": "Approve budget", "owner": "James Brown"}]}}).json()
    assert set(titles(c, AMY)) == {"New action from your 1-2-1: Book MD-102 training", "James Brown recorded your 1-2-1 on 18 Nov"}
    assert titles(c, BOB) == ["New action from Amy Chen's 1-2-1: Ask Bob about Jamf"]
    assert titles(c, JAMES) == []   # his own action, his own doing
    c.post(f"/team/api/members/{AMY}/entries", headers=tm(JAMES), json={"kind": "well", "body": "Great kickoff"})
    assert titles(c, AMY)[0] == "James Brown added feedback: what's going well"
    c.post(f"/team/api/members/{AMY}/entries", headers=tm(JAMES), json={"kind": "manager_note", "body": "Secret"})
    assert "Secret" not in str(inbox_of(c, AMY)) and len(titles(c, AMY)) == 3   # manager notes never notify
    # Open actions show in each owner's inbox until ticked off.
    acts = inbox_of(c, AMY)["actions"]
    assert [a["text"] for a in acts] == ["Book MD-102 training"] and acts[0]["own"]
    assert [a["text"] for a in inbox_of(c, BOB)["actions"]] == ["Ask Bob about Jamf"]
    assert c.get("/inbox/api/count", headers=I[BOB]).json()["actions"] == 1
    one = r["entries"]["one_to_one"][0]
    bob_action = [a for a in one["data"]["actions"] if a["owner"] == BOB][0]
    # Only the owner ticks it off; James hears about it.
    assert c.post(f"/inbox/api/actions/{one['id']}/{bob_action['id']}", headers=I[AMY], json={"done": True}).status_code == 404
    assert c.post(f"/inbox/api/actions/{one['id']}/{bob_action['id']}", headers=I[BOB], json={"done": True}).json()["actions"] == []
    assert titles(c, JAMES)[0] == "Bob Smith ticked off a 1-2-1 action"
    assert c.post(f"/inbox/api/actions/{one['id']}/{bob_action['id']}", headers=I[BOB], json={"done": False}).json()["actions"][0]["text"] == "Ask Bob about Jamf"
    # A new action added later tells only its owner.
    data = {**one["data"], "actions": one["data"]["actions"] + [{"text": "Write it up", "owner": BOB}]}
    c.put(f"/team/api/members/{AMY}/entries/{one['id']}", headers=tm(JAMES), json={"data": data})
    assert titles(c, BOB)[0] == "New action from Amy Chen's 1-2-1: Write it up"


def test_project_membership(c):
    P = lambda e: who(e, "projects")  # noqa: E731
    pid = c.post("/projects/api/projects", headers=P(JAMES), json={"name": "Laptop refresh"}).json()["id"]
    inbox.setup(main.store, main.STATIC)
    c.post(f"/projects/api/projects/{pid}/members", headers=P(JAMES), json={"email": AMY, "role": "Editor"})
    assert titles(c, AMY) == ["James Brown added you to Laptop refresh"]
    c.patch(f"/projects/api/projects/{pid}/members/{AMY}", headers=P(JAMES), json={"role": "Viewer"})
    assert titles(c, AMY)[0] == "James Brown changed your role on Laptop refresh"
