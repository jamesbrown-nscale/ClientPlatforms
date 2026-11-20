# Nscale Client Platforms

The Client Platforms team's tools, behind one sign-in, as a Docker stack on a small Google Cloud VM at https://clientplatforms.nscale.com.

| Path | Tool | Code |
|---|---|---|
| `/` | Home page, sign-in, Settings (people, tool links, addresses) | [`services/portal`](services/portal) |
| `/notifications/` | IT Notifications: send branded notifications to Windows and Mac devices | [`services/notifications`](services/notifications) (apps: [ITNotifications](https://github.com/jamesbrown-nscale/ITNotifications)) |
| `/elevation/` | Admin elevation log: every Windows admin elevation request | [`services/elevation`](services/elevation) (app: [ElevationApp](https://github.com/jamesbrown-nscale/ElevationApp)) |
| `/changes/` | Change management: raise, review and approve changes, through a flow per change type | [`services/projects`](services/projects) (`app/changes.py`) |
| `/restart/` | Restart reminder: which devices have gone too long without a restart, who put it off | [`services/restart`](services/restart) (app: [WindowsRestart](https://github.com/jamesbrown-nscale/WindowsRestart)) |
| `/projects/` | Projects: small project workspaces (status, timeline, RAID, delivery logs, budget, closure) | [`services/projects`](services/projects) |
| `/tasks/` | Team Planning: plans of scheduled jobs, with a timeline, questions, updates and comments | [`services/projects`](services/projects) (`app/tasks.py`) |
| `/team/` | Team: roles, responsibilities, projects and activity, 1-2-1s, feedback and manager notes | [`services/projects`](services/projects) (`app/team.py`) |
| `/inbox/` | Notifications: your own, for everyone signed in | [`services/projects`](services/projects) (`app/inbox.py`) |

```
 people ── https://clientplatforms.nscale.com ── Sign in with Google (OIDC) ───────► proxy (Caddy, HTTPS)
                                                                                     │
       every request to a tool: proxy asks portal /auth/check ──► 200 + X-CP-User ───┤
                                                                                     ├─► /               portal
 Windows / Mac apps ── device keys ── /notifications/api/v1/checkin ────────────────┼─► /notifications/ notifications
                                      /elevation/api/elevations/ingest ─────────────┼─► /elevation/     elevation
                                      /restart/api/reports ─────────────────────────┴─► /restart/       restart
                                                                       one SQLite volume per service
```

## How sign-in works

- **With the company's identity provider** (`AUTH_MODE=oidc`, on the VM). **Sign in with Google** now; Okta later, by changing settings (OpenID Connect: authorization code with PKCE). The portal checks the ID token's signature, issuer, audience, expiry and nonce, plus a verified email in an allowed domain, before it believes who it is. Only people already on the People list get in. Anyone else sees "you don't have access", even with a valid nscale.com account. Admins add people by finding them in the directory (Google Workspace now, Okta later; read-only). Passwords are kept only for the break-glass admins in `BREAK_GLASS_EMAILS` (default `ADMIN_EMAIL`), for when sign-in is down. See [`deploy/vm/README.md`](deploy/vm/README.md).
- **With a password, on your own machine** (`AUTH_MODE=local`, the default). The portal keeps the passwords (scrypt-hashed, 12-hour sessions, a lockout after repeated wrong passwords).
- **The portal decides who gets which tool.** Signing in isn't enough: the person must also be on the portal's **People** list, with **roles** (see *Permissions* below).
- **The tools trust only the portal.** Before any request reaches a tool, Caddy asks the portal (`forward_auth` to `/auth/check`). If the person may use that tool, the portal answers with `X-CP-User`: a 2-minute token signed with `AUTH_SECRET`, naming the person and the tool. The tool checks the signature, the tool name and the expiry. Caddy removes any `X-CP-User` a client sends, and a token for one tool is refused by the others.
- **Devices don't sign in.** Three paths skip the check: the notification check-in, the elevation ingest and the restart reports. Devices use their own keys on these. Requests over 2 MB are refused. Each app logs a wrong key (a `bad_device_key` line) and turns away an address after 20 wrong keys in 10 minutes.

## Running it

**On Google Cloud** (the live site): one e2-small VM in London runs the stack with [`docker-compose.vm.yml`](docker-compose.vm.yml) on top. Caddy gets its own HTTPS certificate, people sign in with Google (Okta later), and the disk's daily snapshots are the backups. It costs roughly $15–25 a month. Creating it, setting it up, devices, updates, restoring, changing keys, break-glass and the move to Okta are all in [`deploy/vm/README.md`](deploy/vm/README.md):

```bash
PROJECT=<project-id> ./deploy/vm/create.sh                   # once: VM, address, firewall, snapshots
sudo /opt/client-platforms/deploy/vm/update.sh             # on the VM: start, or update to the latest now
```

After that **it updates itself**: every 2 minutes the VM checks GitHub, and a new commit on `main` is pulled, built and started (`journalctl -u cp-update` shows what it did). The sidebar shows the version, which goes up by one with every commit (0.1, 0.2, …; see `deploy/vm/version.sh`).

**Every image runs its tests while it builds**, so a version with failing tests doesn't start, and the running one carries on.

**On your own machine:** copy [`.env.example`](.env.example) to `.env`, fill it in, run `docker compose up -d --build` and open `http://localhost:8080`. Sign in with `ADMIN_EMAIL` and `ADMIN_PASSWORD`.

### First sign-in

Sign in as `ADMIN_EMAIL`, then under **Settings**:

- **People**: add each person (on the VM, find them in the directory), and give them roles. Only people on this list can sign in.
- **Addresses**: the *site address* (what people open) and the *device server address* (what devices send to, if it's different). Devices and installers can read the current addresses from `/api/device-config`.
- **Tools**: where each home page tile links to, and whether it's shown. Point one at another server if that tool runs somewhere else.
- **Device settings**: the exact installer values for each app, built from the device server address.

Data is in one Docker volume per service (`portal-data`, `notifications-data`, `elevation-data`, `restart-data`, `projects-data`).

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

**Document Library files.** A document can hold the file itself, not just a link: drop it on the form or choose it (up to 25 MB). Click the file's name to open it in the page:

| File | In the page |
|---|---|
| Word (`.docx`) | Edit paragraphs and tables: headings, lists, bold, italic and underline. Only what you change is rewritten, so pictures, page layout, headers and footers stay as they were. |
| Excel (`.xlsx`) | Edit cells on each sheet; formulas are kept and Excel works them out when the workbook is next opened. Workbooks with charts, pictures or pivot tables open view only, since saving them here would drop those. |
| CSV, TSV | Edit as a grid (or as text); paste cells straight from Excel. |
| Text, Markdown, JSON … | Edit as text. |
| PDF, pictures | View. |
| Anything else (PowerPoint, `.doc`, `.xls` …) | Download, change it, then **Upload a new version**. |

In the Excel and CSV grids, **Search** finds text in any cell and the **Filter** box under each column narrows the rows (the first row is the header and stays in view). Click a row number or column letter to select the whole row or column; shift-click or drag for several. **Delete** clears them, **⌘C** copies them (to paste into Excel), and **Delete row** / **Delete column** remove them from a CSV.

Each upload or save is a new version: **Versions** lists the newest 20, to download or restore. Viewers can open and download; owners and editors upload and edit. If someone else saves while you're editing, your save is refused rather than overwriting theirs. The files are kept in the projects database (`/data/projects.db`).

**Who sees what.** Give people the **Projects** tool under Settings → People. Anyone with it can create a project and becomes its owner. Each project's **Members** list decides who else sees it: *Owners* change everything, including members and the project's details; *Editors* add and change records; *Viewers* only look. A project always keeps at least one owner. Client Platforms admins can open every project as an owner.

## Team Planning

Team Planning (once called Tasks, then Pipelines; it keeps the `/tasks/` address and the `tasks` tool id, so roles and links carry on). Each plan has a title, description, owner, due date, priority, status (To do, In progress, Blocked, Done), optionally a project, and its **jobs**.

**Jobs.** Each job has a title, description, a note (access, contacts, dependencies…), an expected start date, a duration in working days (which gives its end date; weekends don't count, so five days from a Thursday ends the Wednesday after, and a job always starts on a weekday), the people on it (one or several) and a status; while it's **In progress**, a % complete (shown beside it, and as how much of its bar is filled on the timeline). Add them with **+ Add job**, tick them off, and drag to reorder. Type `@` in a job's note or description to tag someone; they're notified. Everyone given a job is notified, and the plan shows under their **Mine**; a job with several people is under each of them on the timeline and in the list.

**Sub jobs.** A job can be broken into a checklist: open the job (or hover it and choose **+ Sub job**), type each one and press Enter, optionally with who it's for. Tick them off, click one's title to rename it, click its person to change or clear it, or × to remove it. The job shows "2 of 4", and its % complete follows the ticks. Ticking the first one moves a job that hadn't started to *In progress*. Someone given a sub job is notified.

**Three views**, switched at the top:

| View | What it shows |
|---|---|
| **All Tasks** | Every job in every plan in one list, finished plans included. Each row shows its plan, dates, progress and people. Search, filter by status (not done, not started, in progress, blocked, done) and project, and sort by start date, plan, person or status. Tick jobs off there, or click one to open it. |
| **List** | Plans, grouped **By person** or **By plan**. By person: each staff member, with the plans they have jobs in as headings and their jobs underneath. **+** adds a job for them. By plan: the views **Mine** (plans you own or have an open job in), **I asked**, **Waiting on me**, **Mentions**, **All open** and **Done**, with search and a project filter. |
| **Timeline** | A Gantt chart, each job a bar from its start to its end, grouped **By person** or **By plan**. By person: a faint line where someone is busy, red where two of their jobs overlap. By plan: the plan's due date as a diamond. Drag a bar to move a job, or its right edge to change its length; weekends are skipped either way. Click a day in a plan's row to add a job starting then, or in an unscheduled job's row to schedule it. It opens on this week and the next two. Move earlier or later, zoom in and out, **Fit** to show the whole plan, or **Today** to go back to the three weeks. Filter by project or to just your jobs. |

**Plan headings** (in the List by person, and on the Timeline) start folded: click the arrow to show or hide their jobs, and the page remembers which are open. Each shows the plan's **% complete** as a bar, worked out from all its jobs: each job counts equally, Done counts 100%, In progress its own %, anything else 0%. The bar turns green at 100%.

A plan's activity is one thread:

| Entry | What it does |
|---|---|
| **Comment** | Discussion. `@first.last` (or `@first`, or a full email) mentions someone; it shows under their **Mentions**. |
| **Question** | Waits on one person (the owner, or the creator if the owner asks, or whoever you pick) until someone answers. It's highlighted until then and listed under **Waiting on me**. |
| **Update** | A progress note with an optional percentage. It sets the plan's progress, and starts one that was still *To do*. |
| *Changes* | Recorded by the app: owner, status, priority, due date, project, and jobs added, scheduled, finished or removed. |

**Who sees what.** A plan with no project: everyone with the **Team Planning** tool. In a project: that project's members, plus whoever created it, owns it or has a job in it. In a project, viewers can comment but not change it or its jobs. Only its creator, a project owner or an admin can delete it. Team Planning runs inside the projects service (same database).

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

**Statuses:** Draft, In review, Changes requested, Recalled, Awaiting schedule, Scheduled, In progress, Awaiting verification, Completed, Declined, Failed, Cancelled. A strip on each change shows where it is in its flow.

**Rules:** nobody approves their own change; if no reviewers are set, admins can approve. When a reviewer asks for changes, the requestor edits and resubmits, and approvals on that step start again. A change keeps the flow it was submitted with, so editing a flow never disturbs changes under way. Everyone with the tool sees submitted changes; drafts are private.

**Reading and reviewing:** each change shows an *At a glance* strip and a card per section; the implementation plan is a list of steps the implementer ticks off. The **+** on any section comments on just that part (shown under it, and in the thread as "on Why"). The requestor can **Recall** a submitted change any time before implementation starts: it comes back to them (reviewers are told, approvals are cleared) to edit, even its change type, and **resubmit** from the start of its flow, or to delete. Reviewers can **Return to requestor** with what needs changing; when the requestor resubmits, the change says what's different since it was returned: a summary at the top, a *Changed* badge on each section, new plan steps marked *New*, and "Show what it said before".

**Talking about it:** anyone can comment and @tag people (typing @ lists the people involved first); the requestor posts updates. Notifications go to everyone's inbox: your turn to review, schedule, implement or verify; approved, declined, changes requested; comments, tags and updates; completed or failed.

## Notifications (your own)

**Notifications**, just under Home in the sidebar, is everyone's own inbox, whatever tools they have. A red count shows how many are unread. You're told when:

| From | What |
|---|---|
| Team Planning | a plan or a job in one is given to you; someone comments, posts an update or changes a task you're assigned to or created; a question is waiting on you; your question is answered; a task you created is done; someone @mentions you |
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

## Layout

| Folder | What's in it |
|---|---|
| `docker-compose.yml`, `.env.example` | The stack, and its settings for running it on your own machine. |
| `docker-compose.vm.yml`, `deploy/vm/` | The Google Cloud VM: HTTPS and OIDC on top of the stack, the scripts to create, set up and update it, its settings (`env.example`) and the runbook. |
| `proxy/` | Caddy: routing and the sign-in check in front of each tool. |
| `services/portal/` | Home page, sign-in (`oidc.py`, passwords in `db.py`), the directory search (`directory.py`), Settings. `access.py` is shared: the same file is in every service, and a test checks the copies match (as does `keyguard.py`, the wrong-key limiter in the three device apps). To add a tool: add it to `TOOLS` in `app/db.py`, add a service to `docker-compose.yml`, and add its path to `proxy/Caddyfile`. |
| `services/notifications/`, `services/elevation/`, `services/restart/`, `services/projects/` | The tools, each with its page, API and tests. |
