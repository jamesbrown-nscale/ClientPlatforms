"""The portal: sign-in, the front-door check for each tool, people and settings."""
import os
import tempfile
from pathlib import Path

SECRET = "s" * 40
os.environ.update(AUTH_MODE="local", AUTH_SECRET=SECRET, DEV_USER="", ADMIN_EMAIL="james.brown@nscale.com",
                  ADMIN_PASSWORD="first-admin-password", DATABASE_PATH=os.path.join(tempfile.mkdtemp(), "p.db"))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import access, main  # noqa: E402

H = {"X-Requested-With": "nscale-admin"}
HTML = {"accept": "text/html,application/xhtml+xml"}


@pytest.fixture
def c():
    main._failures.clear()
    with TestClient(main.app) as client:
        yield client


def sign_in(c, email="james.brown@nscale.com", password="first-admin-password"):
    c.cookies.clear()
    return c.post("/login", data={"email": email, "password": password, "next": "/"}, follow_redirects=False)


def check(c, uri, method="GET", headers=None):
    return c.get("/auth/check", headers={"x-forwarded-uri": uri, "x-forwarded-method": method, **(headers or {})},
                 follow_redirects=False)


def add(c, email, tools=(), admin=False, password="a-good-password"):
    return c.post("/api/users", headers=H, json={"email": email, "password": password, "tools": list(tools), "admin": admin})


def test_first_admin_and_sign_in(c):
    assert c.get("/", follow_redirects=False).headers["location"] == "/login"
    assert "Sign in" in c.get("/login").text
    bad = sign_in(c, password="wrong-password")
    assert bad.status_code == 401 and "match an account" in bad.text
    r = sign_in(c)
    assert r.status_code == 303 and r.headers["location"] == "/"
    assert "httponly" in r.headers["set-cookie"].lower() and "samesite=lax" in r.headers["set-cookie"].lower()
    home = c.get("/")
    assert "Hi James" in home.text and 'href="/settings"' in home.text
    assert 'href="/notifications/"' in home.text and 'href="/elevation/"' in home.text and 'href="/restart/"' in home.text
    assert c.get("/static/nscale-wordmark.svg").status_code == 200
    assert c.get("/static/dialog.js").headers["cache-control"] == "no-cache"   # updates reach everyone at once
    out = c.get("/logout", follow_redirects=False)
    assert out.headers["location"] == "/login"
    assert c.get("/api/me").status_code == 401


def test_next_is_only_ever_this_site(c):
    for evil in ("//evil.com/x", "https://evil.com", "/\\evil.com"):
        r = c.post("/login", data={"email": "james.brown@nscale.com", "password": "first-admin-password", "next": evil},
                   follow_redirects=False)
        assert r.headers["location"] == "/"


def test_front_door_check(c):
    c.cookies.clear()
    # Not signed in: pages go to sign-in, API calls get 401.
    r = check(c, "/elevation/?days=7", headers=HTML)
    assert r.status_code == 307 and r.headers["location"] == "/login?next=/elevation/%3Fdays%3D7"
    assert check(c, "/elevation/api/elevations").status_code == 401
    sign_in(c)
    r = check(c, "/elevation/api/elevations")
    assert r.status_code == 200
    claims = access.verify(r.headers["x-cp-user"], "elevation", SECRET)
    assert claims["email"] == "james.brown@nscale.com" and claims["admin"] is True
    # A token for one tool is no good for another, and a tampered one is no good at all.
    assert access.verify(r.headers["x-cp-user"], "notifications", SECRET) is None
    assert access.verify(r.headers["x-cp-user"][:-2] + "xx", "elevation", SECRET) is None
    assert access.verify(r.headers["x-cp-user"], "elevation", "t" * 40) is None
    assert check(c, "/somewhere-else/").status_code == 403
    # Everyone has their own notifications, whatever tools they've been given.
    add(c, "nobody@nscale.com", tools=[])
    sign_in(c, "nobody@nscale.com", "a-good-password")
    r = check(c, "/inbox/api/count")
    assert r.status_code == 200 and access.verify(r.headers["x-cp-user"], "inbox", SECRET)["email"] == "nobody@nscale.com"
    assert check(c, "/tasks/").status_code == 403


def test_each_person_only_gets_their_tools(c):
    sign_in(c)
    assert add(c, "amy.chen@nscale.com", tools=["elevation"]).status_code == 200
    sign_in(c, "amy.chen@nscale.com", "a-good-password")
    home = c.get("/").text
    assert 'href="/elevation/"' in home and 'href="/notifications/"' not in home and '>Settings</a>' not in home
    assert check(c, "/elevation/").status_code == 200
    denied = check(c, "/notifications/", headers=HTML)
    assert denied.status_code == 403 and "IT Notifications" in denied.text
    assert c.get("/api/users").status_code == 403
    assert c.get("/api/settings").status_code == 403
    assert c.get("/settings").status_code == 200   # their own account


def test_people_admin(c):
    sign_in(c)
    assert add(c, "not-an-email").status_code == 422
    assert add(c, "bob@nscale.com", password="short").status_code == 422
    assert add(c, "bob@nscale.com", tools=["nope"]).status_code == 422
    assert add(c, "Bob@Nscale.com", tools=["restart"]).json()["email"] == "bob@nscale.com"
    assert add(c, "bob@nscale.com").status_code == 409
    assert c.post("/api/users", json={"email": "x@nscale.com", "password": "a-good-password"}).status_code == 403  # no header
    users = c.get("/api/users").json()["users"]
    assert {u["email"] for u in users} >= {"james.brown@nscale.com", "bob@nscale.com"}
    assert all("_password" not in u and "password" not in u for u in users)

    # Bob signs in; turning him off or resetting his password signs him out.
    bob = TestClient(main.app)
    sign_in(bob, "bob@nscale.com", "a-good-password")
    assert bob.get("/api/me").status_code == 200
    assert c.patch("/api/users/bob@nscale.com", headers=H, json={"disabled": True}).json()["disabled"] is True
    assert bob.get("/api/me").status_code == 401
    assert sign_in(bob, "bob@nscale.com", "a-good-password").status_code == 401
    c.patch("/api/users/bob@nscale.com", headers=H, json={"disabled": False, "tools": ["restart", "elevation"]})
    sign_in(bob, "bob@nscale.com", "a-good-password")
    assert c.post("/api/users/bob@nscale.com/password", headers=H, json={"password": "another-password"}).status_code == 200
    assert bob.get("/api/me").status_code == 401
    assert sign_in(bob, "bob@nscale.com", "another-password").status_code == 303

    # There is always an admin, and you can't lock yourself out.
    me = "/api/users/james.brown@nscale.com"
    assert c.patch(me, headers=H, json={"admin": False}).status_code == 400
    assert c.patch(me, headers=H, json={"disabled": True}).status_code == 400
    assert c.delete(me, headers=H).status_code == 400
    assert c.delete("/api/users/bob@nscale.com", headers=H).status_code == 200
    assert bob.get("/api/me").status_code == 401


def test_change_own_password(c):
    add_admin = sign_in(c)
    assert add_admin.status_code == 303
    add(c, "pat@nscale.com", tools=["restart"], password="pats-password-1")
    sign_in(c, "pat@nscale.com", "pats-password-1")
    assert c.post("/api/me/password", headers=H, json={"current": "wrong", "new": "pats-password-2"}).status_code == 400
    assert c.post("/api/me/password", headers=H, json={"current": "pats-password-1", "new": "pats-password-2"}).status_code == 200
    assert c.get("/api/me").status_code == 200   # still signed in here
    assert sign_in(c, "pat@nscale.com", "pats-password-2").status_code == 303


def test_settings(c):
    sign_in(c)
    s = c.get("/api/settings").json()
    assert [t["id"] for t in s["tools"]] == ["notifications", "elevation", "changes", "restart", "projects", "tasks", "team"]
    assert s["devices"]["elevation"] == "http://testserver/elevation"
    body = {"publicUrl": "https://cp.example.com/", "deviceUrl": "", "tools": [
        {"id": "notifications", "url": "https://notify.example.com/", "enabled": True},
        {"id": "elevation", "url": "/elevation/", "enabled": False},
        {"id": "restart", "url": "", "enabled": True}]}
    s = c.put("/api/settings", headers=H, json=body).json()
    assert s["publicUrl"] == "https://cp.example.com"
    assert s["devices"]["notifications"] == "https://cp.example.com/notifications"
    home = c.get("/").text
    assert 'href="https://notify.example.com/"' in home and 'href="/elevation/"' not in home and 'href="/restart/"' in home
    body["deviceUrl"] = "http://10.0.0.5:8080"
    c.put("/api/settings", headers=H, json=body)
    c.cookies.clear()
    assert c.get("/api/device-config").json()["restart"] == "http://10.0.0.5:8080/restart"   # public
    sign_in(c)
    for bad in ({"publicUrl": "javascript:alert(1)"}, {"deviceUrl": "ftp://x"},
                {"tools": [{"id": "restart", "url": "javascript:alert(1)"}]},
                {"tools": [{"id": "restart", "url": "//evil.com"}]}):
        assert c.put("/api/settings", headers=H, json={**body, **bad}).status_code == 422
    c.put("/api/settings", headers=H, json={"publicUrl": "", "deviceUrl": "", "tools": []})


def test_too_many_wrong_passwords(c):
    for _ in range(main.MAX_PER_EMAIL):
        sign_in(c, password="wrong-password")
    r = sign_in(c)
    assert r.status_code == 429


def test_access_module_is_the_same_everywhere():
    here = Path(__file__).resolve().parents[1] / "app" / "access.py"
    others = list(here.parents[2].glob("*/app/access.py"))
    if len(others) < 2:
        pytest.skip("only this service is here (Docker build)")
    for other in others:
        assert other.read_text() == here.read_text(), f"{other} differs from {here}"


def test_people_list_for_assigning(c):
    sign_in(c)
    add(c, "ann@nscale.com", tools=["tasks"])
    add(c, "ben@nscale.com", tools=["restart"])
    add(c, "off@nscale.com", tools=["tasks"])
    c.patch("/api/users/off@nscale.com", headers=H, json={"disabled": True})
    emails = [p["email"] for p in c.get("/api/people?tool=tasks").json()["people"]]
    assert "ann@nscale.com" in emails and "james.brown@nscale.com" in emails   # admins can use every tool
    assert "ben@nscale.com" not in emails and "off@nscale.com" not in emails
    assert "ben@nscale.com" in [p["email"] for p in c.get("/api/people").json()["people"]]
    assert set(c.get("/api/people").json()["people"][0]) == {"email", "name"}   # nothing else about them
    assert c.get("/api/people?tool=nope").status_code == 404
    c.cookies.clear()
    assert c.get("/api/people").status_code == 401
