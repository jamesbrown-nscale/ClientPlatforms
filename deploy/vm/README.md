# Client Platforms on a Google Cloud VM

One small VM runs the whole site with Docker Compose. Caddy
handles HTTPS for the site's name itself. People sign in with **OpenID Connect**: Google now, Okta
later, and switching is a settings change. Only people an admin has added under **Settings, People**
get in. Admins find them in the company directory when adding them. Devices keep using their keys.

| | |
|---|---|
| VM | `cp-vm`: e2-small, Debian 12, 20 GB disk, europe-west2 (London) |
| Cost | roughly $15–25 a month: VM, disk, fixed IP and snapshots. Google sign-in and the directory search are free. |
| Backups | the disk's daily snapshot schedule `cp-daily`, kept 14 days |
| Data | Docker volumes on the VM disk (SQLite and uploaded files). |

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
| IT Notifications | `https://<DOMAIN>/notifications/api/v1/checkin` | `X-Nscale-Key: <an enrollment key from Settings → Site, or ENROLLMENT_KEY>` |
| Admin elevation | `https://<DOMAIN>/elevation/api/elevations/ingest` | `Authorization: Bearer <an ingest key from Settings → Site, or ELEVATION_INGEST_KEYS>` |
| Restart reminder | `https://<DOMAIN>/restart/api/reports` and `/restart/api/policy` | `Authorization: Bearer <an ingest key from Settings → Site, or RESTART_INGEST_KEYS>` |

Device requests over 2 MB are refused. Each app logs wrong keys, and turns away an address after 20
wrong keys in 10 minutes until those age out. To see the log:

```bash
cd /opt/client-platforms && sudo docker compose logs elevation | grep bad_device_key
```

## Updating

**It updates itself.** Every 2 minutes the VM checks GitHub. When `main` has a new commit, it pulls,
rebuilds and restarts, so a push reaches the site within about 2 minutes plus the build (several minutes on this small VM). Images run
their tests while building: if a test fails, the new version doesn't start, the running one carries
on, and the failure is in the log. The VM tries that commit again every 30 minutes (it remembers what
is actually running in `.cp-deployed`, not just what it pulled), and a newer push is tried at once.
GitHub's checks build each image the same way, so a failure like this usually shows there first.

```bash
journalctl -u cp-update                       # what it did (add -f to watch)
systemctl list-timers cp-update.timer         # when it checks next
sudo systemctl disable --now cp-update.timer  # pause it
sudo systemctl enable --now cp-update.timer   # turn it back on
```

**The version** shows in the sidebar beside "Client Platforms": 0.1 was the first, and each commit
on `main` adds one (0.2, 0.3, … 0.10). It's worked out from the commit count when the site updates
(`deploy/vm/version.sh`), and `https://<DOMAIN>/healthz` shows it too. For 1.0, change `MAJOR` and
`BASE` in that script.

To update straight away instead of waiting:

```bash
sudo /opt/client-platforms/deploy/vm/update.sh
```

`setup.sh` turns automatic updates on. On a VM set up before they existed, turn them on once with
`git -C /opt/client-platforms pull` and then:

```bash
sudo /opt/client-platforms/deploy/vm/install-autoupdate.sh
```

To check every 5 minutes instead, run it with `EVERY=5`. Only one update runs at a time. If the VM's
copy of the repo has a commit GitHub doesn't, it stops and says so rather than guessing.

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
