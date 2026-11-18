"""End-to-end tests of the server against both stores.

    pip install -r requirements.txt -r requirements-dev.txt
    pytest -q

The Firestore store runs against mock-firestore, an in-memory stand-in for the Firestore client.
"""
import os
import tempfile

os.environ.setdefault("AUTH_MODE", "google")   # the standalone setup; test_platform covers Client Platforms
os.environ.setdefault("ENROLLMENT_KEY", "test-key")
os.environ.setdefault("DEV_ADMIN_USER", "admin@nscale.com")
os.environ.setdefault("DATABASE_PATH", os.path.join(tempfile.mkdtemp(), "t.db"))

import pytest
from fastapi.testclient import TestClient

from app import main
from app.store import FirestoreStore, SqliteStore

K = {"X-Nscale-Key": "test-key"}
A = {"X-Requested-With": "nscale-admin"}


@pytest.fixture(params=["sqlite", "firestore"])
def client(request, tmp_path, monkeypatch):
    if request.param == "sqlite":
        s = SqliteStore(str(tmp_path / "t.db"))
    else:
        from mockfirestore import MockFirestore
        s = FirestoreStore(MockFirestore())
    monkeypatch.setattr(main, "store", s)
    with TestClient(main.app) as c:
        yield c


def checkin(c, host, user, platform="windows", events=()):
    r = c.post("/api/v1/checkin", headers=K, json={
        "deviceId": f"guid-{host}", "hostname": host, "username": user, "upn": f"{user}@nscale.com",
        "platform": platform, "osVersion": "test", "appVersion": "1.0.0", "events": list(events)})
    assert r.status_code == 200, r.text
    return r.json()


def send(c, **kw):
    r = c.post("/api/admin/messages", headers=A, json={"subject": "Hi", **kw})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def ids(resp):
    return [m["id"] for m in resp["messages"]]


def test_rejects_bad_key(client):
    assert client.post("/api/v1/checkin", headers={"X-Nscale-Key": "nope"}, json={}).status_code == 401


def test_admin_post_needs_header(client):
    assert client.post("/api/admin/messages", json={"subject": "x"}).status_code == 403


def test_delivery_targeting_and_receipts(client):
    checkin(client, "LT-LON-0142", "james.brown")
    checkin(client, "MB-LON-0007", "sam.ito", "macos")

    everyone = send(client, subject="VPN tonight", bodyRich="<b>Down</b> at 22:00")
    laptops = send(client, subject="Restart", priority="high", audience=["LT-LON-*"])
    mac_only = send(client, subject="Mac", priority="low", platforms=["macos"])

    win = checkin(client, "LT-LON-0142", "james.brown")
    assert set(ids(win)) == {everyone, laptops}
    m = next(x for x in win["messages"] if x["id"] == everyone)
    assert m["bodyRich"] == "<b>Down</b> at 22:00" and m["body"] == "Down at 22:00"
    assert set(ids(checkin(client, "MB-LON-0007", "sam.ito", "macos"))) == {everyone, mac_only}

    # Receipts: delivered stops redelivery; acknowledged implies opened and delivered.
    win = checkin(client, "LT-LON-0142", "james.brown", events=[
        {"messageId": everyone, "event": "delivered"},
        {"messageId": laptops, "event": "acknowledged", "at": "2026-11-17T21:30:00Z"}])
    assert ids(win) == []

    detail = client.get(f"/api/admin/messages/{laptops}").json()
    assert (detail["targeted"], detail["delivered"], detail["opened"], detail["acknowledged"]) == (1, 1, 1, 1)
    row = detail["devices"][0]
    assert row["hostname"] == "LT-LON-0142" and row["acknowledged"] == "2026-11-17T21:30:00Z"

    listing = client.get("/api/admin/messages").json()["messages"]
    assert [x["id"] for x in listing][0] == mac_only
    e = next(x for x in listing if x["id"] == everyone)
    assert (e["targeted"], e["delivered"], e["opened"]) == (2, 1, 0)

    # Repeating an event doesn't double-count.
    checkin(client, "LT-LON-0142", "james.brown", events=[{"messageId": laptops, "event": "acknowledged"}])
    assert client.get(f"/api/admin/messages/{laptops}").json()["acknowledged"] == 1


def test_withdraw_and_expiry(client):
    checkin(client, "LT-LON-0187", "amy.chen")
    gone = send(client, subject="Withdrawn")
    assert client.post(f"/api/admin/messages/{gone}/withdraw", headers=A).json() == {"ok": True}
    assert client.post(f"/api/admin/messages/{gone}/withdraw", headers=A).status_code == 404
    assert gone not in ids(checkin(client, "LT-LON-0187", "amy.chen"))


def test_links_are_checked(client):
    r = client.post("/api/admin/messages", headers=A, json={"subject": "x", "bodyRich": '<a href="https://evil.com">x</a>'})
    assert r.status_code == 422
    r = client.post("/api/admin/messages", headers=A, json={"subject": "x", "linkUrl": "http://nscale.com"})
    assert r.status_code == 422


def test_devices_and_audience(client):
    checkin(client, "LT-LON-0142", "james.brown")
    checkin(client, "LT-LON-0187", "amy.chen")
    assert client.post("/api/admin/audience", headers=A, json={"audience": ["amy.chen@nscale.com"]}).json() == {"devices": 1}
    devs = client.get("/api/admin/devices").json()["devices"]
    assert {d["hostname"] for d in devs} == {"LT-LON-0142", "LT-LON-0187"}
