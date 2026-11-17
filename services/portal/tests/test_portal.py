import os
os.environ["DEV_USER"] = "james.brown@nscale.com"
from fastapi.testclient import TestClient  # noqa: E402
from app import main  # noqa: E402


def test_home_lists_tools():
    c = TestClient(main.app)
    r = c.get("/")
    assert r.status_code == 200 and "Hi James" in r.text
    assert 'href="/notifications/"' in r.text and 'href="/elevation/"' in r.text
    assert c.get("/api/me").json()["email"] == "james.brown@nscale.com"
    assert c.get("/signout", follow_redirects=False).headers["location"].endswith("CLEAR_LOGIN_COOKIE")
    assert c.get("/static/nscale-wordmark.svg").status_code == 200
