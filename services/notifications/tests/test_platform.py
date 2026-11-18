"""Running on Client Platforms: under /notifications, signed in at the front door (the portal, or Google IAP)."""
import importlib
import time

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient
from google.auth import crypt, jwt

import tests.test_server  # noqa: F401  (sets test environment before the app is imported)
from app import access, auth, main

AUDIENCE = "/projects/123/global/backendServices/456"


@pytest.fixture
def platform(monkeypatch):
    """Reloads the app as it runs on Client Platforms, then puts the normal one back."""
    monkeypatch.setenv("AUTH_MODE", "iap")
    monkeypatch.setenv("IAP_AUDIENCE", AUDIENCE)
    monkeypatch.setenv("BASE_PATH", "/notifications")
    monkeypatch.setenv("ADMIN_EMAILS", "james.brown@nscale.com")
    monkeypatch.setenv("DEV_ADMIN_USER", "")
    reload_app()

    key = ec.generate_private_key(ec.SECP256R1())
    pem = key.public_key().public_bytes(serialization.Encoding.PEM,
                                        serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    signer = crypt.ES256Signer.from_string(
        key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                          serialization.NoEncryption()).decode(), key_id="k1")
    monkeypatch.setattr(access.iap_keys, "get", lambda: {"k1": pem})

    def token(email="james.brown@nscale.com", hd="nscale.com", aud=AUDIENCE, iss="https://cloud.google.com/iap"):
        now = int(time.time())
        return jwt.encode(signer, {"iss": iss, "aud": aud, "email": email, "hd": hd,
                                   "sub": "accounts.google.com:1", "iat": now, "exp": now + 600}).decode()

    with TestClient(main.app) as c:
        c.token = token
        yield c

    for name in ("IAP_AUDIENCE", "BASE_PATH", "ADMIN_EMAILS"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AUTH_MODE", "google")
    monkeypatch.setenv("DEV_ADMIN_USER", "admin@nscale.com")
    reload_app()


def reload_app():
    for m in (access, auth, main):
        importlib.reload(m)


def h(c, **kw):
    return {"x-goog-iap-jwt-assertion": c.token(**kw)}


def test_served_under_base_path(platform):
    assert platform.get("/notifications", follow_redirects=False).headers["location"] == "/notifications/"
    assert platform.get("/healthz").json() == {"ok": True}
    assert platform.get("/notifications/healthz").json() == {"ok": True}
    assert platform.get("/notifications/static/nscale-icon.svg").status_code == 200
    page = platform.get("/notifications/", headers=h(platform))
    assert page.status_code == 200 and 'src="static/' in page.text
    assert platform.get("/api/admin/me", headers=h(platform)).status_code == 404   # nothing at the root


def test_iap_identity_is_checked(platform):
    me = platform.get("/notifications/api/admin/me", headers=h(platform))
    assert me.status_code == 200 and me.json()["email"] == "james.brown@nscale.com"
    # No header (someone going round the load balancer), wrong audience, issuer, domain or person.
    assert platform.get("/notifications/api/admin/me").status_code == 401
    assert platform.get("/notifications/api/admin/me", headers=h(platform, aud="/projects/1/x")).status_code == 401
    assert platform.get("/notifications/api/admin/me", headers=h(platform, iss="https://evil")).status_code == 401
    assert platform.get("/notifications/api/admin/me", headers=h(platform, hd="gmail.com")).status_code == 401
    assert platform.get("/notifications/api/admin/me",
                        headers=h(platform, email="amy.chen@nscale.com")).status_code == 401
    assert platform.get("/notifications/api/admin/me", headers={"x-goog-iap-jwt-assertion": "junk"}).status_code == 401
    r = platform.get("/notifications/", follow_redirects=False)
    assert r.status_code == 403


def test_sign_out_uses_iap(platform):
    # The portal's /logout signs out (and, on Google Cloud, clears IAP's cookie).
    r = platform.get("/notifications/logout", follow_redirects=False)
    assert r.headers["location"] == "/logout"


def test_devices_check_in_under_base_path(platform):
    body = {"deviceId": "dev-12345678", "hostname": "LT-LON-1", "username": "jb", "platform": "windows"}
    assert platform.post("/notifications/api/v1/checkin", json=body).status_code == 401
    r = platform.post("/notifications/api/v1/checkin", json=body, headers={"X-Nscale-Key": "test-key"})
    assert r.status_code == 200 and "messages" in r.json()


SECRET = "z" * 40


@pytest.fixture
def stack(monkeypatch):
    """As in the Docker stack: the portal vouches for the person with X-CP-User."""
    monkeypatch.setenv("AUTH_MODE", "local")
    monkeypatch.setenv("AUTH_SECRET", SECRET)
    monkeypatch.setenv("BASE_PATH", "/notifications")
    monkeypatch.setenv("ADMIN_EMAILS", "")
    monkeypatch.setenv("DEV_ADMIN_USER", "")
    reload_app()
    with TestClient(main.app) as c:
        yield c
    for name in ("AUTH_SECRET", "BASE_PATH", "ADMIN_EMAILS"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AUTH_MODE", "google")
    monkeypatch.setenv("DEV_ADMIN_USER", "admin@nscale.com")
    reload_app()


def test_front_door_identity_is_checked(stack):
    def hdr(tool="notifications", secret=SECRET):
        return {"x-cp-user": access.issue("amy.chen@nscale.com", "Amy Chen", tool, secret=secret)}

    me = stack.get("/notifications/api/admin/me", headers=hdr())
    assert me.status_code == 200 and me.json()["email"] == "amy.chen@nscale.com"
    assert stack.get("/notifications/api/admin/me").status_code == 401
    assert stack.get("/notifications/api/admin/me", headers=hdr(tool="elevation")).status_code == 401
    assert stack.get("/notifications/api/admin/me", headers=hdr(secret="q" * 40)).status_code == 401
    assert stack.get("/notifications/", follow_redirects=False).status_code == 403
    assert stack.get("/notifications/login", follow_redirects=False).headers["location"] == "/notifications/"
    assert stack.get("/notifications/logout", follow_redirects=False).headers["location"] == "/logout"
