"""Nscale Tasks: a small shared task list, on Client Platforms at /tasks/.

It runs in the Projects service and keeps its tasks in the same database, so a task can belong to
a project and follow that project's Members.

A task has a title, description, assignee, due date, priority, status, progress and optionally a
project. Its activity is one thread of:
    comment   discussion
    question  waits on one person (the assignee, or the creator if the assignee asked) until
              someone answers it
    answer    a reply to a question; it closes the question
    update    a progress note, optionally with a percentage, which becomes the task's progress
    change    written by the app: assigned, due date moved, status changed …
@name or @email in a comment mentions that person; their mentions are listed for them.

Who sees a task:
    a task with no project   everyone with the Tasks tool
    a task in a project      that project's members, plus whoever created it or is assigned it
Who changes it: whoever can see it, except that in a project, viewers can only comment.
Only its creator, a project owner or a Client Platforms admin can delete it.
"""
import json
import re
import uuid
from datetime import date

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


def can_see(t: dict, user: User) -> bool:
    if user.admin or not t["project"] or user.email in (t["createdBy"], t["assignee"]):
        return True
    return project_role(t["project"]["id"], user) is not None


def can_change(t: dict, user: User) -> bool:
    if user.admin or not t["project"] or user.email in (t["createdBy"], t["assignee"]):
        return True
    return project_role(t["project"]["id"], user) in ("Owner", "Editor")


def can_delete(t: dict, user: User) -> bool:
    return user.admin or user.email == t["createdBy"] or (
        bool(t["project"]) and project_role(t["project"]["id"], user) == "Owner")


def visible_tasks(user: User) -> list[dict]:
    with store().db() as db:
        rows = db.execute("SELECT t.*, p.doc AS project_doc FROM tasks t LEFT JOIN projects p ON p.id = t.project_id").fetchall()
        mine = {r["project_id"] for r in db.execute("SELECT project_id FROM members WHERE email = ?", (user.email,))}
    out = []
    for r in rows:
        t = _view(r)
        if user.admin or not t["project"] or t["project"]["id"] in mine or user.email in (t["createdBy"], t["assignee"]):
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
    return f"TSK-{n + 1:03d}"


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
    """Everyone the Tasks data knows by email (assignees, creators, authors, project members)."""
    with store().db() as db:
        rows = db.execute("SELECT assignee AS email, assignee_name AS name FROM tasks WHERE assignee != '' "
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


def _found(tid: str, user: User) -> dict:
    t = get_task(tid)
    if not t or not can_see(t, user):
        raise HTTPException(404, "Task not found.")
    return t


@router.get("/")
def page(request: Request):
    if not current_user(request, TASKS):
        return HTMLResponse(f"<!doctype html><title>Tasks</title><p style='font-family:system-ui;padding:40px'>"
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
    for t in tasks:
        t["comments"] = st["counts"].get(t["id"], 0)
        t["openQuestions"] = len(st["openQuestions"].get(t["id"], []))
        t["waitingOnMe"] = user.email in st["openQuestions"].get(t["id"], [])
    if project:
        tasks = [t for t in tasks if t["project"] and t["project"]["id"] == project]
    open_ = [t for t in tasks if t["status"] != "Done"]
    views = {
        "mine": [t for t in open_ if t["assignee"] == user.email],
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
    mine = sum(1 for t in tasks if t["assignee"] == user.email and t["status"] != "Done")
    waiting = sum(1 for t in tasks if user.email in st["openQuestions"].get(t["id"], []))
    overdue = sum(1 for t in tasks if t["assignee"] == user.email and t["status"] != "Done"
                  and t["due"] and t["due"] < date.today().isoformat())
    return {"mine": mine, "waiting": waiting, "overdue": overdue, "badge": mine + waiting}


def _clean(body: TaskIn, t: dict | None) -> dict:
    out = {}
    if body.title is not None:
        if not body.title.strip():
            raise HTTPException(422, "Give the task a title.")
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
        raise HTTPException(422, "Give the task a title.")
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
    return {**t, "activity": _activity(tid), "canChange": can_change(t, user), "canDelete": can_delete(t, user)}


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
    kw = {"mentions": mentions_in(text, _people_known())}
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
    """People the Tasks data already knows, for when the portal's people list isn't available."""
    return {"people": sorted(_people_known(), key=lambda p: p["name"].lower())}
