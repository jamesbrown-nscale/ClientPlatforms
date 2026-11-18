"""The elevation log: ingest exactly as the Windows worker sends it, merge rules, the log API, sign-in."""
import importlib
import os
import tempfile
import time
import uuid

os.environ.update(STORE="sqlite", BASE_PATH="/elevation", DEV_USER="viewer@nscale.com",
                  INGEST_KEYS="laptops:" + "k" * 32 + "\nservers:" + "s" * 32)
os.environ["DATABASE_PATH"] = os.path.join(tempfile.mkdtemp(), "e.db")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import access, auth, main, store as store_mod  # noqa: E402

KEY = {"Authorization": "Bearer " + "k" * 32}
OTHER_KEY = {"Authorization": "Bearer " + "s" * 32}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "store", store_mod.SqliteStore(str(tmp_path / "t.db")))
    with TestClient(main.app) as c:
        yield c


def record(**kw):
    """A record as ElevateWorker.ps1 queues it (Add-TechOpsRecord)."""
    r = {"eventId": str(uuid.uuid4()), "serial": "5CG1234ABC", "deviceName": "LT-LON-042", "platform": "windows",
         "user": "amy.chen@nscale.com", "requestedAt": "2026-11-17T10:00:00Z", "grantedAt": "2026-11-17T10:00:00Z",
         "expiresAt": "2026-11-17T10:15:00Z", "durationMinutes": 15, "reason": "Install the Python 3.13 SDK",
         "outcome": "granted"}
    r.update(kw)
    return r


def send(c, *events, headers=KEY):
    return c.post("/elevation/api/elevations/ingest", json={"events": list(events)}, headers=headers)


def test_keys_and_body_shape(client):
    assert send(client, record(), headers={}).status_code == 401
    assert send(client, record(), headers={"Authorization": "Bearer nope"}).status_code == 401
    assert send(client, record(), headers={"Authorization": "Basic " + "k" * 32}).status_code == 401
    assert client.post("/elevation/api/elevations/ingest", content=b"not json", headers=KEY).status_code == 400
    assert client.post("/elevation/api/elevations/ingest", json=[record()], headers=KEY).status_code == 400
    assert client.post("/elevation/api/elevations/ingest", json={"events": {}}, headers=KEY).status_code == 400


def test_grant_then_end_is_one_record(client):
    g = record()
    r = send(client, g)
    assert r.status_code == 200 and r.json() == {"results": [{"eventId": g["eventId"], "status": "created"}]}
    end = {**g, "outcome": "expired", "expiresAt": "2026-11-17T10:15:04Z"}
    assert send(client, end).json()["results"][0]["status"] == "updated"

    rows = client.get("/elevation/api/elevations?days=366").json()
    assert rows["matched"] == 1
    row = rows["items"][0]
    assert row["status"] == "expired" and row["expiresAt"] == "2026-11-17T10:15:04Z" and row["sends"] == 2
    assert row["changed"] == [] and row["keyLabel"] == "laptops"
    detail = client.get(f"/elevation/api/elevations/{g['eventId'].upper()}").json()
    assert [h["outcome"] for h in detail["history"]] == ["granted", "expired"]


def test_bad_records_are_rejected_one_by_one(client):
    good = record()
    r = send(client, good, record(eventId="RequestId-from-user"), record(outcome="maybe"), record(user=""), "x")
    assert [x["status"] for x in r.json()["results"]] == ["created", "rejected", "rejected", "rejected", "rejected"]
    assert "eventId" in r.json()["results"][1]["error"]


def test_denied_has_no_times(client):
    d = record(outcome="denied", grantedAt="", expiresAt=None, durationMinutes=None, reason="")
    assert send(client, d).json()["results"][0]["status"] == "created"
    row = client.get("/elevation/api/elevations?days=366&show=denied").json()["items"][0]
    assert row["status"] == "denied" and row["grantedAt"] is None


def test_rewrites_are_flagged_and_ended_cannot_reopen(client):
    g = record()
    send(client, g)
    send(client, {**g, "outcome": "revoked", "expiresAt": "2026-11-17T10:05:00Z"})
    # Someone with the key tries to rewrite the reason and reopen it, with another key.
    send(client, {**g, "reason": "Nothing to see here", "outcome": "granted", "expiresAt": "2026-11-17T23:00:00Z"},
         headers=OTHER_KEY)
    d = client.get(f"/elevation/api/elevations/{g['eventId']}").json()
    assert d["outcome"] == "revoked" and d["expiresAt"] == "2026-11-17T10:05:00Z"
    assert d["reason"] == "Nothing to see here" and d["first"]["reason"] == "Install the Python 3.13 SDK"
    assert d["changed"] == ["key", "reason"] and d["outOfOrder"] == 1
    assert d["history"][-1]["ignoredAsOutOfOrder"] is True
    assert client.get("/elevation/api/elevations?days=366&show=flagged").json()["matched"] == 1


def test_resend_of_same_state_is_not_a_change(client):
    g = record()
    send(client, g)
    send(client, {**g, "expiresAt": "2026-11-17T10:19:00Z"})   # signed in again while on: new expiry only
    d = client.get(f"/elevation/api/elevations/{g['eventId']}").json()
    assert d["changed"] == [] and d["sends"] == 2 and d["expiresAt"] == "2026-11-17T10:19:00Z"


def test_status_active_and_not_ended(client):
    from app.records import iso, utc_now
    from datetime import timedelta
    now = utc_now()
    active = record(requestedAt=iso(now), grantedAt=iso(now), expiresAt=iso(now + timedelta(minutes=10)))
    stale = record(requestedAt=iso(now - timedelta(hours=3)), grantedAt=iso(now - timedelta(hours=3)),
                   expiresAt=iso(now - timedelta(hours=2)), durationMinutes=60)
    send(client, active, stale)
    data = client.get("/elevation/api/elevations?days=1").json()
    assert data["stats"]["active"] == 1 and data["stats"]["notEnded"] == 1
    assert {r["status"] for r in data["items"]} == {"active", "not_ended"}


def test_search_period_and_csv(client):
    send(client, record(user="amy.chen@nscale.com", reason="=HYPERLINK(\"http://x\")"),
         record(user="bob.lee@nscale.com", deviceName="LT-NYC-007"),
         record(user="old@nscale.com", requestedAt="2020-01-01T00:00:00Z"))
    assert client.get("/elevation/api/elevations?days=366&q=nyc").json()["matched"] == 1
    assert client.get("/elevation/api/elevations?days=366&q=amy hyperlink").json()["matched"] == 1
    assert client.get("/elevation/api/elevations?days=366").json()["matched"] == 2   # 2020 is outside the year
    assert client.get("/elevation/api/elevations?show=bogus").status_code == 422
    csv = client.get("/elevation/api/elevations.csv?days=366")
    assert csv.status_code == 200 and csv.headers["content-type"].startswith("text/csv")
    assert "'=HYPERLINK" in csv.text and csv.text.splitlines()[0].startswith("requestedAt,user")


def test_paths_and_page(client):
    assert client.get("/elevation", follow_redirects=False).headers["location"] == "/elevation/"
    assert client.get("/healthz").json() == {"ok": True}
    page = client.get("/elevation/")
    assert page.status_code == 200 and "Admin elevation log" in page.text
    assert client.get("/elevation/static/nscale-wordmark.svg").status_code == 200


def test_source_ip_is_the_proxys_view(client, monkeypatch):
    def sent_from(xff):
        g = record()
        client.post("/elevation/api/elevations/ingest", json={"events": [g]}, headers={**KEY, "X-Forwarded-For": xff})
        return client.get(f"/elevation/api/elevations/{g['eventId']}").json()["history"][0]["sourceIp"]

    # Caddy (Docker stack): the last address is the one it saw.
    assert sent_from("6.6.6.6, 81.2.69.160") == "81.2.69.160"
    # Google load balancer: "<client>, <load balancer>" is appended.
    monkeypatch.setattr(access, "AUTH_MODE", "iap")
    assert sent_from("6.6.6.6, 81.2.69.160, 34.111.1.1") == "81.2.69.160"


# ---------- Sign-in through IAP ----------

AUDIENCE = "/projects/123/global/backendServices/789"


@pytest.fixture
def iap_client(tmp_path, monkeypatch):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from google.auth import crypt, jwt

    monkeypatch.setenv("DEV_USER", "")
    monkeypatch.setenv("AUTH_MODE", "iap")
    monkeypatch.setenv("IAP_AUDIENCE", AUDIENCE)
    reload_app()
    monkeypatch.setattr(main, "store", store_mod.SqliteStore(str(tmp_path / "t.db")))
    key = ec.generate_private_key(ec.SECP256R1())
    pem = key.public_key().public_bytes(serialization.Encoding.PEM,
                                        serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    signer = crypt.ES256Signer.from_string(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode(), "k")
    monkeypatch.setattr(access.iap_keys, "get", lambda: {"k": pem})

    def token(**kw):
        now = int(time.time())
        claims = {"iss": "https://cloud.google.com/iap", "aud": AUDIENCE, "email": "james.brown@nscale.com",
                  "hd": "nscale.com", "iat": now, "exp": now + 600, **kw}
        return {"x-goog-iap-jwt-assertion": jwt.encode(signer, claims).decode()}

    with TestClient(main.app) as c:
        c.token = token
        yield c
    monkeypatch.setenv("DEV_USER", "viewer@nscale.com")
    monkeypatch.delenv("IAP_AUDIENCE")
    monkeypatch.delenv("AUTH_MODE")
    reload_app()


def reload_app():
    for m in (access, auth, main):
        importlib.reload(m)


def test_iap_sign_in_is_checked(iap_client):
    c = iap_client
    assert c.get("/elevation/api/me", headers=c.token()).json()["email"] == "james.brown@nscale.com"
    assert c.get("/elevation/api/me").status_code == 401
    assert c.get("/elevation/api/me", headers=c.token(aud="/projects/123/global/backendServices/1")).status_code == 401
    assert c.get("/elevation/api/me", headers=c.token(hd="example.com")).status_code == 401
    assert c.get("/elevation/api/me", headers=c.token(exp=int(time.time()) - 60)).status_code == 401
    assert c.get("/elevation/").status_code == 403
    assert c.get("/elevation/api/elevations.csv").status_code == 401
    # Devices don't sign in; they use the ingest key.
    assert send(c, record()).status_code == 200


SECRET = "x" * 40


@pytest.fixture
def local_client(tmp_path, monkeypatch):
    """As in the Docker stack: Caddy asks the portal, which vouches for the person with X-CP-User."""
    monkeypatch.setenv("DEV_USER", "")
    monkeypatch.setenv("AUTH_SECRET", SECRET)
    reload_app()
    monkeypatch.setattr(main, "store", store_mod.SqliteStore(str(tmp_path / "t.db")))
    with TestClient(main.app) as c:
        yield c
    monkeypatch.setenv("DEV_USER", "viewer@nscale.com")
    monkeypatch.delenv("AUTH_SECRET")
    reload_app()


def test_front_door_sign_in_is_checked(local_client):
    c = local_client

    def h(tool="elevation", secret=SECRET, email="amy.chen@nscale.com"):
        return {"x-cp-user": access.issue(email, "Amy Chen", tool, secret=secret)}

    me = c.get("/elevation/api/me", headers=h())
    assert me.status_code == 200 and me.json()["email"] == "amy.chen@nscale.com"
    assert c.get("/elevation/api/me").status_code == 401
    assert c.get("/elevation/api/me", headers=h(tool="notifications")).status_code == 401
    assert c.get("/elevation/api/me", headers=h(secret="y" * 40)).status_code == 401
    assert c.get("/elevation/api/me", headers={"x-cp-user": "junk"}).status_code == 401
    assert c.get("/elevation/").status_code == 403
    assert c.get("/elevation/", headers=h()).status_code == 200
    assert send(c, record()).status_code == 200
