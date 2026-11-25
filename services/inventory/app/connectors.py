"""Reading devices from Jamf Pro and Microsoft Intune, read only.

Jamf Pro: an API client (Settings → System → API roles and clients) with an API role that has
"Read Computers" (and "Read Mobile Devices", for iPhones and iPads). We sign in with its client ID
and secret (OAuth client credentials) and page through
    /api/v1/computers-inventory           Macs
    /api/v2/mobile-devices/detail         iPhones and iPads (skipped, with a note, if the role can't read them)

Intune: an app registration in Microsoft Entra with the Microsoft Graph *application* permission
DeviceManagementManagedDevices.Read.All (admin consent granted) and a client secret. We sign in with
the tenant ID, client ID and secret and page through
    https://graph.microsoft.com/v1.0/deviceManagement/managedDevices

Each device comes back as {id, platform, kind, name, make, model, os, os_version, serial, user_email,
user_name, last_seen}, and "apps" (the names of the apps on it) when those could be read, for Device
customisation's drift check: Jamf lists every app on a Mac (the APPLICATIONS section, same "Read
Computers" permission); Intune is asked only about the apps the catalogue looks for
(/deviceManagement/detectedApps, same permission). If Intune's apps can't be read the devices still
come back, without "apps", and a note says why. Anything else that goes wrong raises SyncError.
"""
import re
from urllib.parse import urlsplit

import httpx

PLATFORMS = {"jamf": "Jamf", "intune": "Intune"}
PAGE = 200
MAX_PAGES = 500        # 100,000 devices: well past anything real, and stops a loop that never ends
TIMEOUT = 30
GRAPH = "https://graph.microsoft.com"
GUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


class SyncError(Exception):
    pass


def jamf_url(v: str) -> str:
    """https://<name>.jamfcloud.com (or your own Jamf Pro server): just the address, no path."""
    v = (v or "").strip().rstrip("/")
    if v and "://" not in v:
        v = "https://" + v
    u = urlsplit(v)
    if u.scheme != "https" or not u.hostname or u.username or u.password or u.query or u.fragment:
        raise ValueError("The Jamf Pro address must look like https://yourcompany.jamfcloud.com")
    path = u.path.rstrip("/")
    return f"https://{u.netloc}{path}"


def tenant_id(v: str) -> str:
    v = (v or "").strip()
    if not GUID.match(v) and not re.match(r"^[a-z0-9-]+(\.[a-z0-9-]+)+$", v.lower()):
        raise ValueError("The tenant ID is the Directory (tenant) ID from Entra: a GUID like 1b2c3d4e-…, or yourcompany.onmicrosoft.com")
    return v


def _why(r: httpx.Response) -> str:
    """What the server said was wrong, if it said (Jamf: {"errors": [{"description"}]}; Graph: {"error": {"message"}})."""
    try:
        j = r.json()
    except ValueError:
        return ""
    if not isinstance(j, dict):
        return ""
    errs = j.get("errors")
    if isinstance(errs, list):
        parts = [" ".join(filter(None, [_s(e.get("description")), f"({_s(e.get('field'))})" if e.get("field") else ""]))
                 for e in errs if isinstance(e, dict)]
        return "; ".join(p for p in parts if p)[:300]
    e = j.get("error")
    if isinstance(e, dict):
        return _s(e.get("message"))[:300]
    return ""


def _fail(r: httpx.Response, who: str, what: str) -> None:
    if r.status_code in (401, 403):
        raise SyncError(f"{who} turned down {what} ({r.status_code}). Check the client ID, the secret and the permissions it has.")
    if r.status_code >= 400:
        why = _why(r)
        raise SyncError(f"{who} answered {r.status_code} to {what}" + (f": {why}" if why else "."))


def _json(r: httpx.Response, who: str):
    try:
        return r.json()
    except ValueError:
        raise SyncError(f"{who} didn't answer with JSON. Is the address right?")


def _call(client: httpx.Client, who: str, method: str, url: str, **kw) -> httpx.Response:
    try:
        return client.request(method, url, timeout=TIMEOUT, **kw)
    except httpx.HTTPError as e:
        raise SyncError(f"Couldn't reach {who}: {type(e).__name__}. Is the address right?")


def _s(v) -> str:
    return str(v).strip() if v is not None else ""


def _email_of(*vals) -> str:
    """The first that's an email address. Jamf often leaves Email empty and has the address in Username
    (or Full Name); Intune may have only the sign-in name."""
    for v in vals:
        v = _s(v)
        if "@" in v and "." in v.split("@")[-1] and " " not in v:
            return v.lower()
    return ""


def _kind(os_name: str) -> str:
    o = os_name.lower()
    if any(x in o for x in ("ios", "ipados", "android", "iphone", "ipad", "tvos", "watchos")):
        return "mobile"
    return "computer"


# ---------- Jamf Pro ----------

def jamf_token(client: httpx.Client, settings: dict, secret: str) -> str:
    base = jamf_url(settings.get("url", ""))
    if not settings.get("client_id") or not secret:
        raise SyncError("Jamf needs a client ID and a client secret.")
    r = _call(client, "Jamf", "POST", f"{base}/api/oauth/token",
              data={"grant_type": "client_credentials", "client_id": settings["client_id"], "client_secret": secret})
    _fail(r, "Jamf", "the client ID and secret")
    token = (_json(r, "Jamf") or {}).get("access_token")
    if not token:
        raise SyncError("Jamf didn't give a token back.")
    return token


def _jamf_pages(client, base, token, path, sections, sort):
    out = []
    for page in range(MAX_PAGES):
        params = [("section", s) for s in sections] + [("page", page), ("page-size", PAGE), ("sort", sort)]
        r = _call(client, "Jamf", "GET", f"{base}{path}", params=params, headers={"Authorization": f"Bearer {token}", "Accept": "application/json"})
        if r.status_code in (403, 404) and path.endswith("mobile-devices/detail"):
            return None   # this API role can't read mobile devices
        _fail(r, "Jamf", f"reading {path}")
        data = _json(r, "Jamf") or {}
        rows = data.get("results") or []
        out.extend(rows)
        total = data.get("totalCount")
        if not rows or len(rows) < PAGE or (isinstance(total, int) and len(out) >= total):
            return out
    return out


def jamf_devices(client: httpx.Client, settings: dict, secret: str, app_terms: list[str] | None = None) -> tuple[list[dict], list[str]]:
    base = jamf_url(settings.get("url", ""))
    token = jamf_token(client, settings, secret)
    devices, notes = [], []
    for c in _jamf_pages(client, base, token, "/api/v1/computers-inventory",
                         ["GENERAL", "HARDWARE", "OPERATING_SYSTEM", "USER_AND_LOCATION", "APPLICATIONS"], "id:asc") or []:
        g, h, o, u = (c.get(k) or {} for k in ("general", "hardware", "operatingSystem", "userAndLocation"))
        if not c.get("id"):
            continue
        os_name = _s(o.get("name")) or "macOS"
        devices.append({"id": f"jamf:c{c['id']}", "platform": "Jamf", "kind": "computer", "name": _s(g.get("name")),
                        "make": _s(h.get("make")) or "Apple", "model": _s(h.get("model")), "os": os_name,
                        "os_version": _s(o.get("version")), "serial": _s(h.get("serialNumber")),
                        "user_email": _email_of(u.get("email"), u.get("username"), u.get("realname")),
                        "user_name": _s(u.get("realname")) or _s(u.get("username")),
                        "last_seen": _s(g.get("lastContactTime")),
                        "apps": sorted({_s(a.get("name")) for a in (c.get("applications") or []) if isinstance(a, dict) and _s(a.get("name"))})})
    # Phones and tablets are extra: if Jamf won't give them, the Macs still count, with a note why.
    try:
        mobiles = _jamf_pages(client, base, token, "/api/v2/mobile-devices/detail", ["GENERAL", "HARDWARE", "USER_AND_LOCATION"],
                              "mobileDeviceId:asc")
    except SyncError as e:
        mobiles = []
        notes.append(f"iPhones and iPads weren't read: {e}")
    if mobiles is None:
        notes.append("iPhones and iPads weren't read: give the Jamf API role \"Read Mobile Devices\" to include them.")
    for m in mobiles or []:
        mid = m.get("mobileDeviceId") or m.get("id")
        if not mid:
            continue
        g, h, u = (m.get(k) or {} for k in ("general", "hardware", "userAndLocation"))
        os_name = _s(m.get("deviceType")) or "iOS"
        devices.append({"id": f"jamf:m{mid}", "platform": "Jamf", "kind": _kind(os_name) if os_name else "mobile",
                        "name": _s(g.get("displayName")) or _s(m.get("name")), "make": "Apple",
                        "model": _s(h.get("model")) or _s(h.get("modelIdentifier")), "os": os_name,
                        "os_version": _s(g.get("osVersion")), "serial": _s(h.get("serialNumber")),
                        "user_email": _email_of(u.get("emailAddress"), u.get("username"), u.get("realName")),
                        "user_name": _s(u.get("realName")) or _s(u.get("username")),
                        "last_seen": _s(g.get("lastInventoryUpdateDate")) or _s(g.get("lastInventoryUpdateTimestamp"))})
    return devices, notes


# ---------- Intune (Microsoft Graph) ----------

def intune_token(client: httpx.Client, settings: dict, secret: str) -> str:
    tenant = tenant_id(settings.get("tenant_id", ""))
    if not settings.get("client_id") or not secret:
        raise SyncError("Intune needs an application (client) ID and a client secret.")
    r = _call(client, "Microsoft", "POST", f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token",
              data={"grant_type": "client_credentials", "client_id": settings["client_id"], "client_secret": secret,
                    "scope": f"{GRAPH}/.default"})
    if r.status_code in (400, 401):
        msg = ((_json(r, "Microsoft") or {}).get("error_description") or "").split("\r")[0].split("\n")[0]
        raise SyncError("Microsoft turned down the tenant, client ID or secret" + (f": {msg[:200]}" if msg else "."))
    _fail(r, "Microsoft", "the client ID and secret")
    token = (_json(r, "Microsoft") or {}).get("access_token")
    if not token:
        raise SyncError("Microsoft didn't give a token back.")
    return token


SELECT = "id,deviceName,manufacturer,model,operatingSystem,osVersion,serialNumber,userPrincipalName,emailAddress,userDisplayName,lastSyncDateTime"


MAX_APP_LOOKUPS = 300   # detected apps whose devices we ask for, per sync


def _word_match(term: str, name: str) -> bool:
    t = re.sub(r"\s+", " ", term or "").strip().lower()
    n = re.sub(r"\s+", " ", re.sub(r"\.(app|exe|msi|pkg)$", "", (name or "").strip(), flags=re.I)).lower()
    return bool(t) and bool(re.search(r"(^|[\s\-_.(])" + re.escape(t), n))


def _graph_pages(client, token, url, what):
    out = []
    for _ in range(MAX_PAGES):
        r = _call(client, "Microsoft Graph", "GET", url, headers={"Authorization": f"Bearer {token}", "Accept": "application/json"})
        _fail(r, "Microsoft Graph", what)
        data = _json(r, "Microsoft Graph") or {}
        out.extend(data.get("value") or [])
        url = data.get("@odata.nextLink")
        if not url:
            return out
        if urlsplit(url).netloc != urlsplit(GRAPH).netloc:
            raise SyncError("Microsoft Graph gave a next page somewhere else; stopped.")
    return out


def intune_apps(client: httpx.Client, token: str, terms: list[str]) -> dict[str, list[str]]:
    """Intune device id → the names of the catalogue's apps found on it. Every detected app in the
    tenant is listed once; only those the catalogue looks for are asked which devices have them."""
    if not terms:
        return {}
    found: dict[str, set] = {}
    hits = [a for a in _graph_pages(client, token, f"{GRAPH}/v1.0/deviceManagement/detectedApps?$select=id,displayName", "reading detected apps")
            if a.get("id") and any(_word_match(t, _s(a.get("displayName"))) for t in terms)]
    for a in hits[:MAX_APP_LOOKUPS]:
        for d in _graph_pages(client, token, f"{GRAPH}/v1.0/deviceManagement/detectedApps/{a['id']}/managedDevices?$select=id",
                              "reading which devices have an app"):
            if d.get("id"):
                found.setdefault(f"intune:{d['id']}", set()).add(_s(a.get("displayName")))
    return {k: sorted(v) for k, v in found.items()}


def intune_devices(client: httpx.Client, settings: dict, secret: str, app_terms: list[str] | None = None) -> tuple[list[dict], list[str]]:
    token = intune_token(client, settings, secret)
    url = f"{GRAPH}/v1.0/deviceManagement/managedDevices?$select={SELECT}&$top=999"
    devices = []
    for _ in range(MAX_PAGES):
        r = _call(client, "Microsoft Graph", "GET", url, headers={"Authorization": f"Bearer {token}", "Accept": "application/json"})
        if r.status_code == 403:
            raise SyncError("Microsoft Graph turned down reading devices (403). Give the app the application permission "
                            "DeviceManagementManagedDevices.Read.All and grant admin consent.")
        _fail(r, "Microsoft Graph", "reading devices")
        data = _json(r, "Microsoft Graph") or {}
        for d in data.get("value") or []:
            if not d.get("id"):
                continue
            os_name = _s(d.get("operatingSystem"))
            devices.append({"id": f"intune:{d['id']}", "platform": "Intune", "kind": _kind(os_name), "name": _s(d.get("deviceName")),
                            "make": _s(d.get("manufacturer")), "model": _s(d.get("model")), "os": os_name,
                            "os_version": _s(d.get("osVersion")), "serial": _s(d.get("serialNumber")),
                            "user_email": _email_of(d.get("emailAddress"), d.get("userPrincipalName")),
                            "user_name": _s(d.get("userDisplayName")), "last_seen": _s(d.get("lastSyncDateTime"))})
        url = data.get("@odata.nextLink")
        if not url:
            break
        if urlsplit(url).netloc != urlsplit(GRAPH).netloc:
            raise SyncError("Microsoft Graph gave a next page somewhere else; stopped.")
    notes = []
    if app_terms is not None:
        try:
            apps = intune_apps(client, token, app_terms)
            for d in devices:
                d["apps"] = apps.get(d["id"], [])
        except SyncError as e:
            notes.append(f"Apps on Intune devices weren't read (Device customisation can't check them): {e}")
    return devices, notes


READERS = {"jamf": jamf_devices, "intune": intune_devices}
TOKENS = {"jamf": jamf_token, "intune": intune_token}

