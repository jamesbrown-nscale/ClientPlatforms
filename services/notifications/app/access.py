"""Who is signed in, as vouched for by the Client Platforms front door.

The same file is in every service (portal, notifications, elevation, restart); a test checks the
copies match. Two ways to run:

AUTH_MODE=local (default, Portainer / Docker)
    People sign in on the portal with an email and password. Caddy asks the portal about every
    request to a tool (forward_auth to /auth/check). If the person is signed in and allowed that
    tool, the portal answers with X-CP-User: a short-lived token, signed with AUTH_SECRET, naming
    the person and the tool it was issued for. Caddy adds it to the request; the tool checks the
    signature, the tool name and the expiry. With no valid token, the request is nobody's.

AUTH_MODE=cloudflare
    As local, but Cloudflare Access signs people in instead of a password: the portal checks
    Cloudflare's signed Cf-Access-Jwt-Assertion header to see who it is, then vouches for them
    to the tools with X-CP-User exactly as above. The portal's people list still says who may
    use which tool.

AUTH_MODE=iap (Google Cloud)
    Google's Identity-Aware Proxy signs people in at the load balancer and adds
    X-Goog-IAP-JWT-Assertion; the tool checks Google's signature, issuer, IAP_AUDIENCE and the
    Workspace domain.

DEV_USER (local development only) pretends this person is signed in. Never set it on a server.
"""
import base64
import hashlib
import hmac
import json
import os
import threading
import time

AUTH_MODE = os.environ.get("AUTH_MODE", "local").lower()
AUTH_SECRET = os.environ.get("AUTH_SECRET", "")
IAP_AUDIENCE = os.environ.get("IAP_AUDIENCE", "")
ALLOWED_DOMAIN = os.environ.get("ALLOWED_DOMAIN", "nscale.com").lower()
DEV_USER = os.environ.get("DEV_USER", "").lower()
HEADER = "x-cp-user"
TOKEN_SECONDS = 120
IAP_HEADER = "x-goog-iap-jwt-assertion"
IAP_CERTS_URL = "https://www.gstatic.com/iap/verify/public_key"


def check_settings() -> None:
    if DEV_USER:
        return
    if AUTH_MODE == "iap" and not IAP_AUDIENCE:
        raise RuntimeError("Set IAP_AUDIENCE (the load balancer backend service this app sits behind).")
    if AUTH_MODE != "iap" and len(AUTH_SECRET) < 32:
        raise RuntimeError("Set AUTH_SECRET to the same long random string (32+ characters) on every service.")


def display_name(email: str) -> str:
    return email.split("@")[0].replace(".", " ").title()


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _mac(body: str, secret: str) -> str:
    return _b64(hmac.new(secret.encode(), b"cp-user-v1." + body.encode(), hashlib.sha256).digest())


def issue(email: str, name: str, tool: str, admin: bool = False, secret: str | None = None) -> str:
    """A token saying this person may use this tool for the next TOKEN_SECONDS. Portal only."""
    body = _b64(json.dumps({"email": email, "name": name, "tool": tool, "admin": bool(admin),
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


class _IapKeys:
    """Google's IAP signing keys, fetched at most once an hour."""
    def __init__(self):
        self._certs: dict = {}
        self._at = 0.0
        self._lock = threading.Lock()

    def get(self) -> dict:
        with self._lock:
            if not self._certs or time.time() - self._at > 3600:
                import httpx
                r = httpx.get(IAP_CERTS_URL, timeout=10)
                r.raise_for_status()
                self._certs, self._at = r.json(), time.time()
            return self._certs


iap_keys = _IapKeys()


def _iap(request) -> dict | None:
    token = request.headers.get(IAP_HEADER)
    if not token:
        return None
    from google.auth import jwt
    try:
        claims = jwt.decode(token, certs=iap_keys.get(), audience=IAP_AUDIENCE)
    except Exception:
        return None
    email = (claims.get("email") or "").lower()
    if claims.get("iss") != "https://cloud.google.com/iap" or (claims.get("hd") or "").lower() != ALLOWED_DOMAIN:
        return None
    return {"email": email, "name": display_name(email), "admin": False} if email else None


def identity(request, tool: str) -> dict | None:
    """{email, name, admin} for whoever made this request to `tool`, or None."""
    if DEV_USER:
        return {"email": DEV_USER, "name": display_name(DEV_USER), "admin": True}
    if AUTH_MODE == "iap":
        return _iap(request)
    claims = verify(request.headers.get(HEADER, ""), tool)
    if not claims:
        return None
    return {"email": claims["email"].lower(), "name": claims.get("name") or display_name(claims["email"]),
            "admin": bool(claims.get("admin"))}


def sign_in_message() -> str:
    if AUTH_MODE == "iap":
        return f"Sign in with your {ALLOWED_DOMAIN} Google account at the Client Platforms address."
    if AUTH_MODE == "cloudflare":
        return "Open this through the Client Platforms address, where Cloudflare Access signs you in."
    return "Sign in at the Client Platforms home page."
