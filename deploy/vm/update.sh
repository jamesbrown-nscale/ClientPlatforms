#!/bin/bash
# Starts Client Platforms on the VM, or updates it to the latest on its branch. Run on the VM:
#
#   sudo /opt/client-platforms/deploy/vm/update.sh
#
# Every image runs its tests while it builds, so a version with failing tests doesn't start: the
# running one carries on. Data lives in Docker volumes and isn't touched.
set -euo pipefail
cd "$(dirname "$0")/../.."
[ -f .env ] || { echo "No .env here: run deploy/vm/setup.sh first." >&2; exit 1; }

if [ -d .git ] && [ "${PULL:-1}" = 1 ]; then
  echo "==> Getting the latest"
  git pull --ff-only
fi
echo "==> Building and starting"
docker compose -f docker-compose.yml -f docker-compose.vm.yml up -d --build --remove-orphans
docker image prune -f >/dev/null
docker compose -f docker-compose.yml -f docker-compose.vm.yml ps
