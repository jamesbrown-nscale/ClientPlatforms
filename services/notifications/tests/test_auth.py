"""Google sign-in rules: domain, admin list, session. Google itself is stubbed out."""
import pytest
from fastapi.responses import RedirectResponse
from fastapi.testclient import TestClient

import tests.test_server  # noqa: F401  (sets test environment before the app is imported)
from app import auth, main


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(auth, "DEV_ADMIN_USER", "")
    monkeypatch.setattr(auth, "ALLOWED_DOMAIN", "nscale.com")
    monkeypatch.setattr(auth, "ADMIN_EMAILS", {"james.brown@nscale.com"})
    seen = {}

    async def fake_redirect(request, redirect_uri, **kw):
        seen.update(kw, redirect_uri=redirect_uri)
        return RedirectResponse("https://accounts.google.com/o/oauth2/v2/auth?stub")

    monkeypatch.setattr(auth.oauth.google, "authorize_redirect", fake_redirect)
    with TestClient(main.app) as c:
        c.seen = seen
        yield c


def sign_in(c, monkeypatch, **userinfo):
    async def fake_token(request):
        return {"userinfo": {"email_verified": True, "hd": "nscale.com", "name": "James Brown",
                             "email": "james.brown@nscale.com", **userinfo}}
    monkeypatch.setattr(auth.oauth.google, "authorize_access_token", fake_token)
    return c.get("/auth/callback?code=x&state=y", follow_redirects=False)


def test_signed_out_goes_to_google(client):
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 307 and r.headers["location"] == "/login"
    assert client.get("/api/admin/me").status_code == 401
    r = client.get("/login", follow_redirects=False)
    assert r.headers["location"].startswith("https://accounts.google.com/")
    assert client.seen["hd"] == "nscale.com" and client.seen["redirect_uri"].endswith("/auth/callback")


def test_admin_signs_in(client, monkeypatch):
    r = sign_in(client, monkeypatch)
    assert r.status_code == 307 and r.headers["location"] == "/"
    me = client.get("/api/admin/me").json()
    assert me["email"] == "james.brown@nscale.com"
    # Removed from the admin list: locked out on the next request.
    monkeypatch.setattr(auth, "ADMIN_EMAILS", {"someone.else@nscale.com"})
    assert client.get("/api/admin/me").status_code == 401
    monkeypatch.setattr(auth, "ADMIN_EMAILS", {"james.brown@nscale.com"})
    client.get("/logout")
    assert client.get("/api/admin/me").status_code == 401


def test_other_domain_refused(client, monkeypatch):
    assert sign_in(client, monkeypatch, email="x@gmail.com", hd="").status_code == 403
    assert sign_in(client, monkeypatch, email="x@other.com", hd="other.com").status_code == 403
    assert client.get("/api/admin/me").status_code == 401


def test_not_on_admin_list_refused(client, monkeypatch):
    r = sign_in(client, monkeypatch, email="amy.chen@nscale.com")
    assert r.status_code == 403 and "allowed to send IT notifications" in r.text


def test_unverified_email_refused(client, monkeypatch):
    assert sign_in(client, monkeypatch, email_verified=False).status_code == 403
