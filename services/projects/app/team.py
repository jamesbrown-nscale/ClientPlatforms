"""Nscale Team: the team, their roles and responsibilities, 1-2-1s and feedback, on Client Platforms at /team/.

It runs in the Projects service (same database), so each person's projects, tasks and recent
activity come from there.

Each team member has a role (job title), department and sub team (their Division), manager, start
date, location and a short "about", plus a list of responsibilities. The departments and divisions to
pick from come from Users (the inventory service, read inside the stack at INVENTORY_URL), and adding
someone who's in Users fills in their name, role, department and division. Their record also holds:

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
import os
import re
import time
import uuid
from datetime import date, timedelta
from html import escape
from html.parser import HTMLParser
from urllib.parse import quote

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel, Field

from . import access, modules, tasks
from .inbox import notify, person_email
from .auth import TEAM, User, current_user, require_team_user
from .store import Store, now

KINDS = {"one_to_one": "1-2-1", "well": "What's going well", "improve": "Needs improvement", "manager_note": "Manager note"}
PERSONAL = ("one_to_one", "well", "improve")      # the person can read these
EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
INVENTORY_URL = os.environ.get("INVENTORY_URL", "http://inventory:8080").rstrip("/")

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
    return {"email": r["email"], "name": r["name"], "title": r["title"], "department": r["department"] or "", "team": r["team"],
            "manager": r["manager"],
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
    department: str | None = Field(default=None, max_length=200)
    team: str | None = Field(default=None, max_length=200)   # their sub team: the Division
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
    return {"members": out, "departments": departments(members)}


# ----- Departments and divisions, from Users -----

_cache: dict = {"at": 0.0, "departments": None}


def _inventory(path: str):
    """GET from the inventory service inside the stack, or None if it can't be reached."""
    try:
        r = httpx.get(INVENTORY_URL + path, timeout=3)
    except httpx.HTTPError:
        return None
    if r.status_code != 200:
        return None
    try:
        return r.json()
    except ValueError:
        return None


def user_departments() -> list[dict]:
    """Departments and their divisions on the Users page (kept a minute), or [] if Users isn't there."""
    if _cache["departments"] is None or time.time() - _cache["at"] > 60:
        data = _inventory("/internal/teams")
        _cache["departments"] = [{"name": d["name"], "divisions": [v["name"] for v in d.get("divisions") or []]}
                                 for d in (data or {}).get("departments") or [] if d.get("name")]
        _cache["at"] = time.time()
    return _cache["departments"]


def departments(members: list[dict]) -> list[dict]:
    """Every department and its sub teams: from Users, plus any the team uses that Users doesn't have.
    [{name, divisions: [..]}], sorted; "" holds sub teams with no department."""
    tree: dict[str, set] = {}
    for d in user_departments():
        tree.setdefault(d["name"], set()).update(d["divisions"])
    for m in members:
        if m["department"] or m["team"]:
            divs = tree.setdefault(m["department"], set())
            if m["team"]:
                divs.add(m["team"])
    return [{"name": k, "divisions": sorted(tree[k], key=str.lower)} for k in sorted(tree, key=lambda x: (x == "", x.lower()))]


@router.get("/api/lookup")
def lookup(email: str, user: User = Depends(require_team_user)):
    """Someone's name, role, department and division from Users, to fill in when adding them."""
    email = email.strip().lower()
    if not EMAIL.match(email):
        return {}
    data = _inventory("/internal/users/" + quote(email, safe="@")) or {}
    return {k: data.get(k, "") for k in ("name", "title", "department", "division")} if data else {}


def _apply(m: dict, body: MemberIn) -> dict:
    for k in ("name", "title", "department", "team", "location", "about"):
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
            d.execute("INSERT INTO team_members (email, name, title, department, team, manager, start_date, location, about, created_at, updated_at, updated_by) "
                      "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                      (m["email"], m["name"], m.get("title", ""), m.get("department", ""), m.get("team", ""), m.get("manager", ""), m.get("startDate", ""),
                       m.get("location", ""), m.get("about", ""), t, t, user.email))
        else:
            d.execute("UPDATE team_members SET name = ?, title = ?, department = ?, team = ?, manager = ?, start_date = ?, location = ?, about = ?, "
                      "updated_at = ?, updated_by = ? WHERE email = ?",
                      (m["name"], m["title"], m["department"], m["team"], m["manager"], m["startDate"], m["location"], m["about"], t, user.email, m["email"]))


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


# ---------- Formatted 1-2-1 notes ----------
# The notes box is a small rich-text editor. Only this formatting is kept; every attribute, link,
# style and script is dropped, so what's shown back is always safe.
KEEP = {"b": "b", "strong": "b", "u": "u", "i": "i", "em": "i", "p": "p", "div": "p", "br": "br", "ul": "ul", "ol": "ol", "li": "li"}
BLOCKS = {"p", "ul", "ol", "li"}


class _Clean(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.html, self.text, self.open, self.skip = [], [], [], 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.skip += 1
            return
        t = KEEP.get(tag)
        if not t:
            return
        if t == "br":
            self.html.append("<br>")
            self.text.append("\n")
            return
        if t in BLOCKS and self.text and not self.text[-1].endswith("\n"):
            self.text.append("\n")
        if t == "li":
            self.text.append("• ")
        self.html.append(f"<{t}>")
        self.open.append(t)

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self.skip = max(0, self.skip - 1)
            return
        t = KEEP.get(tag)
        if t and t != "br" and t in self.open:
            while self.open:   # close anything left open inside it
                x = self.open.pop()
                self.html.append(f"</{x}>")
                if x == t:
                    break
            if t in BLOCKS:
                self.text.append("\n")

    def handle_data(self, data):
        if not self.skip:
            self.html.append(escape(data))
            self.text.append(data)


def clean_notes(html: str) -> tuple[str, str]:
    """(safe HTML, plain text) from what the notes editor sent."""
    p = _Clean()
    p.feed(str(html or "")[:60000])
    p.close()
    out = "".join(p.html) + "".join(f"</{x}>" for x in reversed(p.open))
    out = re.sub(r"(<p>(\s|<br>)*</p>)+$", "", out)   # trailing empty lines
    text = re.sub(r"\n{3,}", "\n\n", "".join(p.text)).strip()
    return out, text


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
    if data.get("html"):
        out["html"], out["text"] = clean_notes(data["html"])
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
    if data and data.get("html") is not None:
        text = data.pop("text")
    if not text and not (data and data["actions"]):
        raise HTTPException(422, "Write something first.")
    eid, t = uuid.uuid4().hex, now()
    with _store.lock, db() as d:
        d.execute("INSERT INTO team_entries (id, member, kind, date, body, data, author, author_name, created_at, updated_at) "
                  "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                  (eid, m["email"], body.kind, when, text, json.dumps(data) if data else None, user.email, user.name, t, t))
    _tell(m, body.kind, when, text, (data or {}).get("actions", []), user)
    return list_entries(email, user)


def _tell(m: dict, kind: str, when: str, text: str, new_actions: list[dict], user: User) -> None:
    """The person hears about their 1-2-1s and feedback; action owners about their actions. Never manager notes."""
    tab = "one_to_one" if kind == "one_to_one" else "feedback"
    url = f"/team/#person={m['email']}&tab={tab}"
    if kind == "one_to_one":
        notify(m["email"], "one_to_one", f"{user.name} recorded your 1-2-1 on {tasks.nice_date(when)}", user, text[:200], url)
    elif kind in ("well", "improve"):
        notify(m["email"], "feedback", f"{user.name} added feedback: {KINDS[kind].lower()}", user, text, url)
    for a in new_actions:
        owner = person_email(a.get("owner", ""))
        if owner:
            whose = "your" if owner == m["email"] else f"{m['name']}'s"
            notify(owner, "action", f"New action from {whose} 1-2-1: {a['text']}", user,
                   f"Due {tasks.nice_date(a['due'])}" if a.get("due") else "", "/inbox/")


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
    if data and "text" in data:
        text = data.pop("text")
    with _store.lock, db() as d:
        d.execute("UPDATE team_entries SET date = ?, body = ?, data = ?, updated_at = ? WHERE id = ?",
                  (when, text, json.dumps(data) if data else None, now(), eid))
    if r["kind"] == "one_to_one" and data:   # tell the owners of actions added since
        before = {a["id"] for a in (json.loads(r["data"]) if r["data"] else {}).get("actions", [])}
        _tell(m, "", when, "", [a for a in data.get("actions", []) if a["id"] not in before], user)
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
