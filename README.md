# Nscale Client Platforms

The Client Platforms team's tools, behind one sign-in, as a Docker stack you run in Portainer.

| Path | Tool | Code |
|---|---|---|
| `/` | Home page, sign-in, Settings (people, tool links, addresses) | [`services/portal`](services/portal) |
| `/notifications/` | IT Notifications: send branded notifications to Windows and Mac devices | [`services/notifications`](services/notifications) (apps: [ITNotifications](https://github.com/jamesbrown-nscale/ITNotifications)) |
| `/elevation/` | Admin elevation log: every Windows admin elevation request | [`services/elevation`](services/elevation) (app: [ElevationApp](https://github.com/jamesbrown-nscale/ElevationApp)) |
| `/changes/` | Change management: raise, review and approve changes, through a flow per change type | [`services/projects`](services/projects) (`app/changes.py`) |
| `/restart/` | Restart reminder: which devices have gone too long without a restart, who put it off | [`services/restart`](services/restart) (app: [WindowsRestart](https://github.com/jamesbrown-nscale/WindowsRestart)) |
| `/projects/` | Projects: small project workspaces (status, timeline, RAID, delivery logs, budget, closure) | [`services/projects`](services/projects) |
| `/tasks/` | Tasks: a shared task list with assignees, questions, updates and comments | [`services/projects`](services/projects) (`app/tasks.py`) |
| `/team/` | Team: roles, responsibilities, projects and activity, 1-2-1s, feedback and manager notes | [`services/projects`](services/projects) (`app/team.py`) |
| `/inbox/` | Notifications: your own, for everyone signed in | [`services/projects`](services/projects) (`app/inbox.py`) |

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
- **The portal decides who gets which tool.** Signing in isn't enough: the person must also be on the portal's **People** list, with **roles** (see *Permissions* below).
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

## Projects

Small project workspaces with the sections of TechOps' Project Management workspace. Their definitions (fields, columns, statuses, standard records) are in `services/projects/app/modules.json`, taken from TechOps' `src/modules.js`, and the page draws every list and form from them.

| Group | Sections |
|---|---|
| **Overview** | Project Dashboard (overall RAG, current phase, next milestone, key figures) · Status Reports (a new one is filled in from the project as it stands) · Timeline (phases and milestones as a Gantt chart, with a standard phase plan in one click) |
| **RAID** | Risk Register (likelihood × impact) · Assumptions · Issues Log · Dependencies |
| **Delivery** | Action Log · Decision Log · Change Log · Reminders · Meeting Notes (each meeting action is also on the Action Log, kept in step both ways) |
| **People** | Stakeholders · Communications Plan |
| **Finance** | Budget & Costs (planned vs actual against the approved budget) |
| **Knowledge** | Document Library |
| **Closure** | Lessons Learned · Closure & Handover checklist (standard set in one click) |
| **Access** | Members |

**Who sees what.** Give people the **Projects** tool under Settings → People. Anyone with it can create a project and becomes its owner. Each project's **Members** list decides who else sees it: *Owners* change everything, including members and the project's details; *Editors* add and change records; *Viewers* only look. A project always keeps at least one owner. Client Platforms admins can open every project as an owner.

## Tasks

A shared task list. Each task has a title, description, assignee, due date, priority, status (To do, In progress, Blocked, Done) and optionally a project. Its activity is one thread:

| Entry | What it does |
|---|---|
| **Comment** | Discussion. `@first.last` (or `@first`, or a full email) mentions someone; it shows under their **Mentions**. |
| **Question** | Waits on one person (the assignee, or the creator if the assignee asks, or whoever you pick) until someone answers. It's highlighted until then and listed under **Waiting on me**. |
| **Update** | A progress note with an optional percentage. It sets the task's progress, and starts a task that was still *To do*. |
| *Changes* | Recorded by the app: assigned, status, priority, due date, project. |

The list's views are **Mine**, **I asked** (tasks you gave someone else), **Waiting on me**, **Mentions**, **All open** and **Done**, with search and a project filter. The sidebar shows how many tasks are assigned to you plus questions waiting on you (red when something's overdue or a question is waiting).

**Who sees what.** A task with no project: everyone with the **Tasks** tool. A task in a project: that project's members, plus whoever created it or is assigned it. In a project, viewers can comment but not change the task. Only its creator, a project owner or an admin can delete it. People to assign come from the portal's accounts that have the Tasks tool. Tasks runs inside the projects service (same database), so each project links to its tasks.

## Team

Everyone on the team, grouped by team, with their role, manager, start date, location and a short "about". Each person's page has:

| Tab | What's on it | Who sees it |
|---|---|---|
| **Overview** | About, responsibilities, figures at a glance (projects, open and overdue tasks, done recently), who reports to them, when their last and next 1-2-1 are | Everyone with Team (1-2-1 dates: as below) |
| **Work** | Their projects (RAG, role, phase, next milestone), their open tasks, and what they've done in projects and tasks over the last 30, 90 or 365 days | Everyone with Team, but only the projects and tasks *the viewer* can see |
| **1-2-1s** | Date, how they're doing (1-5), what was discussed, actions with owners and due dates (tick them off), next 1-2-1 | The person, their manager and admins |
| **Feedback** | *What's going well* and *Needs improvement*, side by side | The person, their manager and admins |
| **Manager notes** | Private notes | Their manager and admins only. **Never the person.** |

Only their manager or an admin adds or changes anything about someone (the person reads their own 1-2-1s and feedback but can't change them). Admins add anyone and set managers; anyone else on the team can add people who report to them. Only admins take someone off the team.

## Change management

Under **Device Management**. A **change requestor** raises a change: title, change type, category, risk, the planned window, what's changing and why, impact, scope, and the implementation, test and rollback plans. It can be saved as a draft (only they see it) and submitted when ready.

Each change type has a **flow**, set by admins in **Settings → Change management** by dragging steps into it:

| Step | What happens |
|---|---|
| **Review** | The *Change reviewers* (or named people) approve, decline or ask for changes. Any one, a number, or all must approve. |
| **Notify** | Tells the reviewers, the requestor or named people, and moves straight on |
| **Schedule** | The requestor confirms the window |
| **Implement** | The requestor (or named implementers) starts it, then marks it complete, or failed and rolled back |
| **Verify** | Someone confirms it worked |

Three flows come ready to edit: **Standard** (pre-approved), **Normal** (two reviewers, then schedule, implement and a post-implementation review) and **Emergency** (one approval, implement, then a retrospective review). The same tab sets the roles (requestors: everyone with the tool, or named people; *Change reviewers*), the categories and the risk levels.

**Statuses:** Draft, In review, Changes requested, Awaiting schedule, Scheduled, In progress, Awaiting verification, Completed, Declined, Failed, Cancelled. A strip on each change shows where it is in its flow.

**Rules:** nobody approves their own change; if no reviewers are set, admins can approve. When a reviewer asks for changes, the requestor edits and resubmits, and approvals on that step start again. A change keeps the flow it was submitted with, so editing a flow never disturbs changes under way. Everyone with the tool sees submitted changes; drafts are private.

**Talking about it:** anyone can comment and @tag people; the requestor posts updates. Notifications go to everyone's inbox: your turn to review, schedule, implement or verify; approved, declined, changes requested; comments, tags and updates; completed or failed.

## Notifications (your own)

**Notifications**, just under Home in the sidebar, is everyone's own inbox, whatever tools they have. A red count shows how many are unread. You're told when:

| From | What |
|---|---|
| Tasks | a task is assigned to you; someone comments, posts an update or changes a task you're assigned to or created; a question is waiting on you; your question is answered; a task you created is done; someone @mentions you |
| Team | your 1-2-1 is recorded; an action from any 1-2-1 is given to you (by name or email); feedback about you is added. **Manager notes never notify anyone.** |
| Projects | you're added to a project, or your role changes |
| Change management | your turn on a change; your change approved, declined or sent back; comments, tags and updates on changes you're involved in; completed or failed |

Nobody is told about their own doing. Opening a notification marks it read and takes you there; each can be marked unread or dismissed, and there's *Mark all as read*. Read notifications older than 120 days are cleared away.

**Your 1-2-1 actions** stay at the top of the page until you tick them off (the action's owner can tick it, even in someone else's 1-2-1; their manager is told).

## Permissions

**Settings** has five tabs, each shown only to those who can use it: *Your account* · *Site* · *People* · *Permissions* · *Change management*.

People are given **roles**; a role says what it allows, and someone with several roles gets the most any of them allows. Make roles under **Permissions**:

| Tool level | Means |
|---|---|
| **None** | No access; the tool isn't in their sidebar. |
| **View** | Sees everything, changes nothing. Enforced at the front door: any change is refused, and the tool shows a *View only* bar. |
| **Edit** | Uses it fully: adds, changes, comments. |
| **Admin** | Edit, plus the tool's own admin powers: every project as an owner, Change management settings, managing anyone in Team, and so on. |

A role can also give the **site abilities**: *Manage people and roles* (People and Permissions tabs) and *Manage site settings* (Site tab: addresses and tool links). The built-in **Administrator** role has admin on every tool (including any added later) and both abilities; it can't be changed or deleted. Someone must always be able to manage people and roles: the portal refuses any change that would leave nobody who can, and you can't take that right away from yourself.

When the portal first starts with roles, existing admins get *Administrator* and everyone else keeps exactly what they had, through an "editor" role for each tool they used. Notifications are everyone's, whatever their roles.

## Dialogs

Every confirmation, prompt and error on the site uses one branded dialog (`services/portal/app/static/dialog.js`, loaded like the sidebar): red for deleting, with the thing's name and what happens; Escape or Cancel backs out, and deleting a project asks you to type its name. The portal serves its shared scripts with `Cache-Control: no-cache`, so changes reach everyone on their next page load.

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
| `services/notifications/`, `services/elevation/`, `services/restart/`, `services/projects/` | The tools, each with its page, API and tests. |
| `infra/`, `deploy.sh`, `cloudbuild.yaml` | Google Cloud (paused). |
