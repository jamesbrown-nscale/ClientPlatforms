"""Nscale App Packaging, on Client Platforms (/packaging/).

Upload an installer (.msi or .exe), a script (.ps1, .cmd, .bat) or a whole folder of files, pick which file
Intune should run, and it's packaged as an .intunewin (intunewin.py: Microsoft's format, made here on
Linux) with its install and uninstall commands, install behaviour and a detection rule worked out from the
installer (installers.py).

People:  /                                   the page
         GET    /api/me
         GET    /api/packages                every package, newest first
         POST   /api/packages                multipart: files (each one's filename is its path in the folder),
                                             setup (the file to run; worked out if left out), name (optional)
         GET    /api/packages/{id}           one package
         PATCH  /api/packages/{id}           change its name, commands, detection or notes (edit)
         GET    /api/packages/{id}/download  the .intunewin
         DELETE /api/packages/{id}           (edit)

Packages are kept KEEP_DAYS (7) and, past MAX_TOTAL_GB (5) in all, the oldest go first: the VM's disk is
small. A package's details and commands stay after its file has gone. Uploads are at most MAX_UPLOAD_MB
(2048) in all.

Settings: DATABASE_PATH (/data/packaging.db; packages are kept next to it), KEEP_DAYS, MAX_TOTAL_GB,
MAX_UPLOAD_MB, AUTH_MODE, AUTH_SECRET, PORTAL_URL, BASE_PATH.
"""
import json
import logging
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.datastructures import UploadFile

from . import access, auth, installers, intunewin
from .auth import User

BASE_PATH = os.environ.get("BASE_PATH", "").rstrip("/")
PORTAL_URL = os.environ.get("PORTAL_URL", "/")
DATABASE_PATH = os.environ.get("DATABASE_PATH", "/data/packaging.db")
KEEP_DAYS = float(os.environ.get("KEEP_DAYS", "7"))
MAX_TOTAL = int(float(os.environ.get("MAX_TOTAL_GB", "5")) * 1024 ** 3)
MAX_UPLOAD = int(float(os.environ.get("MAX_UPLOAD_MB", "2048")) * 1024 ** 2)
MAX_FILES = 5000
STATIC = Path(__file__).parent / "static"

log = logging.getLogger("packaging")
log.setLevel(logging.INFO)
_h = logging.StreamHandler(sys.stdout)
_h.setFormatter(logging.Formatter("%(message)s"))
log.addHandler(_h)
log.propagate = False


def now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


class Store:
    """packages: one row per package made. Its .intunewin is <dir>/packages/<id>.intunewin until it expires."""
    def __init__(self, path: str):
        self.path = path
        self.dir = Path(path).parent
        self.files = self.dir / "packages"
        self.work = self.dir / "work"
        self.files.mkdir(parents=True, exist_ok=True)
        self.work.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
        with self.db() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS packages (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, version TEXT DEFAULT '', publisher TEXT DEFAULT '',
                kind TEXT DEFAULT '', builder TEXT DEFAULT '', setup_file TEXT NOT NULL, files TEXT NOT NULL DEFAULT '[]',
                install TEXT DEFAULT '', uninstall TEXT DEFAULT '', behaviour TEXT DEFAULT 'System', detection TEXT DEFAULT '{}',
                notes TEXT DEFAULT '[]', product_code TEXT DEFAULT '', upgrade_code TEXT DEFAULT '', comment TEXT DEFAULT '',
                size INTEGER DEFAULT 0, source_size INTEGER DEFAULT 0, digest TEXT DEFAULT '', has_file INTEGER DEFAULT 1,
                removed_why TEXT DEFAULT '', created_at TEXT, created_by TEXT DEFAULT '', created_by_name TEXT DEFAULT '',
                updated_at TEXT, updated_by TEXT DEFAULT '')""")
        # Anything left half-done by a restart mid-upload.
        for p in self.work.iterdir():
            shutil.rmtree(p, ignore_errors=True) if p.is_dir() else p.unlink(missing_ok=True)

    def db(self):
        d = sqlite3.connect(self.path, timeout=10)
        d.row_factory = sqlite3.Row
        return d

    def file_of(self, pid: str) -> Path:
        return self.files / f"{pid}.intunewin"


store = Store(DATABASE_PATH)
auth.check_settings()


def view(r) -> dict:
    created = r["created_at"] or ""
    try:
        expires = (datetime.strptime(created, "%Y-%m-%dT%H:%M:%SZ") + timedelta(days=KEEP_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        expires = ""
    stem = re.sub(r"[^\w.-]+", "_", PurePosixPath(r["setup_file"]).stem) or "package"
    return {"id": r["id"], "name": r["name"], "version": r["version"], "publisher": r["publisher"], "kind": r["kind"],
            "builder": r["builder"], "setupFile": r["setup_file"], "files": json.loads(r["files"] or "[]"),
            "install": r["install"], "uninstall": r["uninstall"], "behaviour": r["behaviour"],
            "detection": json.loads(r["detection"] or "{}"), "notes": json.loads(r["notes"] or "[]"),
            "productCode": r["product_code"], "upgradeCode": r["upgrade_code"], "comment": r["comment"],
            "size": r["size"], "sourceSize": r["source_size"], "digest": r["digest"],
            "available": bool(r["has_file"]), "removedWhy": r["removed_why"], "expiresAt": expires if r["has_file"] else "",
            "fileName": f"{stem}.intunewin", "createdAt": created, "createdBy": r["created_by"], "createdByName": r["created_by_name"],
            "updatedAt": r["updated_at"], "updatedBy": r["updated_by"]}


def prune() -> int:
    """Removes package files past KEEP_DAYS, then the oldest until the rest fit in MAX_TOTAL. Their details stay."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=KEEP_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    gone = 0
    with store.lock, store.db() as db:
        rows = db.execute("SELECT id, created_at, size FROM packages WHERE has_file = 1 ORDER BY created_at DESC, rowid DESC").fetchall()
        total = 0
        for r in rows:
            why = ""
            if (r["created_at"] or "") < cutoff:
                why = f"Removed after {KEEP_DAYS:g} days"
            elif total + (r["size"] or 0) > MAX_TOTAL:
                why = "Removed to make room (packages are kept up to {:g} GB in all)".format(MAX_TOTAL / 1024 ** 3)
            else:
                total += r["size"] or 0
            if why:
                store.file_of(r["id"]).unlink(missing_ok=True)
                db.execute("UPDATE packages SET has_file = 0, removed_why = ? WHERE id = ?", (why, r["id"]))
                gone += 1
    return gone


def _loop():
    while True:
        try:
            prune()
        except Exception:
            log.exception("prune")
        threading.Event().wait(3600)


# ---------- The app ----------

app = FastAPI(title="Nscale App Packaging", docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.exception_handler(HTTPException)
def _http_error(request: Request, exc: HTTPException):
    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code, headers=exc.headers)


@app.exception_handler(ValueError)
def _value_error(request: Request, exc: ValueError):
    return JSONResponse({"detail": str(exc)}, status_code=422)


def _page(message: str, status_code: int) -> HTMLResponse:
    return HTMLResponse(f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Client Platforms</title></head>
<body style="font:14px/1.5 'Segoe UI',system-ui,sans-serif;display:grid;place-items:center;min-height:90vh;background:#F3F5FB;color:#040E27">
<p>{message}</p></body></html>""", status_code=status_code)


@app.get("/")
def page(request: Request):
    if not auth.current_user(request):
        return _page(access.sign_in_message(), 403)
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-store"})


@app.get("/api/me")
def me(user: User = Depends(auth.viewer)):
    return {"email": user.email, "name": user.name, "level": user.level, "canEdit": user.can_edit, "portalUrl": PORTAL_URL,
            "keepDays": KEEP_DAYS, "maxTotalGb": MAX_TOTAL / 1024 ** 3, "maxUploadMb": MAX_UPLOAD / 1024 ** 2}


@app.get("/api/packages")
def list_packages(user: User = Depends(auth.viewer)):
    with store.db() as db:
        rows = db.execute("SELECT * FROM packages ORDER BY created_at DESC, rowid DESC").fetchall()
    items = [view(r) for r in rows]
    return {"items": items, "stored": sum(i["size"] for i in items if i["available"])}


def _one(pid: str) -> dict:
    with store.db() as db:
        r = db.execute("SELECT * FROM packages WHERE id = ?", (pid,)).fetchone()
    if not r:
        raise HTTPException(404, "That package isn't here any more.")
    return view(r)


@app.get("/api/packages/{pid}")
def get_package(pid: str, user: User = Depends(auth.viewer)):
    return _one(pid)


def clean_path(name: str) -> str:
    """A file's path inside the package: forward slashes, no drive, no .. and nothing hidden from Windows."""
    parts = [p for p in re.split(r"[\\/]+", name or "") if p not in ("", ".")]
    if not parts or any(p == ".." for p in parts) or re.match(r"^[A-Za-z]:$", parts[0]):
        raise ValueError(f"“{name}” isn't a file name we can use.")
    bad = re.compile(r'[<>:"|?*\x00-\x1f]')
    if any(bad.search(p) or p.endswith((" ", ".")) for p in parts):
        raise ValueError(f"“{name}” has characters Windows doesn't allow in a file name.")
    return "/".join(parts)


def _common_root(paths: list[str]) -> str:
    """A folder upload sends "MyApp/setup.exe": when every file is in one top folder, that folder is the package."""
    tops = {p.split("/", 1)[0] for p in paths}
    return next(iter(tops)) if len(tops) == 1 and all("/" in p for p in paths) else ""


def make_package(src: Path, setup: str, name: str, user: User, files: list[dict]) -> dict:
    info = installers.analyse(src, setup)
    pid = uuid.uuid4().hex[:16]
    built = intunewin.build(src, setup, store.file_of(pid), name=info["name"] or PurePosixPath(setup).name, msi=info["msi"])
    t = now()
    with store.lock, store.db() as db:
        db.execute("""INSERT INTO packages (id, name, version, publisher, kind, builder, setup_file, files, install, uninstall, behaviour,
                      detection, notes, product_code, upgrade_code, size, source_size, digest, has_file, created_at, created_by, created_by_name,
                      updated_at, updated_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?)""",
                   (pid, (name or info["name"] or PurePosixPath(setup).stem)[:200], info["version"][:100], info["publisher"][:200],
                    info["kind"], info["builder"], setup, json.dumps(files), info["install"], info["uninstall"], info["behaviour"],
                    json.dumps(info["detection"]), json.dumps(info["notes"]), info["productCode"], info["upgradeCode"],
                    built["size"], sum(f["size"] for f in files), built["digest"], t, user.email, user.name, t, user.email))
    log.info(json.dumps({"severity": "NOTICE", "message": f"packaged {setup}", "id": pid, "size": built["size"], "by": user.email}))
    prune()
    return _one(pid)


@app.post("/api/packages")
async def add_package(request: Request, user: User = Depends(auth.editor)):
    if int(request.headers.get("content-length") or 0) > MAX_UPLOAD + 1024 * 1024:
        raise HTTPException(413, f"That's more than {MAX_UPLOAD / 1024 ** 2:,.0f} MB. Upload the installer and only the files it needs.")
    work = Path(tempfile.mkdtemp(prefix="up-", dir=store.work))
    try:
        form = await request.form(max_files=MAX_FILES, max_fields=20)
        uploads = [f for f in form.getlist("files") if isinstance(f, UploadFile)]
        if not uploads:
            raise ValueError("Choose an installer, a script or a folder to package.")
        paths = [clean_path(f.filename or "") for f in uploads]
        if len({p.lower() for p in paths}) != len(paths):
            raise ValueError("Two of the files have the same name and folder.")
        root = _common_root(paths)
        src = work / "src"
        total, files = 0, []
        for f, p in zip(uploads, paths):
            rel = p[len(root) + 1:] if root else p
            dest = src / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            with open(dest, "wb") as out:
                while chunk := await f.read(1024 * 1024):
                    total += len(chunk)
                    if total > MAX_UPLOAD:
                        raise HTTPException(413, f"That's more than {MAX_UPLOAD / 1024 ** 2:,.0f} MB.")
                    out.write(chunk)
            await f.close()
            files.append({"path": rel, "size": dest.stat().st_size})
        setup = str(form.get("setup") or "").strip()
        if setup:
            setup = clean_path(setup)
            if root and setup.startswith(root + "/"):
                setup = setup[len(root) + 1:]
        setup = setup or installers.pick_setup([f["path"] for f in files])
        if not setup or not (src / setup).is_file():
            raise ValueError("Pick the file Intune should run: an .msi, .exe, .ps1, .cmd or .bat from the ones you chose.")
        name = re.sub(r"\s+", " ", str(form.get("name") or "")).strip()[:200]
        files.sort(key=lambda f: f["path"].lower())
        return await run_in_threadpool(make_package, src, setup, name, user, files)
    finally:
        shutil.rmtree(work, ignore_errors=True)


class Change(BaseModel):
    name: str | None = Field(default=None, max_length=200)
    install: str | None = Field(default=None, max_length=2000)
    uninstall: str | None = Field(default=None, max_length=2000)
    behaviour: str | None = None
    detection: str | None = Field(default=None, max_length=2000)
    comment: str | None = Field(default=None, max_length=4000)


@app.patch("/api/packages/{pid}")
def change_package(pid: str, body: Change, user: User = Depends(auth.editor)):
    old = _one(pid)
    f = {}
    if body.name is not None:
        if not body.name.strip():
            raise ValueError("Give the package a name.")
        f["name"] = re.sub(r"\s+", " ", body.name).strip()
    for k in ("install", "uninstall"):
        v = getattr(body, k)
        if v is not None:
            if not v.strip():
                raise ValueError(f"The {k} command can't be empty.")
            f[k] = v.strip()
    if body.behaviour is not None:
        if body.behaviour not in ("System", "User"):
            raise ValueError("Install behavior is System or User.")
        f["behaviour"] = body.behaviour
    if body.detection is not None:
        f["detection"] = json.dumps({**old["detection"], "text": body.detection.strip()})
    if body.comment is not None:
        f["comment"] = body.comment.strip()
    if f:
        with store.lock, store.db() as db:
            db.execute(f"UPDATE packages SET {', '.join(k + ' = ?' for k in f)}, updated_at = ?, updated_by = ? WHERE id = ?",
                       (*f.values(), now(), user.email, pid))
    return _one(pid)


@app.get("/api/packages/{pid}/download")
def download(pid: str, user: User = Depends(auth.viewer)):
    p = _one(pid)
    path = store.file_of(pid)
    if not p["available"] or not path.is_file():
        raise HTTPException(410, (p["removedWhy"] or "Its file has gone") + ". Package it again to download it.")
    return FileResponse(path, media_type="application/octet-stream", filename=p["fileName"], headers={"Cache-Control": "no-store"})


@app.delete("/api/packages/{pid}")
def delete_package(pid: str, user: User = Depends(auth.editor)):
    p = _one(pid)
    with store.lock, store.db() as db:
        db.execute("DELETE FROM packages WHERE id = ?", (pid,))
    store.file_of(pid).unlink(missing_ok=True)
    log.info(json.dumps({"severity": "NOTICE", "message": f"deleted package {p['name']}", "id": pid, "by": user.email}))
    return {"ok": True}


@app.get("/healthz", include_in_schema=False)
def healthz():
    return {"ok": True}


# ---------- Serving under a path on a shared host ----------
if BASE_PATH:
    _app = app
    app = FastAPI(title="Nscale App Packaging", docs_url=None, redoc_url=None, openapi_url=None)
    app.mount(BASE_PATH, _app)

    @app.get(BASE_PATH, include_in_schema=False)
    def _add_slash():
        return RedirectResponse(BASE_PATH + "/")

    @app.get("/healthz", include_in_schema=False)
    def _healthz():
        return {"ok": True}


# Expired packages are cleared every hour (tests turn this off with PRUNE_LOOP=0).
if os.environ.get("PRUNE_LOOP", "1") == "1":
    threading.Thread(target=_loop, daemon=True, name="prune").start()
