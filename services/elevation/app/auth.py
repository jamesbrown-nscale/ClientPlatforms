"""Who is signed in to the elevation log.

The front door does the signing in (the portal: the company identity provider, or passwords) and
vouches for the person on every request; access.py checks that. VIEWER_EMAILS optionally narrows it
further to a fixed list.
"""
import os
from dataclasses import dataclass

from fastapi import HTTPException, Request

from . import access

TOOL = "elevation"
VIEWER_EMAILS = {e.strip().lower() for e in os.environ.get("VIEWER_EMAILS", "").split(",") if e.strip()}


@dataclass
class User:
    email: str
    name: str


def check_settings() -> None:
    access.check_settings()


def current_user(request: Request) -> User | None:
    who = access.identity(request, TOOL)
    if not who or (VIEWER_EMAILS and who["email"] not in VIEWER_EMAILS):
        return None
    return User(who["email"], who["name"])


def require_user(request: Request) -> User:
    user = current_user(request)
    if not user:
        raise HTTPException(401, access.sign_in_message())
    return user
