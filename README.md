# Nscale Client Platforms

The Client Platforms team's tools, behind one sign-in, as a Docker stack you run in Portainer.

| Path | Tool | Code |
|---|---|---|
| `/` | Home page, sign-in, Settings (people, tool links, addresses) | [`services/portal`](services/portal) |
| `/notifications/` | IT Notifications: send branded notifications to Windows and Mac devices | [`services/notifications`](services/notifications) (apps: [ITNotifications](https://github.com/jamesbrown-nscale/ITNotifications)) |
| `/elevation/` | Admin elevation log: every Windows admin elevation request | [`services/elevation`](services/elevation) (app: [ElevationApp](https://github.com/jamesbrown-nscale/ElevationApp)) |
| `/restart/` | Restart reminder: which devices have gone too long without a restart, who put it off | [`services/restart`](services/restart) (app: [WindowsRestart](https://github.com/jamesbrown-nscale/WindowsRestart)) |

```
 people ── http://<host>:8080 (password) ──────────────────────────────────────► proxy (Caddy)
 people ──► Cloudflare Access (optional) ──► Cloudflare Tunnel ──► cloudflared ──►    │
                                                                                     │
       every request to a tool: proxy asks portal /auth/check ──► 200 + X-CP-User ───┤
                                                                                     ├─► /               portal
 Windows / Mac apps ── device keys ── /notifications/api/v1/checkin ────────────────┼─► /notifications/ notifications
                                      /elevation/api/elevations/ingest ─────────────┼─► /elevation/     elevation
                                      /restart/api/reports ─────────────────────────┴─► /restart/       restart
                                                                       one SQLite volume per service
```

## How sign-in works

- **By IP, with a password** (`AUTH_MODE=local`, the default). Open `http://<host>:8080` and sign in with an email and password the portal keeps (scrypt-hashed, 12-hour sessions, a lockout after repeated wrong passwords).
- **Through Cloudflare as well, if you set it up.** When `CF_ACCESS_TEAM_DOMAIN` and `CF_ACCESS_AUD` are set, people coming in through Cloudflare Access are signed in by it, with no password. The portal checks Cloudflare's signed header (`Cf-Access-Jwt-Assertion`) against your team's keys, issuer and the application's AUD tag before it believes the email in it. `AUTH_MODE=cloudflare` turns passwords off, leaving Cloudflare as the only way in (and no sign-in by IP).
- **The portal decides who gets which tool.** Signing in isn't enough: the person must also be on the portal's **People** list, which sets the tools each person can use. Admins can use every tool and change Settings.
- **The tools trust only the portal.** Before any request reaches a tool, Caddy asks the portal (`forward_auth` to `/auth/check`). If the person may use that tool, the portal answers with `X-CP-User`: a 2-minute token signed with `AUTH_SECRET`, naming the person and the tool. The tool checks the signature, the tool name and the expiry. Caddy removes any `X-CP-User` a client sends, and a token for one tool is refused by the others.
- **Devices don't sign in.** Three paths skip the check: the notification check-in, the elevation ingest and the restart reports. Devices use their own keys on these.

## Running it in Portainer

### 1. The stack

In Portainer: **Stacks → Add stack → Repository**. Point it at this repo (`refs/heads/main`), with compose path `docker-compose.yml`. Then set the environment variables listed in [`.env.example`](.env.example), or use **Load variables from .env file**:

| Variable | What |
|---|---|
| `AUTH_SECRET` | `openssl rand -hex 32`. Shared by every service. |
| `ADMIN_EMAIL`, `ADMIN_PASSWORD` | The first admin. Only used while the portal has no accounts; change the password after signing in. |
| `ENROLLMENT_KEY` | IT Notifications device key. |
| `ELEVATION_INGEST_KEYS`, `RESTART_INGEST_KEYS` | `label:key` pairs, comma separated, keys 24+ characters. Each label is shown against what its devices send, so give each group of devices its own key. |
| `HTTP_PORT` | The port on the host (default 8080). |

Deploy. **Every image runs its tests while it builds**, so if a test fails the stack doesn't deploy. That is the "check" step. To roll out a change, use **Pull and redeploy** on the stack (tick *Re-pull image and redeploy*), or turn on Portainer's GitOps updates to redeploy when `main` changes.

### 2. Cloudflare (optional, any time later)

1. **Tunnel.** In Zero Trust → Networks → Tunnels, create a tunnel and copy its token. Add a public hostname, e.g. `clientplatforms.nscale.com`, with service `http://proxy:80`.
2. **Access application.** In Zero Trust → Access → Applications, add a self-hosted app for that hostname with an *Allow* policy for your people (e.g. emails ending `@nscale.com`). Copy the **Application Audience (AUD) tag** from its overview.
3. **Let devices through.** Devices can't sign in, so add a second application (or a policy with action **Bypass**) for these paths on the same hostname:
   `/notifications/api/v1/checkin`, `/elevation/api/elevations/ingest`, `/restart/api/reports`, `/restart/api/policy`, `/api/device-config`.
4. **The stack.** Add `docker-compose.cloudflare.yml` under **Additional paths**, and set `CLOUDFLARE_TUNNEL_TOKEN`, `CF_ACCESS_TEAM_DOMAIN` (e.g. `nscale.cloudflareaccess.com`) and `CF_ACCESS_AUD`. Redeploy. Sign-in by IP keeps working.

### 3. First sign-in

Open `http://<host>:8080` and sign in with `ADMIN_EMAIL` and `ADMIN_PASSWORD`. Change the password (Settings → Your account), then:

- **People**: add each person and tick their tools. Give them a password for sign-in by IP; with Cloudflare set up, leave it empty for people who only come in through Cloudflare (use the email Cloudflare signs them in with).
- **Addresses**: the *site address* (what people open) and the *device server address* (what devices send to, if it's different). Devices and installers can read the current addresses from `/api/device-config`.
- **Tools**: where each home page tile links to, and whether it's shown. Point one at another server if that tool runs somewhere else.
- **Device settings**: the exact installer values for each app, built from the device server address.

Data is in four Docker volumes (`portal-data`, `notifications-data`, `elevation-data`, `restart-data`). Back them up.

## IT Notifications

The server code from the ITNotifications repo now lives here (its `client-platforms` branch, with sign-in from the front door). The Windows and Mac apps stay in that repo. Install the Windows MSI with `SERVERURL=<device server address>/notifications ENROLLMENTKEY=<key>`, and put the same two values in the Mac profile. `NOTIFY_ADMIN_EMAILS` optionally narrows who can send, on top of the portal's access list.

## Admin elevation log

Both Windows elevation apps send each elevation here as well as to TechOps. To set them up, put a `ClientPlatforms.json` beside `Install.ps1` when you build the Intune package (see the ElevationApp README):

```json
{ "url": "https://clientplatforms.nscale.com/elevation", "key": "<ingest key>" }
```

**What's flagged.**

| On the page | Means |
|---|---|
| **Admin right now** | Granted and not yet expired. |
| **Never reported ending** | Granted, more than 20 minutes past expiry, and no end record has arrived. |
| **Changed after reporting** | A later send changed the person, device, serial, reason or times, or came with a different key. The worker never does that, so someone may be rewriting the record with a key read from a device. |
| **Tried to reopen** | A send tried to set an ended or denied elevation back to granted. It's kept in the history and ignored. |

**API.** `POST /elevation/api/elevations/ingest` with `Authorization: Bearer <key>` and `{"events": [...]}`, the same request the worker sends TechOps.

## Restart reminder

The Windows Restart Reminder app reports what happens on each device, and the page shows every device's state:

| On the page | Means |
|---|---|
| **Within the limit** | Up less than `MAX_UPTIME_DAYS` (3). |
| **Restart due** | The countdown is running; shows when it restarts and whether the person picked the time. |
| **Put off** | The person picked a later time on the current boot. |
| **Overdue** | Its restart time passed over an hour ago with no restart, or it's been up past the limit with no countdown running (the app may not be working). |
| **Restart didn't happen** | A restart was issued over 30 minutes ago, but the device still reports the same boot. |
| **Not reporting** | Nothing for 48 hours. It may be off, or the app isn't running. |

**API for the app.** `POST /restart/api/reports` with `Authorization: Bearer <key>`:

```json
{ "reports": [ {
  "reportId": "<new GUID per report>", "event": "status", "at": "2026-11-18T09:00:00Z",
  "deviceName": "LT-LON-042", "serial": "5CG1234ABC", "user": "amy.chen@nscale.com",
  "bootTime": "2026-11-14T08:12:00Z", "deadline": null, "userScheduled": false,
  "reason": "", "appVersion": "1.3.0", "dryRun": false } ] }
```

| Field | What to send |
|---|---|
| `event` | `status` on each regular check; `countdown_started`, `scheduled` (picked a time), `hidden`, `restart_now`, `restarting` (with `reason`), `booted` (first run after a restart), `dry_run`. |
| `bootTime` | The last real boot (WMI `LastBootUpTime`). A new boot time is how a restart is detected, even if `restarting` never arrived. |
| `deadline` | When it will restart, while a countdown is running. Otherwise `null`. |

The answer is `{"results": [{"reportId", "status": "created" | "updated" | "duplicate" | "rejected", "error"?}]}`. Resending a report is safe. `GET /restart/api/policy` returns the policy (`maxUptimeDays`, `countdownHours`, `latestScheduleTime`), so the app can follow the site instead of its `appsettings.json`.

**Before the page has data,** the WindowsRestart app needs to send these reports (`ReportUrl` and `ReportKey` in its `appsettings.json`, queued and retried like the elevation worker).

## Running locally and tests

```bash
cd services/restart && pip install -r requirements.txt -r requirements-dev.txt
python -m pytest -q tests
DEV_USER=you@nscale.com BASE_PATH=/restart DATABASE_PATH=./dev.db INGEST_KEYS="dev:$(openssl rand -hex 16)" \
  uvicorn app.main:app --reload       # http://localhost:8000/restart/
```

Every service runs the same way. `DEV_USER` skips sign-in, so never set it on a server. GitHub Actions runs every service's tests on each push.

## Google Cloud (paused)

`infra/` (Terraform) and `deploy.sh` still deploy the portal, elevation log and IT Notifications to Cloud Run behind Google IAP (`AUTH_MODE=iap`, Firestore). They don't include the restart reminder or the People/Settings pages, because the portal keeps those in its SQLite volume. To go back to it, see `./deploy.sh` (`infra`, `all`, `keys`, `status`).

## Layout

| Folder | What's in it |
|---|---|
| `docker-compose.yml`, `.env.example` | The Portainer stack and its settings. `docker-compose.cloudflare.yml` adds the Cloudflare Tunnel. |
| `proxy/` | Caddy: routing and the sign-in check in front of each tool. |
| `services/portal/` | Home page, sign-in (`cloudflare.py`, passwords in `db.py`), Settings. `access.py` is shared: the same file is in every service, and a test checks the copies match. To add a tool: add it to `TOOLS` in `app/db.py`, add a service to `docker-compose.yml`, and add its path to `proxy/Caddyfile`. |
| `services/notifications/`, `services/elevation/`, `services/restart/` | The tools, each with its page, API and tests. |
| `infra/`, `deploy.sh`, `cloudbuild.yaml` | Google Cloud (paused). |
