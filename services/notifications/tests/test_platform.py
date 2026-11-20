"""Running on Client Platforms: under /notifications, signed in at the front door (the portal)."""
import importlib

import pytest
from fastapi.testclient import TestClient

import tests.test_server  # noqa: F401  (sets test environment before the app is imported)
from app import access, auth, main


def reload_app():
    for m in (access, auth, main):
        importlib.reload(m)


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
    monkeypatch.setenv("AUTH_MODE", "local")
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


def me(tool="notifications", secret=SECRET, email="amy.chen@nscale.com"):
    return {"x-cp-user": access.issue(email, "Amy Chen", tool, secret=secret)}


def test_served_under_base_path(stack):
    assert stack.get("/notifications", follow_redirects=False).headers["location"] == "/notifications/"
    assert stack.get("/healthz").json() == {"ok": True}
    assert stack.get("/notifications/healthz").json() == {"ok": True}
    assert stack.get("/notifications/static/nscale-icon.svg").status_code == 200
    page = stack.get("/notifications/", headers=me())
    assert page.status_code == 200 and 'src="static/' in page.text
    assert stack.get("/api/admin/me", headers=me()).status_code == 404   # nothing at the root


def test_admin_emails_narrow_it(stack, monkeypatch):
    monkeypatch.setattr(auth, "ADMIN_EMAILS", {"james.brown@nscale.com"})
    assert stack.get("/notifications/api/admin/me", headers=me()).status_code == 401
    assert stack.get("/notifications/api/admin/me", headers=me(email="james.brown@nscale.com")).status_code == 200


def test_devices_check_in_under_base_path(stack):
    body = {"deviceId": "dev-12345678", "hostname": "LT-LON-1", "username": "jb", "platform": "windows"}
    assert stack.post("/notifications/api/v1/checkin", json=body).status_code == 401
    r = stack.post("/notifications/api/v1/checkin", json=body, headers={"X-Nscale-Key": "test-key"})
    assert r.status_code == 200 and "messages" in r.json()
