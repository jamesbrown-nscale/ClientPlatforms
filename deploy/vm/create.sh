#!/bin/bash
# Creates the Google Cloud side of Client Platforms: one small VM and what it needs.
# Run it from your laptop or Cloud Shell, signed in as someone who can create these in the project:
#
#   gcloud auth login
#   PROJECT=my-project ./deploy/vm/create.sh
#
# It's safe to run again: anything that already exists is left as it is.
#
# What it makes (names start with cp-):
#   cp-vm             the VM: e2-small, Debian 12, 20 GB disk, Shielded VM, no SSH from the internet
#   cp-ip             its fixed public address (point DNS at it)
#   cp-vm@…           the VM's own service account: no project roles, only allowed to sign as itself
#                     (for the Google directory search, so no key file is needed)
#   cp-daily          a snapshot schedule on the disk: every day, kept KEEP_DAYS days (the backups)
#   firewall          80/443 open to everyone (people and devices); SSH only through Google's IAP tunnel
set -euo pipefail

PROJECT=${PROJECT:?Set PROJECT to the Google Cloud project id}
REGION=${REGION:-europe-west2}
ZONE=${ZONE:-europe-west2-b}
MACHINE=${MACHINE:-e2-small}
DISK_GB=${DISK_GB:-20}
KEEP_DAYS=${KEEP_DAYS:-14}
SA="cp-vm@$PROJECT.iam.gserviceaccount.com"
g() { gcloud --project "$PROJECT" --quiet "$@"; }
exists() { "$@" >/dev/null 2>&1; }

echo "==> Turning on the APIs (Compute Engine, IAM Credentials for keyless directory search)"
g services enable compute.googleapis.com iamcredentials.googleapis.com admin.googleapis.com

echo "==> Service account cp-vm"
exists g iam service-accounts describe "$SA" || \
  g iam service-accounts create cp-vm --display-name "Client Platforms VM"
# Only lets the VM sign tokens as itself (domain-wide delegation without a key file). No project roles.
g iam service-accounts add-iam-policy-binding "$SA" --member "serviceAccount:$SA" \
  --role roles/iam.serviceAccountTokenCreator >/dev/null

echo "==> Fixed address cp-ip"
exists g compute addresses describe cp-ip --region "$REGION" || \
  g compute addresses create cp-ip --region "$REGION" --network-tier PREMIUM
IP=$(g compute addresses describe cp-ip --region "$REGION" --format 'value(address)')

echo "==> Firewall"
exists g compute firewall-rules describe cp-allow-web || \
  g compute firewall-rules create cp-allow-web --network default --direction INGRESS --action ALLOW \
    --rules tcp:80,tcp:443,udp:443 --source-ranges 0.0.0.0/0 --target-tags cp-web
exists g compute firewall-rules describe cp-allow-iap-ssh || \
  g compute firewall-rules create cp-allow-iap-ssh --network default --direction INGRESS --action ALLOW \
    --rules tcp:22 --source-ranges 35.235.240.0/20 --target-tags cp-web
# The default network's own "allow SSH from anywhere" rule, if it's there, would undo that: say so.
if exists g compute firewall-rules describe default-allow-ssh; then
  echo "    Note: the default network has default-allow-ssh (SSH from anywhere). Delete it unless something else needs it:"
  echo "      gcloud compute firewall-rules delete default-allow-ssh --project $PROJECT"
fi

echo "==> Snapshot schedule cp-daily (every day, kept $KEEP_DAYS days)"
exists g compute resource-policies describe cp-daily --region "$REGION" || \
  g compute resource-policies create snapshot-schedule cp-daily --region "$REGION" \
    --daily-schedule --start-time 02:00 --max-retention-days "$KEEP_DAYS" \
    --on-source-disk-delete keep-auto-snapshots --storage-location "$REGION"

echo "==> VM cp-vm"
if ! exists g compute instances describe cp-vm --zone "$ZONE"; then
  g compute instances create cp-vm --zone "$ZONE" --machine-type "$MACHINE" \
    --image-family debian-12 --image-project debian-cloud \
    --boot-disk-size "${DISK_GB}GB" --boot-disk-type pd-balanced \
    --address "$IP" --network-tier PREMIUM --tags cp-web \
    --service-account "$SA" --scopes cloud-platform \
    --shielded-secure-boot --shielded-vtpm --shielded-integrity-monitoring \
    --metadata enable-oslogin=TRUE
fi
DISK=$(g compute instances describe cp-vm --zone "$ZONE" --format 'value(disks[0].source.basename())')
g compute disks add-resource-policies "$DISK" --zone "$ZONE" --resource-policies cp-daily 2>/dev/null || true

cat <<EOF

Done. Next:
  1. DNS: point your site's name (e.g. clientplatforms.nscale.com) at $IP (an A record).
  2. Directory search (Google): in the Google Admin console, Security, API controls, Domain-wide
     delegation, add the client ID below with the scope
       https://www.googleapis.com/auth/admin.directory.user.readonly
     Client ID: $(g iam service-accounts describe "$SA" --format 'value(oauth2ClientId)')
  3. Sign in to the VM and set it up (deploy/vm/README.md, "Set up the VM"):
       gcloud compute ssh cp-vm --zone $ZONE --project $PROJECT --tunnel-through-iap
EOF
