"""Change management: settings and flows, roles, the workflow, approvals, comments and notifications."""
import pytest
from fastapi.testclient import TestClient

import tests.test_projects as base  # noqa: F401  (sets the test environment first)
from app import access, changes, inbox, main, store as store_mod, tasks, team

SECRET = base.SECRET
H = {"X-Requested-With": "nscale-admin"}


def who(email, tool="changes", admin=False):
    return {**H, "x-cp-user": access.issue(email, access.display_name(email), tool, admin, secret=SECRET)}


ADMIN, AMY, BOB, CAT, DAN = (who("boss@nscale.com", admin=True), who("amy.chen@nscale.com"), who("bob.smith@nscale.com"),
                             who("cat.jones@nscale.com"), who("dan.ray@nscale.com"))
API = "/changes/api"
FORM = {"category": "Intune policy", "risk": "Medium", "start": "2026-11-25T18:00", "end": "2026-11-25T20:00",
        "fields": {"summary": "Turn on BitLocker silently", "reason": "Audit finding", "plan": "Assign to pilot, then all",
                   "rollback": "Unassign the policy"}}


@pytest.fixture
def c(tmp_path, monkeypatch):
    s = store_mod.Store(str(tmp_path / "t.db"))
    monkeypatch.setattr(main, "store", s)
    for m in (tasks, team, inbox, changes):
        m.setup(s, main.STATIC)
    with TestClient(main.app) as client:
        r = client.put(f"{API}/settings", headers=ADMIN, json={"roles": {"requestors": "everyone",
                       "reviewers": ["bob.smith@nscale.com", "cat.jones@nscale.com", "amy.chen@nscale.com"]}})
        assert r.status_code == 200, r.text
        yield client


def raise_change(c, headers=AMY, flow="normal", **kw):
    r = c.post(f"{API}/changes", headers=headers, json={"title": "BitLocker policy", "flowId": flow, **FORM, **kw})
    assert r.status_code == 200, r.text
    return r.json()


def act(c, cid, headers, action, body="", **kw):
    return c.post(f"{API}/changes/{cid}/act", headers=headers, json={"action": action, "body": body, **kw})


def inbox_titles(c, email):
    return [n["title"] for n in c.get("/inbox/api/notifications", headers=who(email, "inbox")).json()["notifications"]]


def test_sign_in_and_settings_are_admin_only(c):
    assert c.get(f"{API}/me").status_code == 401
    assert c.get(f"{API}/me", headers=who("amy.chen@nscale.com", "tasks")).status_code == 401
    assert c.get("/changes/", headers=AMY).status_code == 200
    assert c.get(f"{API}/settings", headers=AMY).status_code == 403
    assert c.put(f"{API}/settings", headers=AMY, json={"roles": {}}).status_code == 403
    s = c.get(f"{API}/settings", headers=ADMIN).json()
    assert [f["name"] for f in s["flows"]] == ["Standard", "Normal", "Emergency"] and "review" in s["stepTypes"]
    # Flows are checked when saved.
    bad = [{"name": "", "steps": [{"type": "review"}]}, {"name": "X", "steps": []}, {"name": "X", "steps": [{"type": "magic"}]},
           {"name": "X", "steps": [{"type": "review", "who": "people", "people": []}]},
           {"name": "X", "steps": [{"type": "review", "rule": "count", "count": "lots"}]}]
    for f in bad:
        assert c.put(f"{API}/settings", headers=ADMIN, json={"flows": [f]}).status_code == 422
    assert c.put(f"{API}/settings", headers=ADMIN, json={"roles": {"reviewers": ["not-an-email"]}}).status_code == 422
    assert c.put(f"{API}/settings", headers=ADMIN, json={"risks": []}).status_code == 422


def test_requestor_role(c):
    c.put(f"{API}/settings", headers=ADMIN, json={"roles": {"requestors": ["amy.chen@nscale.com"], "reviewers": ["bob.smith@nscale.com"]}})
    assert c.get(f"{API}/me", headers=DAN).json()["canRequest"] is False
    assert c.post(f"{API}/changes", headers=DAN, json={"title": "x", "flowId": "normal"}).status_code == 403
    assert c.get(f"{API}/me", headers=AMY).json()["canRequest"] is True
    assert c.get(f"{API}/me", headers=BOB).json()["isReviewer"] is True


def test_normal_flow_end_to_end(c):
    ch = raise_change(c)
    cid = ch["id"]
    assert ch["ref"] == "CHG-001" and ch["status"] == "Draft" and ch["canEdit"]
    # Drafts are private and need the essentials before submitting.
    assert c.get(f"{API}/changes?view=all", headers=BOB).json()["changes"] == []
    c.patch(f"{API}/changes/{cid}", headers=AMY, json={"fields": {"rollback": ""}})
    assert "Rollback plan" in act(c, cid, AMY, "submit").json()["detail"]
    assert c.patch(f"{API}/changes/{cid}", headers=BOB, json={"title": "x"}).status_code == 404   # can't even see the draft
    c.patch(f"{API}/changes/{cid}", headers=AMY, json={"fields": {"rollback": "Unassign the policy"}})
    r = act(c, cid, AMY, "submit").json()
    assert r["status"] == "In review" and r["stepName"] == "Change review"
    assert set(r["stepActors"]) == {"bob.smith@nscale.com", "cat.jones@nscale.com"}   # never the requestor
    assert r["stepNeeded"] == 2
    assert "Your turn to review CHG-001 BitLocker policy" in inbox_titles(c, "bob.smith@nscale.com")
    assert [x["ref"] for x in c.get(f"{API}/changes?view=review", headers=BOB).json()["changes"]] == ["CHG-001"]
    # Amy can't approve her own; Dan isn't a reviewer.
    assert act(c, cid, AMY, "approve").status_code == 403
    assert act(c, cid, DAN, "approve").status_code == 403
    # Cat sends it back; Amy fixes it and resubmits; approvals start again.
    assert act(c, cid, CAT, "request_changes").status_code == 422   # needs a reason
    r = act(c, cid, BOB, "approve", "Looks fine").json()
    assert r["stepApprovals"] == ["bob.smith@nscale.com"] and r["status"] == "In review"
    r = act(c, cid, CAT, "request_changes", "Add a test plan").json()
    assert r["status"] == "Changes requested" and not r["canEdit"]   # Cat can't; Amy can
    assert c.get(f"{API}/changes/{cid}", headers=AMY).json()["canEdit"]
    assert inbox_titles(c, "amy.chen@nscale.com")[0] == "Cat Jones asked for changes to CHG-001 BitLocker policy"
    c.patch(f"{API}/changes/{cid}", headers=AMY, json={"fields": {"test": "Pilot group of 5"}})
    r = act(c, cid, AMY, "submit", "Added a test plan").json()
    assert r["status"] == "In review" and r["stepApprovals"] == []
    act(c, cid, BOB, "approve")
    assert act(c, cid, BOB, "approve").status_code == 409   # once each
    r = act(c, cid, CAT, "approve", "Good").json()
    # Approved → schedule (Amy) → the notify step passes straight through → implement.
    assert r["status"] == "Awaiting schedule" and r["myTurn"] is False
    assert inbox_titles(c, "amy.chen@nscale.com")[0] == "Your turn to schedule CHG-001 BitLocker policy"
    assert act(c, cid, BOB, "schedule").status_code == 403
    r = act(c, cid, AMY, "schedule", start="2026-11-26T18:00", end="2026-11-26T19:00").json()
    assert r["status"] == "Scheduled" and r["fields"]["start"] == "2026-11-26T18:00"
    assert "CHG-001 BitLocker policy: Tell the reviewers it's scheduled" in inbox_titles(c, "bob.smith@nscale.com")
    assert act(c, cid, AMY, "start").json()["status"] == "In progress"
    r = act(c, cid, AMY, "complete", "Deployed to all").json()
    assert r["status"] == "Awaiting verification" and set(r["stepActors"]) == {"bob.smith@nscale.com", "cat.jones@nscale.com"}
    r = act(c, cid, CAT, "approve", "Compliance is green").json()
    assert r["status"] == "Completed" and r["closedAt"]
    assert "CHG-001 BitLocker policy is completed" in inbox_titles(c, "amy.chen@nscale.com")
    assert [x["ref"] for x in c.get(f"{API}/changes?view=closed", headers=BOB).json()["changes"]] == ["CHG-001"]
    kinds = [a["kind"] for a in r["activity"]]
    assert kinds.count("approve") == 4 and "request_changes" in kinds and "resubmit" in kinds


def test_decline_fail_cancel_and_standard(c):
    d = raise_change(c, flow="emergency")
    act(c, d["id"], AMY, "submit")
    r = act(c, d["id"], BOB, "decline", "Not an emergency, use Normal").json()
    assert r["status"] == "Declined" and act(c, d["id"], CAT, "approve").status_code == 409
    assert inbox_titles(c, "amy.chen@nscale.com")[0] == "Bob Smith declined CHG-001 BitLocker policy"
    # Emergency: one approval, implement, then a retrospective review.
    e = raise_change(c, flow="emergency")
    act(c, e["id"], AMY, "submit")
    assert act(c, e["id"], CAT, "approve").json()["status"] == "Scheduled"
    assert act(c, e["id"], AMY, "fail").status_code == 422   # say what happened
    r = act(c, e["id"], AMY, "fail", "Broke VPN; rolled back").json()
    assert r["status"] == "Failed"
    # Standard: no approval, scheduling uses the window already on the form.
    s = raise_change(c, flow="standard")
    r = act(c, s["id"], AMY, "submit").json()
    assert r["status"] == "Awaiting schedule"
    assert act(c, s["id"], AMY, "schedule").json()["status"] == "Scheduled"
    r = act(c, s["id"], AMY, "cancel", "Not needed after all").json()
    assert r["status"] == "Cancelled" and act(c, s["id"], AMY, "start").status_code == 409
    # Only drafts are deleted.
    dr = raise_change(c)
    assert c.delete(f"{API}/changes/{dr['id']}", headers=BOB).status_code in (403, 404)
    assert c.delete(f"{API}/changes/{s['id']}", headers=AMY).status_code == 403
    assert c.delete(f"{API}/changes/{dr['id']}", headers=AMY).status_code == 200


def test_comments_tags_updates_and_flow_copies(c):
    ch = raise_change(c)
    cid = ch["id"]
    act(c, cid, AMY, "submit")
    r = act(c, cid, BOB, "comment", "@cat.jones can you check the scope?").json()
    assert r["activity"][-1]["mentions"] == ["cat.jones@nscale.com"]
    assert inbox_titles(c, "cat.jones@nscale.com")[0] == "Bob Smith tagged you on CHG-001 BitLocker policy"
    assert inbox_titles(c, "amy.chen@nscale.com")[0] == "Bob Smith commented on CHG-001 BitLocker policy"
    assert act(c, cid, BOB, "update", "x").status_code == 403   # updates are the requestor's
    act(c, cid, AMY, "update", "Scope is the pilot group only")
    assert inbox_titles(c, "bob.smith@nscale.com")[0] == "Amy Chen posted an update on CHG-001 BitLocker policy"
    # Editing a flow doesn't change one already under way.
    flows = c.get(f"{API}/settings", headers=ADMIN).json()["flows"]
    flows[1]["steps"] = [{"type": "review", "name": "Just one", "rule": "any"}]
    c.put(f"{API}/settings", headers=ADMIN, json={"flows": flows})
    got = c.get(f"{API}/changes/{cid}", headers=AMY).json()
    assert got["stepName"] == "Change review" and got["stepNeeded"] == 2 and len(got["flow"]["steps"]) == 5
    assert raise_change(c)["flow"]["steps"][0]["name"] == "Just one"


def test_people_picked_from_the_at_list_are_tagged(c):
    ch = raise_change(c)
    act(c, ch["id"], AMY, "submit")
    # Dan has never been near a change, so his @tag alone wouldn't be recognised; picked from the list, it is.
    r = act(c, ch["id"], BOB, "comment", "@dan.ray please check the scope", mentions=["dan.ray@nscale.com", "eve@nscale.com", "not-an-email"]).json()
    assert r["activity"][-1]["mentions"] == ["dan.ray@nscale.com"]   # Eve's tag isn't in the text
    assert inbox_titles(c, "dan.ray@nscale.com")[0] == "Bob Smith tagged you on CHG-001 BitLocker policy"
