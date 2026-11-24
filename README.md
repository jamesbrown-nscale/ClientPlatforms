# Nscale Client Platforms

The Client Platforms team's tools, behind one sign-in, as a Docker stack on a small Google Cloud VM at https://clientplatforms.nscale.com.

| Path | Tool | Code |
|---|---|---|
| `/` | Home page, sign-in, Settings (people, tool links, addresses) | [`services/portal`](services/portal) |
| `/notifications/` | IT Notifications: send branded notifications to Windows and Mac devices | [`services/notifications`](services/notifications) (apps: [ITNotifications](https://github.com/jamesbrown-nscale/ITNotifications)) |
| `/elevation/` | Admin elevation log: every Windows admin elevation request | [`services/elevation`](services/elevation) (app: [ElevationApp](https://github.com/jamesbrown-nscale/ElevationApp)) |
| `/changes/` | Change management: raise, review and approve changes, through a flow per change type | [`services/projects`](services/projects) (`app/changes.py`) |
| `/devices/` | Devices: every device in Jamf and Intune (make, model, OS, platform, who uses it) | [`services/inventory`](services/inventory) |
| `/restart/` | Restart reminder: which devices have gone too long without a restart, who put it off | [`services/restart`](services/restart) (app: [WindowsRestart](https://github.com/jamesbrown-nscale/WindowsRestart)) |
| `/projects/` | Projects: small project workspaces (status, timeline, RAID, delivery logs, budget, closure) | [`services/projects`](services/projects) |
| `/tasks/` | Team Planning: plans of scheduled jobs, with a timeline, questions, updates and comments | [`services/projects`](services/projects) (`app/tasks.py`) |
| `/users/` | Users: the people we look after, imported by CSV, tagged with department and division, line manager, VIP or Standard and primary device. |
| `/org/`, `/departments/` | Org chart and Departments: who reports to whom, and every department with its divisions and people, from Users | [`services/inventory`](services/inventory) | [`services/inventory`](services/inventory) |
| `/team/` | Client Platform Team: roles, responsibilities, projects and activity, 1-2-1s, feedback and manager notes | [`services/projects`](services/projects) (`app/team.py`) |
| `/inbox/` | Notifications: your own, for everyone signed in | [`services/projects`](services/projects) (`app/inbox.py`) |

```
 people ── https://clientplatforms.nscale.com ── Sign in with Google (OIDC) ───────► proxy (Caddy, HTTPS)
                                                                                     │
       every request to a tool: proxy asks portal /auth/check ──► 200 + X-CP-User ───┤
                                                                                     ├─► /               portal
 Windows / Mac apps ── device keys ── /notifications/api/v1/checkin ────────────────┼─► /notifications/ notifications
                                      /elevation/api/elevations/ingest ─────────────┼─► /elevation/     elevation
                                      /restart/api/reports ─────────────────────────┴─► /restart/       restart
 Jamf Pro, Intune (Microsoft Graph) ◄── read only, every hour ── /devices/, /users/   inventory
                                                                       one SQLite volume per service
```

## How sign-in works

- **With the company's identity provider** (`AUTH_MODE=oidc`, on the VM). **Sign in with Google** now; Okta later, by changing settings (OpenID Connect: authorization code with PKCE). The portal checks the ID token's signature, issuer, audience, expiry and nonce, plus a verified email in an allowed domain, before it believes who it is. Only people already on the People list get in. Anyone else sees "you don't have access", even with a valid nscale.com account. Admins add people by finding them in the directory (Google Workspace now, Okta later; read-only). Passwords are kept only for the break-glass admins in `BREAK_GLASS_EMAILS` (default `ADMIN_EMAIL`), for when sign-in is down. See [`deploy/vm/README.md`](deploy/vm/README.md).
- **With a password, on your own machine** (`AUTH_MODE=local`, the default). The portal keeps the passwords (scrypt-hashed, 12-hour sessions, a lockout after repeated wrong passwords).
- **The portal decides who gets which tool.** Signing in isn't enough: the person must also be on the portal's **People** list, with **roles** (see *Permissions* below).
- **The tools trust only the portal.** Before any request reaches a tool, Caddy asks the portal (`forward_auth` to `/auth/check`). If the person may use that tool, the portal answers with `X-CP-User`: a 2-minute token signed with `AUTH_SECRET`, naming the person and the tool. The tool checks the signature, the tool name and the expiry. Caddy removes any `X-CP-User` a client sends, and a token for one tool is refused by the others.
- **Devices don't sign in.** Three paths skip the check: the notification check-in, the elevation ingest and the restart reports. Devices use their own keys on these, created with **Create key** beside each app under **Settings → Applications** (admins of each app; the key is shown once, and only its SHA-256 is kept, in that app's own database, `devicekeys.py`). A key never expires unless given a date; its date can be changed (or set to never) while it still works, and **Expire now** stops it at once and keeps it listed as expired (it can't be turned back on; delete it to remove it), or set on the server as before (`ENROLLMENT_KEY`, `ELEVATION_INGEST_KEYS`, `RESTART_INGEST_KEYS`, listed there read only). An expired or deleted key stops working at once. Requests over 2 MB are refused. Each app logs a wrong key (a `bad_device_key` line) and turns away an address after 20 wrong keys in 10 minutes.

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
  Adding someone to the **Client Platform Team** adds them here too, with the *Role for new team members* chosen at the bottom of People. Team members without a sign-in (added before this, or while the portal was down) are listed there with **Add them all**. Taking someone off the team doesn't remove their sign-in.
- **Addresses**: the *site address* (what people open) and the *device server address* (what devices send to, if it's different). Devices and installers can read the current addresses from `/api/device-config`.
- **Tools**: where each tool links to, and whether it has a tile on the home page. Unticking *On home page* only takes the tile off the home page: the tool stays in the sidebar for everyone who can use it. Point one at another server if that tool runs somewhere else.
- **Applications**: for each app on the devices (IT Notifications, Admin Elevation, Restart Reminder), the exact installer values built from the device server address, its install instructions, and its keys (create, expire, delete). Admins of those apps see this tab even without site settings.
- **Recycle bin**: what people delete is kept for 30 days, then removed for good. **Restore** puts it back where it was, with everything that went with it (a project's records, files and members; a plan's jobs and comments; every version of a document; a person's roles). It covers projects and their records and documents, plans, jobs, sub jobs, comments, change requests, team members and their 1-2-1s and notes, Users and People. Each service keeps its own bin (`recycle.py`, the same file in each); you see the parts you're an admin of. If what something belonged to has been deleted since, restore that first.

Data is in one Docker volume per service (`portal-data`, `notifications-data`, `elevation-data`, `restart-data`, `projects-data`, `inventory-data`).

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

**The app sends these from version 1.2** (`ReportUrl` and `ReportKey` in its `appsettings.json`, queued and retried like the elevation worker; see the WindowsRestart README). Earlier versions don't report.

## Devices

Every device in **Jamf Pro** (Macs, and iPhones and iPads) and **Microsoft Intune** (Windows PCs, Macs and phones), read only, with its make, model, OS and version, platform (Jamf or Intune), serial, who it's assigned to, its last check-in, and whose primary device it is on the Users page. Search, filter by platform or OS, sort by any column, and click a device for its details. *Download CSV* gives the list.

Devices are read again every hour (`SYNC_MINUTES`), and anyone who can edit Devices can press **Sync now**. Each sync replaces what came from that platform: new devices appear, changed ones update and ones no longer there are removed. If a sync fails, the devices from the last good one stay and the bar at the top says what went wrong. Check-ins over 30 days old are shown in amber, and the **Not checked in for 30 days** tile counts them (with any that have never checked in) and shows just them when clicked.

**Settings → Connections** (Devices admins): the Jamf Pro address and API client ID and secret, and the Intune tenant ID, app ID and secret. Each has the steps to set it up in Jamf or Entra, and checks it straight away by reading every device. Secrets are encrypted with a key made from `AUTH_SECRET` and never shown again; change `AUTH_SECRET` and they need entering again. *Disconnect* deletes the details and takes that platform's devices off the page.

| Platform | What it needs | Read from |
|---|---|---|
| Jamf Pro | An API client whose API role has **Read Computers** (and **Read Mobile Devices** for iPhones and iPads) | `/api/v1/computers-inventory`, `/api/v2/mobile-devices/detail` |
| Intune | An Entra app registration with the Graph *application* permission **DeviceManagementManagedDevices.Read.All** (admin consent) and a client secret | `graph.microsoft.com/v1.0/deviceManagement/managedDevices` |

## Users

The people we look after (not who can sign in: that's Settings → People), each tagged with:

- **Department**, and **Division**: their team within the department (what used to be the Team tag). Both are free text, suggested from the ones already used.
- **Line manager**: an email, or a name that's matched to a user. The page shows their name.
- **User type**: **Standard**, **VIP**, or any type you add (e.g. *Contractor*), from a user's panel (*+ New type*) or a CSV. Every type in use except Standard gets its own tile at the top of the page (VIP always has one); click it to see just them. Types match whatever the case, so "contractor" is the same as "Contractor".
- **Primary device**: picked from Devices, starting with what Jamf or Intune says is theirs. The Users list shows its name, platform, make, model and OS. If it leaves Jamf or Intune, it shows as *Device gone*.

**Import CSV** takes a file with an **Email** column, plus any of *Name* (or First name and Last name), *Job title*, *Department*, *Division* (a *Team* column fills it too), *Line manager*, *User type* (VIP, Standard or any other type; in a column headed *VIP*, yes/no also work) and *Primary device* (a serial or device name). Commas, semicolons or tabs all work, and common heading names (e.g. *E-mail Address*, *Display Name*, *Serial Number*) are recognised. Each column is matched by its heading and only those fields change, so a file with just *Email* and *User type* only changes user types. Before anything is saved it shows how each heading was read, and every person who'll change with each field's old → new value (and afterwards, what changed), plus anything it couldn't read. People already on Users are updated; empty cells leave what's there alone. New emails are added too, unless you untick that; it's unticked when the file has no Name column, so nobody is added with just an email. *Download a template* gives the columns.

**Departments.** Filter by department, then by division within it, or tick **Group by department** (or the *Departments* tile) to see everyone under Department, then Division headings, each of which folds; **Expand all** and **Collapse all** open or shut them together.

**Org chart** (People → Org chart) is built from the line managers: it starts at the top of the organisation (whoever has the most people under them and no line manager on Users), shows that person, the chain of managers above and their direct reports, each with how many report to them. Click anyone to move to them, or find someone by name, title or department. People with no line manager on Users are listed under the top. A line manager given as a name that isn't on Users can't be placed.

**Departments** (People → Departments) lists every department with how many people, divisions and VIPs it has and who leads it (of the people whose manager isn't in it, the one with the most of it below them). Open one to see its divisions and the people in each, with who they report to and their primary device; **Expand all** and **Collapse all**, and a search. Click someone to open them on Users.

Org chart and Departments have their own permissions (Settings → Permissions, under People), so someone can see the org chart without seeing everyone's devices on Users. They only read: changing anyone is done on Users. When they got their own permissions, every role with Users was given the same level on both, once.

**Review.** An import doesn't change Users straight away: what it would change goes to **Review** (`/users/review`), and a *Review* tile on Users shows how many are waiting. It has three lists: **New** (in the file, not on Users: *Accept* adds them, *Delete* leaves them out), **Changed** (each field's old → new: *Accept* makes the change, *Keep as is* doesn't, *Delete user* takes them off Users) and **Left** (on Users but not in the file: *Delete user* or *Keep*). Each list has buttons for all of them at once. People are only listed as Left when the import box's *This file is everyone* is ticked (it is when the file has names), so a file of just a few columns or people doesn't mark everyone else as gone. Deleted users go to the recycle bin for 30 days. A new import replaces whatever was still waiting.

**All Users** (`/all-users/`, first under People) shows everyone on Users - FTE and Users - CT in one list, read only, with a FTE or CT badge on each, an *All / FTE / CT* pill, tiles, search, department and user-type filters, a filter on the *Type* and *Platform* headings (tick one or more values) and *Download CSV* (with a List column). Clicking someone opens them on their own list. It has its own access (roles that had Users were given the same level on it once). Users itself is called **Users - FTE** on the site (still `/users/`).

**Users - CT** (`/users-ct/`, under People) is a second list of users kept apart from Users - FTE: the same page, CSV import, Review, tags, user types, platform, Match devices and Mismatches, with its own people and its own access (Settings → Permissions; roles that had Users were given the same level on it once). Both lists match against the same Jamf and Intune devices, and a device whose person is on either list isn't a mismatch on the other. Org chart, Departments and the Client Platform Team read Users only. In the database, its tables are `ct_users`, `ct_user_review` and `ct_email_links`; deleted CT users go to the same recycle bin, marked *User (CT)*.

**Mismatches.** A device is someone's when the email Jamf or Intune gives is their email on Users. When it isn't (an alias, an old surname, an `onmicrosoft.com` sign-in name), the device belongs to nobody, so the *Device emails not matched* tile on Users opens **Mismatches**: *Devices with no user* (each device email that isn't on Users, with up to three suggestions, matched word by word on the email and the name Jamf or Intune gives: the same email on another domain or without a tag like `.ct`, `-adm` or a number; first and last name in either order, or an initial with a surname of 4+ letters; the same or a similar name. People on the other list (Users or Users - CT) are suggested too, marked, and **Link** links them on that list), *Users with no device* (with any device emails that look like theirs) and *Linked*. **Link** says the devices under that email are this person's: it's kept across syncs and imports, gives them a primary device if they had none, and can be undone with **Unlink**. **Download CSV** gives the open tab: a row per unmatched device with its best suggestion, the users with no device, or the links. Devices has a *Not on Users* tile for the same devices. Emails are compared in lower case without spaces, invisible characters or accents, with look-alike letters from other alphabets (a Cyrillic *о*) read as the Latin ones; emails already saved are tidied the same way when the service starts. When Jamf or Intune leaves a device's email empty but its username (or full name) is an email address, that is used as its email; devices already saved get the same when the service starts. A link never beats a real person: if its email is later added to the list, the devices are that person's and the link shows *Not used* on the Linked tab (Unlink it to tidy up).

**Match devices** gives everyone without a primary device one of the devices Jamf or Intune says are theirs: the computer that checked in most recently (or, with no computer, the device that did). Every other device assigned to them in Jamf or Intune is listed with them too: **More (+n)** under their primary device on the list, and *Their other devices* in their panel, each with **Make primary**. **Platform** (Mac, Windows or Linux) comes from their primary device's OS in Jamf or Intune; if that's a phone, from the computer of theirs that checked in last. It can be set by hand in their panel (Mac, Windows or Linux, shown with ✎ on the list); *Automatic* goes back to their devices. A CSV import's *Platform* (or *OS*) column does the same: Mac, Windows or Linux sets it by hand, *Automatic* clears it, an empty cell leaves it, and a platform their devices already show changes nothing (so a downloaded CSV can be imported back as it is). *Download CSV* gives every user with their tags, platform, primary device and other devices.

Users and Devices run in one service (`services/inventory`, one database). The Team tool reads the departments and divisions from it inside the stack (`/internal/teams`, never routed by the front door). View sees everything; Edit imports, tags and syncs; Admin on Devices also manages Connections.

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

**Half days.** A job's duration can be in half days: 0.5, 1, 1.5 and so on. It reads "½ day" or "1½ days", and on the timeline the bar stops halfway through its last day. Two half-day jobs on the same day aren't counted as a clash. Dragging a bar's right edge changes it a whole day at a time and keeps the half.

**Job priority.** A job's priority is its place in its plan: 1 is the top. The number shows on the left of every job (in the plan, List by person, All Tasks and the timeline), and each plan's jobs are listed in that order. Drag a job up or down, in the plan or in List by person, and the plan's jobs are renumbered. A job moved to another plan goes to the bottom of it.

**Moving a job.** A job's **Plan** box lists every plan you can change; pick another and save to move the job there (to the end), with its dates, people, sub jobs, note and repeats. Both plans' activity says it moved.

**Leave.** On the timeline by person, a person's **+** (or a click on their row) has **🌴 Add leave**: first and last day and an optional note (e.g. *Holiday*). It shows as a striped block on that person's own row, not as a job under them, and only on the timeline. A job on a leave day turns their busy line red. Click the block to change the dates or remove it. Anyone who can change Team Planning can add leave for anyone.

**Recurring jobs.** Tick **Recurring** on a job and pick how often: *Weekly*, *Every 2 weeks*, *Monthly*, *Quarterly* or *Yearly*, and optionally a date to repeat *until*. It repeats from its expected start, each repeat as many working days as the first: monthly, quarterly and yearly keep its day of the month (the 31st falls back to the month's last day), and a repeat that lands on a weekend moves to the Monday. The repeats show on the timeline as dashed bars, in each assigned person's row (and count towards their busy line); click one to open the job. The job shows "↻ Weekly" (etc.), and the person given it is told it repeats. Marking the job *Done* stops it repeating. Repeats are worked out two years ahead.

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

## Client Platform Team

Everyone on the team, grouped by **department** and then **sub team** (their Division), with their role, manager, start date, location and a short "about". The departments and sub teams to pick from are the ones on the Users page (plus any the team already uses), and adding someone who's in Users fills in their name, role, department and sub team. Each person's page has:

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

**Settings** has six tabs, each shown only to those who can use it: *Your account* · *Site* · *People* · *Permissions* · *Change management* · *Connections* (Devices admins: Jamf and Intune).

People are given **roles**; a role says what it allows, and someone with several roles gets the most any of them allows. Make roles under **Permissions**:

| Tool level | Means |
|---|---|
| **None** | No access; the tool isn't in their sidebar. |
| **View** | Sees everything, changes nothing. Enforced at the front door: any change is refused, and the tool shows a *View only* bar. |
| **Edit** | Uses it fully: adds, changes, comments. |
| **Admin** | Edit, plus the tool's own admin powers: every project as an owner, Change management settings, managing anyone in Team, the Jamf and Intune connections, and so on. |

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
| `services/notifications/`, `services/elevation/`, `services/restart/`, `services/projects/`, `services/inventory/` | The tools, each with its page, API and tests. `inventory` is Users and Devices, and reads Jamf and Intune (`app/connectors.py`). |
