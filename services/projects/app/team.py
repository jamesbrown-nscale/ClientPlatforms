"""Nscale Team: the team, their roles and responsibilities, 1-2-1s and feedback, on Client Platforms at /team/.

It runs in the Projects service (same database), so each person's projects, tasks and recent
activity come from there.

Each team member has a role (job title), team, manager, start date, location and a short "about",
plus a list of responsibilities. Their record also holds:

    one_to_one    1-2-1s: date, what was discussed, how they're doing (1-5), actions, next 1-2-1
    well          what's going well
    improve       what needs improvement
    manager_note  manager notes

Who sees what:
    everyone with the Team tool   the team list, roles, responsibilities, and the projects and
                                  activity they themselves can see
    the person themself           their own 1-2-1s, going well and needs improvement (read only)
    their manager and admins      all of it, including manager notes, and can change it
Manager notes are never shown to the person they're about. Only admins add people with any manager
or remove them; a manager can add someone who reports to them.
"""
import json
import re
import uuid
from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel, Field

from . import access, modules, tasks
from .auth import TEAM, User, current_user, require_team_user
from .store import Store, now

KINDS = {"one_to_one": "1-2-1", "well": "What's going well", "improve": "Needs improvement", "manager_note": "Manager note"}
PERSONAL = ("one_to_one", "well", "improve")      # the person can read these
EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

router = APIRouter()
_store: Store | None = None
STATIC = None


def setup(store: Store, static_dir) -> None:
    global _store, STATIC
    _store, STATIC = store, static_dir


def db():
    return _store.db()


# ---------- People ----------

def _member(r) -> dict:
    return {"email": r["email"], "name": r["name"], "title": r["title"], "team": r["team"], "manager": r["manager"],
            "startDate": r["start_date"], "location": r["location"], "about": r["about"],
            "updatedAt": r["updated_at"], "updatedBy": r["updated_by"]}


def get_member(email: str) -> dict | None:
    with db() as d:
        r = d.execute("SELECT * FROM team_members WHERE email = ?", (email.lower(),)).fetchone()
    return _member(r) if r else None


def all_members() -> list[dict]:
    with db() as d:
        return [_member(r) for r in d.execute("SELECT * FROM team_members ORDER BY name COLLATE NOCASE")]


def manages(user: User, m: dict) -> bool:
    return user.admin or (bool(m.get("manager")) and m["manager"] == user.email)


def rights(user: User, m: dict) -> dict:
    mgr = manages(user, m)
    me = m["email"] == user.email
    return {"edit": mgr, "personal": mgr or me, "managerNotes": mgr, "isMe": me, "isManager": mgr and not user.admin,
            "delete": user.admin}


def responsibilities(email: str) -> list[dict]:
    with db() as d:
        return [{"id": r["id"], "title": r["title"], "detail": r["detail"]}
                for r in d.execute("SELECT * FROM team_responsibilities WHERE member = ? ORDER BY position, rowid", (email,))]


# ---------- Work: projects, tasks, activity (only what the viewer may see) ----------

def work(email: str, viewer: User, days: int = 30) -> dict:
    since = (date.today() - timedelta(days=days)).isoformat()
    today = date.today().isoformat()
    with db() as d:
        mine = {r["project_id"]: r["role"] for r in d.execute("SELECT project_id, role FROM members WHERE email = ?", (email,))}
        seen_by_viewer = {r["project_id"] for r in d.execute("SELECT project_id FROM members WHERE email = ?", (viewer.email,))}
        item_rows = d.execute("SELECT i.project_id, i.module, i.ref, i.data, i.updated_at, i.created_at, i.created_by "
                              "FROM items i WHERE i.updated_by = ? AND i.updated_at >= ? ORDER BY i.updated_at DESC LIMIT 60",
                              (email, since)).fetchall()
        act_rows = d.execute("SELECT a.kind, a.body, a.progress, a.data, a.created_at, a.task_id FROM task_activity a "
                             "WHERE a.author = ? AND a.created_at >= ? ORDER BY a.created_at DESC LIMIT 60",
                             (email, since)).fetchall()
    visible = lambda pid: viewer.admin or pid in seen_by_viewer or viewer.email == email and pid in mine  # noqa: E731

    projects = []
    for pid, role in mine.items():
        if not visible(pid):
            continue
        p = _store.project(pid)
        if not p:
            continue
        s = modules.summarize(_store.items(pid), today, p)
        projects.append({"id": pid, "name": p["name"], "status": p.get("status"), "role": role,
                         "rag": s["status"]["overallRag"], "phase": s["status"]["phase"],
                         "openActions": s["actions"]["open"], "openRisks": s["risks"]["open"],
                         "next": s["milestones"]["next"]})
    projects.sort(key=lambda p: (p["status"] != "active", p["name"].lower()))

    tlist = [t for t in tasks.visible_tasks(viewer) if t["assignee"] == email]
    open_tasks = sorted((t for t in tlist if t["status"] != "Done"), key=lambda t: (t["due"] or "9999", t["createdAt"]))
    done_recent = [t for t in tlist if t["status"] == "Done" and (t["doneAt"] or "")[:10] >= since]

    activity = []
    names = {p["id"]: p["name"] for p in projects}
    for r in item_rows:
        if not visible(r["project_id"]):
            continue
        mod = modules.BY_KEY.get(r["module"], {})
        data = json.loads(r["data"])
        title = str(data.get(mod.get("titleField", "title")) or "").split("\n")[0][:120]
        verb = "added" if r["created_at"] == r["updated_at"] and r["created_by"] == email else "updated"
        activity.append({"at": r["updated_at"], "kind": "project", "text": f"{verb} {mod.get('singular', 'record').lower()} "
                         f"{r['ref'] + ' ' if r['ref'] else ''}{title}".strip(),
                         "where": names.get(r["project_id"]) or (_store.project(r["project_id"]) or {}).get("name", ""),
                         "link": f"/projects/#/p/{r['project_id']}/{r['module']}"})
    task_by_id = {t["id"]: t for t in tasks.visible_tasks(viewer)}
    for r in act_rows:
        t = task_by_id.get(r["task_id"])
        if not t:
            continue
        what = {"comment": "commented on", "question": "asked a question on", "answer": "answered a question on",
                "update": "posted an update on", "change": "changed"}.get(r["kind"], r["kind"])
        extra = f" ({r['progress']}%)" if r["kind"] == "update" and r["progress"] is not None else ""
        activity.append({"at": r["created_at"], "kind": "task", "text": f"{what} {t['ref']} {t['title']}{extra}",
                         "where": (t["project"] or {}).get("name", ""), "link": f"/tasks/#view=all&task={t['id']}"})
    activity.sort(key=lambda a: a["at"], reverse=True)
    return {"projects": projects, "openTasks": open_tasks[:50], "doneRecently": len(done_recent),
            "overdue": sum(1 for t in open_tasks if t["due"] and t["due"] < today), "activity": activity[:40], "days": days}


# ---------- API ----------

class MemberIn(BaseModel):
    email: str | None = Field(default=None, max_length=254)
    name: str | None = Field(default=None, max_length=100)
    title: str | None = Field(default=None, max_length=120)
    team: str | None = Field(default=None, max_length=80)
    manager: str | None = Field(default=None, max_length=254)
    startDate: str | None = None
    location: str | None = Field(default=None, max_length=80)
    about: str | None = Field(default=None, max_length=2000)


class Responsibility(BaseModel):
    id: str | None = None
    title: str = Field(max_length=200)
    detail: str = Field(default="", max_length=2000)


class ResponsibilitiesIn(BaseModel):
    items: list[Responsibility] = Field(default_factory=list, max_length=100)


class EntryIn(BaseModel):
    kind: str | None = None
    date: str | None = None
    body: str | None = Field(default=None, max_length=20000)
    data: dict | None = None


def _found(email: str) -> dict:
    m = get_member(email)
    if not m:
        raise HTTPException(404, "Not on the team.")
    return m


@router.get("/")
def page(request: Request):
    if not current_user(request, TEAM):
        return HTMLResponse(f"<!doctype html><title>Team</title><p style='font-family:system-ui;padding:40px'>"
                            f"{access.sign_in_message()}</p>", status_code=403)
    return FileResponse(STATIC / "team.html", headers={"Cache-Control": "no-store"})


@router.get("/api/me")
def me(user: User = Depends(require_team_user)):
    reports = [m["email"] for m in all_members() if m["manager"] == user.email]
    return {"email": user.email, "name": user.name, "admin": user.admin, "reports": reports,
            "canAdd": user.admin or bool(reports) or get_member(user.email) is not None, "kinds": KINDS}


@router.get("/api/members")
def list_members(user: User = Depends(require_team_user)):
    members = all_members()
    by_email = {m["email"]: m for m in members}
    with db() as d:
        proj = {r["email"]: r["n"] for r in d.execute("SELECT email, COUNT(*) AS n FROM members GROUP BY email")}
        open_t = {r["assignee"]: r["n"] for r in d.execute(
            "SELECT assignee, COUNT(*) AS n FROM tasks WHERE status != 'Done' GROUP BY assignee")}
        last121 = {r["member"]: r["d"] for r in d.execute(
            "SELECT member, MAX(date) AS d FROM team_entries WHERE kind = 'one_to_one' GROUP BY member")}
        resp = {r["member"]: r["n"] for r in d.execute("SELECT member, COUNT(*) AS n FROM team_responsibilities GROUP BY member")}
    out = []
    for m in members:
        r = rights(user, m)
        out.append({**m, "managerName": (by_email.get(m["manager"]) or {}).get("name") or (access.display_name(m["manager"]) if m["manager"] else ""),
                    "projects": proj.get(m["email"], 0), "openTasks": open_t.get(m["email"], 0),
                    "responsibilities": resp.get(m["email"], 0),
                    "lastOneToOne": last121.get(m["email"]) if r["personal"] else None, "rights": r})
    return {"members": out, "teams": sorted({m["team"] for m in members if m["team"]})}


def _apply(m: dict, body: MemberIn) -> dict:
    for k in ("name", "title", "team", "location", "about"):
        v = getattr(body, k)
        if v is not None:
            m[k] = v.strip()
    if body.manager is not None:
        mg = body.manager.strip().lower()
        if mg and not EMAIL.match(mg):
            raise HTTPException(422, "Manager must be an email address.")
        if mg and mg == m.get("email"):
            raise HTTPException(422, "Someone can't be their own manager.")
        m["manager"] = mg
    if body.startDate is not None:
        if body.startDate and not DATE.match(body.startDate):
            raise HTTPException(422, "Start date must be a date.")
        m["startDate"] = body.startDate
    if not m.get("name"):
        raise HTTPException(422, "Give their name.")
    return m


def _save(m: dict, user: User, new: bool) -> None:
    t = now()
    with _store.lock, db() as d:
        if new:
            d.execute("INSERT INTO team_members (email, name, title, team, manager, start_date, location, about, created_at, updated_at, updated_by) "
                      "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                      (m["email"], m["name"], m.get("title", ""), m.get("team", ""), m.get("manager", ""), m.get("startDate", ""),
                       m.get("location", ""), m.get("about", ""), t, t, user.email))
        else:
            d.execute("UPDATE team_members SET name = ?, title = ?, team = ?, manager = ?, start_date = ?, location = ?, about = ?, "
                      "updated_at = ?, updated_by = ? WHERE email = ?",
                      (m["name"], m["title"], m["team"], m["manager"], m["startDate"], m["location"], m["about"], t, user.email, m["email"]))


@router.post("/api/members")
def add_member(body: MemberIn, user: User = Depends(require_team_user)):
    email = (body.email or "").strip().lower()
    if not EMAIL.match(email):
        raise HTTPException(422, "Enter their email address.")
    if get_member(email):
        raise HTTPException(409, f"{email} is already on the team.")
    m = _apply({"email": email, "name": (body.name or "").strip() or access.display_name(email), "manager": ""}, body)
    if not user.admin:
        if m["manager"] and m["manager"] != user.email:
            raise HTTPException(403, "You can add people who report to you. An admin can add anyone.")
        m["manager"] = user.email
    _save(m, user, new=True)
    return read_member(email, user)


@router.get("/api/members/{email}")
def read_member(email: str, user: User = Depends(require_team_user)):
    m = _found(email)
    r = rights(user, m)
    mgr = get_member(m["manager"]) if m["manager"] else None
    reports = [{"email": x["email"], "name": x["name"], "title": x["title"]} for x in all_members() if x["manager"] == m["email"]]
    return {**m, "managerName": (mgr or {}).get("name") or (access.display_name(m["manager"]) if m["manager"] else ""),
            "reports": reports, "responsibilities": responsibilities(m["email"]), "rights": r}


@router.patch("/api/members/{email}")
def update_member(email: str, body: MemberIn, user: User = Depends(require_team_user)):
    m = _found(email)
    if not manages(user, m):
        raise HTTPException(403, "Only their manager or an admin can change this.")
    if body.manager is not None and not user.admin and body.manager.strip().lower() != m["manager"]:
        raise HTTPException(403, "Only an admin can change someone's manager.")
    _save(_apply(m, body), user, new=False)
    return read_member(email, user)


@router.delete("/api/members/{email}")
def remove_member(email: str, user: User = Depends(require_team_user)):
    m = _found(email)
    if not user.admin:
        raise HTTPException(403, "Only an admin can take someone off the team.")
    with _store.lock, db() as d:
        d.execute("UPDATE team_members SET manager = '' WHERE manager = ?", (m["email"],))
        d.execute("DELETE FROM team_members WHERE email = ?", (m["email"],))
    return {"ok": True}


@router.put("/api/members/{email}/responsibilities")
def set_responsibilities(email: str, body: ResponsibilitiesIn, user: User = Depends(require_team_user)):
    m = _found(email)
    if not manages(user, m):
        raise HTTPException(403, "Only their manager or an admin can change responsibilities.")
    rows = [r for r in body.items if r.title.strip()]
    with _store.lock, db() as d:
        d.execute("DELETE FROM team_responsibilities WHERE member = ?", (m["email"],))
        for i, r in enumerate(rows):
            d.execute("INSERT INTO team_responsibilities (id, member, title, detail, position) VALUES (?, ?, ?, ?, ?)",
                      (r.id if r.id and re.fullmatch(r"[\w-]{1,64}", r.id) else uuid.uuid4().hex, m["email"],
                       r.title.strip(), r.detail.strip(), i))
    return {"responsibilities": responsibilities(m["email"])}


@router.get("/api/members/{email}/work")
def member_work(email: str, days: int = 30, user: User = Depends(require_team_user)):
    m = _found(email)
    return work(m["email"], user, max(1, min(days, 365)))


# ----- 1-2-1s, feedback, manager notes -----

def _entry(r) -> dict:
    return {"id": r["id"], "kind": r["kind"], "date": r["date"], "body": r["body"],
            "data": json.loads(r["data"]) if r["data"] else {}, "author": r["author"], "authorName": r["author_name"],
            "createdAt": r["created_at"], "updatedAt": r["updated_at"]}


def _can_read(kind: str, user: User, m: dict) -> bool:
    r = rights(user, m)
    return r["managerNotes"] if kind == "manager_note" else r["personal"]


def _clean_121(data: dict | None) -> dict:
    data = data or {}
    out = {}
    if data.get("wellbeing") not in (None, ""):
        try:
            w = int(data["wellbeing"])
        except (TypeError, ValueError):
            raise HTTPException(422, "How they're doing is a number from 1 to 5.")
        if not 1 <= w <= 5:
            raise HTTPException(422, "How they're doing is a number from 1 to 5.")
        out["wellbeing"] = w
    nxt = data.get("next") or ""
    if nxt and not DATE.match(nxt):
        raise HTTPException(422, "Next 1-2-1 must be a date.")
    out["next"] = nxt
    actions = []
    for a in (data.get("actions") or [])[:100]:
        text = str((a or {}).get("text", "")).strip()[:1000]
        if not text:
            continue
        due = (a or {}).get("due") or ""
        actions.append({"id": a.get("id") if isinstance(a.get("id"), str) and re.fullmatch(r"[\w-]{1,64}", a.get("id")) else uuid.uuid4().hex,
                        "text": text, "owner": str(a.get("owner") or "").strip()[:200],
                        "due": due if DATE.match(due) else "", "done": bool(a.get("done"))})
    out["actions"] = actions
    return out


@router.get("/api/members/{email}/entries")
def list_entries(email: str, user: User = Depends(require_team_user)):
    m = _found(email)
    kinds = [k for k in KINDS if _can_read(k, user, m)]
    if not kinds:
        raise HTTPException(403, "Only they, their manager and admins can see this.")
    with db() as d:
        rows = d.execute(f"SELECT * FROM team_entries WHERE member = ? AND kind IN ({','.join('?' * len(kinds))}) "
                         "ORDER BY date DESC, created_at DESC", (m["email"], *kinds)).fetchall()
    out = {k: [] for k in kinds}
    for r in rows:
        out[r["kind"]].append(_entry(r))
    return {"entries": out, "canWrite": manages(user, m)}


@router.post("/api/members/{email}/entries")
def add_entry(email: str, body: EntryIn, user: User = Depends(require_team_user)):
    m = _found(email)
    if body.kind not in KINDS:
        raise HTTPException(422, "Unknown kind of entry.")
    if not manages(user, m):
        raise HTTPException(403, "Only their manager or an admin can add this.")
    when = body.date or date.today().isoformat()
    if not DATE.match(when):
        raise HTTPException(422, "Give a date.")
    text = (body.body or "").strip()
    data = _clean_121(body.data) if body.kind == "one_to_one" else None
    if not text and not (data and data["actions"]):
        raise HTTPException(422, "Write something first.")
    eid, t = uuid.uuid4().hex, now()
    with _store.lock, db() as d:
        d.execute("INSERT INTO team_entries (id, member, kind, date, body, data, author, author_name, created_at, updated_at) "
                  "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                  (eid, m["email"], body.kind, when, text, json.dumps(data) if data else None, user.email, user.name, t, t))
    return list_entries(email, user)


@router.put("/api/members/{email}/entries/{eid}")
def update_entry(email: str, eid: str, body: EntryIn, user: User = Depends(require_team_user)):
    m = _found(email)
    if not manages(user, m):
        raise HTTPException(403, "Only their manager or an admin can change this.")
    with db() as d:
        r = d.execute("SELECT * FROM team_entries WHERE id = ? AND member = ?", (eid, m["email"])).fetchone()
    if not r:
        raise HTTPException(404, "Not found.")
    when = body.date if body.date is not None else r["date"]
    if not DATE.match(when or ""):
        raise HTTPException(422, "Give a date.")
    text = body.body.strip() if body.body is not None else r["body"]
    data = _clean_121(body.data) if r["kind"] == "one_to_one" and body.data is not None else (json.loads(r["data"]) if r["data"] else None)
    with _store.lock, db() as d:
        d.execute("UPDATE team_entries SET date = ?, body = ?, data = ?, updated_at = ? WHERE id = ?",
                  (when, text, json.dumps(data) if data else None, now(), eid))
    return list_entries(email, user)


@router.delete("/api/members/{email}/entries/{eid}")
def delete_entry(email: str, eid: str, user: User = Depends(require_team_user)):
    m = _found(email)
    if not manages(user, m):
        raise HTTPException(403, "Only their manager or an admin can delete this.")
    with _store.lock, db() as d:
        if d.execute("DELETE FROM team_entries WHERE id = ? AND member = ?", (eid, m["email"])).rowcount == 0:
            raise HTTPException(404, "Not found.")
    return list_entries(email, user)
