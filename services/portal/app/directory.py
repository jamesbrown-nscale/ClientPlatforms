"""Searching the company directory when an admin adds someone (AUTH_MODE=oidc).

Read-only: it looks people up by name or email so the admin picks the right person (and their proper
name and email come with them). It never adds anyone by itself; the admin still chooses who gets in
and with which roles.

DIRECTORY=google   Google Workspace, through the Admin SDK Directory API (read-only users scope).
    A service account with domain-wide delegation for
    https://www.googleapis.com/auth/admin.directory.user.readonly, acting as a Workspace admin:
      DIRECTORY_GOOGLE_ADMIN          the admin it acts as (any admin who can see users)
      GOOGLE_APPLICATION_CREDENTIALS  the service account's key file, or leave it unset on a Google
                                      Cloud VM to use the VM's own service account with no key file
                                      (it then needs "Service Account Token Creator" on itself and
                                      the IAM Credentials API turned on)
      DIRECTORY_GOOGLE_CUSTOMER       default my_customer (your own Workspace)

DIRECTORY=okta     Okta's Users API, read-only.
      OKTA_ORG_URL     e.g. https://nscale.okta.com (default: from OIDC_ISSUER)
      OKTA_API_TOKEN   an API token from a read-only admin (Security, API, Tokens)

Unset: no search; admins type the email.
"""
import os
import re
import threading
from urllib.parse import urlsplit

KIND = os.environ.get("DIRECTORY", "").strip().lower()
GOOGLE_ADMIN = os.environ.get("DIRECTORY_GOOGLE_ADMIN", "").strip()
GOOGLE_CUSTOMER = os.environ.get("DIRECTORY_GOOGLE_CUSTOMER", "my_customer").strip() or "my_customer"
GOOGLE_SCOPE = "https://www.googleapis.com/auth/admin.directory.user.readonly"
GOOGLE_USERS = "https://admin.googleapis.com/admin/directory/v1/users"
_issuer = urlsplit(os.environ.get("OIDC_ISSUER", ""))
OKTA_ORG = (os.environ.get("OKTA_ORG_URL", "").strip() or
            (f"{_issuer.scheme}://{_issuer.netloc}" if "okta" in _issuer.netloc else "")).rstrip("/")
OKTA_TOKEN = os.environ.get("OKTA_API_TOKEN", "").strip()
LIMIT = 10


class DirectoryError(Exception):
    """Something the admin can be told: the search didn't work."""


def name() -> str:
    """What the directory is called on the page, or "" when there isn't one."""
    if KIND == "google" and GOOGLE_ADMIN:
        return "Google Workspace"
    if KIND == "okta" and OKTA_ORG and OKTA_TOKEN:
        return "Okta"
    return ""


def _words(q: str) -> list[str]:
    """The search, as words safe to put in a directory query (letters, digits, . _ - ' @)."""
    return [w for w in re.sub(r"[^\w.\-'@]+", " ", q.lower()).split() if w][:4]


def _matches(p: dict, words: list[str]) -> bool:
    hay = f"{p['name']} {p['email']}".lower()
    return all(w in hay for w in words)


def search(q: str) -> list[dict]:
    """Up to LIMIT active people matching every word of q: [{email, name, title}], by name."""
    words = _words(q)
    if not words:
        return []
    found = _google(words) if KIND == "google" else _okta(words) if KIND == "okta" else []
    seen, out = set(), []
    for p in found:
        if p["email"] and p["email"] not in seen and _matches(p, words):
            seen.add(p["email"])
            out.append(p)
    return sorted(out, key=lambda p: (p["name"].lower(), p["email"]))[:LIMIT]


# ---------- Google Workspace ----------

class _GoogleToken:
    """An access token for the Directory API, acting as DIRECTORY_GOOGLE_ADMIN, refreshed as needed."""
    def __init__(self):
        self._creds = None
        self._lock = threading.Lock()

    def _make(self):
        from google.oauth2 import service_account
        key = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "")
        if key:
            return service_account.Credentials.from_service_account_file(key, scopes=[GOOGLE_SCOPE], subject=GOOGLE_ADMIN)
        # No key file: sign as the VM's own service account through the IAM Credentials API.
        import google.auth
        from google.auth import iam
        from google.auth.transport.requests import Request
        base, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
        base.refresh(Request())
        email = getattr(base, "service_account_email", "")
        if not email or email == "default":
            raise DirectoryError("Couldn't tell which service account this server runs as. Set GOOGLE_APPLICATION_CREDENTIALS.")
        return service_account.Credentials(iam.Signer(Request(), base, email), email, "https://oauth2.googleapis.com/token",
                                           scopes=[GOOGLE_SCOPE], subject=GOOGLE_ADMIN)

    def get(self) -> str:
        from google.auth.transport.requests import Request
        with self._lock:
            if self._creds is None:
                self._creds = self._make()
            if not self._creds.valid:
                self._creds.refresh(Request())
            return self._creds.token


google_token = _GoogleToken()


def _google_person(u: dict) -> dict:
    n = u.get("name") or {}
    return {"email": (u.get("primaryEmail") or "").lower(),
            "name": n.get("fullName") or " ".join(x for x in (n.get("givenName"), n.get("familyName")) if x) or "",
            "title": next((o.get("title", "") for o in u.get("organizations") or [] if o.get("primary")), "")}


def _google(words: list[str]) -> list[dict]:
    import httpx
    try:
        token = google_token.get()
    except DirectoryError:
        raise
    except Exception:
        raise DirectoryError("Couldn't sign in to the Google Workspace directory. Check the directory settings.")
    w = words[0].replace("'", "")
    out = []
    # The Directory API ANDs the parts of one query, so ask by first name, last name and email in turn.
    for field in ("givenName", "familyName", "email"):
        try:
            r = httpx.get(GOOGLE_USERS, timeout=10, headers={"Authorization": f"Bearer {token}"},
                          params={"customer": GOOGLE_CUSTOMER, "query": f"{field}:{w}* isSuspended=false",
                                  "maxResults": 20, "projection": "basic", "viewType": "admin_view"})
        except Exception:
            raise DirectoryError("The Google Workspace directory isn't reachable just now.")
        if r.status_code in (401, 403):
            raise DirectoryError("The Google Workspace directory turned down the search. Check the service account's access.")
        if r.status_code != 200:
            raise DirectoryError("The Google Workspace directory search didn't work. Try again.")
        out += [_google_person(u) for u in r.json().get("users", []) if not u.get("suspended") and not u.get("archived")]
    return out


# ---------- Okta ----------

def _okta(words: list[str]) -> list[dict]:
    import httpx
    w = words[0].replace('"', "")
    search = (f'status eq "ACTIVE" and (profile.firstName sw "{w}" or profile.lastName sw "{w}" '
              f'or profile.email sw "{w}" or profile.login sw "{w}")')
    try:
        r = httpx.get(f"{OKTA_ORG}/api/v1/users", timeout=10, params={"search": search, "limit": 25},
                      headers={"Authorization": f"SSWS {OKTA_TOKEN}", "Accept": "application/json"})
    except Exception:
        raise DirectoryError("Okta isn't reachable just now.")
    if r.status_code in (401, 403):
        raise DirectoryError("Okta turned down the search. Check OKTA_API_TOKEN.")
    if r.status_code != 200:
        raise DirectoryError("The Okta search didn't work. Try again.")
    out = []
    for u in r.json():
        p = u.get("profile") or {}
        out.append({"email": (p.get("email") or p.get("login") or "").lower(),
                    "name": p.get("displayName") or " ".join(x for x in (p.get("firstName"), p.get("lastName")) if x),
                    "title": p.get("title") or ""})
    return out
