"""App Packaging: the .intunewin format, reading installers, the commands, uploads, clearing out old packages, access."""
import base64
import io
import os
import re
import struct
import tempfile
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

os.environ.update(BASE_PATH="/packaging", AUTH_MODE="local", AUTH_SECRET="s" * 40, PRUNE_LOOP="0",
                  DATABASE_PATH=os.path.join(tempfile.mkdtemp(), "p.db"))
os.environ.pop("DEV_USER", None)

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import access, installers, intunewin, main  # noqa: E402

H = {"X-Requested-With": "nscale-admin"}


def who(level="edit", tool="packaging"):
    return {**H, "X-CP-User": access.issue("amy.chen@nscale.com", "Amy Chen", tool, level == "admin", level=level)}


EDIT, VIEW = who(), who("view")


@pytest.fixture
def c(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "store", main.Store(str(tmp_path / "p.db")))
    with TestClient(main.app) as client:
        yield client


# ---------- The format ----------

def test_intunewin_round_trip(tmp_path):
    src = tmp_path / "src"
    (src / "files").mkdir(parents=True)
    (src / "Install.ps1").write_text("Write-Host hi")
    (src / "files" / "app.dat").write_bytes(os.urandom(300_000))
    r = intunewin.build(src, "Install.ps1", tmp_path / "out.intunewin", name="Thing")
    with zipfile.ZipFile(tmp_path / "out.intunewin") as z:
        assert z.namelist() == ["IntuneWinPackage/Contents/IntunePackage.intunewin", "IntuneWinPackage/Metadata/Detection.xml"]
        xml = z.read("IntuneWinPackage/Metadata/Detection.xml").decode()
        enc = z.read("IntuneWinPackage/Contents/IntunePackage.intunewin")
    tag = lambda k: re.search(f"<{k}>([^<]*)</{k}>", xml).group(1)   # noqa: E731
    assert tag("Name") == "Thing" and tag("SetupFile") == "Install.ps1" and tag("FileName") == "IntunePackage.intunewin"
    assert tag("ProfileIdentifier") == "ProfileVersion1" and tag("FileDigestAlgorithm") == "SHA256" and "<MsiInfo>" not in xml
    assert len(base64.b64decode(tag("EncryptionKey"))) == 32 and len(base64.b64decode(tag("InitializationVector"))) == 16
    assert enc[32:48] == base64.b64decode(tag("InitializationVector")) and enc[:32] == base64.b64decode(tag("Mac"))
    (tmp_path / "enc").write_bytes(enc)
    intunewin.decrypt(tmp_path / "enc", {"EncryptionKey": tag("EncryptionKey"), "MacKey": tag("MacKey")}, tmp_path / "plain.zip")
    import hashlib
    plain = (tmp_path / "plain.zip").read_bytes()
    assert base64.b64encode(hashlib.sha256(plain).digest()).decode() == tag("FileDigest") == r["digest"]
    assert int(tag("UnencryptedContentSize")) == len(plain)
    with zipfile.ZipFile(io.BytesIO(plain)) as z:
        assert sorted(z.namelist()) == ["Install.ps1", "files/app.dat"]   # the folder's layout, no top folder
        assert z.read("files/app.dat") == (src / "files" / "app.dat").read_bytes()
    (tmp_path / "bad").write_bytes(enc[:-1] + bytes([enc[-1] ^ 1]))
    with pytest.raises(ValueError):
        intunewin.decrypt(tmp_path / "bad", {"EncryptionKey": tag("EncryptionKey"), "MacKey": tag("MacKey")}, tmp_path / "x")
    xml2 = intunewin.detection_xml("App", "a.msi", {**{k: "x" for k in ("EncryptionKey", "MacKey", "InitializationVector", "Mac",
                                   "ProfileIdentifier", "FileDigest", "FileDigestAlgorithm")}, "_size": 1},
                                   {"MsiProductCode": "{ABC}", "MsiIsMachineInstall": True, "MsiPublisher": "A & B"})
    assert "<MsiProductCode>{ABC}</MsiProductCode>" in xml2 and "<MsiIsMachineInstall>true</MsiIsMachineInstall>" in xml2 and "A &amp; B" in xml2


# ---------- Reading an MSI's tables ----------

def _enc_name(name: str, table=True) -> str:
    """The inverse of msi_stream_name, to check it."""
    alpha = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz._"
    out, i = ["\u4840"] if table else [], 0
    while i < len(name):
        a = alpha.index(name[i])
        if i + 1 < len(name):
            out.append(chr(0x3800 + a + (alpha.index(name[i + 1]) << 6))); i += 2
        else:
            out.append(chr(0x4800 + a)); i += 1
    return "".join(out)


def test_msi_tables():
    for n in ("Property", "_StringPool", "ServiceInstall", "File"):
        assert installers.msi_stream_name(_enc_name(n)) == (n, True)
    assert installers.msi_stream_name("\x05SummaryInformation") == ("\x05SummaryInformation", False)
    strings = ["ProductCode", "{11111111-2222-3333-4444-555555555555}", "ProductName", "Café App", "ALLUSERS", "1"]
    data = "".join(strings).encode("cp1252")
    pool = struct.pack("<HH", 1252, 0) + b"".join(struct.pack("<HH", len(s.encode("cp1252")), 1) for s in strings)
    got, width = installers.msi_strings(pool, data)
    assert got == [""] + strings and width == 2
    keys, vals = [1, 3, 5], [2, 4, 6]
    table = b"".join(struct.pack("<H", k) for k in keys) + b"".join(struct.pack("<H", v) for v in vals)
    k, v = installers.msi_columns(table, 2, width)
    assert {got[a]: got[b] for a, b in zip(k, v)} == {"ProductCode": "{11111111-2222-3333-4444-555555555555}", "ProductName": "Café App", "ALLUSERS": "1"}
    # Long string references (3 bytes) are flagged in the pool's header.
    assert installers.msi_strings(struct.pack("<HH", 1252, 0x8000), b"")[1] == 3


def test_msi_commands(tmp_path, monkeypatch):
    (tmp_path / "app.msi").write_bytes(b"x")
    (tmp_path / "custom.mst").write_bytes(b"x")
    monkeypatch.setattr(installers, "msi_info", lambda p: {
        "name": "Agent Manager", "version": "2.1", "publisher": "Acme", "productCode": "{AAA}", "upgradeCode": "{BBB}", "packageCode": "{CCC}",
        "detection_xml": {"MsiProductCode": "{AAA}"}, "machine": False})
    a = installers.analyse(tmp_path, "app.msi")
    assert a["install"].startswith('msiexec /i "app.msi" TRANSFORMS="custom.mst" /qn /norestart /l*v "%ProgramData%\\Microsoft\\IntuneManagementExtension\\Logs\\AgentManager-install.log"')
    assert a["uninstall"].startswith('msiexec /x "{AAA}" /qn /norestart')
    assert a["detection"]["productCode"] == "{AAA}" and a["behaviour"] == "User" and any("per user" in n for n in a["notes"])
    assert a["msi"] == {"MsiProductCode": "{AAA}"}


@pytest.mark.parametrize("marker,builder,install,uninstall", [
    (b"Inno Setup Setup Data (6.2.0)", "Inno Setup", '"setup.exe" /VERYSILENT /SUPPRESSMSGBOXES /NORESTART /SP-', "unins000.exe"),
    (b"Nullsoft.NSIS.exehead", "NSIS", '"setup.exe" /S', "Uninstall.exe"),
    (b"\x00.wixburn\x00", "WiX Burn", '"setup.exe" /quiet /norestart', '"setup.exe" /uninstall /quiet /norestart'),
    (b"Advanced Installer 21.0", "Advanced Installer", '"setup.exe" /exenoui /qn /norestart', '"setup.exe" /x /exenoui /qn /norestart'),
    (b"InstallShield(R)", "InstallShield", '"setup.exe" /s /v"/qn /norestart"', "<ProductCode>"),
    (b"nothing to see", "", '"setup.exe" /S', "<Uninstall command"),
])
def test_exe_builders(tmp_path, marker, builder, install, uninstall):
    (tmp_path / "setup.exe").write_bytes(b"MZ" + b"\0" * 100 + marker + b"\0" * 100)
    a = installers.analyse(tmp_path, "setup.exe")
    assert a["kind"] == "EXE" and a["builder"] == builder and a["install"].startswith(install) and uninstall in a["uninstall"]
    assert a["name"] == "setup" and a["detection"]["type"] == "Registry"


def test_scripts_and_picking(tmp_path):
    (tmp_path / "Install.ps1").write_text("x")
    (tmp_path / "Uninstall.ps1").write_text("x")
    a = installers.analyse(tmp_path, "Install.ps1")
    assert a["install"].endswith('-File "Install.ps1"') and "sysnative" in a["install"] and a["uninstall"].endswith('-File "Uninstall.ps1"')
    (tmp_path / "Uninstall.ps1").unlink()
    assert installers.analyse(tmp_path, "Install.ps1")["uninstall"].endswith('"Install.ps1" -Uninstall')
    (tmp_path / "go.cmd").write_text("x")
    assert installers.analyse(tmp_path, "go.cmd")["install"] == 'cmd.exe /c "go.cmd"'
    (tmp_path / "readme.txt").write_text("x")
    with pytest.raises(ValueError):
        installers.analyse(tmp_path, "readme.txt")
    assert installers.pick_setup(["readme.txt", "tools/helper.exe", "setup.exe", "app.msi"]) == "app.msi"
    assert installers.pick_setup(["Uninstall.ps1", "Install.ps1", "files/a.dll"]) == "Install.ps1"
    assert installers.pick_setup(["Deploy/Install.ps1", "Deploy/setup.exe"]) == "Deploy/setup.exe"
    assert installers.pick_setup(["a.txt"]) == ""


# ---------- The API ----------

def upload(c, files, headers=EDIT, **form):
    return c.post("/packaging/api/packages", headers=headers, files=[("files", (n, d)) for n, d in files], data=form)


def test_package_a_folder(c):
    r = upload(c, [("Deploy/Install.ps1", b"Write-Host hi"), ("Deploy/Uninstall.ps1", b"x"), ("Deploy/files/app.dat", b"1234")])
    assert r.status_code == 200, r.text
    p = r.json()
    # The folder they picked is the package: its own name isn't part of the paths.
    assert p["setupFile"] == "Install.ps1" and [f["path"] for f in p["files"]] == ["files/app.dat", "Install.ps1", "Uninstall.ps1"]
    assert p["kind"] == "PowerShell" and p["name"] == "Install" and p["available"] and p["fileName"] == "Install.intunewin"
    assert p["uninstall"].endswith('"Uninstall.ps1"') and p["sourceSize"] == 13 + 1 + 4
    d = c.get(f"/packaging/api/packages/{p['id']}/download", headers=VIEW)
    assert d.status_code == 200 and 'filename="Install.intunewin"' in d.headers["content-disposition"]
    with zipfile.ZipFile(io.BytesIO(d.content)) as z:
        assert "IntuneWinPackage/Metadata/Detection.xml" in z.namelist()
    items = c.get("/packaging/api/packages", headers=VIEW).json()
    assert [i["id"] for i in items["items"]] == [p["id"]] and items["stored"] == p["size"]
    assert not list(main.store.work.iterdir())   # nothing left behind


def test_upload_checks(c):
    assert upload(c, [("a.ps1", b"x")], headers=VIEW).status_code == 403                       # viewers can't package
    assert upload(c, [("a.ps1", b"x")], headers=who(tool="restart")).status_code == 401         # its own access
    assert c.post("/packaging/api/packages", headers={"X-CP-User": EDIT["X-CP-User"]}, files=[("files", ("a.ps1", b"x"))]).status_code == 403
    assert "Choose" in c.post("/packaging/api/packages", headers=EDIT, data={"name": "x"}).json()["detail"]
    assert "Pick the file" in upload(c, [("readme.txt", b"x")]).json()["detail"]
    for bad in ("../evil.ps1", "C:/Windows/a.ps1", "a/../../b.ps1", "con?.ps1"):
        assert upload(c, [(bad, b"x")]).status_code == 422, bad
    assert "same name" in upload(c, [("a.ps1", b"x"), ("A.PS1", b"y")]).json()["detail"]
    r = upload(c, [("tool.exe", b"MZ" + b"\0" * 64), ("Install.ps1", b"x")], setup="Install.ps1", name="  My   App ")
    assert r.json()["setupFile"] == "Install.ps1" and r.json()["name"] == "My App"
    assert "Pick the file" in upload(c, [("a.ps1", b"x")], setup="b.ps1").json()["detail"]


def test_too_big(c, monkeypatch):
    monkeypatch.setattr(main, "MAX_UPLOAD", 1000)
    assert upload(c, [("a.ps1", b"x" * 5000)]).status_code == 413
    assert not list(main.store.work.iterdir())


def test_change_and_delete(c):
    p = upload(c, [("app.cmd", b"echo")]).json()
    assert p["uninstall"] == "<Uninstall command>"
    assert c.patch(f"/packaging/api/packages/{p['id']}", headers=VIEW, json={"name": "x"}).status_code == 403
    r = c.patch(f"/packaging/api/packages/{p['id']}", headers=EDIT, json={"name": "Thing", "uninstall": 'cmd.exe /c "remove.cmd"',
                                                                          "behaviour": "User", "detection": "File C:\\x", "comment": "Tested"}).json()
    assert (r["name"], r["uninstall"], r["behaviour"], r["detection"]["text"], r["comment"]) == ("Thing", 'cmd.exe /c "remove.cmd"', "User", "File C:\\x", "Tested")
    assert c.patch(f"/packaging/api/packages/{p['id']}", headers=EDIT, json={"install": " "}).status_code == 422
    assert c.patch(f"/packaging/api/packages/{p['id']}", headers=EDIT, json={"behaviour": "Both"}).status_code == 422
    assert c.delete(f"/packaging/api/packages/{p['id']}", headers=VIEW).status_code == 403
    assert c.delete(f"/packaging/api/packages/{p['id']}", headers=EDIT).status_code == 200
    assert not main.store.file_of(p["id"]).exists() and c.get(f"/packaging/api/packages/{p['id']}", headers=VIEW).status_code == 404


def test_old_and_too_many_packages_are_cleared(c, monkeypatch):
    a = upload(c, [("a.cmd", b"a")]).json()
    b = upload(c, [("b.cmd", b"b" * 10)]).json()
    old = (datetime.now(timezone.utc) - timedelta(days=8)).strftime("%Y-%m-%dT%H:%M:%SZ")
    with main.store.db() as db:
        db.execute("UPDATE packages SET created_at = ? WHERE id = ?", (old, a["id"]))
    assert main.prune() == 1
    got = c.get(f"/packaging/api/packages/{a['id']}", headers=VIEW).json()
    assert not got["available"] and "7 days" in got["removedWhy"] and got["install"] == 'cmd.exe /c "a.cmd"'   # its details stay
    assert c.get(f"/packaging/api/packages/{a['id']}/download", headers=VIEW).status_code == 410
    # Past the total, the oldest go first.
    monkeypatch.setattr(main, "MAX_TOTAL", b["size"] + 10)
    cc = upload(c, [("c.cmd", b"c")]).json()
    assert c.get(f"/packaging/api/packages/{b['id']}", headers=VIEW).json()["available"] is False
    assert c.get(f"/packaging/api/packages/{cc['id']}", headers=VIEW).json()["available"] is True


def test_page_and_me(c):
    assert c.get("/packaging/", headers=VIEW).status_code == 200
    assert c.get("/packaging/").status_code == 403
    me = c.get("/packaging/api/me", headers=VIEW).json()
    assert me["canEdit"] is False and me["keepDays"] == 7 and me["maxTotalGb"] == 5
    assert c.get("/healthz").json() == {"ok": True}
