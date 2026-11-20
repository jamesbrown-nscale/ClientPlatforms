# Client Platforms on a Google Cloud VM

One small VM runs the whole site with Docker Compose, the same containers as on Portainer. Caddy
handles HTTPS for the site's name itself. People sign in with **OpenID Connect**: Google now, Okta
later, and switching is a settings change. Only people an admin has added under **Settings, People**
get in. Admins find them in the company directory when adding them. Devices keep using their keys.

| | |
|---|---|
| VM | `cp-vm`: e2-small, Debian 12, 20 GB disk, europe-west2 (London) |
| Cost | roughly $15–25 a month: VM, disk, fixed IP and snapshots. Google sign-in and the directory search are free. |
| Backups | the disk's daily snapshot schedule `cp-daily`, kept 14 days |
| Data | Docker volumes on the VM disk (SQLite and uploaded files). Starts empty: nothing is copied from Portainer. |

## 1. Create it (once)

From your laptop or Cloud Shell, signed in to Google Cloud as someone who can create these:

```bash
PROJECT=<project-id> ./deploy/vm/create.sh
```

This creates the VM, its fixed address, the firewall (80 and 443 open; SSH only through Google's IAP
tunnel), the VM's service account and the daily snapshot schedule. At the end it prints the address
and a client ID.

## 2. DNS

Add an **A record** for the site's name, e.g. `clientplatforms.nscale.com`, pointing at the address
`create.sh` printed. Caddy can only get the HTTPS certificate once this resolves.

## 3. Google sign-in (OAuth client)

In the Google Cloud console, **APIs & Services**:
1. **OAuth consent screen**: User type **Internal** (only nscale.com accounts), app name "Client Platforms".
2. **Credentials, Create credentials, OAuth client ID**, type **Web application**.
   Authorised redirect URI: `https://<DOMAIN>/auth/callback`.
3. Keep the client ID and secret for step 5.

## 4. Directory search (Google Workspace)

A Workspace **super admin**, in the Google Admin console: **Security, Access and data control, API
controls, Domain-wide delegation, Add new**:
- Client ID: the one `create.sh` printed (the VM's service account)
- Scope: `https://www.googleapis.com/auth/admin.directory.user.readonly`

This is read-only: it lets admins look people up when adding them. There's no key file, because the
VM signs as its own service account.

## 5. Set up the VM

```bash
gcloud compute ssh cp-vm --zone europe-west2-b --project <project-id> --tunnel-through-iap
```

On the VM, get `setup.sh` there (copy it over, or `curl` it if the repo is public) and run it:

```bash
sudo REPO=git@github.com:<owner>/<repo>.git bash setup.sh
```

For a private repo it prints a **deploy key**. Add it to the repository under **Settings, Deploy
keys**, without write access, then run the script again. The script installs Docker and turns on
security updates. It clones the repo to `/opt/client-platforms` and creates `.env` with random
secrets and device keys.

Fill in the rest of `.env` (`sudo nano /opt/client-platforms/.env`):
- `DOMAIN`
- `ADMIN_EMAIL`: you, the first admin
- `OIDC_CLIENT_ID` and `OIDC_CLIENT_SECRET` from step 3
- `DIRECTORY_GOOGLE_ADMIN`: a Workspace admin the directory search acts as

Copy `ADMIN_PASSWORD` into your password manager: it's the break-glass password.

## 6. Start it

```bash
sudo /opt/client-platforms/deploy/vm/update.sh
```

Open `https://<DOMAIN>`, choose **Sign in with Google**, and you're in as the first admin. Under
**Settings, People, Add person**, type a name to find someone, pick them, give them roles and save.
Anyone not on that list who signs in is told they don't have access.

## 7. Devices

The laptop agents keep using keys, not sign-in. Give them the new address and the keys from `.env`:

| Agent | Sends to | Key |
|---|---|---|
| IT Notifications | `https://<DOMAIN>/notifications/api/v1/checkin` | `X-Nscale-Key: <ENROLLMENT_KEY>` |
| Admin elevation | `https://<DOMAIN>/elevation/api/elevations/ingest` | `Authorization: Bearer <a key from ELEVATION_INGEST_KEYS>` |
| Restart reminder | `https://<DOMAIN>/restart/api/reports` and `/restart/api/policy` | `Authorization: Bearer <a key from RESTART_INGEST_KEYS>` |

Device requests over 2 MB are refused. Each app logs wrong keys, and turns away an address after 20
wrong keys in 10 minutes until those age out. To see the log:

```bash
cd /opt/client-platforms && sudo docker compose logs elevation | grep bad_device_key
```

## Updating

Push to `main`, then on the VM:

```bash
sudo /opt/client-platforms/deploy/vm/update.sh
```

It pulls, rebuilds and restarts. Images run their tests while building, so a version with failing
tests doesn't start, and the running one carries on.

## Backups and restoring

The `cp-daily` schedule snapshots the VM's disk every day at 02:00 and keeps 14 days. That covers
everything: the databases, uploaded documents, settings and certificates. To see them:

```bash
gcloud compute snapshots list --project <project-id> --filter="sourceDisk~cp-vm"
```

To restore, make a disk from a snapshot and swap it in:

```bash
gcloud compute instances stop cp-vm --zone europe-west2-b
gcloud compute disks create cp-vm-restored --zone europe-west2-b --source-snapshot <snapshot-name>
gcloud compute instances detach-disk cp-vm --zone europe-west2-b --disk <current-boot-disk>
gcloud compute instances attach-disk cp-vm --zone europe-west2-b --disk cp-vm-restored --boot
gcloud compute disks add-resource-policies cp-vm-restored --zone europe-west2-b --resource-policies cp-daily
gcloud compute instances start cp-vm --zone europe-west2-b
```

A snapshot is taken while the site runs. SQLite treats restoring one like a power cut and recovers on
its own.

## Changing device keys

1. Add the new key alongside the old one in `.env`, e.g. `laptops:<old>,laptops2:<new>`, and run `update.sh`.
2. Move the devices to the new key.
3. Remove the old one and run `update.sh` again.

To cut a group off at once, remove its label and run `update.sh`.

## Break-glass

If Google or Okta sign-in is down: on the sign-in page, open **Admin break-glass sign-in** and use
`ADMIN_EMAIL` with `ADMIN_PASSWORD`. Only the accounts in `BREAK_GLASS_EMAILS` (default: just
`ADMIN_EMAIL`) can use a password. Everyone else can only sign in with OIDC.

## Moving to Okta later

1. In Okta, create an **OIDC Web app** with sign-in redirect URI `https://<DOMAIN>/auth/callback`.
   Assign it to a group such as "Client Platforms users". Only that group can then reach the sign-in,
   on top of the People list.
2. For the directory search, create an API token as a **read-only admin**.
3. In `.env`:
   ```
   OIDC_ISSUER=https://<your-org>.okta.com
   OIDC_CLIENT_ID=<from the Okta app>
   OIDC_CLIENT_SECRET=<from the Okta app>
   DIRECTORY=okta
   OKTA_API_TOKEN=<the token>
   ```
4. Run `update.sh`. People are matched by email, so everyone keeps their roles.

Someone turned off in Okta can't sign in. An admin can also turn someone off under People, which
takes effect straight away.

## Once it's confirmed working

Remove the old ways of running it: `infra/`, `deploy.sh`, `cloudbuild.yaml`, the IAP code, and the
Cloudflare files (`docker-compose.cloudflare.yml`, `cloudflare.py`).
