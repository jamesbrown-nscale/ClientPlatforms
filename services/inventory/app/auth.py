"""Who is signed in to Users and Devices (one service, two tools on the site).

The front door does the signing in (the portal: the company identity provider, or passwords) and
vouches for the person on every request, with their level on that tool; access.py checks that.

    view    see everything
    edit    import and tag users, sync devices now
    admin   also the Jamf and Intune connections (Devices admins; Settings → Connections)
"""
from dataclasses import dataclass

from fastapi import HTTPException, Request

from . import access

DEVICES = "devices"
USERS = "users"
ORG = "org"                  # Org chart: its own access, read only
DEPARTMENTS = "departments"  # Departments: its own access, read only
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


def current_user(request: Request, tool: str) -> User | None:
    who = access.identity(request, tool)
    if not who:
        return None
    return User(who["email"], who["name"], who.get("level") or ("admin" if who.get("admin") else "edit"))


def _require(request: Request, tool: str, need: str = "view") -> User:
    user = current_user(request, tool)
    if not user:
        raise HTTPException(401, access.sign_in_message())
    if request.method not in ("GET", "HEAD") and request.headers.get("x-requested-with") != "nscale-admin":
        raise HTTPException(403, "Missing request header.")   # only our own pages send it (no CORS)
    if RANK.get(user.level, 0) < RANK[need]:
        raise HTTPException(403, "Only Devices admins can change the connections." if need == "admin"
                            else "You can view this but not change it. Ask for edit access.")
    return user


def devices_viewer(request: Request) -> User:
    return _require(request, DEVICES)


def devices_editor(request: Request) -> User:
    return _require(request, DEVICES, "edit")


def devices_admin(request: Request) -> User:
    return _require(request, DEVICES, "admin")


def users_viewer(request: Request) -> User:
    return _require(request, USERS)


def users_editor(request: Request) -> User:
    return _require(request, USERS, "edit")


def org_viewer(request: Request) -> User:
    return _require(request, ORG)


def departments_viewer(request: Request) -> User:
    return _require(request, DEPARTMENTS)
