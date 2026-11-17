#!/bin/bash
# Client Platforms: set up the infrastructure and roll out each tool.
# Needs gcloud (signed in as a project owner: gcloud auth login && gcloud auth application-default login)
# and Terraform 1.6+ (or OpenTofu: TF=tofu).
#
#   ./deploy.sh infra                          create or update everything in infra/ (terraform apply)
#   ./deploy.sh portal                         build and roll out the home page
#   ./deploy.sh elevation                      build and roll out the elevation log
#   ./deploy.sh notifications <ITNotifications repo folder>
#                                              build and roll out IT Notifications from its own repo
#   ./deploy.sh all <ITNotifications repo folder>
#   ./deploy.sh keys                           show the device keys (enrollment key, elevation ingest keys)
#   ./deploy.sh add-ingest-key <label>         add an elevation ingest key for a group of devices
#   ./deploy.sh remove-ingest-key <label>      revoke one
#   ./deploy.sh status                         certificate, DNS and service status
set -euo pipefail
cd "$(dirname "$0")"
ROOT=$(pwd)
TF=${TF:-terraform}

tfvar() {   # reads a simple  name = "value"  from infra/terraform.tfvars
  [ -f infra/terraform.tfvars ] || return 0
  sed -n "s/^[[:space:]]*$1[[:space:]]*=[[:space:]]*\"\(.*\)\".*/\1/p" infra/terraform.tfvars 2>/dev/null | head -1
}
PROJECT=${PROJECT:-$(tfvar project_id)}
REGION=${REGION:-$(tfvar region)}; REGION=${REGION:-europe-west2}
DOMAIN=${DOMAIN:-$(tfvar domain)}; DOMAIN=${DOMAIN:-clientplatforms.nscale.com}
: "${PROJECT:?Set project_id in infra/terraform.tfvars (copy terraform.tfvars.example), or PROJECT=…}"
REPO="$REGION-docker.pkg.dev/$PROJECT/client-platforms"
BUILD_SA="projects/$PROJECT/serviceAccounts/cp-build@$PROJECT.iam.gserviceaccount.com"

infra() {
  if [ ! -f infra/terraform.tfvars ]; then
    echo "Copy infra/terraform.tfvars.example to infra/terraform.tfvars and fill it in first." >&2; exit 1
  fi
  local bucket="${TF_STATE_BUCKET:-$PROJECT-tfstate}"
  if ! gcloud storage buckets describe "gs://$bucket" --project "$PROJECT" >/dev/null 2>&1; then
    echo "==> Creating the Terraform state bucket gs://$bucket (private, versioned)"
    gcloud storage buckets create "gs://$bucket" --project "$PROJECT" --location "$REGION" \
      --uniform-bucket-level-access --public-access-prevention
    gcloud storage buckets update "gs://$bucket" --versioning
  fi
  (cd infra && $TF init -input=false -reconfigure -backend-config="bucket=$bucket" && $TF apply -input=false)
  echo
  status
}

build_and_roll() {   # build_and_roll <workload> <service> <source folder>
  local name=$1 service=$2 src=$3
  local tag; tag="$(date -u +%Y%m%d-%H%M%S)-$(git -C "$src" rev-parse --short HEAD 2>/dev/null || echo local)"
  local image="$REPO/$name:$tag"
  echo "==> Building $image from $src"
  gcloud builds submit "$src" --project "$PROJECT" --region "$REGION" --config "$ROOT/cloudbuild.yaml" \
    --substitutions "_IMAGE=$image" --service-account "$BUILD_SA" \
    --default-buckets-behavior=regional-user-owned-bucket --quiet
  echo "==> Rolling out $service"
  gcloud run deploy "$service" --project "$PROJECT" --region "$REGION" --image "$image" --quiet
}

notify_src() {
  local src=${1:-}
  [ -n "$src" ] || { echo "Give the path to the ITNotifications repo, e.g. ./deploy.sh notifications ../ITNotifications" >&2; exit 1; }
  [ -f "$src/server/Dockerfile" ] || { echo "$src/server/Dockerfile not found: is that the ITNotifications repo?" >&2; exit 1; }
  echo "$src/server"
}

secret_value() { gcloud secrets versions access latest --secret "$1" --project "$PROJECT"; }

roll_secret() {   # new instances read the new secret version
  gcloud run services update "$1" --project "$PROJECT" --region "$REGION" --quiet \
    --update-labels "secrets-rolled=$(date -u +%Y%m%d%H%M%S)" >/dev/null
}

keys() {
  echo "IT Notifications (Windows MSI / Mac profile):"
  echo "  SERVERURL=https://$DOMAIN/notifications ENROLLMENTKEY=$(secret_value cp-notify-enrollment-key)"
  echo
  echo "Admin Elevation, ClientPlatforms.json beside Install.ps1:"
  secret_value cp-elevation-ingest-keys | while IFS=: read -r label key; do
    [ -n "$label" ] && printf '  %-12s {"url": "https://%s/elevation", "key": "%s"}\n' "$label" "$DOMAIN" "$key"
  done
}

add_ingest_key() {
  local label=${1:?Give a label, e.g. ./deploy.sh add-ingest-key laptops-us}
  [[ "$label" =~ ^[A-Za-z0-9_-]{1,40}$ ]] || { echo "Labels are letters, digits, - and _." >&2; exit 1; }
  local current; current=$(secret_value cp-elevation-ingest-keys)
  if grep -q "^$label:" <<<"$current"; then echo "$label already has a key." >&2; exit 1; fi
  local key; key=$(openssl rand -base64 48 | tr -d '/+=\n' | cut -c1-48)
  printf '%s\n%s:%s' "$current" "$label" "$key" | sed '/^$/d' |
    gcloud secrets versions add cp-elevation-ingest-keys --data-file=- --project "$PROJECT" --quiet >/dev/null
  roll_secret cp-elevation
  echo "Added. ClientPlatforms.json for this group:"
  printf '{"url": "https://%s/elevation", "key": "%s"}\n' "$DOMAIN" "$key"
}

remove_ingest_key() {
  local label=${1:?Give the label to revoke}
  local current; current=$(secret_value cp-elevation-ingest-keys)
  grep -q "^$label:" <<<"$current" || { echo "No key labelled $label." >&2; exit 1; }
  grep -v "^$label:" <<<"$current" | gcloud secrets versions add cp-elevation-ingest-keys --data-file=- --project "$PROJECT" --quiet >/dev/null
  roll_secret cp-elevation
  echo "Revoked $label. Devices using it now get 401 and keep their records queued until they get a new key."
}

status() {
  local ip; ip=$(gcloud compute addresses describe client-platforms --global --project "$PROJECT" --format='value(address)' 2>/dev/null || true)
  local cert; cert=$(gcloud compute ssl-certificates list --global --project "$PROJECT" --filter="name~^client-platforms-" \
    --format='value(managed.status,managed.domainStatus)' 2>/dev/null || true)
  local dns; dns=$(getent hosts "$DOMAIN" 2>/dev/null | awk '{print $1}' | head -1 || true)
  echo "Site:          https://$DOMAIN"
  echo "Load balancer: ${ip:-not created yet}"
  echo "DNS now:       ${dns:-no A record yet}$([ -n "$ip" ] && [ "$dns" != "$ip" ] && echo "   <- add an A record: $DOMAIN -> $ip")"
  echo "Certificate:   ${cert:-not created yet}   (ACTIVE about 15-60 minutes after DNS points at the load balancer)"
  gcloud run services list --project "$PROJECT" --region "$REGION" --filter="metadata.name~^cp-" \
    --format='table(metadata.name:label=SERVICE,status.url.basename():label=URL,status.conditions[0].status:label=READY)' 2>/dev/null || true
}

case "${1:-}" in
  infra)               infra ;;
  portal)              build_and_roll portal cp-portal services/portal ;;
  elevation)           build_and_roll elevation cp-elevation services/elevation ;;
  notifications)       src=$(notify_src "${2:-}")
                       build_and_roll notifications cp-notify "$src" ;;
  all)                 src=$(notify_src "${2:-}")
                       build_and_roll portal cp-portal services/portal
                       build_and_roll elevation cp-elevation services/elevation
                       build_and_roll notifications cp-notify "$src" ;;
  keys)                keys ;;
  add-ingest-key)      add_ingest_key "${2:-}" ;;
  remove-ingest-key)   remove_ingest_key "${2:-}" ;;
  status)              status ;;
  *) sed -n '2,16p' "$0" | sed 's/^# \{0,1\}//'; exit 1 ;;
esac
