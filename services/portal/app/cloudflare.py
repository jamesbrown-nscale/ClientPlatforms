"""Who Cloudflare Access says is signed in (AUTH_MODE=cloudflare).

Cloudflare Access signs people in before a request reaches the site and adds
Cf-Access-Jwt-Assertion: a JWT signed by your Zero Trust team. This checks the signature (the team's
published keys), the issuer (the team domain), the audience (this Access application's AUD tag)
and the expiry. Only then is the email in it believed.

Settings:
  CF_ACCESS_TEAM_DOMAIN   e.g. nscale.cloudflareaccess.com
  CF_ACCESS_AUD           the Application Audience (AUD) tag from the Access application's overview
"""
import os
import threading
import time

TEAM = os.environ.get("CF_ACCESS_TEAM_DOMAIN", "").strip().removeprefix("https://").rstrip("/")
AUD = os.environ.get("CF_ACCESS_AUD", "").strip()
HEADER = "cf-access-jwt-assertion"


def check_settings() -> None:
    if not TEAM or not AUD:
        raise RuntimeError("Set CF_ACCESS_TEAM_DOMAIN (e.g. nscale.cloudflareaccess.com) and CF_ACCESS_AUD "
                           "(the Access application's AUD tag).")


class _Keys:
    """The team's signing certificates, fetched at most once an hour (Cloudflare rotates them)."""
    def __init__(self):
        self._certs: dict = {}
        self._at = 0.0
        self._lock = threading.Lock()

    def get(self, refresh: bool = False) -> dict:
        with self._lock:
            age = time.time() - self._at
            if not self._certs or age > 3600 or (refresh and age > 300):
                import httpx
                r = httpx.get(f"https://{TEAM}/cdn-cgi/access/certs", timeout=10)
                r.raise_for_status()
                self._certs = {c["kid"]: c["cert"] for c in r.json().get("public_certs", [])}
                self._at = time.time()
            return self._certs


keys = _Keys()


def email(request) -> str | None:
    """The verified email of whoever Cloudflare Access signed in, or None."""
    token = request.headers.get(HEADER)
    if not token:
        return None
    from google.auth import jwt
    for attempt in (False, True):   # a key we haven't seen yet: fetch the keys again once
        try:
            claims = jwt.decode(token, certs=keys.get(refresh=attempt))   # signature and expiry
            break
        except Exception:
            if attempt:
                return None
    aud = claims.get("aud")
    if AUD not in (aud if isinstance(aud, list) else [aud]):   # Cloudflare sends a list
        return None
    if claims.get("iss") != f"https://{TEAM}" or claims.get("type", "app") != "app":
        return None
    return (claims.get("email") or "").lower() or None
