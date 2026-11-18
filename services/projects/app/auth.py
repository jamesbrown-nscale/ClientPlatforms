"""Who is signed in to Projects, Tasks, Team and the personal inbox (one service, four tools on the site).

The front door does the signing in (the portal's accounts, Cloudflare Access, or Google IAP) and
vouches for the person on every request; access.py checks that. Which projects they can see is
each project's own Members list (see main.py).
"""
from dataclasses import dataclass

from fastapi import HTTPException, Request

from . import access

TOOL = "projects"
TASKS = "tasks"
TEAM = "team"
INBOX = "inbox"
CHANGES = "changes"


@dataclass
class User:
    email: str
    name: str
    admin: bool   # a Client Platforms admin: every project, as an owner


def check_settings() -> None:
    access.check_settings()


def current_user(request: Request, tool: str = TOOL) -> User | None:
    who = access.identity(request, tool)
    if not who:
        return None
    return User(who["email"], who["name"], bool(who.get("admin")))


def require_user(request: Request) -> User:
    return _require(request, TOOL)


def require_task_user(request: Request) -> User:
    return _require(request, TASKS)


def require_team_user(request: Request) -> User:
    return _require(request, TEAM)


def require_inbox_user(request: Request) -> User:
    return _require(request, INBOX)


def require_changes_user(request: Request) -> User:
    return _require(request, CHANGES)


def _require(request: Request, tool: str) -> User:
    user = current_user(request, tool)
    if not user:
        raise HTTPException(401, access.sign_in_message())
    if request.method not in ("GET", "HEAD") and request.headers.get("x-requested-with") != "nscale-admin":
        raise HTTPException(403, "Missing request header.")   # only our own page sends it (no CORS)
    return user
