# Nscale Client Platforms

**https://clientplatforms.nscale.com** — the Client Platforms team's tools, on Google Cloud, behind one Google sign-in.

| Path | Tool | Code |
|---|---|---|
| `/` | Home page | [`services/portal`](services/portal) |
| `/notifications/` | IT Notifications: send branded notifications to Windows and Mac devices | [ITNotifications](https://github.com/jamesbrown-nscale/ITNotifications) repo |
| `/elevation/` | Admin elevation log: every Windows admin elevation request | [`services/elevation`](services/elevation), fed by the [ElevationApp](https://github.com/jamesbrown-nscale/ElevationApp) worker |

```
                         clientplatforms.nscale.com  (global HTTPS load balancer, Google-managed certificate)
 people ──► Google sign-in (IAP, nscale.com only, one access list per tool) ──┐
                                                                               ├──► /               cp-portal
 Windows / Mac notification app ── enrollment key ── /notifications/api/v1/checkin ──► cp-notify ◄── /notifications/*
 Windows elevation worker ──────── ingest key ────── /elevation/api/elevations/ingest ─► cp-elevation ◄── /elevation/*
                                                                                          │
                                         Cloud Run, London (europe-west2)  ·  one Firestore database per tool
```

## How it fits together

- **One sign-in.** Google Identity-Aware Proxy (IAP) on the load balancer signs people in with their nscale.com Google account. Each tool has its own access list (`portal_access`, `notifications_access`, `elevation_access`), so someone can be allowed the elevation log but not IT Notifications.
- **Belt and braces.** Every app also checks the signed header IAP adds (`X-Goog-IAP-JWT-Assertion`): Google's signature, the issuer, that the token was issued for *that* tool's backend, and the nscale.com domain. Cloud Run only accepts traffic from the load balancer, so there's no way round IAP; if there were, the apps would still refuse.
- **Devices don't sign in.** Exactly two paths skip IAP: the notification check-in and the elevation ingest. The apps check the device keys there, and Cloud Armor rate-limits each source address.
- **Separate workloads.** Each tool is its own Cloud Run service with its own service account, its own Firestore database (`notifications`, `elevation`) and its own secrets. An IAM condition limits each service account to its own database, so a fault in one tool can't touch another's data.
- **Logs.** The load balancer logs every request. The elevation service also writes one structured line per elevation record to Cloud Logging (`WARNING` when a record was changed after it was first reported), so you can alert on it or export it to a SIEM.

## First-time setup

You need: a Google Cloud project, `gcloud` signed in as an owner (`gcloud auth login` and `gcloud auth application-default login`), Terraform 1.6+ (or OpenTofu, with `TF=tofu`), and someone who can add a DNS record for nscale.com.

1. **Settings.** Copy `infra/terraform.tfvars.example` to `infra/terraform.tfvars`. Set the project and the Google groups for each tool.
2. **Infrastructure.** `./deploy.sh infra`. This creates a private state bucket, then the load balancer, IAP, Cloud Run services (with a placeholder image), Firestore databases with daily backups, secrets and service accounts. It ends by printing the load balancer's IP.
3. **DNS.** Add an A record: `clientplatforms.nscale.com → <that IP>`. (Or set `dns_managed_zone` if nscale.com is in Cloud DNS in this project, and Terraform makes it.) The certificate becomes active 15 to 60 minutes after DNS resolves. `./deploy.sh status` shows where it is.
4. **Code.**
   ```bash
   ./deploy.sh all ../ITNotifications      # portal, elevation log, and IT Notifications from its repo
   ```
5. **Device keys.** `./deploy.sh keys` prints the IT Notifications enrollment key and the elevation ingest key(s), in the form each installer wants.

IAP is set up with a Google-managed OAuth client, which works for accounts in your own Workspace. If the organisation hasn't used IAP before, the first `apply` may ask you to configure the OAuth consent screen (APIs & Services → OAuth consent screen → **Internal**). If org policy blocks `allUsers` on Cloud Run (domain-restricted sharing), allow it for this project: ingress is already limited to the load balancer.

## Rolling out changes

| What | Command |
|---|---|
| Home page | `./deploy.sh portal` |
| Elevation log | `./deploy.sh elevation` |
| IT Notifications | `./deploy.sh notifications ../ITNotifications` |
| Infrastructure, access lists | edit `infra/`, then `./deploy.sh infra` |

Each build is tagged with the time and git commit. Cloud Run keeps earlier revisions, so rolling back is `gcloud run services update-traffic cp-<name> --to-revisions <revision>=100 --region europe-west2`.

## IT Notifications

The server code stays in its own repo. Here it runs with `AUTH_MODE=iap` and `BASE_PATH=/notifications`, so the OAuth client and session secret from its standalone setup aren't needed.

Install the Windows MSI with `SERVERURL=https://clientplatforms.nscale.com/notifications ENROLLMENTKEY=<key>`, and put the same two values in the Mac profile. `notify_admin_emails` optionally narrows who can send, on top of `notifications_access`.

## Admin elevation log

Both Windows elevation apps (the 15-minute **Admin Elevation** and the 60-minute **Admin Elevation 60**) send each elevation here, as well as to TechOps. They queue separately, so one being down doesn't hold up the other.

**On devices.** Put a `ClientPlatforms.json` beside `Install.ps1` when you build the Intune package (see the ElevationApp README):

```json
{ "url": "https://clientplatforms.nscale.com/elevation", "key": "<ingest key>" }
```

**Ingest keys.** The secret `cp-elevation-ingest-keys` holds one `label:key` per line. The label is shown against every record, so give each group of devices its own key and one can be revoked on its own:

```bash
./deploy.sh add-ingest-key laptops-us       # prints the ClientPlatforms.json for that group
./deploy.sh remove-ingest-key laptops-us    # those devices get 401 and keep their records queued
```

**What's recorded.** One record per elevation, upserted by the worker's `eventId`, with who, which device and serial, the reason, how long, when it was requested and granted, and how it ended (`granted`, `denied`, `expired`, `user_ended`, `revoked`). Every send is also kept unchanged in the record's history, with the time, source address and key label.

**What's flagged.**

| On the page | Means |
|---|---|
| **Admin right now** | Granted and not yet expired. |
| **Never reported ending** | Granted, more than 20 minutes past expiry, and no end record has arrived. The device may be offline, or someone stopped the worker reporting. |
| **Changed after reporting** | A later send changed the person, device, serial, reason or times, or came with a different key. The worker never does that, so someone may be rewriting the record with a key read from a device. The first values are kept and shown alongside. |
| **Tried to reopen** | A send tried to set an ended or denied elevation back to granted. It's kept in the history and ignored. |

**API.** `POST /elevation/api/elevations/ingest` with `Authorization: Bearer <key>` and `{"events": [...]}` — the same request the worker sends TechOps. It answers `{"results": [{"eventId", "status": "created" | "updated" | "rejected", "error"?}]}`; 400 if the body isn't that shape, 401 for an unknown key.

## Running locally

```bash
cd services/elevation && pip install -r requirements.txt -r requirements-dev.txt
STORE=sqlite DATABASE_PATH=./dev.db BASE_PATH=/elevation DEV_USER=you@nscale.com INGEST_KEYS="dev:$(openssl rand -hex 16)" \
  uvicorn app.main:app --reload       # http://localhost:8000/elevation/
python -m pytest -q tests
```

The portal runs the same way (`DEV_USER=… uvicorn app.main:app`). Never set `DEV_USER` on Cloud Run: it skips sign-in.

## Layout

| Folder | What's in it |
|---|---|
| `infra/` | Terraform for everything on Google Cloud (`main.tf`: services, data, secrets; `lb.tf`: load balancer, IAP, routing, Cloud Armor). |
| `services/portal/` | The home page. To add a tool: add it to `TOOLS` in `app/main.py`, add a service in `infra/main.tf` and its paths in `infra/lb.tf`. |
| `services/elevation/` | The elevation log: ingest API, merge rules (`app/records.py`), storage, page, tests. |
| `deploy.sh`, `cloudbuild.yaml` | Build and roll out; manage device keys. |
