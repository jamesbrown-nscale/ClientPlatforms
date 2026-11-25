"""Device customisation: what each department needs on its devices, and the policies behind it.

Kept in the same SQLite file as Users and Devices:

    cz_apps             the app catalogue: each app once, with its platforms, how it's delivered, and the
                        names it shows up under on a device (for the drift check)
    cz_policies         the policy library: a name, owner, platforms, type, status, change reference and
                        a settings table [{platform, setting, value, notes}]; version goes up on each save
    cz_policy_history   every earlier version of a policy, as it was, with who saved it and when
    cz_profiles         one per scope: "baseline" (everyone), "dept:<department>" and
                        "div:<department>\\x01<division>". Each has apps [{app, mode, platforms}],
                        policies [policy id] and exclusions [{id, target, title, reason, approval, review}]
    device_apps         the apps Jamf and Intune say are on each device (Jamf: everything; Intune: the
                        ones the catalogue looks for), read with the devices
    app_reads           when each platform's apps were last read (a device with no apps listed only
                        counts as "missing" when its platform's apps were read)

Someone's effective build is the baseline, then their department, then their division: a later layer's
setting for an app replaces an earlier one, and an exclusion takes an app or policy out.
"""
import json
import re
import sqlite3
import threading
import uuid
from datetime import date

from .recycle import Bin
from .store import now

PLATFORMS = ("Mac", "Windows", "Linux")
MODES = ("required", "optional", "blocked")      # Required, Self Service / optional, Blocked
DELIVERY = ("Jamf policy", "Jamf Self Service", "Intune required", "Company Portal", "Manual", "Other")
POLICY_TYPES = ("Configuration profile", "Compliance", "Script", "App deployment", "Security baseline", "Other")
STATUSES = ("Draft", "In review", "Live", "Retired")
BASELINE = "baseline"
SEP = "\x01"


def dept_scope(dept: str) -> str:
    return "dept:" + dept


def div_scope(dept: str, div: str) -> str:
    return f"div:{dept}{SEP}{div}"


def scope_label(scope: str) -> str:
    if scope == BASELINE:
        return "Baseline"
    if scope.startswith("dept:"):
        return scope[5:]
    if scope.startswith("div:"):
        return scope[4:].replace(SEP, " › ")
    return scope


def valid_scope(scope: str) -> str:
    s = (scope or "").strip()
    if s == BASELINE or (s.startswith("dept:") and s[5:].strip() and SEP not in s) or \
            (s.startswith("div:") and s.count(SEP) == 1 and all(p.strip() for p in s[4:].split(SEP))):
        return s
    raise ValueError("That isn't a department, division or the baseline.")


def layers(dept: str, div: str) -> list[str]:
    """The scopes someone's build comes from, in order."""
    out = [BASELINE]
    if dept:
        out.append(dept_scope(dept))
        if div:
            out.append(div_scope(dept, div))
    return out


def _text(v, n: int = 200) -> str:
    return re.sub(r"\s+", " ", str(v or "")).strip()[:n]


def _long(v, n: int = 4000) -> str:
    return str(v or "").strip()[:n]


def _platforms(v) -> list[str]:
    got = {p.strip().lower() for p in (v or []) if isinstance(p, str)}
    return [p for p in PLATFORMS if p.lower() in got]


def _one_of(v, allowed, what: str, default: str | None = None) -> str:
    v = _text(v)
    if not v and default is not None:
        return default
    for a in allowed:
        if a.lower() == v.lower():
            return a
    raise ValueError(f"{what} must be one of: {', '.join(allowed)}.")


def _link(v) -> str:
    v = _text(v, 500)
    if v and not re.match(r"^https://[^\s<>\"']+$", v):
        raise ValueError("A link must start with https://")
    return v


def _date(v) -> str:
    v = _text(v, 10)
    if not v:
        return ""
    try:
        return date.fromisoformat(v).isoformat()
    except ValueError:
        raise ValueError("A review date must be a date (YYYY-MM-DD).")


def norm_app(name: str) -> str:
    """An app name as we compare it: lower case, no ".app" / ".exe", single spaces."""
    n = re.sub(r"\.(app|exe|msi|pkg)$", "", (name or "").strip(), flags=re.I)
    return re.sub(r"\s+", " ", n).strip().lower()


def matches(term: str, name: str) -> bool:
    """A device's app name matches a term when the term starts a word in it ("docker" matches
    "Docker Desktop", "teams" matches "Microsoft Teams"; "git" doesn't match "digit")."""
    t, n = norm_app(term), norm_app(name)
    return bool(t) and bool(re.search(r"(^|[\s\-_.(])" + re.escape(t), n))


class Custom:
    def __init__(self, path: str, lock=None):
        self.path = path
        self._lock = lock or threading.Lock()
        with self._db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS cz_apps (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, vendor TEXT DEFAULT '', platforms TEXT NOT NULL DEFAULT '[]',
                    delivery TEXT DEFAULT '', detect TEXT NOT NULL DEFAULT '[]', licence TEXT DEFAULT '', owner TEXT DEFAULT '',
                    link TEXT DEFAULT '', notes TEXT DEFAULT '', created_at TEXT, updated_at TEXT, updated_by TEXT DEFAULT '');
                CREATE TABLE IF NOT EXISTS cz_policies (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, description TEXT DEFAULT '', owner TEXT DEFAULT '',
                    platforms TEXT NOT NULL DEFAULT '[]', type TEXT DEFAULT '', status TEXT DEFAULT 'Draft', change_ref TEXT DEFAULT '',
                    link TEXT DEFAULT '', settings TEXT NOT NULL DEFAULT '[]', version INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT, created_by TEXT DEFAULT '', updated_at TEXT, updated_by TEXT DEFAULT '');
                CREATE TABLE IF NOT EXISTS cz_policy_history (
                    policy_id TEXT NOT NULL, version INTEGER NOT NULL, data TEXT NOT NULL, saved_at TEXT, saved_by TEXT DEFAULT '',
                    PRIMARY KEY (policy_id, version));
                CREATE TABLE IF NOT EXISTS cz_profiles (
                    scope TEXT PRIMARY KEY, notes TEXT DEFAULT '', apps TEXT NOT NULL DEFAULT '[]', policies TEXT NOT NULL DEFAULT '[]',
                    exclusions TEXT NOT NULL DEFAULT '[]', updated_at TEXT, updated_by TEXT DEFAULT '');
                CREATE TABLE IF NOT EXISTS device_apps (device_id TEXT NOT NULL, name TEXT NOT NULL, PRIMARY KEY (device_id, name));
                CREATE TABLE IF NOT EXISTS app_reads (platform TEXT PRIMARY KEY, read_at TEXT, scope TEXT DEFAULT '', note TEXT DEFAULT '');
            """)
        self.bin = Bin(self._db, self._lock)

    def _db(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    # ----- the app catalogue -----

    @staticmethod
    def _app_row(r) -> dict:
        d = dict(r)
        d["platforms"], d["detect"] = json.loads(d["platforms"] or "[]"), json.loads(d["detect"] or "[]")
        return d

    def apps(self) -> list[dict]:
        with self._db() as db:
            return [self._app_row(r) for r in db.execute("SELECT * FROM cz_apps ORDER BY name COLLATE NOCASE")]

    def app(self, aid: str) -> dict | None:
        with self._db() as db:
            r = db.execute("SELECT * FROM cz_apps WHERE id = ?", (aid,)).fetchone()
        return self._app_row(r) if r else None

    def save_app(self, aid: str | None, body: dict, by: str) -> dict:
        name = _text(body.get("name"), 120)
        if not name:
            raise ValueError("Give the app a name.")
        detect = [t for t in (_text(x, 120) for x in (body.get("detect") or [])) if t][:10]
        row = {"name": name, "vendor": _text(body.get("vendor"), 120), "platforms": json.dumps(_platforms(body.get("platforms"))),
               "delivery": _one_of(body.get("delivery"), DELIVERY, "Delivery", ""), "detect": json.dumps(detect),
               "licence": _text(body.get("licence"), 200), "owner": _text(body.get("owner"), 200),
               "link": _link(body.get("link")), "notes": _long(body.get("notes"), 2000)}
        t = now()
        with self._lock, self._db() as db:
            clash = db.execute("SELECT id FROM cz_apps WHERE lower(name) = lower(?) AND id != ?", (name, aid or "")).fetchone()
            if clash:
                raise ValueError(f"There's already an app called “{name}” in the catalogue.")
            if aid:
                if not db.execute("UPDATE cz_apps SET " + ", ".join(f"{k} = ?" for k in row) + ", updated_at = ?, updated_by = ? WHERE id = ?",
                                  (*row.values(), t, by, aid)).rowcount:
                    raise LookupError("That app isn't in the catalogue any more.")
            else:
                aid = uuid.uuid4().hex[:12]
                db.execute(f"INSERT INTO cz_apps (id, {', '.join(row)}, created_at, updated_at, updated_by) VALUES (?, {', '.join('?' * len(row))}, ?, ?, ?)",
                           (aid, *row.values(), t, t, by))
        return self.app(aid)

    def delete_app(self, aid: str, by: str, tool: str) -> bool:
        """Into the recycle bin; it comes off every profile (and exclusion) that listed it."""
        with self._lock, self._db() as db:
            r = db.execute("SELECT name FROM cz_apps WHERE id = ?", (aid,)).fetchone()
            if not r:
                return False
            self.bin.put(db, kind="App", label=r["name"], context="Device customisation", table="cz_apps", where={"id": aid}, by=by, tool=tool)
            db.execute("DELETE FROM cz_apps WHERE id = ?", (aid,))
            self._drop_refs(db, "app", aid)
            return True

    # ----- the policy library -----

    @staticmethod
    def _policy_row(r) -> dict:
        d = dict(r)
        d["platforms"], d["settings"] = json.loads(d["platforms"] or "[]"), json.loads(d["settings"] or "[]")
        return d

    def policies(self) -> list[dict]:
        with self._db() as db:
            return [self._policy_row(r) for r in db.execute("SELECT * FROM cz_policies ORDER BY name COLLATE NOCASE")]

    def policy(self, pid: str) -> dict | None:
        with self._db() as db:
            r = db.execute("SELECT * FROM cz_policies WHERE id = ?", (pid,)).fetchone()
        return self._policy_row(r) if r else None

    def history(self, pid: str) -> list[dict]:
        with self._db() as db:
            return [{**json.loads(r["data"]), "version": r["version"], "savedAt": r["saved_at"], "savedBy": r["saved_by"]}
                    for r in db.execute("SELECT * FROM cz_policy_history WHERE policy_id = ? ORDER BY version DESC", (pid,))]

    def save_policy(self, pid: str | None, body: dict, by: str) -> dict:
        name = _text(body.get("name"), 160)
        if not name:
            raise ValueError("Give the policy a name.")
        settings = []
        for s in (body.get("settings") or [])[:200]:
            if not isinstance(s, dict):
                continue
            row = {"platform": _one_of(s.get("platform"), ("All",) + PLATFORMS, "A setting's platform", "All"),
                   "setting": _text(s.get("setting"), 200), "value": _long(s.get("value"), 1000), "notes": _long(s.get("notes"), 1000)}
            if row["setting"] or row["value"]:
                if not row["setting"]:
                    raise ValueError("Every setting needs a name.")
                settings.append(row)
        row = {"name": name, "description": _long(body.get("description"), 4000), "owner": _text(body.get("owner"), 200),
               "platforms": json.dumps(_platforms(body.get("platforms"))),
               "type": _one_of(body.get("type"), POLICY_TYPES, "Type", ""), "status": _one_of(body.get("status"), STATUSES, "Status", "Draft"),
               "change_ref": _text(body.get("changeRef"), 120), "link": _link(body.get("link")), "settings": json.dumps(settings)}
        t = now()
        with self._lock, self._db() as db:
            if db.execute("SELECT 1 FROM cz_policies WHERE lower(name) = lower(?) AND id != ?", (name, pid or "")).fetchone():
                raise ValueError(f"There's already a policy called “{name}”.")
            if pid:
                old = db.execute("SELECT * FROM cz_policies WHERE id = ?", (pid,)).fetchone()
                if not old:
                    raise LookupError("That policy isn't in the library any more.")
                if all((old[k] or "") == (v or "") for k, v in row.items()):
                    return self._policy_row(old)   # nothing changed: no new version
                was = self._policy_row(old)
                db.execute("INSERT OR REPLACE INTO cz_policy_history (policy_id, version, data, saved_at, saved_by) VALUES (?, ?, ?, ?, ?)",
                           (pid, old["version"], json.dumps({k: was[k] for k in ("name", "description", "owner", "platforms", "type", "status",
                                                                                   "change_ref", "link", "settings")}),
                            old["updated_at"], old["updated_by"]))
                db.execute("UPDATE cz_policies SET " + ", ".join(f"{k} = ?" for k in row) + ", version = version + 1, updated_at = ?, updated_by = ? WHERE id = ?",
                           (*row.values(), t, by, pid))
            else:
                pid = uuid.uuid4().hex[:12]
                db.execute(f"INSERT INTO cz_policies (id, {', '.join(row)}, version, created_at, created_by, updated_at, updated_by) "
                           f"VALUES (?, {', '.join('?' * len(row))}, 1, ?, ?, ?, ?)", (pid, *row.values(), t, by, t, by))
        return self.policy(pid)

    def delete_policy(self, pid: str, by: str, tool: str) -> bool:
        with self._lock, self._db() as db:
            r = db.execute("SELECT name FROM cz_policies WHERE id = ?", (pid,)).fetchone()
            if not r:
                return False
            self.bin.put(db, kind="Policy", label=r["name"], context="Device customisation", table="cz_policies", where={"id": pid}, by=by,
                         tool=tool, also=[("cz_policy_history", {"policy_id": pid})])
            db.execute("DELETE FROM cz_policy_history WHERE policy_id = ?", (pid,))
            db.execute("DELETE FROM cz_policies WHERE id = ?", (pid,))
            self._drop_refs(db, "policy", pid)
            return True

    def _drop_refs(self, db, kind: str, rid: str) -> None:
        for r in db.execute("SELECT * FROM cz_profiles").fetchall():
            apps, pols, exc = (json.loads(r[k] or "[]") for k in ("apps", "policies", "exclusions"))
            a2 = [a for a in apps if not (kind == "app" and a.get("app") == rid)]
            p2 = [p for p in pols if not (kind == "policy" and p == rid)]
            e2 = [e for e in exc if e.get("target") != f"{kind}:{rid}"]
            if (a2, p2, e2) != (apps, pols, exc):
                db.execute("UPDATE cz_profiles SET apps = ?, policies = ?, exclusions = ? WHERE scope = ?",
                           (json.dumps(a2), json.dumps(p2), json.dumps(e2), r["scope"]))

    # ----- profiles -----

    @staticmethod
    def _profile_row(r) -> dict:
        d = dict(r)
        for k in ("apps", "policies", "exclusions"):
            d[k] = json.loads(d[k] or "[]")
        return d

    def profiles(self) -> dict[str, dict]:
        with self._db() as db:
            return {r["scope"]: self._profile_row(r) for r in db.execute("SELECT * FROM cz_profiles")}

    def profile(self, scope: str) -> dict:
        with self._db() as db:
            r = db.execute("SELECT * FROM cz_profiles WHERE scope = ?", (scope,)).fetchone()
        return self._profile_row(r) if r else {"scope": scope, "notes": "", "apps": [], "policies": [], "exclusions": [],
                                                "updated_at": None, "updated_by": ""}

    def save_profile(self, scope: str, body: dict, by: str) -> dict:
        scope = valid_scope(scope)
        app_ids = {a["id"] for a in self.apps()}
        pol_ids = {p["id"] for p in self.policies()}
        apps, seen = [], set()
        for a in (body.get("apps") or [])[:500]:
            aid = (a or {}).get("app")
            if aid not in app_ids:
                raise ValueError("One of the apps isn't in the catalogue any more. Reload the page.")
            if aid in seen:
                continue
            seen.add(aid)
            apps.append({"app": aid, "mode": _one_of(a.get("mode"), MODES, "An app's setting", "required"),
                         "platforms": _platforms(a.get("platforms"))})
        pols = []
        for p in (body.get("policies") or [])[:500]:
            if p not in pol_ids:
                raise ValueError("One of the policies isn't in the library any more. Reload the page.")
            if p not in pols:
                pols.append(p)
        exc = []
        for e in (body.get("exclusions") or [])[:500]:
            e = e or {}
            target = _text(e.get("target"), 40)
            if target and not ((target.startswith("app:") and target[4:] in app_ids) or (target.startswith("policy:") and target[7:] in pol_ids)):
                raise ValueError("An exclusion points at an app or policy that's gone. Reload the page.")
            title = _text(e.get("title"), 200)
            if not target and not title:
                raise ValueError("Say what each exclusion is for: an app, a policy, or a short title.")
            reason = _long(e.get("reason"), 1000)
            if not reason:
                raise ValueError("Every exclusion needs a reason.")
            exc.append({"id": _text(e.get("id"), 20) or uuid.uuid4().hex[:10], "target": target, "title": title, "reason": reason,
                        "approval": _text(e.get("approval"), 200), "review": _date(e.get("review"))})
        if scope == BASELINE and exc:
            raise ValueError("The baseline is what everyone gets: exclusions go on a department or division.")
        t = now()
        with self._lock, self._db() as db:
            db.execute("INSERT INTO cz_profiles (scope, notes, apps, policies, exclusions, updated_at, updated_by) VALUES (?, ?, ?, ?, ?, ?, ?) "
                       "ON CONFLICT(scope) DO UPDATE SET notes = excluded.notes, apps = excluded.apps, policies = excluded.policies, "
                       "exclusions = excluded.exclusions, updated_at = excluded.updated_at, updated_by = excluded.updated_by",
                       (scope, _long(body.get("notes"), 4000), json.dumps(apps), json.dumps(pols), json.dumps(exc), t, by))
        return self.profile(scope)

    def copy_profile(self, src: str, dst: str, by: str) -> dict:
        """Start a profile from another's apps, policies and exclusions (dst's own are replaced)."""
        p = self.profile(valid_scope(src))
        body = {"notes": p["notes"], "apps": p["apps"], "policies": p["policies"], "exclusions": [] if dst == BASELINE else
                [{**e, "id": ""} for e in p["exclusions"]]}
        return self.save_profile(dst, body, by)

    # ----- someone's effective build -----

    def build(self, dept: str, div: str, platform: str = "", apps: dict | None = None, policies: dict | None = None,
              profiles: dict | None = None) -> dict:
        """Baseline, then department, then division. platform ("Mac" …) keeps what applies to it ("" = all)."""
        apps = apps if apps is not None else {a["id"]: a for a in self.apps()}
        policies = policies if policies is not None else {p["id"]: p for p in self.policies()}
        profiles = profiles if profiles is not None else self.profiles()
        got_apps: dict[str, dict] = {}
        got_pols: dict[str, dict] = {}
        exclusions, excluded = [], set()
        for scope in layers(dept, div):
            prof = profiles.get(scope)
            if not prof:
                continue
            src = scope_label(scope)
            for a in prof["apps"]:
                if a["app"] in apps:
                    got_apps[a["app"]] = {**a, "from": src, "scope": scope}
            for p in prof["policies"]:
                if p in policies:
                    got_pols.setdefault(p, {"policy": p, "from": src, "scope": scope})
            for e in prof["exclusions"]:
                exclusions.append({**e, "from": src, "scope": scope})
                if e["target"]:
                    excluded.add(e["target"])

        def fits(plats: list[str]) -> bool:
            return not platform or not plats or platform in plats

        out_apps = []
        for aid, a in got_apps.items():
            cat = apps[aid]
            plats = a["platforms"] or cat["platforms"]
            if f"app:{aid}" in excluded or not fits(plats):
                continue
            out_apps.append({"id": aid, "name": cat["name"], "vendor": cat["vendor"], "mode": a["mode"], "platforms": plats,
                             "delivery": cat["delivery"], "detect": cat["detect"] or [cat["name"]], "from": a["from"]})
        out_pols = []
        for pid, p in got_pols.items():
            pol = policies[pid]
            if f"policy:{pid}" in excluded or pol["status"] == "Retired" or not fits(pol["platforms"]):
                continue
            out_pols.append({"id": pid, "name": pol["name"], "status": pol["status"], "type": pol["type"], "platforms": pol["platforms"],
                             "from": p["from"]})
        for e in exclusions:
            kind, _, rid = e["target"].partition(":")
            e["targetName"] = (apps.get(rid) or {}).get("name") if kind == "app" else (policies.get(rid) or {}).get("name") if kind == "policy" else ""
        order = {m: i for i, m in enumerate(MODES)}
        out_apps.sort(key=lambda a: (order[a["mode"]], a["name"].lower()))
        out_pols.sort(key=lambda p: p["name"].lower())
        return {"apps": out_apps, "policies": out_pols, "exclusions": exclusions, "layers": ["Baseline"] + [x for x in (dept, dept and div) if x]}

    # ----- apps on devices (for the drift check) -----

    def replace_device_apps(self, platform: str, device_ids: list[str], found: dict[str, list[str]] | None, scope: str, note: str = "") -> None:
        """found: device id → app names, for this platform's devices (None: couldn't be read, so what we
        had is dropped and nothing counts as missing). scope: "all" (every app) or "catalogue"."""
        with self._lock, self._db() as db:
            db.execute("CREATE TEMP TABLE IF NOT EXISTS plat_ids (id TEXT PRIMARY KEY)")
            db.execute("DELETE FROM plat_ids")
            db.executemany("INSERT OR IGNORE INTO plat_ids (id) VALUES (?)", [(i,) for i in device_ids])
            db.execute("DELETE FROM device_apps WHERE device_id IN (SELECT id FROM plat_ids) OR device_id NOT IN (SELECT id FROM devices)")
            if found is None:
                db.execute("DELETE FROM app_reads WHERE platform = ?", (platform,))
                return
            rows = {(d, _text(n, 200)) for d, names in found.items() for n in names[:1000] if _text(n)}
            db.executemany("INSERT OR IGNORE INTO device_apps (device_id, name) VALUES (?, ?)", sorted(rows))
            db.execute("INSERT INTO app_reads (platform, read_at, scope, note) VALUES (?, ?, ?, ?) ON CONFLICT(platform) DO UPDATE SET "
                       "read_at = excluded.read_at, scope = excluded.scope, note = excluded.note", (platform, now(), scope, note[:500]))

    def device_apps(self) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        with self._db() as db:
            for r in db.execute("SELECT device_id, name FROM device_apps"):
                out.setdefault(r["device_id"], []).append(r["name"])
        return out

    def app_reads(self) -> dict[str, dict]:
        with self._db() as db:
            return {r["platform"]: dict(r) for r in db.execute("SELECT * FROM app_reads")}

    def detect_terms(self) -> list[str]:
        """Every name the catalogue looks for (what Intune is asked about)."""
        out = []
        for a in self.apps():
            for t in a["detect"] or [a["name"]]:
                if norm_app(t) and norm_app(t) not in out:
                    out.append(norm_app(t))
        return out


def drift_for(build: dict, names: list[str] | None) -> dict:
    """What's wrong on one device: required apps not found, blocked apps found. names None = not read."""
    if names is None:
        return {"read": False, "missing": [], "blocked": []}
    has = lambda a: any(matches(t, n) for t in a["detect"] for n in names)   # noqa: E731
    return {"read": True, "missing": [a["name"] for a in build["apps"] if a["mode"] == "required" and not has(a)],
            "blocked": [a["name"] for a in build["apps"] if a["mode"] == "blocked" and has(a)]}
