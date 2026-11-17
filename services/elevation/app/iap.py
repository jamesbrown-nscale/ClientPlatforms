"""Who is signed in, from Google Identity-Aware Proxy.

On clientplatforms.nscale.com, IAP at the load balancer signs people in with Google and only lets
through the groups granted access to this tool. It then adds a signed header,
X-Goog-IAP-JWT-Assertion, to every request. This module checks that header on every request:
signature (Google's ES256 keys), issuer, audience (this app's backend service, so a token for
another tool on the site is refused), expiry, and the Workspace domain.

That makes the app safe even if someone reaches it without going through the load balancer: with no
valid header, they are nobody.

Settings:
  IAP_AUDIENCE    /projects/<number>/global/backendServices/<id>   (set by deploy.sh)
  ALLOWED_DOMAIN  nscale.com
  VIEWER_EMAILS   optional comma-separated list, on top of IAP's group check
  DEV_USER        local development only: pretend this person is signed in. Never set on Cloud Run.
"""
import os
import threading
import time
from dataclasses import dataclass

import httpx
from fastapi import HTTPException, Request

IAP_AUDIENCE = os.environ.get("IAP_AUDIENCE", "")
ALLOWED_DOMAIN = os.environ.get("ALLOWED_DOMAIN", "nscale.com").lower()
VIEWER_EMAILS = {e.strip().lower() for e in os.environ.get("VIEWER_EMAILS", "").split(",") if e.strip()}
DEV_USER = os.environ.get("DEV_USER", "")
HEADER = "x-goog-iap-jwt-assertion"
CERTS_URL = "https://www.gstatic.com/iap/verify/public_key"
ISSUER = "https://cloud.google.com/iap"


@dataclass
class User:
    email: str

    @property
    def name(self) -> str:
        return self.email.split("@")[0].replace(".", " ").title()


class _Keys:
    """Google's IAP signing keys, fetched at most once an hour."""
    def __init__(self):
        self._certs: dict = {}
        self._at = 0.0
        self._lock = threading.Lock()

    def get(self) -> dict:
        with self._lock:
            if not self._certs or time.time() - self._at > 3600:
                r = httpx.get(CERTS_URL, timeout=10)
                r.raise_for_status()
                self._certs, self._at = r.json(), time.time()
            return self._certs


keys = _Keys()


def check_settings() -> None:
    if not IAP_AUDIENCE and not DEV_USER:
        raise RuntimeError("Set IAP_AUDIENCE (the load balancer backend service this app sits behind).")


def current_user(request: Request) -> User | None:
    if DEV_USER:
        return User(DEV_USER.lower())
    token = request.headers.get(HEADER)
    if not token:
        return None
    from google.auth import jwt
    try:
        claims = jwt.decode(token, certs=keys.get(), audience=IAP_AUDIENCE)
    except Exception:
        return None
    email = (claims.get("email") or "").lower()
    if claims.get("iss") != ISSUER or (claims.get("hd") or "").lower() != ALLOWED_DOMAIN or not email:
        return None
    if VIEWER_EMAILS and email not in VIEWER_EMAILS:
        return None
    return User(email)


def require_user(request: Request) -> User:
    user = current_user(request)
    if not user:
        raise HTTPException(401, "Sign in with your Nscale Google account.")
    return user
