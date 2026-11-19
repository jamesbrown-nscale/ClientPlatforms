"""Nscale Change Management, on Client Platforms at /changes/ (Device Management).

A change request is raised by a requestor and goes through the flow for its change type. Flows
and roles are set by admins in Settings → Change management (stored in change_settings):

    roles      requestors: "everyone" with the tool, or a list of people
               reviewers: the Change reviewers
    flows      one per change type (Standard, Normal, Emergency …), each a list of steps:
                 review     who approves (the reviewers, or named people) and how many must:
                            any one, a number, or all. They can approve, decline or ask for changes.
                 notify     tells people (reviewers, the requestor, named people) and moves on
                 schedule   the requestor confirms the implementation window
                 implement  the requestor (or named implementers) starts, then completes or fails it
                 verify     someone confirms it worked (reviewers, the requestor or named people)
    categories, risks   the choices on the form

A change keeps a copy of the flow it was submitted with, so editing a flow never disturbs changes
already under way. The requestor never approves their own change.

States:  draft → active (on a step) → completed | declined | failed | cancelled
         active → recalled (the requestor took it back before implementation started) → edited and
         resubmitted from the start of its flow, or deleted
         active → changes_requested (a reviewer asked) → active again when the requestor resubmits;
         approvals on that step start again.
Everyone involved is told in their notifications (inbox.py): submitted, approved, declined, changes
requested, your turn, comments, @mentions, and the requestor's updates.

Who sees changes: everyone with the Change management tool sees all of them (changes are meant to
be visible). Only the requestor edits a draft or a change sent back to them.
"""
import json
import re
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel, Field

from . import access
from .auth import CHANGES, User, current_user, require_changes_user
from .inbox import notify
from .store import Store, now
from .tasks import mentions_in, picked_mentions

STEP_TYPES = {
    "review": "Review",
    "notify": "Notify",
    "schedule": "Schedule",
    "implement": "Implement",
    "verify": "Verify",
}
WHO = ("reviewers", "requestor", "people")
RULES = ("any", "count", "all")
TERMINAL = ("completed", "declined", "failed", "cancelled")
EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
DT = re.compile(r"^\d{4}-\d{2}-\d{2}(T\d{2}:\d{2})?$")
FIELDS = {   # the change form: key → (label, max length)
    "summary": ("What's changing", 4000), "reason": ("Why", 4000), "impact": ("Impact and who's affected", 4000),
    "scope": ("Devices, groups or services", 2000), "plan": ("Implementation plan", 8000), "test": ("Test plan", 4000),
    "rollback": ("Rollback plan", 4000),
}

# Parts of a change people can comment on (the + on each section), and what's compared after it's
# returned to the requestor and resubmitted.
SECTIONS = {"details": "Details", "summary": "What's changing", "reason": "Why", "impact": "Impact and who's affected",
            "scope": "Devices, groups or services", "plan": "Implementation plan", "test": "Test plan", "rollback": "Rollback plan"}
COMPARED = [("title", "Title"), ("category", "Category"), ("risk", "Risk"), ("start", "Planned start"), ("end", "Planned end"),
            ("summary", "What's changing"), ("reason", "Why"), ("impact", "Impact and who's affected"),
            ("scope", "Devices, groups or services"), ("plan", "Implementation plan"), ("test", "Test plan"), ("rollback", "Rollback plan")]


def snapshot(c: dict) -> dict:
    """The parts of a change that are compared later, as they stand now."""
    f = c["fields"]
    out = {k: f.get(k, "") or "" for k, _ in COMPARED if k != "title"}
    out["title"] = c["title"]
    out["plan"] = [x["text"] for x in f.get("planSteps") or []] or [ln for ln in (f.get("plan") or "").splitlines() if ln.strip()]
    return out


def differences(before: dict, after: dict) -> list[dict]:
    """What changed between two snapshots, field by field; for the plan, which steps were added, removed or reworded."""
    out = []
    for k, label in COMPARED:
        a, b = before.get(k, "" if k != "plan" else []), after.get(k, "" if k != "plan" else [])
        if a == b:
            continue
        item = {"field": k, "section": k if k in SECTIONS else "details", "label": label, "from": a, "to": b}
        if k == "plan":
            item["added"] = [x for x in b if x not in a]
            item["removed"] = [x for x in a if x not in b]
            item["reordered"] = not item["added"] and not item["removed"]
        out.append(item)
    return out


DEFAULTS = {
    "roles": {"requestors": "everyone", "reviewers": []},
    "categories": ["Intune policy", "Jamf policy", "Software deployment", "OS update", "Device build", "Identity and access",
                   "Network or VPN", "Other"],
    "risks": ["Low", "Medium", "High"],
    "flows": [
        {"id": "standard", "name": "Standard", "description": "Pre-approved, low-risk, repeatable changes.",
         "steps": [{"id": "s1", "type": "notify", "name": "Tell the reviewers", "who": "reviewers", "people": []},
                   {"id": "s2", "type": "schedule", "name": "Schedule", "who": "requestor", "people": []},
                   {"id": "s3", "type": "implement", "name": "Implement", "who": "requestor", "people": []},
                   {"id": "s4", "type": "verify", "name": "Check it worked", "who": "requestor", "people": [], "rule": "any"}]},
        {"id": "normal", "name": "Normal", "description": "Reviewed and approved before it's scheduled.",
         "steps": [{"id": "n1", "type": "review", "name": "Change review", "who": "reviewers", "people": [], "rule": "count", "count": 2},
                   {"id": "n2", "type": "schedule", "name": "Schedule", "who": "requestor", "people": []},
                   {"id": "n3", "type": "notify", "name": "Tell the reviewers it's scheduled", "who": "reviewers", "people": []},
                   {"id": "n4", "type": "implement", "name": "Implement", "who": "requestor", "people": []},
                   {"id": "n5", "type": "verify", "name": "Post-implementation review", "who": "reviewers", "people": [], "rule": "any"}]},
        {"id": "emergency", "name": "Emergency", "description": "Urgent fixes: one approval, then reviewed afterwards.",
         "steps": [{"id": "e1", "type": "review", "name": "Emergency approval", "who": "reviewers", "people": [], "rule": "any"},
                   {"id": "e2", "type": "implement", "name": "Implement", "who": "requestor", "people": []},
                   {"id": "e3", "type": "review", "name": "Retrospective review", "who": "reviewers", "people": [], "rule": "any"}]},
    ],
}

router = APIRouter()
_store: Store | None = None
STATIC = None


def setup(store: Store, static_dir) -> None:
    global _store, STATIC
    _store, STATIC = store, static_dir


# ---------- Settings ----------

def settings() -> dict:
    with _store.db() as d:
        rows = {r["key"]: json.loads(r["value"]) for r in d.execute("SELECT key, value FROM change_settings")}
    return {k: rows.get(k, v) for k, v in DEFAULTS.items()}


def _clean_step(s: dict, i: int) -> dict:
    t = s.get("type")
    if t not in STEP_TYPES:
        raise HTTPException(422, f"Step {i + 1}: unknown kind of step.")
    who = s.get("who") or ("requestor" if t in ("schedule", "implement") else "reviewers")
    if who not in WHO:
        raise HTTPException(422, f"Step {i + 1}: who acts must be the reviewers, the requestor or named people.")
    people = [p.strip().lower() for p in (s.get("people") or []) if isinstance(p, str) and p.strip()]
    bad = [p for p in people if not EMAIL.match(p)]
    if bad:
        raise HTTPException(422, f"Step {i + 1}: {bad[0]} isn't an email address.")
    if who == "people" and not people:
        raise HTTPException(422, f"Step {i + 1}: name at least one person.")
    out = {"id": s.get("id") if isinstance(s.get("id"), str) and re.fullmatch(r"[\w-]{1,40}", s["id"]) else uuid.uuid4().hex[:8],
           "type": t, "name": (s.get("name") or STEP_TYPES[t]).strip()[:80], "who": who, "people": people}
    if t in ("review", "verify"):
        rule = s.get("rule") or "any"
        if rule not in RULES:
            raise HTTPException(422, f"Step {i + 1}: approval rule must be any, a number, or all.")
        out["rule"] = rule
        if rule == "count":
            try:
                out["count"] = max(1, min(int(s.get("count") or 1), 20))
            except (TypeError, ValueError):
                raise HTTPException(422, f"Step {i + 1}: how many must approve is a number.")
    if t == "notify":
        out["message"] = (s.get("message") or "").strip()[:500]
    return out


def clean_settings(body: dict) -> dict:
    out = settings()
    if "roles" in body:
        r = body["roles"] or {}
        req = r.get("requestors", "everyone")
        if req != "everyone":
            req = [p.strip().lower() for p in (req or []) if isinstance(p, str) and p.strip()]
            if any(not EMAIL.match(p) for p in req):
                raise HTTPException(422, "Requestors must be email addresses.")
        rev = [p.strip().lower() for p in (r.get("reviewers") or []) if isinstance(p, str) and p.strip()]
        if any(not EMAIL.match(p) for p in rev):
            raise HTTPException(422, "Reviewers must be email addresses.")
        out["roles"] = {"requestors": req, "reviewers": sorted(set(rev))}
    for k in ("categories", "risks"):
        if k in body:
            vals = [v.strip()[:60] for v in (body[k] or []) if isinstance(v, str) and v.strip()]
            if not vals:
                raise HTTPException(422, f"Keep at least one {k[:-1] if k == 'categories' else 'risk level'}.")
            out[k] = list(dict.fromkeys(vals))[:40]
    if "flows" in body:
        flows, ids = [], set()
        for f in body["flows"] or []:
            name = (f.get("name") or "").strip()[:60]
            if not name:
                raise HTTPException(422, "Every flow needs a name.")
            steps = [_clean_step(s, i) for i, s in enumerate(f.get("steps") or [])]
            if not steps:
                raise HTTPException(422, f"The {name} flow needs at least one step.")
            fid = f.get("id") if isinstance(f.get("id"), str) and re.fullmatch(r"[\w-]{1,40}", f["id"]) else re.sub(r"\W+", "-", name.lower()).strip("-")[:30] or uuid.uuid4().hex[:8]
            while fid in ids:
                fid += "-2"
            ids.add(fid)
            flows.append({"id": fid, "name": name, "description": (f.get("description") or "").strip()[:300], "steps": steps})
        if not flows:
            raise HTTPException(422, "Keep at least one flow.")
        out["flows"] = flows
    return out


def save_settings(values: dict, user: User) -> None:
    with _store.lock, _store.db() as d:
        for k, v in values.items():
            d.execute("INSERT OR REPLACE INTO change_settings (key, value, updated_at, updated_by) VALUES (?, ?, ?, ?)",
                      (k, json.dumps(v), now(), user.email))


def can_request(user: User, s: dict | None = None) -> bool:
    req = (s or settings())["roles"]["requestors"]
    return user.admin or req == "everyone" or user.email in req


def is_reviewer(user: User, s: dict | None = None) -> bool:
    return user.email in (s or settings())["roles"]["reviewers"]


# ---------- Changes ----------

def _change(r) -> dict:
    flow = json.loads(r["flow"])
    fields = json.loads(r["fields"])
    if "planSteps" not in fields and fields.get("plan"):
        fields["planSteps"] = steps_from_text(fields["plan"])
    return {"id": r["id"], "ref": r["ref"], "title": r["title"], "flow": flow, "state": r["state"], "step": r["step"],
            "stepStartedAt": r["step_started_at"], "started": bool(r["started"]), "fields": fields,
            "requestor": r["requestor"], "requestorName": r["requestor_name"], "createdAt": r["created_at"],
            "updatedAt": r["updated_at"], "submittedAt": r["submitted_at"], "closedAt": r["closed_at"]}


def get_change(cid: str) -> dict | None:
    with _store.db() as d:
        r = d.execute("SELECT * FROM changes WHERE id = ?", (cid,)).fetchone()
    return _change(r) if r else None


def current_step(c: dict) -> dict | None:
    steps = c["flow"]["steps"]
    return steps[c["step"]] if c["state"] in ("active", "changes_requested") and c["step"] < len(steps) else None


def actors(c: dict, step: dict, s: dict | None = None) -> list[str]:
    """Who acts on (or, for notify, hears about) this step of this change."""
    s = s or settings()
    if step["who"] == "requestor":
        return [c["requestor"]]
    people = step["people"] if step["who"] == "people" else s["roles"]["reviewers"]
    if step["type"] in ("review", "verify") and step["who"] != "requestor":
        people = [p for p in people if p != c["requestor"]]   # nobody approves their own change
    return list(dict.fromkeys(people))


def needed(step: dict, n_actors: int) -> int:
    rule = step.get("rule", "any")
    if rule == "all":
        return max(1, n_actors)
    if rule == "count":
        return min(step.get("count", 1), max(1, n_actors))
    return 1


def _activity(cid: str) -> list[dict]:
    with _store.db() as d:
        rows = d.execute("SELECT * FROM change_activity WHERE change_id = ? ORDER BY created_at, rowid", (cid,)).fetchall()
    return [{"id": r["id"], "kind": r["kind"], "author": r["author"], "authorName": r["author_name"], "body": r["body"],
             "step": r["step"], "mentions": json.loads(r["mentions"] or "[]"), "data": json.loads(r["data"]) if r["data"] else None,
             "createdAt": r["created_at"]} for r in rows]


def approvals(c: dict) -> list[dict]:
    """Approvals on the current step since it last started (a resubmit starts it again). Goes by the
    order things happened in the thread, not timestamps, which can tie within a second."""
    got = []
    for a in _activity(c["id"]):
        if a["step"] != c["step"]:
            continue
        if a["kind"] == "step":
            got = []
        elif a["kind"] == "approve":
            got.append(a)
    return got


def status(c: dict) -> str:
    if c["state"] == "draft":
        return "Draft"
    if c["state"] == "recalled":
        return "Recalled"
    if c["state"] == "changes_requested":
        return "Changes requested"
    if c["state"] in TERMINAL:
        return c["state"].capitalize()
    step = current_step(c)
    if not step:
        return "Completed"
    return {"review": "In review", "notify": "In review", "verify": "Awaiting verification",
            "schedule": "Awaiting schedule", "implement": "In progress" if c["started"] else "Scheduled"}[step["type"]]


def my_turn(c: dict, user: User, s: dict | None = None) -> bool:
    step = current_step(c)
    if not step or c["state"] != "active":
        return c["state"] in ("draft", "changes_requested", "recalled") and c["requestor"] == user.email
    if step["type"] in ("review", "verify"):
        return user.email in actors(c, step, s) and user.email not in {a["author"] for a in approvals(c)}
    return user.email in actors(c, step, s)


def can_recall(c: dict) -> bool:
    """The requestor can take a change back until implementation starts."""
    if c["state"] not in ("active", "changes_requested"):
        return False
    step = current_step(c)
    reached_implement = any(x["type"] == "implement" for x in c["flow"]["steps"][:c["step"]])
    return not reached_implement and not (step and step["type"] == "implement" and c["started"])


def view(c: dict, user: User, s: dict | None = None, full: bool = False) -> dict:
    s = s or settings()
    step = current_step(c)
    out = {**c, "status": status(c), "myTurn": my_turn(c, user, s), "stepName": step["name"] if step else ""}
    if full:
        acts = actors(c, step, s) if step else []
        appr = approvals(c) if step and step["type"] in ("review", "verify") else []
        activity = _activity(c["id"])
        last = next((x for x in reversed(activity) if x["kind"] in ("resubmit", "request_changes", "recall")), None)
        revision = ({"at": last["createdAt"], "by": last["authorName"], "returnedBy": (last["data"] or {}).get("returnedBy"),
                     "recalled": bool((last["data"] or {}).get("recalled")),
                     "note": last["body"], "changes": (last["data"] or {}).get("changes", [])}
                    if last and last["kind"] == "resubmit" else None)
        out.update(activity=activity, revision=revision, sections=SECTIONS, stepActors=acts, stepApprovals=[a["author"] for a in appr],
                   stepNeeded=needed(step, len(acts)) if step and step["type"] in ("review", "verify") else 0,
                   canEdit=c["requestor"] == user.email and c["state"] in ("draft", "changes_requested", "recalled"),
                   canRecall=c["requestor"] == user.email and can_recall(c),
                   canCancel=(c["requestor"] == user.email or user.admin) and c["state"] not in TERMINAL)
    return out


def _add(cid: str, kind: str, user: User | None, body: str = "", step: int | None = None, data: dict | None = None,
         mentions: list | None = None) -> None:
    with _store.lock, _store.db() as d:
        d.execute("INSERT INTO change_activity (id, change_id, kind, author, author_name, body, step, mentions, data, created_at) "
                  "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                  (uuid.uuid4().hex, cid, kind, user.email if user else None, user.name if user else "Change management",
                   body, step, json.dumps(mentions or []), json.dumps(data) if data else None, now()))


def _set(cid: str, **cols) -> None:
    cols["updated_at"] = now()
    with _store.lock, _store.db() as d:
        d.execute(f"UPDATE changes SET {', '.join(k + ' = ?' for k in cols)} WHERE id = ?", (*cols.values(), cid))


def link(c: dict) -> str:
    return f"/changes/#change={c['id']}"


def _label(c: dict) -> str:
    return f"{c['ref']} {c['title']}"


def _enter_step(cid: str, user: User, s: dict) -> None:
    """Move onto the change's current step and run it: notify steps pass straight through."""
    while True:
        c = get_change(cid)
        steps = c["flow"]["steps"]
        if c["step"] >= len(steps):
            _set(cid, state="completed", closed_at=now())
            _add(cid, "status", None, "Completed: every step of the flow is done.")
            notify([c["requestor"]] + s["roles"]["reviewers"], "change", f"{_label(c)} is completed", user, "", link(c))
            return
        step = steps[c["step"]]
        _set(cid, state="active", step_started_at=now(), started=0)
        _add(cid, "step", None, step["name"], c["step"], {"type": step["type"]})
        who = actors(c, step, s)
        if step["type"] == "notify":
            notify(who, "change", f"{_label(c)}: {step['name']}", user, step.get("message") or c["fields"].get("summary", "")[:200], link(c))
            _set(cid, step=c["step"] + 1)
            continue
        verb = {"review": "review", "verify": "check", "schedule": "schedule", "implement": "implement"}[step["type"]]
        notify(who, "change", f"Your turn to {verb} {_label(c)}", user, f"{step['name']} · risk {c['fields'].get('risk', '')}", link(c))
        if step["type"] in ("review", "verify") and not who:
            _add(cid, "status", None, f"Nobody can act on “{step['name']}”: add Change reviewers in Settings, or an admin can act.")
        return


def _advance(cid: str, user: User, s: dict) -> None:
    c = get_change(cid)
    _set(cid, step=c["step"] + 1)
    _enter_step(cid, user, s)


# ---------- API ----------

class ChangeIn(BaseModel):
    title: str | None = Field(default=None, max_length=200)
    planSteps: list[dict] | None = Field(default=None, max_length=100)   # the implementation plan, one step each
    flowId: str | None = None
    category: str | None = None
    risk: str | None = None
    start: str | None = None
    end: str | None = None
    fields: dict | None = None


class ActIn(BaseModel):
    action: str
    section: str | None = None   # a comment on one part of the change (the + on each section)
    stepId: str | None = None   # "check": which plan step
    done: bool = True
    body: str = Field(default="", max_length=10000)
    mentions: list[str] = Field(default_factory=list, max_length=50)   # picked from the @ list
    start: str | None = None
    end: str | None = None


@router.get("/")
def page(request: Request):
    if not current_user(request, CHANGES):
        return HTMLResponse(f"<!doctype html><title>Change management</title><p style='font-family:system-ui;padding:40px'>"
                            f"{access.sign_in_message()}</p>", status_code=403)
    return FileResponse(STATIC / "changes.html", headers={"Cache-Control": "no-store"})


@router.get("/api/me")
def me(user: User = Depends(require_changes_user)):
    s = settings()
    return {"email": user.email, "name": user.name, "admin": user.admin, "canRequest": can_request(user, s),
            "isReviewer": is_reviewer(user, s), "fields": {k: v[0] for k, v in FIELDS.items()},
            "flows": [{"id": f["id"], "name": f["name"], "description": f["description"],
                       "steps": [{"type": x["type"], "name": x["name"]} for x in f["steps"]]} for f in s["flows"]],
            "categories": s["categories"], "risks": s["risks"], "reviewers": s["roles"]["reviewers"]}


@router.get("/api/settings")
def get_settings(user: User = Depends(require_changes_user)):
    if not user.admin:
        raise HTTPException(403, "Only admins change the change management settings.")
    return {**settings(), "stepTypes": STEP_TYPES}


@router.put("/api/settings")
async def put_settings(request: Request, user: User = Depends(require_changes_user)):
    if not user.admin:
        raise HTTPException(403, "Only admins change the change management settings.")
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(422, "Send the settings.")
    values = clean_settings(body)
    save_settings({k: values[k] for k in body if k in DEFAULTS}, user)
    return {**settings(), "stepTypes": STEP_TYPES}


VIEWS = ("mine", "review", "open", "closed", "all")


@router.get("/api/changes")
def list_changes(v: str = Query("open", alias="view"), user: User = Depends(require_changes_user)):
    if v not in VIEWS:
        raise HTTPException(422, f"view must be one of {', '.join(VIEWS)}")
    s = settings()
    with _store.db() as d:
        rows = [_change(r) for r in d.execute("SELECT * FROM changes ORDER BY COALESCE(submitted_at, created_at) DESC")]
    rows = [c for c in rows if c["state"] != "draft" or c["requestor"] == user.email]   # drafts are private
    out = [view(c, user, s) for c in rows]
    views = {
        "mine": [c for c in out if c["requestor"] == user.email and c["state"] not in TERMINAL],
        "review": [c for c in out if c["myTurn"] and c["state"] != "draft"],   # reviews, and your own changes when it's your turn
        "open": [c for c in out if c["state"] not in TERMINAL and c["state"] != "draft"],
        "closed": [c for c in out if c["state"] in TERMINAL],
        "all": out,
    }
    return {"changes": views[v], "counts": {k: len(x) for k, x in views.items()}}


def _fields(body: ChangeIn, current: dict, s: dict) -> dict:
    f = dict(current)
    if body.category is not None:
        if body.category and body.category not in s["categories"]:
            raise HTTPException(422, "Pick a category from the list.")
        f["category"] = body.category
    if body.risk is not None:
        if body.risk and body.risk not in s["risks"]:
            raise HTTPException(422, "Pick a risk level from the list.")
        f["risk"] = body.risk
    for k in ("start", "end"):
        v = getattr(body, k)
        if v is not None:
            if v and not DT.match(v):
                raise HTTPException(422, "Dates and times look like 2026-11-20T18:00.")
            f[k] = v
    if f.get("start") and f.get("end") and f["end"] < f["start"]:
        raise HTTPException(422, "The window ends before it starts.")
    for k, v in (body.fields or {}).items():
        if k in FIELDS:
            f[k] = str(v or "").strip()[:FIELDS[k][1]]
    if body.planSteps is not None:
        f["planSteps"] = clean_steps(body.planSteps, f.get("planSteps") or [])
        f["plan"] = plan_text(f["planSteps"])
    return f


def clean_steps(raw: list, old: list) -> list[dict]:
    """The implementation plan's steps, in order. Keeps the ticks on steps that are still there."""
    before = {x["id"]: x for x in old}
    out = []
    for x in raw[:100]:
        text = str((x or {}).get("text", "")).strip()[:500]
        if not text:
            continue
        sid = x.get("id") if isinstance(x.get("id"), str) and re.fullmatch(r"[\w-]{1,40}", x["id"]) else uuid.uuid4().hex[:10]
        prev = before.get(sid, {})
        out.append({"id": sid, "text": text, "done": bool(prev.get("done")), "doneBy": prev.get("doneBy"), "doneAt": prev.get("doneAt")})
    return out


def plan_text(steps: list[dict]) -> str:
    return "\n".join(f"{i}. {x['text']}" for i, x in enumerate(steps, 1))


def steps_from_text(text: str) -> list[dict]:
    """An older change's typed plan, as steps: one per line, without its own numbering or bullets."""
    lines = [re.sub(r"^\s*(\d+[.)]|[-*•])\s*", "", ln).strip() for ln in (text or "").splitlines()]
    return [{"id": f"p{i}", "text": ln[:500], "done": False, "doneBy": None, "doneAt": None} for i, ln in enumerate(lines) if ln]


def _ready(c: dict) -> None:
    if not c["fields"].get("planSteps") and c["fields"].get("plan"):
        c["fields"]["planSteps"] = steps_from_text(c["fields"]["plan"])
    missing = [lbl for k, lbl in (("summary", "What's changing"), ("reason", "Why"), ("planSteps", "Implementation plan"),
                                  ("rollback", "Rollback plan")) if not c["fields"].get(k)]
    if not c["fields"].get("risk"):
        missing.append("Risk")
    if missing:
        raise HTTPException(422, "Fill in before submitting: " + ", ".join(missing) + ".")


@router.post("/api/changes")
def create_change(body: ChangeIn, user: User = Depends(require_changes_user)):
    s = settings()
    if not can_request(user, s):
        raise HTTPException(403, "You're not a change requestor. Ask an admin to add you in Settings.")
    title = (body.title or "").strip()
    if not title:
        raise HTTPException(422, "Give the change a title.")
    flow = next((f for f in s["flows"] if f["id"] == body.flowId), None)
    if not flow:
        raise HTTPException(422, "Pick a change type.")
    fields = _fields(body, {}, s)
    cid, t = uuid.uuid4().hex, now()
    with _store.lock, _store.db() as d:
        refs = [r["ref"] for r in d.execute("SELECT ref FROM changes")]
        n = max([int(re.sub(r"\D", "", r) or 0) for r in refs if r] or [0]) + 1
        d.execute("INSERT INTO changes (id, ref, title, flow, state, step, fields, requestor, requestor_name, created_at, updated_at) "
                  "VALUES (?, ?, ?, ?, 'draft', 0, ?, ?, ?, ?, ?)",
                  (cid, f"CHG-{n:03d}", title, json.dumps(flow), json.dumps(fields), user.email, user.name, t, t))
    return read_change(cid, user)


def _found(cid: str, user: User) -> dict:
    c = get_change(cid)
    if not c or (c["state"] == "draft" and c["requestor"] != user.email and not user.admin):
        raise HTTPException(404, "Change not found.")
    return c


@router.get("/api/changes/{cid}")
def read_change(cid: str, user: User = Depends(require_changes_user)):
    return view(_found(cid, user), user, full=True)


@router.patch("/api/changes/{cid}")
def edit_change(cid: str, body: ChangeIn, user: User = Depends(require_changes_user)):
    c = _found(cid, user)
    if c["requestor"] != user.email or c["state"] not in ("draft", "changes_requested", "recalled"):
        raise HTTPException(403, "Only the requestor edits a change, while it's a draft, returned to them or recalled.")
    s = settings()
    cols = {"fields": json.dumps(_fields(body, c["fields"], s))}
    if body.title is not None:
        if not body.title.strip():
            raise HTTPException(422, "Give the change a title.")
        cols["title"] = body.title.strip()
    if body.flowId is not None and body.flowId != c["flow"]["id"]:
        if c["state"] not in ("draft", "recalled"):
            raise HTTPException(422, "The change type can only be changed while it's a draft or recalled.")
        flow = next((f for f in s["flows"] if f["id"] == body.flowId), None)
        if not flow:
            raise HTTPException(422, "Pick a change type.")
        cols["flow"] = json.dumps(flow)
    _set(cid, **cols)
    return read_change(cid, user)


@router.delete("/api/changes/{cid}")
def delete_draft(cid: str, user: User = Depends(require_changes_user)):
    c = _found(cid, user)
    if c["state"] not in ("draft", "recalled") or (c["requestor"] != user.email and not user.admin):
        raise HTTPException(403, "Only a draft or a recalled change can be deleted, by its requestor. Recall or cancel it first.")
    with _store.lock, _store.db() as d:
        d.execute("DELETE FROM changes WHERE id = ?", (cid,))
    return {"ok": True}


ACTIONS = ("submit", "recall", "approve", "decline", "request_changes", "schedule", "start", "check", "complete", "fail", "cancel", "comment",
           "update")


@router.post("/api/changes/{cid}/act")
def act(cid: str, body: ActIn, user: User = Depends(require_changes_user)):
    """Everything that moves a change on, plus comments and the requestor's updates."""
    c = _found(cid, user)
    s = settings()
    a, text = body.action, body.body.strip()
    if a not in ACTIONS:
        raise HTTPException(422, "Unknown action.")
    step = current_step(c)
    label, url = _label(c), link(c)
    mentions = list(dict.fromkeys(picked_mentions(text, body.mentions) + mentions_in(text, _people(s)))) if text else []

    if a in ("comment", "update"):
        if not text:
            raise HTTPException(422, "Write something first.")
        if a == "update" and c["requestor"] != user.email:
            raise HTTPException(403, "Updates come from the requestor; use a comment.")
        if body.section and body.section not in SECTIONS:
            raise HTTPException(422, "Unknown section.")
        _add(cid, a, user, text, c["step"], {"section": body.section} if body.section else None, mentions=mentions)
        on = f" ({SECTIONS[body.section]})" if body.section else ""
        if mentions:
            notify(mentions, "mention", f"{user.name} tagged you on {label}{on}", user, text, url)
        involved = {c["requestor"], *(actors(c, step, s) if step else []), *{x["author"] for x in _activity(cid) if x["author"]}}
        notify([e for e in involved if e not in mentions], "change",
               f"{user.name} {'posted an update on' if a == 'update' else 'commented on'} {label}{on}", user, text, url)
        return read_change(cid, user)

    if a == "cancel":
        if c["state"] in TERMINAL or (c["requestor"] != user.email and not user.admin):
            raise HTTPException(403, "Only the requestor (or an admin) cancels an open change.")
        _set(cid, state="cancelled", closed_at=now())
        _add(cid, "cancel", user, text, c["step"])
        notify([c["requestor"], *(actors(c, step, s) if step else [])], "change", f"{user.name} cancelled {label}", user, text, url)
        return read_change(cid, user)

    if a == "recall":
        if c["requestor"] != user.email:
            raise HTTPException(403, "Only the requestor can recall a change.")
        if not can_recall(c):
            raise HTTPException(409, "It can only be recalled while it's submitted and before implementation starts.")
        _set(cid, state="recalled", step=0, started=0)
        _add(cid, "recall", user, text, c["step"], {"snapshot": snapshot(c)})
        involved = {*(actors(c, step, s) if step else []), *{x["author"] for x in _activity(cid) if x["author"]}}
        notify(involved, "change", f"{user.name} recalled {label} to make changes", user,
               text or "It will go through the flow again from the start when it's resubmitted.", url)
        return read_change(cid, user)

    if a == "submit":
        if c["requestor"] != user.email or c["state"] not in ("draft", "changes_requested", "recalled"):
            raise HTTPException(403, "Only the requestor submits a draft, or resubmits a change sent back to them or recalled.")
        _ready(c)
        if c["state"] == "draft":
            _set(cid, submitted_at=now())
            _add(cid, "submit", user, text, 0)
            _enter_step(cid, user, s)
        else:
            returned = next((x for x in reversed(_activity(cid)) if x["kind"] in ("request_changes", "recall")), None)
            recalled = c["state"] == "recalled"
            diff = differences(((returned or {}).get("data") or {}).get("snapshot") or snapshot(c), snapshot(c))
            _add(cid, "resubmit", user, text, c["step"], {"changes": diff, "recalled": recalled,
                                                          "returnedBy": None if recalled else (returned or {}).get("authorName")})
            # Returned: the same step again. Recalled: the start of the flow. Either way, approvals start over.
            _enter_step(cid, user, s)
            if diff:
                notify(actors(get_change(cid), current_step(get_change(cid)) or {"who": "reviewers", "type": "review", "people": []}, s),
                       "change", f"{user.name} resubmitted {label}: {len(diff)} change{'s' if len(diff) > 1 else ''}", user,
                       ", ".join(d["label"] for d in diff), url)
        return read_change(cid, user)

    if c["state"] != "active" or not step:
        raise HTTPException(409, f"{c['ref']} isn't waiting on that.")
    who = actors(c, step, s)
    allowed = user.email in who or (user.admin and step["type"] in ("review", "verify") and user.email != c["requestor"])
    if not allowed:
        raise HTTPException(403, f"It's not your turn on “{step['name']}”.")

    if a in ("approve", "decline", "request_changes"):
        if step["type"] not in ("review", "verify"):
            raise HTTPException(409, "This step isn't a review.")
        if a != "approve" and not text:
            raise HTTPException(422, "Say why, so the requestor knows what to do.")
        if a == "approve":
            if user.email in {x["author"] for x in approvals(c)}:
                raise HTTPException(409, "You've already approved this step.")
            _add(cid, "approve", user, text, c["step"])
            got = len(approvals(get_change(cid)))
            need = needed(step, len(who))
            notify(c["requestor"], "change", f"{user.name} approved {label}" + (f" ({got} of {need})" if need > 1 else ""), user, text, url)
            if got >= need:
                _add(cid, "status", None, f"“{step['name']}” approved ({got} of {need}).", c["step"])
                _advance(cid, user, s)
        elif a == "decline":
            _set(cid, state="declined", closed_at=now())
            _add(cid, "decline", user, text, c["step"])
            notify([c["requestor"], *who], "change", f"{user.name} declined {label}", user, text, url)
        else:
            _set(cid, state="changes_requested")
            _add(cid, "request_changes", user, text, c["step"], {"snapshot": snapshot(c)})   # to show what changed later
            notify(c["requestor"], "change", f"{user.name} returned {label} to you for changes", user, text, url)
        return read_change(cid, user)

    if a == "schedule":
        if step["type"] != "schedule":
            raise HTTPException(409, "This step isn't scheduling.")
        start, end = body.start or c["fields"].get("start"), body.end or c["fields"].get("end")
        if not start or not DT.match(start) or (end and (not DT.match(end) or end < start)):
            raise HTTPException(422, "Give the window's start (and an end after it).")
        _set(cid, fields=json.dumps({**c["fields"], "start": start, "end": end or ""}))
        _add(cid, "schedule", user, text, c["step"], {"start": start, "end": end or ""})
        _advance(cid, user, s)
        return read_change(cid, user)

    if a in ("start", "check", "complete", "fail"):
        if step["type"] != "implement":
            raise HTTPException(409, "This step isn't implementation.")
        if a == "check":
            steps = c["fields"].get("planSteps") or []
            x = next((p for p in steps if p["id"] == body.stepId), None)
            if not x:
                raise HTTPException(404, "That step isn't in the plan.")
            x.update(done=body.done, doneBy=user.name if body.done else None, doneAt=now() if body.done else None)
            _set(cid, fields=json.dumps({**c["fields"], "planSteps": steps}))
            if not c["started"]:   # ticking the first step starts it
                _set(cid, started=1)
                _add(cid, "start", user, "", c["step"])
                notify(s["roles"]["reviewers"], "change", f"{user.name} started implementing {label}", user, "", url)
            done = sum(1 for p in steps if p["done"])
            _add(cid, "check", user, x["text"], c["step"], {"done": body.done, "of": f"{done}/{len(steps)}"})
            return read_change(cid, user)
        if a == "start":
            _set(cid, started=1)
            _add(cid, "start", user, text, c["step"])
            notify(s["roles"]["reviewers"], "change", f"{user.name} started implementing {label}", user, text, url)
        elif a == "complete":
            left = [p for p in c["fields"].get("planSteps") or [] if not p["done"]]
            if left and not text:
                raise HTTPException(422, f"{len(left)} step{'s' if len(left) > 1 else ''} of the plan aren't ticked off. "
                                         "Say why in a note, or tick them first.")
            _add(cid, "complete", user, text, c["step"])
            _advance(cid, user, s)
        else:
            if not text:
                raise HTTPException(422, "Say what happened and whether it was rolled back.")
            _set(cid, state="failed", closed_at=now())
            _add(cid, "fail", user, text, c["step"])
            notify([c["requestor"], *s["roles"]["reviewers"]], "change", f"{label} failed", user, text, url)
        return read_change(cid, user)

    raise HTTPException(409, "That doesn't apply to this step.")


def _people(s: dict) -> list[dict]:
    """People who can be @tagged: reviewers, requestors, and anyone already on a change."""
    emails = set(s["roles"]["reviewers"]) | (set(s["roles"]["requestors"]) if s["roles"]["requestors"] != "everyone" else set())
    with _store.db() as d:
        emails |= {r["requestor"] for r in d.execute("SELECT DISTINCT requestor FROM changes")}
        emails |= {r["author"] for r in d.execute("SELECT DISTINCT author FROM change_activity WHERE author IS NOT NULL")}
    return [{"email": e, "name": access.display_name(e)} for e in emails if e]
