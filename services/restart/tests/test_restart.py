"""The restart report: device reports, how they build each device's state, the page's API, sign-in."""
import importlib
import os
import tempfile
import uuid
from datetime import datetime, timedelta, timezone

os.environ.update(BASE_PATH="/restart", DEV_USER="viewer@nscale.com", AUTH_MODE="local",
                  INGEST_KEYS="laptops:" + "k" * 32, DATABASE_PATH=os.path.join(tempfile.mkdtemp(), "r.db"))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import access, auth, main, store as store_mod  # noqa: E402
from app.records import Policy, status  # noqa: E402

KEY = {"Authorization": "Bearer " + "k" * 32}
NOW = datetime.now(timezone.utc).replace(microsecond=0)


def t(**kw) -> str:
    return (NOW + timedelta(**kw)).strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "store", store_mod.Store(str(tmp_path / "t.db")))
    with TestClient(main.app) as c:
        yield c


def rep(event="status", at=None, boot=None, **kw):
    r = {"reportId": str(uuid.uuid4()), "event": event, "at": at or t(), "deviceName": "LT-LON-042",
         "serial": "5CG1234ABC", "user": "amy.chen@nscale.com", "bootTime": boot or t(days=-1), "appVersion": "1.2.0"}
    r.update(kw)
    return r


def send(c, *reports, headers=KEY):
    return c.post("/restart/api/reports", json={"reports": list(reports)}, headers=headers)


def device(c, key="5cg1234abc"):
    return c.get(f"/restart/api/devices/{key}").json()


def test_keys_and_shape(client):
    assert send(client, rep(), headers={}).status_code == 401
    assert send(client, rep(), headers={"Authorization": "Bearer nope"}).status_code == 401
    assert client.post("/restart/api/reports", content=b"nope", headers=KEY).status_code == 400
    assert client.post("/restart/api/reports", json={"events": []}, headers=KEY).status_code == 400
    r = send(client, rep(), rep(reportId="not-a-guid"), rep(event="exploded"), rep(deviceName=""))
    assert [x["status"] for x in r.json()["results"]] == ["created", "rejected", "rejected", "rejected"]
    assert client.get("/restart/api/policy").json()["maxUptimeDays"] == 3


def test_a_resent_report_counts_once(client):
    r = rep("scheduled", deadline=t(hours=2), userScheduled=True)
    assert send(client, r).json()["results"][0]["status"] == "created"
    assert send(client, r).json()["results"][0]["status"] == "duplicate"
    assert device(client)["postponedThisBoot"] == 1


def test_countdown_put_off_restart(client):
    boot = t(days=-4)
    send(client, rep("countdown_started", at=t(hours=-2), boot=boot, deadline=t(hours=1)))
    d = device(client)
    assert d["status"] == "countdown" and d["deadline"] == t(hours=1) and d["countdownStartedAt"] == t(hours=-2)
    send(client, rep("scheduled", at=t(hours=-1), boot=boot, deadline=t(hours=3), userScheduled=True))
    d = device(client)
    assert d["userScheduled"] and d["postponedThisBoot"] == 1 and d["countdownStartedAt"] == t(hours=-2)
    # A late report from before doesn't wind it back.
    send(client, rep("status", at=t(hours=-3), boot=boot))
    assert device(client)["lastEvent"] == "scheduled"
    send(client, rep("restarting", at=t(minutes=-5), boot=boot, reason="scheduled restart time reached"))
    # The device comes back on a new boot: that's a restart, and the countdown is over.
    send(client, rep("booted", at=t(), boot=t(minutes=-2)))
    d = device(client)
    assert d["status"] == "ok" and d["restarts"] == 1 and d["deadline"] is None and d["postponedThisBoot"] == 0
    assert d["restartIssuedAt"] is None
    h = d["restartHistory"][0]
    assert h["afterReminder"] and h["postponed"] == 1 and h["uptimeHours"] == pytest.approx(4 * 24 - 2 / 60, abs=0.1)
    assert [r["event"] for r in d["reports"]][:2] == ["booted", "restarting"]
    assert client.get("/restart/api/restarts?days=1").json()["items"][0]["key"] == "5cg1234abc"


def test_device_states():
    p = Policy(3, 3, 48)
    now = NOW
    assert status({"bootTime": t(days=-1), "lastReportAt": t(), "lastReceivedAt": t()}, p, now) == "ok"
    assert status({"bootTime": t(days=-4), "lastReportAt": t(), "lastReceivedAt": t(), "deadline": t(hours=1)}, p, now) == "countdown"
    assert status({"bootTime": t(days=-5), "lastReportAt": t(), "lastReceivedAt": t()}, p, now) == "overdue"
    assert status({"bootTime": t(days=-4), "lastReportAt": t(), "lastReceivedAt": t(), "deadline": t(hours=-2)}, p, now) == "overdue"
    assert status({"bootTime": t(days=-1), "lastReportAt": t(days=-3), "lastReceivedAt": t(days=-3)}, p, now) == "silent"
    assert status({"bootTime": t(days=-4), "lastReportAt": t(), "lastReceivedAt": t(),
                   "restartIssuedAt": t(hours=-1)}, p, now) == "restart_failed"
    # A test run never counts as a failed restart.
    assert status({"bootTime": t(days=-1), "lastReportAt": t(), "lastReceivedAt": t(),
                   "restartIssuedAt": t(hours=-1), "dryRun": True}, p, now) == "ok"


def test_list_filters_csv_and_forget(client):
    send(client, rep(boot=t(days=-6)))
    send(client, rep(serial="", deviceName="LT-LON-007", boot=t(hours=-3)))
    data = client.get("/restart/api/devices").json()
    assert data["stats"]["devices"] == 2 and data["stats"]["overdue"] == 1
    assert data["items"][0]["status"] == "overdue"   # worst first
    assert client.get("/restart/api/devices?show=ok").json()["items"][0]["key"] == "lt-lon-007"
    assert client.get("/restart/api/devices?q=lon-007").json()["matched"] == 1
    assert client.get("/restart/api/devices?show=nope").status_code == 422
    csv = client.get("/restart/api/devices.csv")
    assert csv.status_code == 200 and "LT-LON-007" in csv.text
    assert client.delete("/restart/api/devices/lt-lon-007").status_code == 403   # no header
    assert client.delete("/restart/api/devices/lt-lon-007", headers={"X-Requested-With": "nscale-admin"}).status_code == 200
    assert client.get("/restart/api/devices").json()["stats"]["devices"] == 1


def test_page_and_base_path(client):
    assert client.get("/restart", follow_redirects=False).headers["location"] == "/restart/"
    assert client.get("/restart/").status_code == 200
    assert client.get("/healthz").json() == {"ok": True}


SECRET = "r" * 40


@pytest.fixture
def stack(tmp_path, monkeypatch):
    monkeypatch.setenv("DEV_USER", "")
    monkeypatch.setenv("AUTH_SECRET", SECRET)
    for m in (access, auth, main):
        importlib.reload(m)
    monkeypatch.setattr(main, "store", store_mod.Store(str(tmp_path / "t.db")))
    with TestClient(main.app) as c:
        yield c
    monkeypatch.setenv("DEV_USER", "viewer@nscale.com")
    monkeypatch.delenv("AUTH_SECRET")
    for m in (access, auth, main):
        importlib.reload(m)


def test_front_door_sign_in_is_checked(stack):
    def h(tool="restart"):
        return {"x-cp-user": access.issue("amy.chen@nscale.com", "Amy Chen", tool, secret=SECRET)}
    assert stack.get("/restart/api/me", headers=h()).json()["email"] == "amy.chen@nscale.com"
    assert stack.get("/restart/api/me").status_code == 401
    assert stack.get("/restart/api/devices", headers=h("elevation")).status_code == 401
    assert stack.get("/restart/").status_code == 403
    assert send(stack, rep()).status_code == 200   # devices use the ingest key


def test_guessing_keys_is_slowed_down(client):
    main.guard.clear()
    bad, ip = {"Authorization": "Bearer " + "x" * 32}, {"X-Forwarded-For": "203.0.113.9"}
    for _ in range(main.keyguard.LIMIT):
        assert send(client, headers={**bad, **ip}).status_code == 401
    assert send(client, headers={**KEY, **ip}).status_code == 429
    assert send(client, headers={**KEY, "X-Forwarded-For": "198.51.100.4"}).status_code != 429
    main.guard.clear()


def test_device_keys_made_on_the_site(client, monkeypatch, tmp_path):
    from datetime import date, timedelta as td
    H = {"X-Requested-With": "nscale-admin"}
    items = client.get("/restart/api/device-keys").json()["items"]
    assert items[0]["source"] == "env" and items[0]["label"] == "laptops" and items[0]["start"] == "kkkkkk"   # never the whole key
    assert client.delete("/restart/api/device-keys/env-0", headers=H).status_code == 400

    r = client.post("/restart/api/device-keys", headers=H, json={"label": " Windows  laptops ", "expires": ""}).json()
    key, item = r["key"], r["item"]
    assert key.startswith("cpr_") and len(key) > 30 and item["label"] == "Windows laptops" and key.startswith(item["start"])
    assert key not in str(client.get("/restart/api/device-keys").json())   # shown once only
    assert send(client, rep(), headers={"Authorization": "Bearer " + key}).status_code == 200
    assert client.get("/restart/api/device-keys").json()["items"][1]["lastUsed"]

    # Expiry: works through its last day, not after.
    today = date.today().isoformat()
    assert client.post("/restart/api/device-keys", headers=H, json={"label": "x", "expires": "2001-01-01"}).status_code == 422
    short = client.post("/restart/api/device-keys", headers=H, json={"label": "Until today", "expires": today}).json()
    assert send(client, rep(), headers={"Authorization": "Bearer " + short["key"]}).status_code == 200
    with main.KEYS._db() as db:
        db.execute("UPDATE device_keys SET expires = ? WHERE id = ?", ((date.today() - td(days=1)).isoformat(), short["item"]["id"]))
    assert send(client, rep(), headers={"Authorization": "Bearer " + short["key"]}).status_code == 401
    assert next(k for k in client.get("/restart/api/device-keys").json()["items"] if k["id"] == short["item"]["id"])["expired"]

    # Deleting stops it at once.
    assert client.delete(f"/restart/api/device-keys/{item['id']}", headers=H).status_code == 200
    assert send(client, rep(), headers={"Authorization": "Bearer " + key}).status_code == 401
    assert client.delete(f"/restart/api/device-keys/{item['id']}", headers=H).status_code == 404
    assert send(client, rep()).status_code == 200   # the .env key still works

    # Only admins of the tool.
    monkeypatch.setattr(access, "DEV_USER", "")
    monkeypatch.setattr(access, "AUTH_SECRET", "s" * 40)
    edit = {**H, "X-CP-User": access.issue("e@nscale.com", "E", "restart", level="edit")}
    adm = {**H, "X-CP-User": access.issue("a@nscale.com", "A", "restart", admin=True)}
    assert client.get("/restart/api/device-keys", headers=edit).status_code == 403
    assert client.get("/restart/api/device-keys", headers=adm).status_code == 200
    assert client.get("/restart/api/device-keys").status_code == 401
