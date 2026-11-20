"""Users and Devices: who can do what, the Jamf and Intune syncs, CSV import and the user tags."""
import json
import os
import sqlite3
import tempfile

os.environ.update(AUTH_MODE="local", AUTH_SECRET="s" * 40, SYNC_MINUTES="0",
                  DATABASE_PATH=os.path.join(tempfile.mkdtemp(), "i.db"))
os.environ.pop("DEV_USER", None)

import httpx  # noqa: E402
import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import access, connectors, main, store as store_mod  # noqa: E402

H = {"X-Requested-With": "nscale-admin"}


def who(tool: str, level: str = "admin", email: str = "admin@nscale.com") -> dict:
    return {**H, "X-CP-User": access.issue(email, "Admin", tool, level == "admin", level=level)}


DA, DE, DV = who("devices"), who("devices", "edit"), who("devices", "view")
UE, UV = who("users", "edit"), who("users", "view")


@pytest.fixture
def c(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "store", store_mod.Store(str(tmp_path / "t.db"), "s" * 40))
    with TestClient(main.app) as client:
        yield client


class Fake:
    """Jamf and Microsoft, as far as the sync can tell."""

    def __init__(self):
        self.computers = [computer(1, "MBP-AMY", "C02AAA", "amy.chen@nscale.com"), computer(2, "MBA-SAM", "C02BBB", "sam.lee@nscale.com"),
                          computer(3, "MBP-SPARE", "C02CCC", "")]
        self.mobile_status = 403
        self.intune = [intune("w1", "LT-LON-042", "5CG111", "amy.chen@nscale.com", "Windows"),
                       intune("w2", "PIXEL-PAT", "PX222", "pat@nscale.com", "Android")]
        self.token_status = 200
        self.graph_status = 200
        self.calls = []

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.calls.append((req.method, str(req.url)))
        u = req.url
        if u.path == "/api/oauth/token":
            body = dict(x.split("=") for x in req.content.decode().split("&"))
            if self.token_status != 200 or body.get("client_secret") != "jamf-secret":
                return httpx.Response(401, json={"error": "invalid_client"})
            return httpx.Response(200, json={"access_token": "jt", "expires_in": 1199})
        if u.path == "/api/v1/computers-inventory":
            assert req.headers["authorization"] == "Bearer jt"
            page, size = int(u.params["page"]), int(u.params["page-size"])
            return httpx.Response(200, json={"totalCount": len(self.computers), "results": self.computers[page * size:(page + 1) * size]})
        if u.path == "/api/v2/mobile-devices/detail":
            if u.params["sort"] != "mobileDeviceId:asc":   # Jamf: "id" isn't a field here
                return httpx.Response(400, json={"httpStatus": 400, "errors": [{"code": "INVALID_FIELD", "field": "sort", "description": "Invalid sort field"}]})
            if self.mobile_status == 400:
                return httpx.Response(400, json={"httpStatus": 400, "errors": [{"code": "INVALID_FIELD", "field": "section", "description": "Bad section"}]})
            if self.mobile_status != 200:
                return httpx.Response(self.mobile_status, json={})
            return httpx.Response(200, json={"totalCount": 1, "results": [
                {"mobileDeviceId": "9", "deviceType": "iOS", "general": {"displayName": "Amy's iPhone", "osVersion": "18.1"},
                 "hardware": {"model": "iPhone 15", "serialNumber": "F2LXX"}, "userAndLocation": {"emailAddress": "Amy.Chen@nscale.com"}}]})
        if u.host == "login.microsoftonline.com":
            if self.token_status != 200:
                return httpx.Response(401, json={"error_description": "AADSTS7000215: Invalid client secret provided.\r\nTrace ID: x"})
            return httpx.Response(200, json={"access_token": "mt"})
        if u.host == "graph.microsoft.com":
            if self.graph_status != 200:
                return httpx.Response(self.graph_status, json={})
            assert req.headers["authorization"] == "Bearer mt"
            if "skiptoken" in str(u):
                return httpx.Response(200, json={"value": self.intune[1:]})
            return httpx.Response(200, json={"value": self.intune[:1],
                                             "@odata.nextLink": "https://graph.microsoft.com/v1.0/deviceManagement/managedDevices?$skiptoken=2"})
        return httpx.Response(404)


def computer(i, name, serial, email):
    return {"id": str(i), "general": {"name": name, "lastContactTime": "2026-11-19T10:00:00Z"},
            "hardware": {"make": "Apple", "model": "MacBook Pro (14-inch, 2023)", "serialNumber": serial},
            "operatingSystem": {"name": "macOS", "version": "15.1"}, "userAndLocation": {"email": email, "realname": name.title()}}


def intune(i, name, serial, email, os_name):
    return {"id": i, "deviceName": name, "manufacturer": "Dell" if os_name == "Windows" else "Google", "model": "Latitude 7440",
            "operatingSystem": os_name, "osVersion": "10.0.22631", "serialNumber": serial, "userPrincipalName": email,
            "emailAddress": email, "userDisplayName": "Somebody", "lastSyncDateTime": "2026-11-19T09:00:00Z"}


@pytest.fixture
def fake(monkeypatch):
    f = Fake()
    monkeypatch.setattr(main, "make_client", lambda: httpx.Client(transport=httpx.MockTransport(f)))
    monkeypatch.setattr(connectors, "PAGE", 2)   # so paging is exercised
    return f


def connect_jamf(c, secret="jamf-secret"):
    return c.put("/devices/api/connections/jamf", headers=DA,
                 json={"url": "nscale.jamfcloud.com/", "clientId": "abc", "secret": secret})


def connect_intune(c):
    return c.put("/devices/api/connections/intune", headers=DA,
                 json={"tenantId": "1b2c3d4e-0000-4000-8000-123456789abc", "clientId": "app", "secret": "ms-secret"})


# ---------- Access ----------

def test_who_can_do_what(c):
    assert c.get("/healthz").json() == {"ok": True}
    assert c.get("/devices", follow_redirects=False).headers["location"] == "/devices/"
    assert c.get("/devices/api/devices").status_code == 401
    assert c.get("/devices/").status_code == 403
    assert "Devices" in c.get("/devices/", headers=DV).text and "Users" in c.get("/users/", headers=UV).text
    assert "Org chart" in c.get("/users/org", headers=UV).text and "Departments" in c.get("/users/departments", headers=UV).text
    assert c.get("/users/org").status_code == 403 and c.get("/users/departments", headers=DV).status_code == 403
    assert c.get("/devices/api/devices", headers=UV).status_code == 401    # a Users token isn't a Devices one
    assert c.get("/devices/api/devices", headers=DV).status_code == 200
    assert c.get("/devices/api/connections", headers=DE).status_code == 403   # admins only
    assert c.put("/devices/api/connections/jamf", headers=DE, json={"clientId": "x"}).status_code == 403
    assert c.post("/devices/api/sync", headers=DV).status_code == 403
    no_header = {"X-CP-User": UE["X-CP-User"]}
    assert c.post("/users/api/users", headers=no_header, json={"email": "a@nscale.com"}).status_code == 403
    assert c.post("/users/api/users", headers=UV, json={"email": "a@nscale.com"}).status_code == 403
    assert c.post("/users/api/users", headers=UE, json={"email": "a@nscale.com"}).status_code == 200
    me = c.get("/devices/api/me", headers=DA).json()
    assert me["admin"] and me["canEdit"]
    assert c.get("/users/api/me", headers=UV).json()["canEdit"] is False
    js = c.get("/devices/static/connections.js")   # Settings loads it
    assert js.status_code == 200 and js.headers["cache-control"] == "no-cache"


# ---------- Connections and syncs ----------

def test_jamf_connection_and_sync(c, fake, tmp_path):
    assert c.post("/devices/api/sync", headers=DE).status_code == 400   # nothing connected
    bad = c.put("/devices/api/connections/jamf", headers=DA, json={"url": "http://jamf.local", "clientId": "a", "secret": "x"})
    assert bad.status_code == 422 and "https://" in bad.json()["detail"]
    assert c.put("/devices/api/connections/jamf", headers=DA, json={"url": "nscale.jamfcloud.com", "clientId": "a"}).status_code == 422  # no secret
    r = connect_jamf(c)
    assert r.status_code == 200
    j = next(x for x in r.json()["items"] if x["kind"] == "jamf")
    assert j["url"] == "https://nscale.jamfcloud.com" and j["hasSecret"] and "secret" not in j and j["connected"]
    raw = sqlite3.connect(main.store.path).execute("SELECT secret FROM connections").fetchone()[0]
    assert raw and "jamf-secret" not in raw   # encrypted at rest

    t = c.post("/devices/api/connections/jamf/test", headers=DA).json()
    assert t["ok"] and t["count"] == 3 and "Read Mobile Devices" in t["note"]
    devs = c.get("/devices/api/devices", headers=DV).json()
    assert devs["stats"]["devices"] == 3 and devs["stats"]["Jamf"] == 3 and devs["stats"]["computers"] == 3
    amy = next(d for d in devs["items"] if d["serial"] == "C02AAA")
    assert (amy["platform"], amy["make"], amy["model"], amy["os"], amy["osVersion"], amy["userEmail"]) == \
        ("Jamf", "Apple", "MacBook Pro (14-inch, 2023)", "macOS", "15.1", "amy.chen@nscale.com")
    conn = devs["connections"][0]
    assert conn["count"] == 3 and conn["lastOk"] and not conn["lastError"]

    # Next sync: a computer gone, phones readable now.
    fake.computers = fake.computers[:2]
    fake.mobile_status = 200
    assert main.sync("jamf") == {"ok": True, "count": 3, "note": ""}
    items = c.get("/devices/api/devices", headers=DV).json()["items"]
    assert {d["serial"] for d in items} == {"C02AAA", "C02BBB", "F2LXX"}
    phone = next(d for d in items if d["serial"] == "F2LXX")
    assert phone["kind"] == "mobile" and phone["userEmail"] == "amy.chen@nscale.com" and phone["model"] == "iPhone 15"

    # Jamf turning down the phone list doesn't lose the Macs; the note says why.
    fake.mobile_status = 400
    r = main.sync("jamf")
    assert r["ok"] and r["count"] == 2 and "Bad section (section)" in r["note"]
    fake.mobile_status = 200

    # Saving without a secret keeps it; a wrong one is reported, and the devices stay.
    assert c.put("/devices/api/connections/jamf", headers=DA, json={"url": "https://nscale.jamfcloud.com", "clientId": "abc", "secret": ""}).status_code == 200
    assert main.sync("jamf")["ok"]
    connect_jamf(c, secret="wrong")
    t = c.post("/devices/api/connections/jamf/test", headers=DA).json()
    assert not t["ok"] and "turned down" in t["error"]
    assert len(c.get("/devices/api/devices", headers=DV).json()["items"]) == 3
    assert next(x for x in t["items"] if x["kind"] == "jamf")["lastError"]

    # A different AUTH_SECRET can't read the saved secret.
    other = store_mod.Store(main.store.path, "t" * 40)
    assert other.secret("jamf") == "" and main.store.secret("jamf") == "wrong"

    # Disconnecting removes its devices.
    gone = c.delete("/devices/api/connections/jamf", headers=DA).json()
    assert gone["removed"] == 3 and not c.get("/devices/api/devices", headers=DV).json()["items"]
    assert c.delete("/devices/api/connections/jamf", headers=DA).status_code == 404
    assert c.put("/devices/api/connections/nope", headers=DA, json={"clientId": "x"}).status_code == 404


def test_intune_sync(c, fake, monkeypatch):
    assert c.put("/devices/api/connections/intune", headers=DA, json={"tenantId": "not a tenant", "clientId": "a", "secret": "b"}).status_code == 422
    assert connect_intune(c).status_code == 200
    t = c.post("/devices/api/connections/intune/test", headers=DA).json()
    assert t["ok"] and t["count"] == 2   # two pages, followed by @odata.nextLink
    items = {d["serial"]: d for d in c.get("/devices/api/devices", headers=DV).json()["items"]}
    assert items["5CG111"]["platform"] == "Intune" and items["5CG111"]["kind"] == "computer" and items["5CG111"]["make"] == "Dell"
    assert items["PX222"]["kind"] == "mobile" and items["PX222"]["os"] == "Android"

    fake.graph_status = 403
    assert "DeviceManagementManagedDevices.Read.All" in main.sync("intune")["error"]
    fake.graph_status, fake.token_status = 200, 401
    assert "Invalid client secret provided." in main.sync("intune")["error"]
    fake.token_status = 200

    # A next page that points somewhere else isn't followed.
    orig = fake.__call__

    def evil(req):
        r = orig(req)
        if req.url.host == "graph.microsoft.com" and "skiptoken" not in str(req.url):
            return httpx.Response(200, json={"value": [], "@odata.nextLink": "https://evil.example.com/steal"})
        return r
    monkeypatch.setattr(main, "make_client", lambda: httpx.Client(transport=httpx.MockTransport(evil)))
    assert "somewhere else" in main.sync("intune")["error"]
    assert not any("evil" in u for _, u in fake.calls)


def test_sync_all_runs_whats_due(c, fake, monkeypatch):
    connect_jamf(c)
    connect_intune(c)
    c.put("/devices/api/connections/intune", headers=DA,
          json={"tenantId": "1b2c3d4e-0000-4000-8000-123456789abc", "clientId": "app", "enabled": False})
    assert main.sync_all() == {}                         # SYNC_MINUTES=0: only when asked
    monkeypatch.setattr(main, "SYNC_MINUTES", 60)
    assert set(main.sync_all()) == {"jamf"}             # never synced, so due; Intune is paused
    assert main.sync_all() == {}                         # synced a moment ago
    assert set(main.sync_all(force=True)) == {"jamf"}


# ---------- Users ----------

CSV = "﻿E-mail Address;Display Name;Dept;Team;Line Manager;VIP;Serial Number;Cost centre\n" \
      "Amy.Chen@nscale.com;Amy Chen;Engineering;Platform;Sam Lee;yes;C02AAA;123\n" \
      "sam.lee@nscale.com;Sam Lee;Engineering;Platform;;Standard;;\n" \
      "not-an-email;Nobody;;;;;;\n" \
      "pat@nscale.com;Pat Jones;;Support;boss@nscale.com;gold;NOPE;\n" \
      "\n"


def test_csv_import(c, fake):
    connect_jamf(c)
    main.sync("jamf")
    assert c.post("/users/api/import", headers=UE, json={"csv": "Name,Team\nAmy,X"}).status_code == 422   # no Email column
    assert c.post("/users/api/import", headers=UE, json={"csv": " "}).status_code == 422
    dry = c.post("/users/api/import", headers=UE, json={"csv": CSV, "dryRun": True}).json()
    assert dry["dryRun"] and (dry["rows"], dry["added"], dry["updated"]) == (3, 3, 0)
    assert dry["unusedColumns"] == ["Cost centre"]
    assert not c.get("/users/api/users", headers=UV).json()["items"]    # a dry run saves nothing

    r = c.post("/users/api/import", headers=UE, json={"csv": CSV}).json()
    assert (r["added"], r["updated"]) == (3, 0)
    msgs = {}
    for prob in r["problems"]:
        msgs[prob["row"]] = msgs.get(prob["row"], "") + prob["message"]
    assert "no valid email" in msgs[4] and "NOPE" in msgs[5]
    users = {u["email"]: u for u in c.get("/users/api/users", headers=UV).json()["items"]}
    amy = users["amy.chen@nscale.com"]
    assert (amy["name"], amy["department"], amy["division"], amy["type"], amy["manager"], amy["managerName"]) == \
        ("Amy Chen", "Engineering", "Platform", "VIP", "sam.lee@nscale.com", "Sam Lee")     # manager by name, from the same file
    assert amy["primaryDevice"]["serial"] == "C02AAA" and amy["primaryDevice"]["model"] == "MacBook Pro (14-inch, 2023)"
    assert users["pat@nscale.com"]["type"] == "Gold" and users["pat@nscale.com"]["manager"] == "boss@nscale.com"
    assert users["pat@nscale.com"]["primaryDevice"] is None

    # Again, with some changes: empty cells leave things alone.
    again = c.post("/users/api/import", headers=UE, json={"csv": "email,division,user type\namy.chen@nscale.com,,Standard\nsam.lee@nscale.com,Platform,\n"}).json()
    assert (again["added"], again["updated"], again["unchanged"]) == (0, 1, 1)
    amy = next(u for u in c.get("/users/api/users", headers=UV).json()["items"] if u["email"] == "amy.chen@nscale.com")
    assert amy["division"] == "Platform" and amy["type"] == "Standard" and amy["primaryDevice"]["serial"] == "C02AAA"

    stats = c.get("/users/api/users", headers=UV).json()
    assert stats["departments"] == [
        {"name": "Engineering", "count": 2, "divisions": [{"name": "Platform", "count": 2}]},
        {"name": "", "count": 1, "divisions": [{"name": "Support", "count": 1}]}]
    assert stats["stats"] == {"users": 3, "vip": 0, "departments": 1, "withDevice": 1, "noDevice": 2}
    # The Team tool reads these, inside the stack.
    assert c.get("/internal/teams").json()["departments"] == stats["departments"]
    assert c.get("/internal/users/Amy.Chen@nscale.com").json() == {"email": "amy.chen@nscale.com", "name": "Amy Chen", "title": "",
        "department": "Engineering", "division": "Platform", "manager": "sam.lee@nscale.com", "managerName": "Sam Lee"}
    assert c.get("/internal/users/ghost@nscale.com").status_code == 404
    devs = {d["serial"]: d for d in c.get("/devices/api/devices", headers=DV).json()["items"]}
    assert devs["C02AAA"]["primaryFor"] == [{"email": "amy.chen@nscale.com", "name": "Amy Chen", "type": "Standard"}]


def test_tags(c, fake):
    connect_jamf(c)
    main.sync("jamf")
    assert c.post("/users/api/users", headers=UE, json={"email": "nope"}).status_code == 422
    u = c.post("/users/api/users", headers=UE, json={"email": "Amy.Chen@nscale.com", "name": "Amy Chen", "division": " Platform ",
                                                     "type": "VIP", "manager": "sam.lee@nscale.com"}).json()
    assert (u["email"], u["division"], u["type"], u["manager"], u["managerName"]) == ("amy.chen@nscale.com", "Platform", "VIP", "sam.lee@nscale.com", "")
    assert c.post("/users/api/users", headers=UE, json={"email": "amy.chen@nscale.com"}).status_code == 409
    c.post("/users/api/users", headers=UE, json={"email": "sam.lee@nscale.com", "name": "Sam Lee"})
    u = c.get("/users/api/users", headers=UV).json()["items"][0]
    assert u["managerName"] == "Sam Lee"

    p = lambda body: c.patch("/users/api/users/amy.chen@nscale.com", headers=UE, json=body)
    assert p({"manager": "amy.chen@nscale.com"}).status_code == 422          # not your own manager
    assert p({"type": "<b>x</b>"}).status_code == 422 and p({"type": "x" * 41}).status_code == 422
    assert p({"type": "contractor"}).json()["type"] == "Contractor"         # a new type
    c.post("/users/api/users", headers=UE, json={"email": "kai@nscale.com", "type": "CONTRACTOR"})
    got = c.get("/users/api/users", headers=UV).json()
    assert next(u for u in got["items"] if u["email"] == "kai@nscale.com")["type"] == "Contractor"   # the same one
    assert got["types"] == [{"name": "VIP", "count": 0}, {"name": "Contractor", "count": 2}, {"name": "Standard", "count": 1}]
    c.delete("/users/api/users/kai@nscale.com", headers=UE)
    assert p({"primaryDevice": "C02ZZZ"}).status_code == 422
    did = next(d["id"] for d in c.get("/users/api/devices", headers=UV).json()["items"] if d["serial"] == "C02AAA")
    u = p({"primaryDevice": did, "type": "Standard", "division": ""}).json()
    assert u["primaryDevice"]["id"] == did and u["primaryDevice"]["platform"] == "Jamf" and u["type"] == "Standard" and u["division"] == ""
    assert p({"manager": "Sam Lee"}).json()["manager"] == "sam.lee@nscale.com"   # by name
    assert c.patch("/users/api/users/ghost@nscale.com", headers=UE, json={"division": "x"}).status_code == 404

    # The device leaves Jamf: the tag stays, shown as gone.
    fake.computers = [x for x in fake.computers if x["hardware"]["serialNumber"] != "C02AAA"]
    main.sync("jamf")
    u = next(x for x in c.get("/users/api/users", headers=UV).json()["items"] if x["email"] == "amy.chen@nscale.com")
    assert u["primaryDevice"] == {"id": did, "missing": True}

    # Match devices: Sam has exactly one computer in Jamf; Amy's is gone and she has none now.
    m = c.post("/users/api/match-devices", headers=UE).json()
    assert m == {"matched": 1, "several": 0}
    sam = next(x for x in c.get("/users/api/users", headers=UV).json()["items"] if x["email"] == "sam.lee@nscale.com")
    assert sam["primaryDevice"]["serial"] == "C02BBB"

    assert c.delete("/users/api/users/amy.chen@nscale.com", headers=UE).status_code == 200
    assert c.delete("/users/api/users/amy.chen@nscale.com", headers=UE).status_code == 404


def test_csv_downloads(c):
    c.post("/users/api/users", headers=UE, json={"email": "eve@nscale.com", "name": "=HYPERLINK(1)"})
    body = c.get("/users/api/users.csv", headers=UV).text
    assert body.startswith("﻿Email,Name") and "'=HYPERLINK(1)" in body
    assert "Email" in c.get("/users/api/template.csv", headers=UV).text
    assert c.get("/devices/api/devices.csv", headers=DV).text.startswith("﻿Name,Platform,Make,Model,OS")


def test_addresses():
    assert connectors.jamf_url("nscale.jamfcloud.com/") == "https://nscale.jamfcloud.com"
    assert connectors.jamf_url("https://jamf.nscale.com:8443/") == "https://jamf.nscale.com:8443"
    for bad in ("http://x.com", "https://u:p@x.com", "https://x.com/?a=1", "ftp://x", ""):
        with pytest.raises(ValueError):
            connectors.jamf_url(bad)
    assert connectors.tenant_id("nscale.onmicrosoft.com") == "nscale.onmicrosoft.com"
    with pytest.raises(ValueError):
        connectors.tenant_id("../evil")


def test_old_team_tags_become_divisions(tmp_path):
    import sqlite3 as sq
    path = str(tmp_path / "old.db")
    db = sq.connect(path)
    db.execute("CREATE TABLE users (email TEXT PRIMARY KEY, name TEXT NOT NULL DEFAULT '', team TEXT DEFAULT '', manager TEXT DEFAULT '', "
               "user_type TEXT DEFAULT 'Standard', primary_device TEXT DEFAULT '', title TEXT DEFAULT '', department TEXT DEFAULT '', "
               "created_at TEXT, updated_at TEXT, updated_by TEXT DEFAULT '')")
    db.execute("INSERT INTO users (email, name, team) VALUES ('a@nscale.com', 'A', 'Platform')")
    db.commit()
    db.close()
    s = store_mod.Store(path, "s" * 40)
    assert s.user("a@nscale.com")["division"] == "Platform"
    store_mod.Store(path, "s" * 40)   # a second start doesn't run it again
    assert s.user("a@nscale.com")["division"] == "Platform"


def test_two_column_csv_updates_just_that(c):
    c.post("/users/api/users", headers=UE, json={"email": "amy@nscale.com", "name": "Amy Chen", "department": "Tech", "type": "Standard"})
    c.post("/users/api/users", headers=UE, json={"email": "sam@nscale.com", "name": "Sam Lee", "type": "VIP"})
    csv_text = "Email,Usertype\namy@nscale.com,VIP\nsam@nscale.com,VIP\nnew@nscale.com,Standard\n"
    dry = c.post("/users/api/import", headers=UE, json={"csv": csv_text, "dryRun": True}).json()
    assert dry["columns"] == [{"heading": "Email", "field": "Email"}, {"heading": "Usertype", "field": "User type"}]
    assert (dry["added"], dry["updated"], dry["unchanged"], dry["newInFile"]) == (1, 1, 1, 1)
    assert dry["changes"][0] == {"email": "amy@nscale.com", "name": "Amy Chen", "new": False,
                                 "fields": [{"field": "User type", "from": "Standard", "to": "VIP"}]}
    assert dry["changes"][1]["new"] and dry["changes"][1]["email"] == "new@nscale.com"

    # Only people already here: the new email is left out.
    r = c.post("/users/api/import", headers=UE, json={"csv": csv_text, "addNew": False}).json()
    assert (r["added"], r["updated"], r["unchanged"], r["skipped"]) == (0, 1, 1, 1)
    users = {u["email"]: u for u in c.get("/users/api/users", headers=UV).json()["items"]}
    assert set(users) == {"amy@nscale.com", "sam@nscale.com"}
    assert users["amy@nscale.com"]["type"] == "VIP" and users["amy@nscale.com"]["department"] == "Tech" and users["amy@nscale.com"]["name"] == "Amy Chen"
