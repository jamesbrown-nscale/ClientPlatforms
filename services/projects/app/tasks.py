"""Nscale Team Planning (once called Tasks): shared plans of scheduled jobs, on Client Platforms at /tasks/.

The tool's id, its URL and its tables keep the old name "tasks", so roles, links and data carry on;
everything people see says Team Planning. In this file a "task" is a plan.

It runs in the Projects service and keeps its plans in the same database, so a plan can
belong to a project and follow that project's Members.

A plan has a title, description, assignee, due date, priority, status, progress and optionally a
project, and a list of jobs. A job has a title, description, note, expected start date, duration
in days (so an end date), an assignee and a status; scheduled jobs show on the calendar
(GET /api/calendar). A plan's activity is one thread of:
    comment   discussion
    question  waits on one person (the assignee, or the creator if the assignee asked) until
              someone answers it
    answer    a reply to a question; it closes the question
    update    a progress note, optionally with a percentage, which becomes the plan's progress
    change    written by the app: assigned, due date moved, status changed, job added …
@name or @email in a comment mentions that person; their mentions are listed for them.

Who sees a plan:
    no project      everyone with the Team Planning tool
    in a project    that project's members, plus whoever created it, is assigned it or has a job in it
Who changes it (and its jobs): whoever can see it, except that in a project, viewers can only comment.
Only its creator, a project owner or a Client Platforms admin can delete it.
"""
import json
import re
import uuid
from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel, Field

from . import access
from .inbox import notify
from .auth import TASKS, User, current_user, require_task_user
from .store import Store, now

STATUSES = ["To do", "In progress", "Blocked", "Done"]
PRIORITIES = ["Low", "Medium", "High", "Urgent"]
KINDS = ["comment", "question", "answer", "update"]
EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MENTION = re.compile(r"@([\w.+-]+@[\w-]+\.[\w.-]+|[\w.-]+)")

router = APIRouter()
_store: Store | None = None
STATIC = None


def setup(store: Store, static_dir) -> None:
    """Called by main.py with the shared store (whose tables include Tasks', see store.py)."""
    global _store, STATIC
    _store, STATIC = store, static_dir


def store() -> Store:
    return _store


# ---------- Reading ----------

def _task(r) -> dict:
    return {k: r[k] for k in r.keys()}


def get_task(tid: str) -> dict | None:
    with store().db() as db:
        r = db.execute("SELECT t.*, p.doc AS project_doc FROM tasks t LEFT JOIN projects p ON p.id = t.project_id "
                       "WHERE t.id = ?", (tid,)).fetchone()
    return _view(r) if r else None


def _view(r) -> dict:
    t = _task(r)
    doc = t.pop("project_doc", None)
    t["project"] = {"id": t["project_id"], "name": json.loads(doc)["name"]} if doc else None
    return {"id": t["id"], "ref": t["ref"], "title": t["title"], "description": t["description"], "status": t["status"],
            "priority": t["priority"], "assignee": t["assignee"], "assigneeName": t["assignee_name"], "due": t["due"],
            "progress": t["progress"], "project": t["project"], "createdBy": t["created_by"],
            "createdByName": t["created_by_name"], "createdAt": t["created_at"], "updatedAt": t["updated_at"],
            "doneAt": t["done_at"]}


def project_role(pid: str | None, user: User) -> str | None:
    if not pid:
        return None
    return store().role(pid, user.email) or ("Owner" if user.admin else None)


def _people_of(r) -> list[dict]:
    """Everyone on a job ([{email, name}], the first is also its `assignee`)."""
    try:
        people = json.loads(r["people"] or "[]")
    except (ValueError, TypeError, IndexError, KeyError):
        people = []
    if not people and r["assignee"]:
        people = [{"email": r["assignee"], "name": r["assignee_name"]}]
    return [p for p in people if isinstance(p, dict) and p.get("email")]


def _job_people(tid: str) -> set[str]:
    with store().db() as db:
        rows = db.execute("SELECT assignee, assignee_name, people FROM pipeline_jobs WHERE task_id = ?", (tid,)).fetchall()
    return {p["email"] for r in rows for p in _people_of(r)}


def can_see(t: dict, user: User) -> bool:
    if user.admin or not t["project"] or user.email in (t["createdBy"], t["assignee"]) or user.email in _job_people(t["id"]):
        return True
    return project_role(t["project"]["id"], user) is not None


def can_change(t: dict, user: User) -> bool:
    if user.admin or not t["project"] or user.email in (t["createdBy"], t["assignee"]) or user.email in _job_people(t["id"]):
        return True
    return project_role(t["project"]["id"], user) in ("Owner", "Editor")


def can_delete(t: dict, user: User) -> bool:
    return user.admin or user.email == t["createdBy"] or (
        bool(t["project"]) and project_role(t["project"]["id"], user) == "Owner")


def visible_tasks(user: User) -> list[dict]:
    with store().db() as db:
        rows = db.execute("SELECT t.*, p.doc AS project_doc FROM tasks t LEFT JOIN projects p ON p.id = t.project_id").fetchall()
        mine = {r["project_id"] for r in db.execute("SELECT project_id FROM members WHERE email = ?", (user.email,))}
        my_jobs = {r["task_id"] for r in db.execute("SELECT task_id, assignee, assignee_name, people FROM pipeline_jobs")
                   if any(p["email"] == user.email for p in _people_of(r))}
    out = []
    for r in rows:
        t = _view(r)
        if user.admin or not t["project"] or t["project"]["id"] in mine or user.email in (t["createdBy"], t["assignee"]) \
                or t["id"] in my_jobs:
            out.append(t)
    return out


def _activity(tid: str) -> list[dict]:
    with store().db() as db:
        rows = db.execute("SELECT * FROM task_activity WHERE task_id = ? ORDER BY created_at, rowid", (tid,)).fetchall()
    return [{"id": r["id"], "kind": r["kind"], "author": r["author"], "authorName": r["author_name"], "body": r["body"],
             "progress": r["progress"], "waitingOn": r["waiting_on"], "answers": r["answers"],
             "answeredAt": r["answered_at"], "answeredBy": r["answered_by"], "mentions": json.loads(r["mentions"] or "[]"),
             "data": json.loads(r["data"]) if r["data"] else None, "createdAt": r["created_at"]} for r in rows]


def _stats(user: User) -> dict:
    """Per-task comment counts and open questions, and what's waiting on this person."""
    with store().db() as db:
        counts = {r["task_id"]: r["n"] for r in db.execute(
            "SELECT task_id, COUNT(*) AS n FROM task_activity WHERE kind IN ('comment','question','answer','update') GROUP BY task_id")}
        open_q = {}
        for r in db.execute("SELECT task_id, waiting_on FROM task_activity WHERE kind = 'question' AND answered_at IS NULL"):
            open_q.setdefault(r["task_id"], []).append(r["waiting_on"])
        mentioned = {r["task_id"] for r in db.execute(
            "SELECT DISTINCT task_id FROM task_activity, json_each(task_activity.mentions) WHERE json_each.value = ?",
            (user.email,))}
    return {"counts": counts, "openQuestions": open_q, "mentioned": mentioned}


# ---------- Writing ----------

def _add_activity(tid: str, kind: str, user: User, body: str = "", **kw) -> None:
    with store().lock, store().db() as db:
        db.execute("INSERT INTO task_activity (id, task_id, kind, author, author_name, body, progress, waiting_on, "
                   "answers, mentions, data, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                   (uuid.uuid4().hex, tid, kind, user.email, user.name, body, kw.get("progress"), kw.get("waiting_on"),
                    kw.get("answers"), json.dumps(kw.get("mentions") or []),
                    json.dumps(kw["data"]) if kw.get("data") else None, now()))
        db.execute("UPDATE tasks SET updated_at = ? WHERE id = ?", (now(), tid))


def _next_ref() -> str:
    with store().db() as db:
        refs = [r["ref"] for r in db.execute("SELECT ref FROM tasks")]
    n = max([int(re.sub(r"\D", "", r) or 0) for r in refs if r] or [0])
    return f"PL-{n + 1:03d}"


def picked_mentions(text: str, picked: list | None) -> list[str]:
    """People chosen from the @ list whose @tag (first.last) is still in the text."""
    out = []
    for e in picked or []:
        e = str(e).strip().lower()
        if EMAIL.match(e) and re.search(rf"(^|[\s(])@{re.escape(e.split('@')[0])}\b", text or "", re.I) and e not in out:
            out.append(e)
    return out


def mentions_in(text: str, people: list[dict]) -> list[str]:
    """Emails of the people @mentioned: @first.last, @first or @full@email (matched to known people)."""
    found = []
    for tag in MENTION.findall(text or ""):
        tag = tag.lower().rstrip(".")
        for p in people:
            local = p["email"].split("@")[0]
            first = local.split(".")[0]
            if tag in (p["email"], local, first) and p["email"] not in found:
                found.append(p["email"])
    if not people:   # nobody to match against: full addresses only
        found = [t.lower() for t in MENTION.findall(text or "") if EMAIL.match(t)]
    return found


def _people_known() -> list[dict]:
    """Everyone the Team Planning data knows by email (assignees, creators, authors, project members)."""
    with store().db() as db:
        rows = db.execute("SELECT assignee AS email, assignee_name AS name FROM tasks WHERE assignee != '' "
                          "UNION SELECT json_extract(p.value, '$.email'), json_extract(p.value, '$.name') "
                          "FROM pipeline_jobs, json_each(pipeline_jobs.people) p "
                          "UNION SELECT created_by, created_by_name FROM tasks "
                          "UNION SELECT author, author_name FROM task_activity "
                          "UNION SELECT email, name FROM members").fetchall()
    seen = {}
    for r in rows:
        if r["email"] and r["email"] not in seen:
            seen[r["email"]] = r["name"] or access.display_name(r["email"])
    return [{"email": e, "name": n} for e, n in seen.items()]


# ---------- API ----------

class TaskIn(BaseModel):
    title: str | None = Field(default=None, max_length=200)
    description: str | None = Field(default=None, max_length=10000)
    status: str | None = None
    priority: str | None = None
    assignee: str | None = Field(default=None, max_length=254)
    assigneeName: str | None = Field(default=None, max_length=100)
    due: str | None = None
    projectId: str | None = None


class ActivityIn(BaseModel):
    kind: str = "comment"
    body: str = Field(default="", max_length=10000)
    progress: int | None = Field(default=None, ge=0, le=100)
    answers: str | None = None
    to: str | None = Field(default=None, max_length=254)
    mentions: list[str] = Field(default_factory=list, max_length=50)   # picked from the @ list


def _found(tid: str, user: User) -> dict:
    t = get_task(tid)
    if not t or not can_see(t, user):
        raise HTTPException(404, "Plan not found.")
    return t


@router.get("/")
def page(request: Request):
    if not current_user(request, TASKS):
        return HTMLResponse(f"<!doctype html><title>Team Planning</title><p style='font-family:system-ui;padding:40px'>"
                            f"{access.sign_in_message()}</p>", status_code=403)
    return FileResponse(STATIC / "tasks.html", headers={"Cache-Control": "no-store"})


@router.get("/api/me")
def me(user: User = Depends(require_task_user)):
    return {"email": user.email, "name": user.name, "admin": user.admin, "statuses": STATUSES, "priorities": PRIORITIES}


VIEWS = ("mine", "asked", "waiting", "mentioned", "all", "done")


@router.get("/api/tasks")
def list_tasks(view: str = "mine", project: str = "", user: User = Depends(require_task_user)):
    if view not in VIEWS:
        raise HTTPException(422, f"view must be one of {', '.join(VIEWS)}")
    tasks = visible_tasks(user)
    st = _stats(user)
    summary = _job_summary(user)
    for t in tasks:
        t["jobs"] = summary.get(t["id"], {"total": 0, "done": 0, "start": "", "end": "", "mine": 0})
        t["comments"] = st["counts"].get(t["id"], 0)
        t["openQuestions"] = len(st["openQuestions"].get(t["id"], []))
        t["waitingOnMe"] = user.email in st["openQuestions"].get(t["id"], [])
    if project:
        tasks = [t for t in tasks if t["project"] and t["project"]["id"] == project]
    open_ = [t for t in tasks if t["status"] != "Done"]
    views = {
        "mine": [t for t in open_ if t["assignee"] == user.email or t["jobs"]["mine"]],
        "asked": [t for t in open_ if t["createdBy"] == user.email and t["assignee"] != user.email],
        "waiting": [t for t in tasks if t["waitingOnMe"]],
        "mentioned": [t for t in tasks if t["id"] in st["mentioned"]],
        "all": open_,
        "done": sorted((t for t in tasks if t["status"] == "Done"), key=lambda t: t["doneAt"] or "", reverse=True),
    }
    rows = views[view]
    if view != "done":
        order = {p: i for i, p in enumerate(reversed(PRIORITIES))}
        rows.sort(key=lambda t: (t["due"] or "9999", order.get(t["priority"], 9), t["createdAt"]))
    return {"tasks": rows[:2000], "counts": {k: len(v) for k, v in views.items()}}


@router.get("/api/counts")
def counts(user: User = Depends(require_task_user)):
    """For the sidebar badge: open tasks assigned to me, and questions waiting on me."""
    tasks = visible_tasks(user)
    st = _stats(user)
    summary = _job_summary(user)
    mine = sum(1 for t in tasks if t["status"] != "Done" and (t["assignee"] == user.email or summary.get(t["id"], {}).get("mine")))
    waiting = sum(1 for t in tasks if user.email in st["openQuestions"].get(t["id"], []))
    overdue = sum(1 for t in tasks if t["assignee"] == user.email and t["status"] != "Done"
                  and t["due"] and t["due"] < date.today().isoformat())
    return {"mine": mine, "waiting": waiting, "overdue": overdue, "badge": mine + waiting}


def _clean(body: TaskIn, t: dict | None) -> dict:
    out = {}
    if body.title is not None:
        if not body.title.strip():
            raise HTTPException(422, "Give the plan a title.")
        out["title"] = body.title.strip()
    if body.description is not None:
        out["description"] = body.description.strip()
    if body.status is not None:
        if body.status not in STATUSES:
            raise HTTPException(422, f"Status must be one of {', '.join(STATUSES)}.")
        out["status"] = body.status
    if body.priority is not None:
        if body.priority not in PRIORITIES:
            raise HTTPException(422, f"Priority must be one of {', '.join(PRIORITIES)}.")
        out["priority"] = body.priority
    if body.assignee is not None:
        a = body.assignee.strip().lower()
        if a and not EMAIL.match(a):
            raise HTTPException(422, "Assign it to someone's email address.")
        out["assignee"] = a
        out["assignee_name"] = (body.assigneeName or "").strip() or (access.display_name(a) if a else "")
    if body.due is not None:
        if body.due and not re.match(r"^\d{4}-\d{2}-\d{2}$", body.due):
            raise HTTPException(422, "Due date must be a date (YYYY-MM-DD).")
        out["due"] = body.due
    return out


def _check_project(pid: str, user: User) -> None:
    if project_role(pid, user) not in ("Owner", "Editor"):
        raise HTTPException(403, "You can only add tasks to projects where you're an owner or editor.")


@router.post("/api/tasks")
def create_task(body: TaskIn, user: User = Depends(require_task_user)):
    fields = _clean(body, None)
    if not fields.get("title"):
        raise HTTPException(422, "Give the plan a title.")
    pid = body.projectId or None
    if pid:
        _check_project(pid, user)
    tid = uuid.uuid4().hex
    t = now()
    with store().lock, store().db() as db:
        db.execute("INSERT INTO tasks (id, ref, title, description, status, priority, assignee, assignee_name, due, "
                   "project_id, created_by, created_by_name, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                   (tid, _next_ref(), fields["title"], fields.get("description", ""), fields.get("status", "To do"),
                    fields.get("priority", "Medium"), fields.get("assignee", ""), fields.get("assignee_name", ""),
                    fields.get("due", ""), pid, user.email, user.name, t, t))
    t = get_task(tid)
    if fields.get("assignee") and fields["assignee"] != user.email:
        _add_activity(tid, "change", user, data={"field": "assignee", "to": fields["assignee_name"], "toEmail": fields["assignee"]})
        notify(fields["assignee"], "task_assigned", f"{user.name} assigned you {t['ref']} {t['title']}", user,
               _due_line(t), link(tid))
    return t


def link(tid: str) -> str:
    return f"/tasks/#view=all&task={tid}"


def nice_date(iso: str) -> str:
    """2026-11-20 → 20 Nov (the year too if it isn't this year)."""
    try:
        d = date.fromisoformat(iso)
    except (TypeError, ValueError):
        return iso or ""
    return f"{d.day} {d:%b}" + ("" if d.year == date.today().year else f" {d.year}")


def _due_line(t: dict) -> str:
    bits = [f"Due {nice_date(t['due'])}" if t.get("due") else "", t["priority"] if t.get("priority") in ("High", "Urgent") else "",
            (t.get("project") or {}).get("name", "")]
    return " · ".join(b for b in bits if b)


@router.get("/api/tasks/{tid}")
def read_task(tid: str, user: User = Depends(require_task_user)):
    t = _found(tid, user)
    return {**t, "jobs": jobs_of(tid), "activity": _activity(tid), "canChange": can_change(t, user), "canDelete": can_delete(t, user)}


LABEL = {"title": "title", "status": "status", "priority": "priority", "assignee": "assignee", "due": "due date",
         "project": "project"}


@router.patch("/api/tasks/{tid}")
def update_task(tid: str, body: TaskIn, user: User = Depends(require_task_user)):
    t = _found(tid, user)
    if not can_change(t, user):
        raise HTTPException(403, "Only the project's owners and editors can change this task.")
    fields = _clean(body, t)
    changes = []
    if "projectId" in body.model_fields_set:
        pid = body.projectId or None
        if pid != (t["project"] or {}).get("id"):
            if pid:
                _check_project(pid, user)
            fields["project_id"] = pid
            name = (store().project(pid) or {}).get("name") if pid else ""
            changes.append({"field": "project", "from": (t["project"] or {}).get("name", ""), "to": name})
    current = {"title": t["title"], "description": t["description"], "status": t["status"], "priority": t["priority"],
               "assignee": t["assignee"], "assignee_name": t["assigneeName"], "due": t["due"]}
    for k in ("title", "status", "priority", "due"):
        if k in fields and fields[k] != current[k]:
            changes.append({"field": k, "from": current[k], "to": fields[k]})
    if "assignee" in fields and fields["assignee"] != current["assignee"]:
        changes.append({"field": "assignee", "from": current["assignee_name"], "to": fields["assignee_name"],
                        "toEmail": fields["assignee"]})
    if "status" in fields and fields["status"] != current["status"]:
        fields["done_at"] = now() if fields["status"] == "Done" else None
        if fields["status"] == "Done":
            fields["progress"] = 100
    if fields:
        cols = ", ".join(f"{k} = ?" for k in fields)
        with store().lock, store().db() as db:
            db.execute(f"UPDATE tasks SET {cols}, updated_at = ? WHERE id = ?", (*fields.values(), now(), tid))
    for c in changes:
        _add_activity(tid, "change", user, data=c)
    _tell_about_changes(t, changes, user)
    return read_task(tid, user)


def _tell_about_changes(t: dict, changes: list[dict], user: User) -> None:
    label = f"{t['ref']} {t['title']}"
    for c in changes:
        if c["field"] == "assignee" and c.get("toEmail"):
            notify(c["toEmail"], "task_assigned", f"{user.name} assigned you {label}", user, _due_line(t), link(t["id"]))
    others = [c for c in changes if c["field"] != "assignee"]
    if not others:
        return
    done = any(c["field"] == "status" and c["to"] == "Done" for c in others)
    what = "marked done" if done else "changed " + ", ".join(LABEL.get(c["field"], c["field"]) for c in others)
    current = get_task(t["id"]) or t
    notify([current["assignee"], t["createdBy"]], "task_done" if done else "task_change", f"{user.name} {what}: {label}", user,
           "; ".join(f"{LABEL.get(c['field'], c['field'])} → {c['to'] or 'none'}" for c in others), link(t["id"]))


@router.delete("/api/tasks/{tid}")
def delete_task(tid: str, user: User = Depends(require_task_user)):
    t = _found(tid, user)
    if not can_delete(t, user):
        raise HTTPException(403, "Only whoever created this task (or a project owner) can delete it.")
    with store().lock, store().db() as db:
        db.execute("DELETE FROM tasks WHERE id = ?", (tid,))
    return {"ok": True}


@router.post("/api/tasks/{tid}/activity")
def add_activity(tid: str, body: ActivityIn, user: User = Depends(require_task_user)):
    t = _found(tid, user)
    if body.kind not in KINDS:
        raise HTTPException(422, f"kind must be one of {', '.join(KINDS)}.")
    text = body.body.strip()
    if not text and not (body.kind == "update" and body.progress is not None):
        raise HTTPException(422, "Write something first.")
    if body.kind == "update" and not can_change(t, user):
        raise HTTPException(403, "Only the project's owners and editors can post updates.")
    kw = {"mentions": list(dict.fromkeys(picked_mentions(text, body.mentions) + mentions_in(text, _people_known())))}
    if body.kind == "question":
        to = (body.to or "").strip().lower()
        if not to:   # ask the assignee, or the creator if the assignee is asking
            to = t["createdBy"] if user.email == t["assignee"] or not t["assignee"] else t["assignee"]
        if not EMAIL.match(to):
            raise HTTPException(422, "Ask someone by their email address.")
        kw["waiting_on"] = to
    if body.kind == "answer":
        with store().db() as db:
            q = db.execute("SELECT id FROM task_activity WHERE id = ? AND task_id = ? AND kind = 'question'",
                           (body.answers or "", tid)).fetchone()
        if not q:
            raise HTTPException(404, "Question not found.")
        kw["answers"] = q["id"]
    if body.kind == "update":
        kw["progress"] = body.progress
    _add_activity(tid, body.kind, user, text, **kw)
    if body.kind == "answer":
        with store().lock, store().db() as db:
            db.execute("UPDATE task_activity SET answered_at = ?, answered_by = ? WHERE id = ? AND answered_at IS NULL",
                       (now(), user.name, kw["answers"]))
    if body.kind == "update" and body.progress is not None:
        with store().lock, store().db() as db:
            db.execute("UPDATE tasks SET progress = ? WHERE id = ?", (body.progress, tid))
        if t["status"] == "To do" and body.progress > 0:   # work has started
            update_task(tid, TaskIn(status="In progress"), user)
    _tell_about_activity(t, body, kw, text, user)
    return read_task(tid, user)


def _tell_about_activity(t: dict, body: "ActivityIn", kw: dict, text: str, user: User) -> None:
    label, url = f"{t['ref']} {t['title']}", link(t["id"])
    mentioned = set(kw.get("mentions") or [])
    told = set()
    if mentioned:
        notify(mentioned, "mention", f"{user.name} mentioned you on {label}", user, text, url)
        told |= mentioned
    if body.kind == "question":
        notify(kw["waiting_on"], "task_question", f"{user.name} asked you a question on {label}", user, text, url)
        told.add(kw["waiting_on"])
    if body.kind == "answer":
        with store().db() as db:
            q = db.execute("SELECT author FROM task_activity WHERE id = ?", (kw["answers"],)).fetchone()
        if q and q["author"] not in told:
            notify(q["author"], "task_answer", f"{user.name} answered your question on {label}", user, text, url)
            told.add(q["author"])
    what = {"comment": "commented on", "update": "posted an update on", "question": "asked a question on",
            "answer": "answered a question on"}[body.kind]
    if body.kind == "update" and kw.get("progress") is not None:
        what = f"posted an update ({kw['progress']}%) on"
    rest = [e for e in (t["assignee"], t["createdBy"]) if e and e not in told]
    notify(rest, "task_comment", f"{user.name} {what} {label}", user, text, url)


@router.delete("/api/tasks/{tid}/activity/{aid}")
def delete_activity(tid: str, aid: str, user: User = Depends(require_task_user)):
    _found(tid, user)
    with store().lock, store().db() as db:
        r = db.execute("SELECT author, kind, answers FROM task_activity WHERE id = ? AND task_id = ?", (aid, tid)).fetchone()
        if not r or r["kind"] == "change":
            raise HTTPException(404, "Not found.")
        if r["author"] != user.email and not user.admin:
            raise HTTPException(403, "You can only delete what you wrote.")
        db.execute("DELETE FROM task_activity WHERE id = ?", (aid,))
        if r["answers"]:   # deleting an answer reopens its question
            db.execute("UPDATE task_activity SET answered_at = NULL, answered_by = NULL WHERE id = ?", (r["answers"],))
    return read_task(tid, user)


@router.get("/api/projects")
def linkable_projects(user: User = Depends(require_task_user)):
    """Projects this person can put tasks in (owner or editor; admins: all)."""
    rows = store().projects_for(user.email, every=user.admin)
    return {"projects": sorted(({"id": p["id"], "name": p["name"]} for p in rows
                                if user.admin or p.get("role") in ("Owner", "Editor")), key=lambda p: p["name"].lower())}


@router.get("/api/people")
def people(user: User = Depends(require_task_user)):
    """People the Team Planning data already knows, for when the portal's people list isn't available."""
    return {"people": sorted(_people_known(), key=lambda p: p["name"].lower())}


# ---------- Jobs ----------

def workday(d: date) -> date:
    """The day itself on a weekday; the Monday after on a weekend."""
    return d + timedelta(days=(7 - d.weekday()) if d.weekday() >= 5 else 0)


def end_of(start: str, duration: int) -> str:
    """The last day of a job that starts on `start` and lasts `duration` working days (the start day counts).
    Weekends don't count: five days from a Thursday ends the Wednesday after."""
    try:
        d = workday(date.fromisoformat(start))
    except (TypeError, ValueError):
        return ""
    left = max(1, duration or 1) - 1
    d += timedelta(days=7 * (left // 5))
    for _ in range(left % 5):
        d = workday(d + timedelta(days=1))
    return d.isoformat()


def _job(r) -> dict:
    return {"id": r["id"], "pipelineId": r["task_id"], "position": r["position"], "title": r["title"],
            "description": r["description"], "note": r["note"], "start": r["start"], "duration": r["duration"],
            "end": end_of(r["start"], r["duration"]) if r["start"] else "", "assignee": r["assignee"],
            "assigneeName": r["assignee_name"], "people": _people_of(r), "status": r["status"],
            "progress": 100 if r["status"] == "Done" else (r["progress"] or 0), "createdAt": r["created_at"], "updatedAt": r["updated_at"]}


def jobs_of(tid: str) -> list[dict]:
    with store().db() as db:
        rows = db.execute("SELECT * FROM pipeline_jobs WHERE task_id = ? ORDER BY position, created_at", (tid,)).fetchall()
    return [_job(r) for r in rows]


def _job_summary(user: User) -> dict:
    """Per plan: how many jobs, how many done, the first start and last end, and how many open are mine."""
    out = {}
    with store().db() as db:
        rows = db.execute("SELECT task_id, start, duration, status, assignee, assignee_name, people FROM pipeline_jobs").fetchall()
    for r in rows:
        s = out.setdefault(r["task_id"], {"total": 0, "done": 0, "start": "", "end": "", "mine": 0})
        s["total"] += 1
        s["done"] += r["status"] == "Done"
        s["mine"] += any(p["email"] == user.email for p in _people_of(r)) and r["status"] != "Done"
        if r["start"]:
            e = end_of(r["start"], r["duration"])
            s["start"] = min(s["start"] or r["start"], r["start"])
            s["end"] = max(s["end"], e)
    return out


class PersonIn(BaseModel):
    email: str = Field(max_length=254)
    name: str = Field(default="", max_length=100)


class JobIn(BaseModel):
    title: str | None = Field(default=None, max_length=200)
    description: str | None = Field(default=None, max_length=10000)
    note: str | None = Field(default=None, max_length=10000)
    start: str | None = None
    duration: int | None = Field(default=None, ge=1, le=365)
    assignee: str | None = Field(default=None, max_length=254)        # one person (older pages); `people` for several
    assigneeName: str | None = Field(default=None, max_length=100)
    people: list[PersonIn] | None = Field(default=None, max_length=30)
    status: str | None = None
    progress: int | None = Field(default=None, ge=0, le=100)          # % complete, shown while In progress
    mentions: list[str] = Field(default_factory=list, max_length=50)   # picked from the @ list in the note or description


class OrderIn(BaseModel):
    ids: list[str] = Field(default_factory=list, max_length=500)


def _clean_job(body: JobIn) -> dict:
    out = {}
    if body.title is not None:
        if not body.title.strip():
            raise HTTPException(422, "Give the job a title.")
        out["title"] = body.title.strip()
    for k in ("description", "note"):
        if getattr(body, k) is not None:
            out[k] = getattr(body, k).strip()
    if body.start is not None:
        if body.start and not re.match(r"^\d{4}-\d{2}-\d{2}$", body.start):
            raise HTTPException(422, "Start must be a date (YYYY-MM-DD).")
        try:
            body.start and date.fromisoformat(body.start)
        except ValueError:
            raise HTTPException(422, "Start must be a real date.")
        out["start"] = workday(date.fromisoformat(body.start)).isoformat() if body.start else ""   # jobs start on a working day
    if body.duration is not None:
        out["duration"] = body.duration
    if body.status is not None:
        if body.status not in STATUSES:
            raise HTTPException(422, f"Status must be one of {', '.join(STATUSES)}.")
        out["status"] = body.status
        if body.status == "Done":
            out["progress"] = 100
    if body.progress is not None and "progress" not in out:
        out["progress"] = body.progress
    people = body.people
    if people is None and body.assignee is not None:
        people = [PersonIn(email=body.assignee, name=body.assigneeName or "")] if body.assignee.strip() else []
    if people is not None:
        seen, clean = set(), []
        for p in people:
            a = p.email.strip().lower()
            if not EMAIL.match(a):
                raise HTTPException(422, f"“{p.email}” isn't an email address: assign the job to people's emails.")
            if a not in seen:
                seen.add(a)
                clean.append({"email": a, "name": p.name.strip() or access.display_name(a)})
        out["people"] = json.dumps(clean)
        out["assignee"] = clean[0]["email"] if clean else ""
        out["assignee_name"] = clean[0]["name"] if clean else ""
    return out


def _changeable(tid: str, user: User) -> dict:
    t = _found(tid, user)
    if not can_change(t, user):
        raise HTTPException(403, "Only the project's owners and editors can change this plan's jobs.")
    return t


def _job_row(tid: str, jid: str) -> dict:
    with store().db() as db:
        r = db.execute("SELECT * FROM pipeline_jobs WHERE id = ? AND task_id = ?", (jid, tid)).fetchone()
    if not r:
        raise HTTPException(404, "Job not found.")
    return _job(r)


def _when(j: dict) -> str:
    if not j.get("start"):
        return "Not scheduled yet"
    days = j["duration"] or 1
    return f"Starts {nice_date(j['start'])} · {days} working day{'s' if days != 1 else ''}"


def _job_mentions(j: dict, picked: list[str]) -> set[str]:
    text = f"{j.get('description', '')}\n{j.get('note', '')}"
    return set(picked_mentions(text, picked) + mentions_in(text, _people_known()))


def _tell_job_mentions(t: dict, j: dict, body: "JobIn", before: dict | None, user: User) -> None:
    """Tell people @mentioned in a job's note or description, the first time they're mentioned there."""
    new = _job_mentions(j, body.mentions) - (_job_mentions(before, body.mentions) if before else set())
    if new:
        notify(new, "mention", f"{user.name} mentioned you on {t['ref']} {t['title']}: {j['title']}", user,
               (j.get("note") or j.get("description") or "")[:300], link(t["id"]) + f"&job={j['id']}")


def _tell_job_assignee(t: dict, j: dict, user: User, before: dict | None = None) -> None:
    """Tell the people newly on a job (not whoever made the change)."""
    new = {p["email"] for p in j["people"]} - {p["email"] for p in (before or {}).get("people", [])} - {user.email}
    if not new:
        return
    notify(sorted(new), "job_assigned", f"{user.name} gave you a job on {t['ref']} {t['title']}: {j['title']}", user,
           _when(j), link(t["id"]) + f"&job={j['id']}")


@router.post("/api/tasks/{tid}/jobs")
def add_job(tid: str, body: JobIn, user: User = Depends(require_task_user)):
    t = _changeable(tid, user)
    f = _clean_job(body)
    if not f.get("title"):
        raise HTTPException(422, "Give the job a title.")
    jid, stamp = uuid.uuid4().hex, now()
    with store().lock, store().db() as db:
        pos = db.execute("SELECT COALESCE(MAX(position), -1) + 1 AS n FROM pipeline_jobs WHERE task_id = ?", (tid,)).fetchone()["n"]
        db.execute("INSERT INTO pipeline_jobs (id, task_id, position, title, description, note, start, duration, assignee, "
                   "assignee_name, people, status, progress, created_by, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                   (jid, tid, pos, f["title"], f.get("description", ""), f.get("note", ""), f.get("start", ""),
                    f.get("duration", 1), f.get("assignee", ""), f.get("assignee_name", ""), f.get("people", "[]"), f.get("status", "To do"),
                    f.get("progress", 0), user.email, stamp, stamp))
    _add_activity(tid, "change", user, data={"field": "job", "action": "added", "to": f["title"]})
    j = _job_row(tid, jid)
    _tell_job_assignee(t, j, user)
    _tell_job_mentions(t, j, body, None, user)
    return read_task(tid, user)


@router.patch("/api/tasks/{tid}/jobs/{jid}")
def update_job(tid: str, jid: str, body: JobIn, user: User = Depends(require_task_user)):
    t = _changeable(tid, user)
    before = _job_row(tid, jid)
    f = _clean_job(body)
    if f:
        cols = ", ".join(f"{k} = ?" for k in f)
        with store().lock, store().db() as db:
            db.execute(f"UPDATE pipeline_jobs SET {cols}, updated_at = ? WHERE id = ?", (*f.values(), now(), jid))
    j = _job_row(tid, jid)
    if j["status"] != before["status"] and j["status"] == "Done":
        _add_activity(tid, "change", user, data={"field": "job", "action": "done", "to": j["title"]})
    elif before["start"] != j["start"] or before["duration"] != j["duration"]:
        _add_activity(tid, "change", user, data={"field": "job", "action": "scheduled", "to": j["title"], "when": _when(j)})
    _tell_job_assignee(t, j, user, before)
    _tell_job_mentions(t, j, body, before, user)
    return read_task(tid, user)


@router.delete("/api/tasks/{tid}/jobs/{jid}")
def delete_job(tid: str, jid: str, user: User = Depends(require_task_user)):
    _changeable(tid, user)
    j = _job_row(tid, jid)
    with store().lock, store().db() as db:
        db.execute("DELETE FROM pipeline_jobs WHERE id = ?", (jid,))
    _add_activity(tid, "change", user, data={"field": "job", "action": "removed", "to": j["title"]})
    return read_task(tid, user)


@router.post("/api/tasks/{tid}/jobs/order")
def order_jobs(tid: str, body: OrderIn, user: User = Depends(require_task_user)):
    _changeable(tid, user)
    with store().lock, store().db() as db:
        for i, jid in enumerate(body.ids):
            db.execute("UPDATE pipeline_jobs SET position = ? WHERE id = ? AND task_id = ?", (i, jid, tid))
    return read_task(tid, user)


@router.get("/api/calendar")
def calendar(start: str, end: str, project: str = "", mine: bool = False, user: User = Depends(require_task_user)):
    """Scheduled jobs that overlap start..end (inclusive), and open plans due in that range."""
    try:
        a, b = date.fromisoformat(start), date.fromisoformat(end)
    except ValueError:
        raise HTTPException(422, "start and end must be dates (YYYY-MM-DD).")
    if b < a or (b - a).days > 400:
        raise HTTPException(422, "Pick a range of up to 400 days.")
    seen = {t["id"]: t for t in visible_tasks(user) if not project or (t["project"] and t["project"]["id"] == project)}
    with store().db() as db:
        rows = db.execute("SELECT * FROM pipeline_jobs WHERE start != '' AND start <= ? ORDER BY start, position",
                          (b.isoformat(),)).fetchall()
        waiting = db.execute("SELECT task_id, COUNT(*) AS n FROM pipeline_jobs WHERE start = '' AND status != 'Done' "
                             "GROUP BY task_id").fetchall()
    jobs = []
    for r in rows:
        j = _job(r)
        t = seen.get(j["pipelineId"])
        if not t or j["end"] < a.isoformat() or (mine and user.email not in {p["email"] for p in j["people"]}):
            continue
        j["pipeline"] = {"id": t["id"], "ref": t["ref"], "title": t["title"], "status": t["status"],
                         "project": (t["project"] or {}).get("name", "")}
        jobs.append(j)
    due = [{"id": t["id"], "ref": t["ref"], "title": t["title"], "due": t["due"]} for t in seen.values()
           if t["due"] and a.isoformat() <= t["due"] <= b.isoformat() and t["status"] != "Done"
           and (not mine or t["assignee"] == user.email)]
    unscheduled = sum(r["n"] for r in waiting if r["task_id"] in seen)
    return {"jobs": jobs, "due": due, "unscheduled": unscheduled}


@router.get("/api/timeline")
def timeline(project: str = "", mine: bool = False, done: bool = False, user: User = Depends(require_task_user)):
    """Open plans with all their jobs (scheduled or not), for the timeline: one group per plan.
    mine=true keeps plans you own or have a job in, and only your jobs within them.
    done=true includes finished plans too (the All Tasks list)."""
    plans = [t for t in visible_tasks(user) if (done or t["status"] != "Done")
             and (not project or (t["project"] and t["project"]["id"] == project))]
    with store().db() as db:
        rows = db.execute("SELECT * FROM pipeline_jobs ORDER BY position, created_at").fetchall()
    by_plan = {}
    for r in rows:
        by_plan.setdefault(r["task_id"], []).append(_job(r))
    out = []
    for t in plans:
        jobs = by_plan.get(t["id"], [])
        if mine:
            jobs = [j for j in jobs if user.email in {p["email"] for p in j["people"]}]
            if not jobs and t["assignee"] != user.email:
                continue
        starts = [j["start"] for j in jobs if j["start"]]
        out.append({"id": t["id"], "ref": t["ref"], "title": t["title"], "status": t["status"], "due": t["due"],
                    "priority": t["priority"], "assignee": t["assignee"], "assigneeName": t["assigneeName"],
                    "project": (t["project"] or {}).get("name", ""), "canChange": can_change(t, user),
                    "start": min(starts) if starts else "", "end": max((j["end"] for j in jobs if j["end"]), default=""),
                    "jobs": jobs})
    out.sort(key=lambda p: (p["start"] or p["due"] or "9999", p["ref"]))
    return {"plans": out}
