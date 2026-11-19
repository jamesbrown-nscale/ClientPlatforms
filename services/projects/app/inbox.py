"""Personal notifications: each person's own inbox, on Client Platforms at /inbox/.

Projects, Team Planning and Team call notify() when something happens that someone should know about:

    task_assigned    a task was assigned to you
    task_comment     a comment or update on a task you're assigned to or created
    task_question    a question waiting on you
    task_answer      an answer to your question
    task_done        a task you created was marked done
    job_assigned     a job in a plan was assigned to you ("task" means a plan here)
    mention          someone @mentioned you
    one_to_one       a 1-2-1 about you was recorded
    action           an action from a 1-2-1 was given to you
    feedback         new "going well" or "needs improvement" about you
    project_member   you were added to a project, or your role changed

Nobody is notified of their own doing. Manager notes never notify anyone.

The inbox also lists your open 1-2-1 actions (whoever's 1-2-1 they came from) until you tick
them done; ticking one is the only change the action's owner can make to someone else's 1-2-1.
"""
import json
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel

from . import access
from .auth import INBOX, User, current_user, require_inbox_user
from .store import Store, now

KEEP_DAYS = 120   # read notifications older than this are cleared away

router = APIRouter()
_store: Store | None = None
STATIC = None


def setup(store: Store, static_dir) -> None:
    global _store, STATIC
    _store, STATIC = store, static_dir


def notify(recipients, kind: str, title: str, actor: User | None = None, body: str = "", link: str = "") -> int:
    """Tell each recipient (an email, or several) about something. Returns how many were told."""
    if _store is None:
        return 0
    if isinstance(recipients, str):
        recipients = [recipients]
    told = set()
    t = now()
    with _store.lock, _store.db() as d:
        for r in recipients:
            r = (r or "").strip().lower()
            if not r or "@" not in r or r in told or (actor and r == actor.email):
                continue
            told.add(r)
            d.execute("INSERT INTO notifications (id, recipient, kind, title, body, link, actor, actor_name, created_at) "
                      "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                      (uuid.uuid4().hex, r, kind, title[:300], (body or "")[:600], link, actor.email if actor else None,
                       actor.name if actor else None, t))
    return len(told)


def person_email(text: str) -> str:
    """The email for a name or email typed in a 1-2-1 action's owner box ('' if nobody matches)."""
    text = (text or "").strip()
    if not text:
        return ""
    if "@" in text:
        return text.lower()
    with _store.db() as d:
        r = d.execute("SELECT email FROM team_members WHERE lower(name) = lower(?)", (text,)).fetchone()
        if r:
            return r["email"]
        r = d.execute("SELECT email FROM members WHERE lower(name) = lower(?) LIMIT 1", (text,)).fetchone()
    return r["email"] if r else ""


def _cleanup(email: str) -> None:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=KEEP_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    with _store.lock, _store.db() as d:
        d.execute("DELETE FROM notifications WHERE recipient = ? AND read_at IS NOT NULL AND created_at < ?", (email, cutoff))


def _row(r) -> dict:
    return {"id": r["id"], "kind": r["kind"], "title": r["title"], "body": r["body"], "link": r["link"],
            "actor": r["actor"], "actorName": r["actor_name"], "createdAt": r["created_at"], "read": r["read_at"] is not None}


def _names(email: str, name: str) -> set[str]:
    """What a 1-2-1 action's owner box may say for this person: their email, name, or team name."""
    names = {email.lower(), (name or "").strip().lower()}
    with _store.db() as d:
        tm = d.execute("SELECT name FROM team_members WHERE email = ?", (email,)).fetchone()
    if tm:
        names.add(tm["name"].strip().lower())
    return names - {""}


def my_actions(email: str, name: str) -> list[dict]:
    """Open actions from any 1-2-1 that are this person's (by email or by their name)."""
    names = _names(email, name)
    with _store.db() as d:
        rows = d.execute("SELECT e.id, e.member, e.date, e.data, m.name AS member_name FROM team_entries e "
                         "JOIN team_members m ON m.email = e.member WHERE e.kind = 'one_to_one' AND e.data IS NOT NULL "
                         "ORDER BY e.date DESC").fetchall()
    out = []
    for r in rows:
        for a in (json.loads(r["data"]) or {}).get("actions", []):
            if not a.get("done") and (a.get("owner") or "").strip().lower() in names:
                out.append({"entry": r["id"], "id": a["id"], "text": a["text"], "due": a.get("due", ""), "date": r["date"],
                            "member": r["member"], "memberName": r["member_name"], "own": r["member"] == email})
    out.sort(key=lambda a: (a["due"] or "9999", a["date"]))
    return out


# ---------- API ----------

@router.get("/")
def page(request: Request):
    if not current_user(request, INBOX):
        return HTMLResponse(f"<!doctype html><title>Notifications</title><p style='font-family:system-ui;padding:40px'>"
                            f"{access.sign_in_message()}</p>", status_code=403)
    return FileResponse(STATIC / "inbox.html", headers={"Cache-Control": "no-store"})


@router.get("/api/count")
def count(user: User = Depends(require_inbox_user)):
    with _store.db() as d:
        n = d.execute("SELECT COUNT(*) FROM notifications WHERE recipient = ? AND read_at IS NULL", (user.email,)).fetchone()[0]
    return {"unread": n, "actions": len(my_actions(user.email, user.name))}


@router.get("/api/notifications")
def notifications(unread: bool = False, user: User = Depends(require_inbox_user)):
    _cleanup(user.email)
    q = "SELECT * FROM notifications WHERE recipient = ?" + (" AND read_at IS NULL" if unread else "") + \
        " ORDER BY created_at DESC, rowid DESC LIMIT 300"
    with _store.db() as d:
        rows = [_row(r) for r in d.execute(q, (user.email,))]
        n = d.execute("SELECT COUNT(*) FROM notifications WHERE recipient = ? AND read_at IS NULL", (user.email,)).fetchone()[0]
    return {"notifications": rows, "unread": n, "actions": my_actions(user.email, user.name),
            "me": {"email": user.email, "name": user.name}}


class ReadIn(BaseModel):
    read: bool = True


@router.post("/api/notifications/{nid}/read")
def mark(nid: str, body: ReadIn, user: User = Depends(require_inbox_user)):
    with _store.lock, _store.db() as d:
        n = d.execute("UPDATE notifications SET read_at = ? WHERE id = ? AND recipient = ?",
                      (now() if body.read else None, nid, user.email)).rowcount
    if not n:
        raise HTTPException(404, "Not found.")
    return count(user)


@router.post("/api/read-all")
def read_all(user: User = Depends(require_inbox_user)):
    with _store.lock, _store.db() as d:
        d.execute("UPDATE notifications SET read_at = ? WHERE recipient = ? AND read_at IS NULL", (now(), user.email))
    return count(user)


@router.delete("/api/notifications/{nid}")
def dismiss(nid: str, user: User = Depends(require_inbox_user)):
    with _store.lock, _store.db() as d:
        if not d.execute("DELETE FROM notifications WHERE id = ? AND recipient = ?", (nid, user.email)).rowcount:
            raise HTTPException(404, "Not found.")
    return count(user)


class DoneIn(BaseModel):
    done: bool = True


@router.post("/api/actions/{entry}/{aid}")
def tick_action(entry: str, aid: str, body: DoneIn, user: User = Depends(require_inbox_user)):
    """Tick off (or reopen) one of your 1-2-1 actions."""
    with _store.db() as d:
        r = d.execute("SELECT e.data, e.member, m.manager FROM team_entries e JOIN team_members m ON m.email = e.member "
                      "WHERE e.id = ? AND e.kind = 'one_to_one'", (entry,)).fetchone()
    if not r:
        raise HTTPException(404, "Not found.")
    data = json.loads(r["data"] or "{}")
    action = next((a for a in data.get("actions", []) if a["id"] == aid), None)
    if not action or (action.get("owner") or "").strip().lower() not in _names(user.email, user.name):
        raise HTTPException(404, "Not one of your actions.")
    action["done"] = body.done
    with _store.lock, _store.db() as d:
        d.execute("UPDATE team_entries SET data = ?, updated_at = ? WHERE id = ?", (json.dumps(data), now(), entry))
    if body.done and r["manager"]:
        notify(r["manager"], "action", f"{user.name} ticked off a 1-2-1 action", user, action["text"],
               f"/team/#person={r['member']}&tab=one_to_one")
    return {"actions": my_actions(user.email, user.name)}
