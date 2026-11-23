"""Jira (Cloud): the site's connection to Jira, under Settings → Connections → Integrations.

Signed in with an Atlassian account's email and an API token (id.atlassian.com → Security → API tokens),
as Basic auth, and checked with GET /rest/api/3/myself and, if a project key is set, GET
/rest/api/3/project/{key}. Kept in the portal's settings ("jira"), the token encrypted with a key made
from AUTH_SECRET; only people with the "Manage integrations" site ability can see or change it.
"""
import base64
import hashlib
import re
from datetime import datetime, timezone
from urllib.parse import urlsplit

import httpx
from cryptography.fernet import Fernet, InvalidToken

TIMEOUT = 20
KEY = re.compile(r"^[A-Z][A-Z0-9_]{0,19}$")
EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class JiraError(Exception):
    pass


def _fernet(secret: str) -> Fernet:
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(b"cp-portal-jira." + (secret or "dev-only").encode()).digest()))


def seal(token: str, secret: str) -> str:
    return _fernet(secret).encrypt(token.encode()).decode()


def unseal(sealed: str, secret: str) -> str:
    try:
        return _fernet(secret).decrypt(sealed.encode()).decode() if sealed else ""
    except InvalidToken:
        return ""   # AUTH_SECRET changed: the token has to be entered again


def site(v: str) -> str:
    v = (v or "").strip().rstrip("/")
    if v and "://" not in v:
        v = "https://" + v
    u = urlsplit(v)
    if u.scheme != "https" or not u.hostname or u.username or u.password or u.query or u.fragment or u.path not in ("", "/"):
        raise ValueError("The Jira address must look like https://yourcompany.atlassian.net")
    return f"https://{u.netloc}"


def account(v: str) -> str:
    v = (v or "").strip().lower()
    if not EMAIL.match(v):
        raise ValueError("Enter the email address of the Atlassian account the API token belongs to.")
    return v


def project(v: str) -> str:
    v = (v or "").strip().upper()
    if v and not KEY.match(v):
        raise ValueError("A Jira project key is letters and numbers starting with a letter, e.g. CP.")
    return v


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def check(client: httpx.Client, url: str, email: str, token: str, key: str = "") -> str:
    """Signs in; returns what to show ("Signed in as … Project CP: …"). Raises JiraError."""
    if not token:
        raise JiraError("There's no API token saved (or it can't be read any more): enter it again.")
    auth, headers = (email, token), {"Accept": "application/json"}
    try:
        r = client.get(f"{url}/rest/api/3/myself", auth=auth, headers=headers, timeout=TIMEOUT)
    except httpx.HTTPError as e:
        raise JiraError(f"Couldn't reach Jira: {type(e).__name__}. Is the address right?")
    if r.status_code in (401, 403):
        raise JiraError(f"Jira turned down the sign-in ({r.status_code}). Check the email and the API token.")
    if r.status_code >= 400:
        raise JiraError(f"Jira answered {r.status_code} to the sign-in.")
    try:
        me = r.json()
    except ValueError:
        raise JiraError("Jira didn't answer with JSON. Is the address right?")
    who = str(me.get("displayName") or me.get("emailAddress") or email)
    note = f"Signed in as {who}."
    if key:
        try:
            r = client.get(f"{url}/rest/api/3/project/{key}", auth=auth, headers=headers, timeout=TIMEOUT)
        except httpx.HTTPError as e:
            raise JiraError(f"Couldn't reach Jira: {type(e).__name__}.")
        if r.status_code == 404:
            raise JiraError(f"Signed in as {who}, but there's no project {key} that this account can see.")
        if r.status_code >= 400:
            raise JiraError(f"Signed in as {who}, but Jira answered {r.status_code} when reading project {key}.")
        try:
            note += f" Project {key}: {r.json().get('name') or key}."
        except ValueError:
            note += f" Project {key}."
    return note
