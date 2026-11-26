"""Who is signed in, as vouched for by the Client Platforms front door.

The same file is in every service (portal, notifications, elevation, restart, projects); a test
checks the copies match.

People sign in on the portal: AUTH_MODE=oidc (the company identity provider, on the Google Cloud VM)
or AUTH_MODE=local (an email and password, for running it on your own machine). Either way Caddy asks
the portal about every request to a tool (forward_auth to /auth/check). If the person is signed in
and allowed that tool, the portal answers with X-CP-User: a short-lived token, signed with
AUTH_SECRET, naming the person and the tool it was issued for. Caddy adds it to the request; the tool
checks the signature, the tool name and the expiry. With no valid token, the request is nobody's.

DEV_USER (local development only) pretends this person is signed in. Never set it on a server.
"""
import base64
import hashlib
import hmac
import json
import os
import time

AUTH_MODE = os.environ.get("AUTH_MODE", "local").lower()
AUTH_SECRET = os.environ.get("AUTH_SECRET", "")
DEV_USER = os.environ.get("DEV_USER", "").lower()
HEADER = "x-cp-user"
TOKEN_SECONDS = int(os.environ.get("CP_TOKEN_SECONDS") or 120)   # tests set it longer: a slow build mustn't expire them


def check_settings() -> None:
    if DEV_USER:
        return
    if len(AUTH_SECRET) < 32:
        raise RuntimeError("Set AUTH_SECRET to the same long random string (32+ characters) on every service.")


def display_name(email: str) -> str:
    return email.split("@")[0].replace(".", " ").title()


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _mac(body: str, secret: str) -> str:
    return _b64(hmac.new(secret.encode(), b"cp-user-v1." + body.encode(), hashlib.sha256).digest())


def issue(email: str, name: str, tool: str, admin: bool = False, secret: str | None = None, level: str | None = None) -> str:
    """A token saying this person may use this tool for the next TOKEN_SECONDS, and how (view, edit
    or admin; admin also sets `admin`). Portal only."""
    level = level or ("admin" if admin else "edit")
    body = _b64(json.dumps({"email": email, "name": name, "tool": tool, "admin": bool(admin), "level": level,
                            "exp": int(time.time()) + TOKEN_SECONDS}, separators=(",", ":")).encode())
    return f"{body}.{_mac(body, secret or AUTH_SECRET)}"


def verify(token: str, tool: str, secret: str | None = None) -> dict | None:
    """The claims if the token is genuine, unexpired and issued for this tool; otherwise None."""
    secret = secret or AUTH_SECRET
    if not token or not secret:
        return None
    body, _, mac = token.partition(".")
    if not mac or not hmac.compare_digest(mac, _mac(body, secret)):
        return None
    try:
        claims = json.loads(_unb64(body))
    except ValueError:
        return None
    if claims.get("tool") != tool or int(claims.get("exp") or 0) < time.time() or not claims.get("email"):
        return None
    return claims


def identity(request, tool: str) -> dict | None:
    """{email, name, admin, level} for whoever made this request to `tool`, or None."""
    if DEV_USER:
        return {"email": DEV_USER, "name": display_name(DEV_USER), "admin": True, "level": "admin"}
    claims = verify(request.headers.get(HEADER, ""), tool)
    if not claims:
        return None
    return {"email": claims["email"].lower(), "name": claims.get("name") or display_name(claims["email"]),
            "admin": bool(claims.get("admin")), "level": claims.get("level") or ("admin" if claims.get("admin") else "edit")}


def sign_in_message() -> str:
    return "Sign in at the Client Platforms home page."
