"""Who is signed in to App Packaging.

The front door does the signing in (the portal: the company identity provider, or passwords) and
vouches for the person on every request, with their level on this tool; access.py checks that.

    view    see the packages and their commands, download them
    edit    also package apps, change a package's commands and notes, delete packages
    admin   the same as edit
"""
from dataclasses import dataclass

from fastapi import HTTPException, Request

from . import access

TOOL = "packaging"
RANK = {"view": 1, "edit": 2, "admin": 3}


@dataclass
class User:
    email: str
    name: str
    level: str

    @property
    def can_edit(self) -> bool:
        return RANK.get(self.level, 0) >= 2


def check_settings() -> None:
    access.check_settings()


def current_user(request: Request) -> User | None:
    who = access.identity(request, TOOL)
    if not who:
        return None
    return User(who["email"], who["name"], who.get("level") or ("admin" if who.get("admin") else "edit"))


def viewer(request: Request) -> User:
    user = current_user(request)
    if not user:
        raise HTTPException(401, access.sign_in_message())
    if request.method not in ("GET", "HEAD") and request.headers.get("x-requested-with") != "nscale-admin":
        raise HTTPException(403, "Missing request header.")   # only our own page sends it (no CORS)
    return user


def editor(request: Request) -> User:
    user = viewer(request)
    if not user.can_edit:
        raise HTTPException(403, "You can view packages but not make or change them. Ask for edit access.")
    return user
