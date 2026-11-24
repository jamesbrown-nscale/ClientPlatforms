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


def accept_all(c):
    """An import goes to Review: accept everything it sent there."""
    ids = [i["id"] for i in c.get("/users/api/review", headers=UV).json()["items"]]
    if ids:
        assert c.post("/users/api/review/act", headers=UE, json={"ids": ids, "action": "accept"}).status_code == 200


# ---------- Access ----------

def test_who_can_do_what(c):
    assert c.get("/healthz").json() == {"ok": True}
    assert c.get("/devices", follow_redirects=False).headers["location"] == "/devices/"
    assert c.get("/devices/api/devices").status_code == 401
    assert c.get("/devices/").status_code == 403
    assert "Devices" in c.get("/devices/", headers=DV).text and "Users" in c.get("/users/", headers=UV).text
    OV, PV = who("org", "view"), who("departments", "view")
    assert "Org chart" in c.get("/org/", headers=OV).text and "Departments" in c.get("/departments/", headers=PV).text
    assert c.get("/org/").status_code == 403 and c.get("/departments/", headers=UV).status_code == 403   # their own access
    assert c.get("/org/api/users", headers=OV).json()["items"] == [] and c.get("/org/api/users", headers=UV).status_code == 401
    assert c.get("/departments/api/me", headers=PV).json()["level"] == "view"
    assert c.post("/org/api/users", headers=OV).status_code == 405   # read only
    assert c.get("/users/org", headers=UV, follow_redirects=False).headers["location"] == "/org/"
    assert c.get("/org", follow_redirects=False).headers["location"] == "/org/"
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
    accept_all(c)
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
    accept_all(c)
    assert (again["added"], again["updated"], again["unchanged"]) == (0, 1, 1)
    amy = next(u for u in c.get("/users/api/users", headers=UV).json()["items"] if u["email"] == "amy.chen@nscale.com")
    assert amy["division"] == "Platform" and amy["type"] == "Standard" and amy["primaryDevice"]["serial"] == "C02AAA"

    stats = c.get("/users/api/users", headers=UV).json()
    assert stats["departments"] == [
        {"name": "Engineering", "count": 2, "divisions": [{"name": "Platform", "count": 2}]},
        {"name": "", "count": 1, "divisions": [{"name": "Support", "count": 1}]}]
    assert stats["stats"] == {"users": 3, "review": 0, "mismatches": 0, "vip": 0, "departments": 1, "withDevice": 1, "noDevice": 2}
    # The Team tool reads these, inside the stack.
    assert c.get("/internal/teams").json()["departments"] == stats["departments"]
    assert c.get("/internal/users/Amy.Chen@nscale.com").json() == {"email": "amy.chen@nscale.com", "name": "Amy Chen", "title": "",
        "department": "Engineering", "division": "Platform", "manager": "sam.lee@nscale.com", "managerName": "Sam Lee"}
    assert c.get("/internal/users/ghost@nscale.com").status_code == 404
    devs = {d["serial"]: d for d in c.get("/devices/api/devices", headers=DV).json()["items"]}
    assert devs["C02AAA"]["primaryFor"] == [{"email": "amy.chen@nscale.com", "name": "Amy Chen", "type": "Standard", "list": "Users - FTE", "url": "/users/"}]


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
    auto = p({}).json()["platformAuto"]
    u = p({"platform": "linux"}).json()                                       # tagged by hand
    assert (u["platform"], u["platformSet"], u["platformAuto"]) == ("Linux", "Linux", auto)
    assert p({"platform": "BeOS"}).status_code == 422
    u = p({"platform": ""}).json()                                            # back to Automatic
    assert (u["platform"], u["platformSet"]) == (auto, "")
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
    accept_all(c)
    assert (r["added"], r["updated"], r["unchanged"], r["skipped"]) == (0, 1, 1, 1)
    users = {u["email"]: u for u in c.get("/users/api/users", headers=UV).json()["items"]}
    assert set(users) == {"amy@nscale.com", "sam@nscale.com"}
    assert users["amy@nscale.com"]["type"] == "VIP" and users["amy@nscale.com"]["department"] == "Tech" and users["amy@nscale.com"]["name"] == "Amy Chen"


def test_several_devices(c, fake):
    # Amy has a Mac in Jamf (checked in 10:00) and a Windows laptop in Intune (09:00), plus an iPhone.
    connect_jamf(c)
    connect_intune(c)
    fake.mobile_status = 200
    main.sync("jamf")
    main.sync("intune")
    c.post("/users/api/users", headers=UE, json={"email": "amy.chen@nscale.com", "name": "Amy Chen"})
    m = c.post("/users/api/match-devices", headers=UE).json()
    assert m == {"matched": 1, "several": 1}
    amy = c.get("/users/api/users", headers=UV).json()["items"][0]
    assert amy["primaryDevice"]["serial"] == "C02AAA"                       # the computer that checked in last
    assert [d["serial"] for d in amy["otherDevices"]] == ["5CG111", "F2LXX"]   # latest check-in first; none last
    assert amy["devices"] == 3
    # Make another one primary: it leaves the others, the old one joins them.
    other = next(d for d in amy["otherDevices"] if d["serial"] == "5CG111")
    amy = c.patch("/users/api/users/amy.chen@nscale.com", headers=UE, json={"primaryDevice": other["id"]}).json()
    assert amy["primaryDevice"]["serial"] == "5CG111" and "C02AAA" in [d["serial"] for d in amy["otherDevices"]]
    assert "Other devices" in c.get("/users/api/users.csv", headers=UV).text
    assert main.pick_primary([]) is None


def test_not_checked_in_for_30_days(c):
    from datetime import datetime, timedelta, timezone
    ago = lambda days: (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    main.store.replace_devices("Intune", [
        {"id": "intune:a", "platform": "Intune", "name": "FRESH", "last_seen": ago(2)},
        {"id": "intune:b", "platform": "Intune", "name": "OLD", "last_seen": ago(31)},
        {"id": "intune:c", "platform": "Intune", "name": "NEVER", "last_seen": ""},
        {"id": "intune:d", "platform": "Intune", "name": "JAMF-STYLE", "last_seen": ago(45).replace("Z", ".123+00:00")}])
    r = c.get("/devices/api/devices", headers=DV).json()
    assert r["stats"]["stale"] == 3 and r["staleDays"] == 30
    assert {d["name"] for d in r["items"] if d["stale"]} == {"OLD", "NEVER", "JAMF-STYLE"}


def test_deleted_users_go_to_the_recycle_bin(c):
    UA = who("users")
    c.post("/users/api/users", headers=UE, json={"email": "amy.chen@nscale.com", "name": "Amy Chen", "department": "IT"})
    assert c.delete("/users/api/users/amy.chen@nscale.com", headers=UE).status_code == 200
    assert c.get("/users/api/bin", headers=UE).status_code == 403   # editors can delete, only admins see the bin
    item = c.get("/users/api/bin", headers=UA).json()["items"][0]
    assert item["kind"] == "User (FTE)" and item["label"] == "Amy Chen" and item["context"] == "amy.chen@nscale.com"
    assert c.post(f"/users/api/bin/{item['id']}/restore", headers=UA).status_code == 200
    users = {u["email"]: u for u in c.get("/users/api/users", headers=UV).json()["items"]}
    assert users["amy.chen@nscale.com"]["department"] == "IT"


def test_csv_can_leave_out_dep_devices(c):
    main.store.replace_devices("Jamf", [
        {"id": "jamf:1", "platform": "Jamf", "name": "DEP-C02XL0AAJGH5", "serial": "C02XL0AAJGH5"},
        {"id": "jamf:2", "platform": "Jamf", "name": "MB-LON-008", "serial": "C02YY1"},
        {"id": "jamf:3", "platform": "Jamf", "name": "Depot iMac", "serial": "C02ZZ2"}])   # "Dep…", not "DEP…": kept
    names = lambda q="": {row.split(",")[0] for row in c.get("/devices/api/devices.csv" + q, headers=DV).text.splitlines()[1:]}  # noqa: E731
    assert names() == {"DEP-C02XL0AAJGH5", "MB-LON-008", "Depot iMac"}
    assert names("?dep=hide") == {"MB-LON-008", "Depot iMac"}


def test_import_goes_to_review(c):
    """An import doesn't change Users: its new people, changes and leavers wait on Review to be accepted."""
    c.post("/users/api/users", headers=UE, json={"email": "amy@nscale.com", "name": "Amy Chen", "type": "Standard", "department": "Tech"})
    c.post("/users/api/users", headers=UE, json={"email": "sam@nscale.com", "name": "Sam Lee"})
    c.post("/users/api/users", headers=UE, json={"email": "gone@nscale.com", "name": "Gone Person", "department": "Sales"})
    csv_text = "Email,Name,User type\namy@nscale.com,Amy Chen,VIP\nsam@nscale.com,Sam Lee,\nnew@nscale.com,New Person,Standard\n"
    dry = c.post("/users/api/import", headers=UE, json={"csv": csv_text, "dryRun": True, "everyone": True}).json()
    assert (dry["added"], dry["updated"], dry["left"], dry["leftNames"]) == (1, 1, 1, ["Gone Person"])
    assert c.get("/users/api/review", headers=UV).json()["items"] == []   # a dry run sends nothing
    r = c.post("/users/api/import", headers=UE, json={"csv": csv_text, "everyone": True}).json()
    assert r["review"] == {"new": 1, "changed": 1, "left": 1}
    users = {u["email"]: u for u in c.get("/users/api/users", headers=UV).json()["items"]}
    assert users["amy@nscale.com"]["type"] == "Standard" and "new@nscale.com" not in users   # nothing changed yet
    assert c.get("/users/api/users", headers=UV).json()["stats"]["review"] == 3
    assert "<h1>Review</h1>" in c.get("/users/review", headers=UV).text and c.get("/users/review").status_code == 403
    rev = c.get("/users/api/review", headers=UV).json()
    assert rev["counts"] == {"new": 1, "changed": 1, "left": 1} and rev["importedBy"] == "admin@nscale.com"
    by = {i["kind"]: i for i in rev["items"]}
    assert by["changed"]["changes"] == [{"field": "User type", "from": "Standard", "to": "VIP"}] and by["changed"]["fields"] == {"user_type": "VIP"}
    assert by["left"]["email"] == "gone@nscale.com" and by["left"]["changes"][0]["from"] == "Sales"
    assert c.post("/users/api/review/act", headers=UV, json={"ids": [by["new"]["id"]], "action": "accept"}).status_code == 403
    assert c.post("/users/api/review/act", headers=UE, json={"ids": [by["new"]["id"]], "action": "dance"}).status_code == 422

    act = lambda kind, action: c.post("/users/api/review/act", headers=UE, json={"ids": [by[kind]["id"]], "action": action}).json()
    assert act("changed", "accept")["updated"] == 1
    assert act("new", "delete")["added"] == 0            # not added
    assert act("left", "keep")["deleted"] == 0           # kept
    users = {u["email"]: u for u in c.get("/users/api/users", headers=UV).json()["items"]}
    assert users["amy@nscale.com"]["type"] == "VIP" and users["amy@nscale.com"]["department"] == "Tech"
    assert "new@nscale.com" not in users and "gone@nscale.com" in users
    assert c.get("/users/api/review", headers=UV).json()["items"] == []

    # Again: accept the leaver (they're deleted, into the bin) and the new person (added).
    c.post("/users/api/import", headers=UE, json={"csv": csv_text, "everyone": True})
    rev = c.get("/users/api/review", headers=UV).json()["items"]
    out = c.post("/users/api/review/act", headers=UE, json={"ids": [i["id"] for i in rev], "action": "accept"}).json()
    assert (out["added"], out["deleted"], out["left"]) == (1, 1, 0)
    users = {u["email"] for u in c.get("/users/api/users", headers=UV).json()["items"]}
    assert "new@nscale.com" in users and "gone@nscale.com" not in users
    # Without "everyone", nobody is listed as left; a new import replaces what was waiting.
    c.post("/users/api/import", headers=UE, json={"csv": "Email,Department\namy@nscale.com,Ops\n"})
    c.post("/users/api/import", headers=UE, json={"csv": "Email,Department\nsam@nscale.com,Ops\n"})
    items = c.get("/users/api/review", headers=UV).json()["items"]
    assert [(i["kind"], i["email"]) for i in items] == [("changed", "sam@nscale.com")]


def test_platform_from_devices():
    from app.main import os_family, platform_of
    assert os_family("macOS") == "Mac" and os_family("Mac OS X") == "Mac"
    assert os_family("Windows") == "Windows" and os_family("Ubuntu") == "Linux" and os_family("Linux") == "Linux"
    assert os_family("iOS") == "" and os_family("iPadOS") == "" and os_family("Android") == "" and os_family("") == ""
    phone = {"os": "iOS", "last_seen": "2026-11-20T10:00:00Z"}
    win = {"os": "Windows", "last_seen": "2026-11-01T10:00:00Z"}
    mac = {"os": "macOS", "last_seen": "2026-11-10T10:00:00Z"}
    assert platform_of(win, [mac]) == "Windows"          # the primary device decides
    assert platform_of(phone, [win, mac]) == "Mac"       # a phone doesn't: their latest computer does
    assert platform_of(None, []) == ""


def test_invisible_characters_in_emails(c, tmp_path):
    """A zero-width space (HR export) or a non-breaking space (Jamf) mustn't stop a user matching their device."""
    from app.store import clean_email
    assert clean_email(" Aarika.Plunkett​@nscale.com ") == "aarika.plunkett@nscale.com"
    assert clean_email("a‎@x.com﻿") == "a@x.com"
    main.store.replace_devices("Jamf", [{"id": "jamf:c9", "platform": "Jamf", "kind": "computer", "name": "NS-Mac", "os": "macOS",
                                         "user_email": "aarika.plunkett@nscale.com ", "last_seen": "2026-11-23T10:00:00Z"}])
    c.post("/users/api/import", headers=UE, json={"csv": "Email,Name\n﻿aarika.plunkett@nscale.com​,Aarika Plunkett\n"})
    accept_all(c)
    u = c.get("/users/api/users", headers=UV).json()["items"][0]
    assert u["email"] == "aarika.plunkett@nscale.com" and u["platform"] == "Mac" and u["devices"] == 1

    # Saved before this fix: tidied when the service starts.
    path = str(tmp_path / "old.db")
    s = store_mod.Store(path, "s" * 40)
    with s._db() as db:
        db.execute("INSERT INTO users (email, name, manager) VALUES (?, 'Bo', ?)", ("bo@nscale.com​", "amy@nscale.com​"))
        db.execute("INSERT INTO devices (id, platform, user_email) VALUES ('d1', 'Jamf', ?)", ("bo@nscale.com ",))
    s = store_mod.Store(path, "s" * 40)
    assert [(u["email"], u["manager"]) for u in s.users()] == [("bo@nscale.com", "amy@nscale.com")]
    assert s.devices()[0]["user_email"] == "bo@nscale.com"


def test_mismatches_and_links(c):
    from app.main import suggest
    people = [{"email": "aarika.plunkett@nscale.com", "name": "Aarika Plunkett"}, {"email": "sam.lee@nscale.com", "name": "Sam Lee"}]
    assert suggest("aplunkett@nscale.com", "", people)[0]["email"] == "aarika.plunkett@nscale.com"
    assert suggest("x@other.io", "Aarika Plunkett", people)[0]["why"] == "Same name"
    assert suggest("aarika.plunkett@nscale.onmicrosoft.com", "", people)[0]["why"] == "Same before the @"
    assert suggest("Plunkett.Aarika@nscale.com".lower(), "", people)[0]["email"] == "aarika.plunkett@nscale.com"
    assert suggest("zed@nscale.com", "Zed Q", people) == []

    main.store.replace_devices("Jamf", [
        {"id": "jamf:c1", "platform": "Jamf", "kind": "computer", "name": "NS-Mac", "os": "macOS", "user_email": "aplunkett@nscale.com",
         "user_name": "Aarika Plunkett", "last_seen": "2026-11-23T10:00:00Z"},
        {"id": "jamf:c2", "platform": "Jamf", "kind": "computer", "name": "NS-Mac2", "os": "macOS", "user_email": "sam.lee@nscale.com",
         "user_name": "Sam Lee", "last_seen": "2026-11-23T10:00:00Z"}])
    c.post("/users/api/import", headers=UE, json={"csv": "Email,Name\naarika.plunkett@nscale.com,Aarika Plunkett\nsam.lee@nscale.com,Sam Lee\n"})
    accept_all(c)
    assert c.get("/users/api/users", headers=UV).json()["stats"]["mismatches"] == 1
    m = c.get("/users/api/mismatches", headers=UV).json()
    assert [d["email"] for d in m["devices"]] == ["aplunkett@nscale.com"]
    assert m["devices"][0]["suggestions"][0]["email"] == "aarika.plunkett@nscale.com"
    assert [u["email"] for u in m["users"]] == ["aarika.plunkett@nscale.com"] and m["users"][0]["suggestions"][0]["email"] == "aplunkett@nscale.com"
    devs = c.get("/devices/api/devices", headers=DV).json()["items"]
    assert [d["id"] for d in devs if d["notOnUsers"]] == ["jamf:c1"]

    link = lambda f, t: c.post("/users/api/links", headers=UE, json={"fromEmail": f, "toEmail": t})
    assert link("aplunkett@nscale.com", "nobody@nscale.com").status_code == 404
    assert link("sam.lee@nscale.com", "aarika.plunkett@nscale.com").status_code == 422   # Sam is on Users himself
    assert c.post("/users/api/links", headers=UV, json={"fromEmail": "a@b.co", "toEmail": "aarika.plunkett@nscale.com"}).status_code == 403
    assert link("aplunkett@nscale.com", "aarika.plunkett@nscale.com").status_code == 200
    u = next(u for u in c.get("/users/api/users", headers=UV).json()["items"] if u["email"] == "aarika.plunkett@nscale.com")
    assert u["platform"] == "Mac" and u["devices"] == 1 and u["primaryDevice"]["id"] == "jamf:c1"   # given a primary straight away
    m = c.get("/users/api/mismatches", headers=UV).json()
    assert m["devices"] == [] and m["users"] == [] and m["links"][0]["toName"] == "Aarika Plunkett" and m["links"][0]["devices"] == 1
    # The link outlasts a sync.
    main.store.replace_devices("Jamf", [{"id": "jamf:c1", "platform": "Jamf", "kind": "computer", "os": "macOS", "user_email": "aplunkett@nscale.com"}])
    assert c.get("/users/api/mismatches", headers=UV).json()["devices"] == []
    assert c.delete("/users/api/links/aplunkett@nscale.com", headers=UE).status_code == 200
    assert len(c.get("/users/api/mismatches", headers=UV).json()["devices"]) == 1
    assert c.delete("/users/api/links/aplunkett@nscale.com", headers=UE).status_code == 404
    assert "Mismatches" in c.get("/users/mismatches", headers=UV).text


def test_users_ct_is_its_own_list(c):
    """Users - CT (/users-ct/): the same page and options as Users, its own people, access, review and links."""
    CE, CV = who("users-ct", "edit"), who("users-ct", "view")
    main.store.replace_devices("Jamf", [
        {"id": "jamf:c1", "platform": "Jamf", "kind": "computer", "name": "NS-Mac", "os": "macOS", "user_email": "cara@nscale.com",
         "user_name": "Cara Doyle", "last_seen": "2026-11-23T10:00:00Z"},
        {"id": "jamf:c2", "platform": "Jamf", "kind": "computer", "name": "NS-Mac2", "os": "macOS", "user_email": "cdoyle@nscale.com",
         "user_name": "Cara Doyle", "last_seen": "2026-11-22T10:00:00Z"}])
    # Its own access: a Users token doesn't open it, nor a Users - CT token Users.
    assert c.get("/users-ct/api/users", headers=UV).status_code == 401 and c.get("/users/api/users", headers=CV).status_code == 401
    assert c.get("/users-ct/api/me", headers=CV).json()["listName"] == "Users - CT"
    assert "Users" in c.get("/users-ct/", headers=CV).text
    assert c.post("/users-ct/api/import", headers=CV, json={"csv": "Email\na@nscale.com\n"}).status_code == 403   # view only

    r = c.post("/users-ct/api/import", headers=CE, json={"csv": "Email,Name\ncara@nscale.com,Cara Doyle\n"}).json()
    assert r["review"]["new"] == 1
    assert c.get("/users/api/review", headers=UV).json()["items"] == []          # Users' review is untouched
    ids = [i["id"] for i in c.get("/users-ct/api/review", headers=CV).json()["items"]]
    assert c.post("/users-ct/api/review/act", headers=CE, json={"ids": ids, "action": "accept"}).json()["added"] == 1
    ct = c.get("/users-ct/api/users", headers=CV).json()
    assert [u["email"] for u in ct["items"]] == ["cara@nscale.com"] and c.get("/users/api/users", headers=UV).json()["items"] == []
    assert ct["items"][0]["platform"] == "Mac" and ct["items"][0]["devices"] == 1   # matched to the same devices
    assert c.post("/users-ct/api/match-devices", headers=CE).json()["matched"] == 1

    # Mismatches and links are its own too.
    m = c.get("/users-ct/api/mismatches", headers=CV).json()
    assert [d["email"] for d in m["devices"]] == ["cdoyle@nscale.com"] and m["devices"][0]["suggestions"][0]["email"] == "cara@nscale.com"
    assert c.post("/users-ct/api/links", headers=CE, json={"fromEmail": "cdoyle@nscale.com", "toEmail": "cara@nscale.com"}).status_code == 200
    assert c.get("/users-ct/api/users", headers=CV).json()["items"][0]["devices"] == 2
    assert main.store.links() == {} and c.get("/users-ct/api/mismatches", headers=CV).json()["links"][0]["toEmail"] == "cara@nscale.com"
    # A Users - CT user can't be linked from Users (she isn't on it).
    assert c.post("/users/api/links", headers=UE, json={"fromEmail": "cdoyle@nscale.com", "toEmail": "cara@nscale.com"}).status_code == 404

    # Deleting goes to the shared recycle bin, marked as from Users - CT, and comes back to it.
    assert c.delete("/users-ct/api/users/cara@nscale.com", headers=CE).status_code == 200
    assert c.get("/users-ct/api/users", headers=CV).json()["items"] == []
    with sqlite3.connect(main.store.path) as db:
        assert db.execute("SELECT kind, tool FROM recycle_bin").fetchall() == [("User (CT)", "users-ct")]
    # Org chart and Departments stay with Users.
    assert c.get("/org/api/org", headers=who("org", "view")).status_code in (200, 404)


def test_a_device_on_either_list_is_not_a_mismatch(c):
    CE, CV = who("users-ct", "edit"), who("users-ct", "view")
    main.store.replace_devices("Jamf", [
        {"id": "jamf:c1", "platform": "Jamf", "kind": "computer", "os": "macOS", "user_email": "amy@nscale.com", "user_name": "Amy"},
        {"id": "jamf:c2", "platform": "Jamf", "kind": "computer", "os": "macOS", "user_email": "cara@nscale.com", "user_name": "Cara"},
        {"id": "jamf:c3", "platform": "Jamf", "kind": "computer", "os": "macOS", "user_email": "nobody@nscale.com"}])
    c.post("/users/api/users", headers=UE, json={"email": "amy@nscale.com", "name": "Amy"})
    c.post("/users-ct/api/users", headers=CE, json={"email": "cara@nscale.com", "name": "Cara"})
    assert [d["email"] for d in c.get("/users/api/mismatches", headers=UV).json()["devices"]] == ["nobody@nscale.com"]
    assert [d["email"] for d in c.get("/users-ct/api/mismatches", headers=CV).json()["devices"]] == ["nobody@nscale.com"]
    assert [d["id"] for d in c.get("/devices/api/devices", headers=DV).json()["items"] if d["notOnUsers"]] == ["jamf:c3"]
    c.post("/users-ct/api/match-devices", headers=CE)
    d2 = next(d for d in c.get("/devices/api/devices", headers=DV).json()["items"] if d["id"] == "jamf:c2")
    assert d2["primaryFor"][0]["list"] == "Users - CT" and d2["primaryFor"][0]["url"] == "/users-ct/"


def test_mismatches_csv(c):
    main.store.replace_devices("Jamf", [
        {"id": "jamf:c1", "platform": "Jamf", "kind": "computer", "name": "NS-Mac", "serial": "S1", "os": "macOS", "os_version": "27.2",
         "user_email": "aplunkett@nscale.com", "user_name": "Aarika Plunkett", "last_seen": "2026-11-23T10:00:00Z"}])
    c.post("/users/api/users", headers=UE, json={"email": "aarika.plunkett@nscale.com", "name": "Aarika Plunkett"})
    r = c.get("/users/api/mismatches.csv?tab=devices", headers=UV)
    assert r.status_code == 200 and "mismatches-devices-" in r.headers["content-disposition"]
    lines = r.text.lstrip("﻿").splitlines()
    assert lines[0].startswith("Device email,Name in Jamf / Intune,Device,Serial")
    assert lines[1].startswith("aplunkett@nscale.com,Aarika Plunkett,NS-Mac,S1,Jamf,,macOS 27.2,") and "aarika.plunkett@nscale.com,Aarika Plunkett,Same name" in lines[1]
    u = c.get("/users/api/mismatches.csv?tab=users", headers=UV).text
    assert "aarika.plunkett@nscale.com" in u and "<aplunkett@nscale.com> (Same name)" in u
    c.post("/users/api/links", headers=UE, json={"fromEmail": "aplunkett@nscale.com", "toEmail": "aarika.plunkett@nscale.com"})
    assert "aplunkett@nscale.com,aarika.plunkett@nscale.com,Aarika Plunkett,1," in c.get("/users/api/mismatches.csv?tab=links", headers=UV).text
    assert c.get("/users/api/mismatches.csv?tab=x", headers=UV).status_code == 422
    assert "users-ct-mismatches-devices-" in c.get("/users-ct/api/mismatches.csv", headers=who("users-ct", "view")).headers["content-disposition"]


def test_suggestions_are_word_by_word():
    from app.main import suggest
    P = [{"email": "agnethe.vandam@nscale.com", "name": "Agnethe van Dam"}, {"email": "adam.bullington@nscale.com", "name": "Adam Bullington"},
         {"email": "adam.smith@nscale.com", "name": "Adam Smith"}, {"email": "jonathan.ward@nscale.com", "name": "Jonathan Ward"},
         {"email": "mary-kate.o'neil@nscale.com", "name": "Mary-Kate O'Neil"}]
    best = lambda e, n="": (suggest(e, n, P) or [{}])[0]
    # The screenshot: "dam" inside "adam" is not Agnethe van Dam; the .ct tag is ignored.
    s = suggest("adam.bullington.ct@nscale.com", "Adam Bullington", P)
    assert [x["email"] for x in s] == ["adam.bullington@nscale.com"] and s[0]["why"] == "Same email (without “ct”)"
    assert suggest("adam.bullington.ct@nscale.com", "Adam Bullington", [P[0]]) == []
    assert best("abullington@nscale.com")["email"] == "adam.bullington@nscale.com"
    assert best("bullington.adam@nscale.com")["email"] == "adam.bullington@nscale.com"
    assert best("adam.bullington2@nscale.com")["email"] == "adam.bullington@nscale.com"
    assert best("adam.bullington@nscale.onmicrosoft.com")["why"] == "Same before the @"
    assert best("a.vandam@nscale.com")["email"] == "agnethe.vandam@nscale.com"
    assert best("agnethe.dam@nscale.com")["email"] == "agnethe.vandam@nscale.com"
    assert best("xyz@nscale.com", "Jon Ward")["email"] == "jonathan.ward@nscale.com"     # Similar name
    assert best("mkoneil@nscale.com", "Mary-Kate O'Neil")["why"] == "Same name"
    assert suggest("adam@nscale.com", "", P) == []                                     # a first name alone isn't enough
    assert suggest("dam.ops@nscale.com", "", P) == []


def test_suggestions_from_the_other_list(c):
    CE, CV = who("users-ct", "edit"), who("users-ct", "view")
    main.store.replace_devices("Intune", [{"id": "intune:1", "platform": "Intune", "kind": "computer", "os": "Windows",
                                           "user_email": "adam.bullington.ct@nscale.com", "user_name": "Adam Bullington"}])
    c.post("/users-ct/api/users", headers=CE, json={"email": "adam.bullington@nscale.com", "name": "Adam Bullington"})
    s = c.get("/users/api/mismatches", headers=UV).json()["devices"][0]["suggestions"][0]
    assert (s["email"], s["list"], s["listName"], s["linkUrl"]) == ("adam.bullington@nscale.com", "users-ct", "Users - CT", "/users-ct/api/links")
    assert c.get("/users/api/mismatches", headers=UV).json()["users"] == []
    assert "Same email (without “ct”) (on Users - CT)" in c.get("/users/api/mismatches.csv", headers=UV).text
    assert c.post(s["linkUrl"], headers=CE, json={"fromEmail": "adam.bullington.ct@nscale.com", "toEmail": s["email"]}).status_code == 200
    assert c.get("/users/api/mismatches", headers=UV).json()["devices"] == []        # matched on the other list
    assert c.get("/users-ct/api/users", headers=CV).json()["items"][0]["platform"] == "Windows"


def test_location(c):
    """Location: from a CSV (Office or Location column), set by hand, cleared, and in the CSV download."""
    r = c.post("/users/api/import", headers=UE, json={"csv": "Email,Name,Office\namy.chen@nscale.com,Amy Chen,London\nsam.lee@nscale.com,Sam Lee,\n"}).json()
    accept_all(c)
    assert r["added"] == 2
    users = {u["email"]: u for u in c.get("/users/api/users", headers=UV).json()["items"]}
    assert users["amy.chen@nscale.com"]["location"] == "London" and users["sam.lee@nscale.com"]["location"] == ""
    assert c.patch("/users/api/users/sam.lee@nscale.com", headers=UE, json={"location": "  New   York "}).status_code == 200
    again = c.post("/users/api/import", headers=UE, json={"csv": "email,location\namy.chen@nscale.com,Remote\n"}).json()
    accept_all(c)
    assert again["updated"] == 1
    users = {u["email"]: u for u in c.get("/users/api/users", headers=UV).json()["items"]}
    assert users["sam.lee@nscale.com"]["location"] == "New York" and users["amy.chen@nscale.com"]["location"] == "Remote"
    head, *rows = c.get("/users/api/users.csv", headers=UV).text.splitlines()
    cols = head.split(",")
    assert cols.index("Location") == cols.index("Line manager") + 1
    assert "Location" in c.get("/users/api/template.csv", headers=UV).text.splitlines()[0]


def test_all_users_shows_both_lists(c):
    AV = who("all-users", "view")
    main.store.replace_devices("Jamf", [{"id": "jamf:c1", "platform": "Jamf", "kind": "computer", "os": "macOS", "name": "NS-Mac",
                                         "user_email": "cara@nscale.com", "last_seen": "2026-11-23T10:00:00Z"}])
    c.post("/users/api/users", headers=UE, json={"email": "amy@nscale.com", "name": "Amy", "type": "VIP", "department": "Eng"})
    c.post("/users-ct/api/users", headers=who("users-ct", "edit"), json={"email": "cara@nscale.com", "name": "Cara", "department": "Ops"})
    assert c.get("/all-users/api/users", headers=UV).status_code == 401          # its own access
    r = c.get("/all-users/api/users", headers=AV).json()
    assert [(u["email"], u["list"], u["listName"], u["url"]) for u in r["items"]] == [
        ("amy@nscale.com", "users", "Users - FTE", "/users/"), ("cara@nscale.com", "users-ct", "Users - CT", "/users-ct/")]
    assert next(u for u in r["items"] if u["email"] == "cara@nscale.com")["platform"] == "Mac"   # matched on her own list
    assert (r["stats"]["users"], r["stats"]["users_fte"], r["stats"]["users_ct"], r["stats"]["vip"]) == (2, 1, 1, 1)
    assert r["departments"] == ["Eng", "Ops"]
    assert c.get("/all-users/api/me", headers=AV).json()["lists"][0]["name"] == "Users - FTE"
    assert "All Users" in c.get("/all-users/", headers=AV).text
    csv_ = c.get("/all-users/api/users.csv", headers=AV).text.lstrip("﻿").splitlines()
    assert csv_[0].startswith("List,Email,Name") and csv_[1].startswith("Users - FTE,amy@nscale.com") and csv_[2].startswith("Users - CT,cara@nscale.com")
    assert c.post("/all-users/api/users", headers=AV, json={}).status_code == 405   # read only
    assert c.get("/users/api/me", headers=UV).json()["listName"] == "Users - FTE"
