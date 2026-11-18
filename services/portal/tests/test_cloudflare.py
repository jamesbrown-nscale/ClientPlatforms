"""AUTH_MODE=cloudflare: Cloudflare Access says who it is; the portal's people list says what they get."""
import importlib
import time

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from google.auth import crypt, jwt

import tests.test_portal as base  # noqa: F401  (sets the test environment first)
from app import access, cloudflare, main

TEAM, AUD = "nscale.cloudflareaccess.com", "a" * 64


@pytest.fixture(params=["cloudflare"])
def cf(request, monkeypatch, tmp_path):
    monkeypatch.setenv("AUTH_MODE", request.param)
    monkeypatch.setenv("CF_ACCESS_TEAM_DOMAIN", TEAM)
    monkeypatch.setenv("CF_ACCESS_AUD", AUD)
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "cf.db"))
    monkeypatch.setenv("ADMIN_EMAIL", "james.brown@nscale.com")
    if request.param == "cloudflare":
        monkeypatch.delenv("ADMIN_PASSWORD")
    for m in (access, cloudflare, main):
        importlib.reload(m)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    signer = crypt.RSASigner.from_string(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                                           serialization.NoEncryption()).decode(), "kid1")
    monkeypatch.setattr(cloudflare.keys, "get", lambda refresh=False: {"kid1": pem})

    def h(email="james.brown@nscale.com", **kw):
        now = int(time.time())
        claims = {"iss": f"https://{TEAM}", "aud": [AUD], "email": email, "type": "app", "iat": now, "exp": now + 600, **kw}
        return {"cf-access-jwt-assertion": jwt.encode(signer, claims).decode()}

    with TestClient(main.app) as c:
        c.h = h
        yield c
    monkeypatch.undo()
    for m in (access, cloudflare, main):
        importlib.reload(m)


def restart_role(cf, h):
    r = cf.post("/api/roles", headers={**h(), **base.H}, json={"name": "Restart", "levels": {"restart": "edit"}})
    return r.json()["id"] if r.status_code == 200 else [x["id"] for x in cf.get("/api/roles", headers=h()).json()["roles"] if x["name"] == "Restart"][0]


def test_cloudflare_identity_and_people_list(cf):
    h = cf.h
    home = cf.get("/", headers=h())
    assert home.status_code == 200 and "Hi James" in home.text and 'href="/restart/"' in home.text
    assert cf.get("/login", follow_redirects=False).headers["location"] == "/"
    assert cf.get("/logout", follow_redirects=False).headers["location"] == "/cdn-cgi/access/logout"
    # Bad or missing tokens are nobody.
    assert cf.get("/").status_code == 403
    for bad in (h(aud=["other"]), h(iss="https://evil.cloudflareaccess.com"), h(exp=int(time.time()) - 5),
                {"cf-access-jwt-assertion": "junk"}):
        assert cf.get("/api/me", headers=bad).status_code == 401
    # Signed in at Cloudflare but not on the list: nothing.
    r = cf.get("/", headers=h("amy.chen@nscale.com"))
    assert r.status_code == 403 and "amy.chen@nscale.com" in r.text
    assert cf.get("/auth/check", headers={**h("amy.chen@nscale.com"), "x-forwarded-uri": "/elevation/"}).status_code == 401
    # An admin adds her without a password; she gets just her tool.
    assert cf.post("/api/users", headers={**h(), **base.H}, json={"email": "amy.chen@nscale.com", "roles": [restart_role(cf, h)]}).status_code == 200
    ok = cf.get("/auth/check", headers={**h("amy.chen@nscale.com"), "x-forwarded-uri": "/restart/"})
    assert ok.status_code == 200 and access.verify(ok.headers["x-cp-user"], "restart")["email"] == "amy.chen@nscale.com"
    assert cf.get("/auth/check", headers={**h("amy.chen@nscale.com"), "x-forwarded-uri": "/elevation/"}).status_code == 403
    # No passwords in this mode.
    assert cf.post("/login", data={"email": "amy.chen@nscale.com", "password": "x"}, follow_redirects=False).status_code == 401
    assert cf.post("/api/me/password", headers={**h(), **base.H}, json={"current": "x", "new": "y" * 12}).status_code == 400
    assert cf.get("/api/me", headers=h()).json()["passwords"] is False


@pytest.mark.parametrize("cf", ["local"], indirect=True)
def test_by_ip_with_a_password_and_through_cloudflare(cf):
    """AUTH_MODE=local with Cloudflare set up too: the same site by IP (password) and through Cloudflare."""
    h = cf.h
    assert cf.get("/api/me").status_code == 401
    assert cf.get("/", follow_redirects=False).headers["location"] == "/login"
    # Through Cloudflare: signed in by it.
    me = cf.get("/api/me", headers=h()).json()
    assert me["email"] == "james.brown@nscale.com" and me["passwords"] and me["cloudflare"]
    # A Cloudflare-only person needs no password; someone not on the list still gets nothing.
    assert cf.post("/api/users", headers={**h(), **base.H}, json={"email": "amy.chen@nscale.com", "roles": [restart_role(cf, h)]}).status_code == 200
    assert cf.get("/auth/check", headers={**h("amy.chen@nscale.com"), "x-forwarded-uri": "/restart/"}).status_code == 200
    assert cf.get("/", headers=h("bob@nscale.com")).status_code == 403
    assert cf.get("/logout", headers=h(), follow_redirects=False).headers["location"] == "/cdn-cgi/access/logout"
    # By IP: the password works.
    r = cf.post("/login", data={"email": "james.brown@nscale.com", "password": "first-admin-password"}, follow_redirects=False)
    assert r.status_code == 303 and cf.get("/api/me").json()["email"] == "james.brown@nscale.com"
    assert cf.get("/logout", follow_redirects=False).headers["location"] == "/login"


def test_placeholders_mean_not_set_up(monkeypatch):
    monkeypatch.setenv("CF_ACCESS_TEAM_DOMAIN", "REPLACE.cloudflareaccess.com")
    monkeypatch.setenv("CF_ACCESS_AUD", "REPLACE_WITH_APPLICATION_AUD_TAG")
    importlib.reload(cloudflare)
    assert cloudflare.TEAM == "" and cloudflare.AUD == ""
    monkeypatch.undo()
    importlib.reload(cloudflare)
