"""Sign-in with an OpenID Connect provider (AUTH_MODE=oidc): Google now, Okta later, by settings alone.

The flow (authorization code with PKCE):
  /login/oidc      makes a state, a nonce and a PKCE verifier, keeps them in a short-lived signed
                   cookie, and sends the browser to the provider.
  /auth/callback   checks the state, swaps the code for tokens (with the client secret), and checks
                   the ID token: signature (the provider's published keys), issuer, audience, expiry,
                   nonce, a verified email, and the email's domain. Only then is the email believed.
The portal's people list then decides whether they get in at all (main.py).

Settings:
  OIDC_ISSUER           https://accounts.google.com, or https://nscale.okta.com (your Okta org)
  OIDC_CLIENT_ID        from the provider's app / OAuth client
  OIDC_CLIENT_SECRET
  OIDC_PROVIDER_NAME    shown on the button: "Sign in with Google" / "Sign in with Okta"
  OIDC_ALLOWED_DOMAINS  comma separated, default nscale.com: other emails are turned away
  OIDC_SCOPES           default "openid email profile"
  OIDC_REDIRECT_URL     optional: the full /auth/callback address, if it can't be worked out from
                        the site address (PUBLIC_URL / the request)
"""
import base64
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from urllib.parse import urlencode

ISSUER = os.environ.get("OIDC_ISSUER", "").strip().rstrip("/")
CLIENT_ID = os.environ.get("OIDC_CLIENT_ID", "").strip()
CLIENT_SECRET = os.environ.get("OIDC_CLIENT_SECRET", "").strip()
PROVIDER = os.environ.get("OIDC_PROVIDER_NAME", "").strip() or ("Google" if "google" in ISSUER else "Okta" if "okta" in ISSUER else "single sign-on")
DOMAINS = {d.strip().lower().lstrip("@") for d in os.environ.get("OIDC_ALLOWED_DOMAINS", "nscale.com").split(",") if d.strip()}
SCOPES = os.environ.get("OIDC_SCOPES", "openid email profile").strip()
REDIRECT_URL = os.environ.get("OIDC_REDIRECT_URL", "").strip()
COOKIE = "cp_oidc"
FLOW_SECONDS = 600


def check_settings() -> None:
    missing = [n for n, v in (("OIDC_ISSUER", ISSUER), ("OIDC_CLIENT_ID", CLIENT_ID), ("OIDC_CLIENT_SECRET", CLIENT_SECRET)) if not v]
    if missing:
        raise RuntimeError(f"AUTH_MODE=oidc needs {', '.join(missing)}.")
    local = ISSUER.startswith(("http://localhost:", "http://127.0.0.1:"))   # a test provider on this machine
    if not ISSUER.startswith("https://") and not local:
        raise RuntimeError("OIDC_ISSUER must be an https:// address, e.g. https://accounts.google.com")


class SignInError(Exception):
    """Something the person can be told: the sign-in didn't work, and why, in plain words."""


# ---------- The provider's published settings and keys ----------

class _Provider:
    """The provider's discovery document and signing keys, fetched when needed and kept an hour."""
    def __init__(self):
        self._conf: dict = {}
        self._keys = None
        self._conf_at = self._keys_at = 0.0
        self._lock = threading.Lock()

    def conf(self) -> dict:
        with self._lock:
            if not self._conf or time.time() - self._conf_at > 3600:
                import httpx
                r = httpx.get(f"{ISSUER}/.well-known/openid-configuration", timeout=10)
                r.raise_for_status()
                c = r.json()
                if c.get("issuer", "").rstrip("/") != ISSUER:
                    raise SignInError("The sign-in provider's settings don't match OIDC_ISSUER.")
                self._conf, self._conf_at = c, time.time()
            return self._conf

    def keys(self, refresh: bool = False):
        from authlib.jose import JsonWebKey
        with self._lock:
            age = time.time() - self._keys_at
            if self._keys is None or age > 3600 or (refresh and age > 60):
                import httpx
                r = httpx.get(self._conf["jwks_uri"], timeout=10)
                r.raise_for_status()
                self._keys, self._keys_at = JsonWebKey.import_key_set(r.json()), time.time()
            return self._keys


provider = _Provider()


# ---------- The short-lived cookie that carries the flow across the round trip ----------

def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _sign(secret: str, data: dict) -> str:
    body = _b64(json.dumps(data, separators=(",", ":")).encode())
    return body + "." + _b64(hmac.new(secret.encode(), b"oidc." + body.encode(), hashlib.sha256).digest())


def _unsign(secret: str, value: str) -> dict | None:
    try:
        body, sig = value.split(".", 1)
        good = _b64(hmac.new(secret.encode(), b"oidc." + body.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(sig, good):
            return None
        data = json.loads(_unb64(body))
        return data if data.get("exp", 0) > time.time() else None
    except Exception:
        return None


# ---------- The flow ----------

def start(secret: str, redirect_uri: str, nxt: str) -> tuple[str, str]:
    """(where to send the browser, the cookie value to set)."""
    conf = provider.conf()
    state, nonce, verifier = secrets.token_urlsafe(24), secrets.token_urlsafe(24), secrets.token_urlsafe(48)
    challenge = _b64(hashlib.sha256(verifier.encode()).digest())
    params = {"response_type": "code", "client_id": CLIENT_ID, "redirect_uri": redirect_uri, "scope": SCOPES,
              "state": state, "nonce": nonce, "code_challenge": challenge, "code_challenge_method": "S256"}
    if "accounts.google.com" in ISSUER and len(DOMAINS) == 1:
        params["hd"] = next(iter(DOMAINS))   # Google: only offer accounts in our Workspace
    cookie = _sign(secret, {"s": state, "n": nonce, "v": verifier, "r": redirect_uri, "next": nxt, "exp": time.time() + FLOW_SECONDS})
    return conf["authorization_endpoint"] + "?" + urlencode(params), cookie


def finish(secret: str, cookie: str, state: str, code: str) -> tuple[str, str, str]:
    """Checks the provider's answer. (email, name, where to go next), or SignInError."""
    flow = _unsign(secret, cookie or "")
    if not flow:
        raise SignInError("The sign-in took too long or was started in another window. Try again.")
    if not state or not hmac.compare_digest(state, flow["s"]):
        raise SignInError("The sign-in didn't match the one started here. Try again.")
    if not code:
        raise SignInError("The sign-in provider didn't sign you in. Try again.")
    conf = provider.conf()
    import httpx
    r = httpx.post(conf["token_endpoint"], timeout=10, auth=(CLIENT_ID, CLIENT_SECRET),
                   data={"grant_type": "authorization_code", "code": code, "redirect_uri": flow["r"], "code_verifier": flow["v"]})
    if r.status_code != 200:
        raise SignInError("The sign-in provider turned down the sign-in. Try again.")
    id_token = r.json().get("id_token")
    if not id_token:
        raise SignInError("The sign-in provider didn't say who you are. Try again.")
    claims = verify_id_token(id_token, flow["n"])
    email = (claims.get("email") or "").strip().lower()
    name = (claims.get("name") or " ".join(x for x in (claims.get("given_name"), claims.get("family_name")) if x) or "").strip()
    return email, name, flow["next"]


def verify_id_token(token: str, nonce: str) -> dict:
    from authlib.jose import jwt
    from authlib.jose.errors import JoseError
    opts = {"iss": {"essential": True, "values": [ISSUER, ISSUER + "/"]}, "aud": {"essential": True, "value": CLIENT_ID},
            "exp": {"essential": True}, "nonce": {"essential": True, "value": nonce}}
    claims = None
    for refresh in (False, True):   # a key we haven't seen yet: fetch the keys again once
        try:
            claims = jwt.decode(token, provider.keys(refresh), claims_options=opts)
            claims.validate(leeway=60)
            break
        except (JoseError, ValueError):
            if refresh:
                raise SignInError("Your sign-in couldn't be checked. Try again.")
    if claims.get("azp") and claims["azp"] != CLIENT_ID:
        raise SignInError("Your sign-in was meant for another app.")
    email = (claims.get("email") or "").strip().lower()
    if not email:
        raise SignInError("The sign-in provider didn't share your email address.")
    if claims.get("email_verified") is False or str(claims.get("email_verified", "true")).lower() == "false":
        raise SignInError("Your email address isn't verified with the sign-in provider.")
    if DOMAINS and email.rsplit("@", 1)[-1] not in DOMAINS:
        raise SignInError(f"Sign in with your {' or '.join(sorted(DOMAINS))} account.")
    return dict(claims)
