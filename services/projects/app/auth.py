"""Who is signed in to Projects.

The front door does the signing in (the portal's accounts, Cloudflare Access, or Google IAP) and
vouches for the person on every request; access.py checks that. Which projects they can see is
each project's own Members list (see main.py).
"""
from dataclasses import dataclass

from fastapi import HTTPException, Request

from . import access

TOOL = "projects"


@dataclass
class User:
    email: str
    name: str
    admin: bool   # a Client Platforms admin: every project, as an owner


def check_settings() -> None:
    access.check_settings()


def current_user(request: Request) -> User | None:
    who = access.identity(request, TOOL)
    if not who:
        return None
    return User(who["email"], who["name"], bool(who.get("admin")))


def require_user(request: Request) -> User:
    user = current_user(request)
    if not user:
        raise HTTPException(401, access.sign_in_message())
    if request.method not in ("GET", "HEAD") and request.headers.get("x-requested-with") != "nscale-admin":
        raise HTTPException(403, "Missing request header.")   # only our own page sends it (no CORS)
    return user
