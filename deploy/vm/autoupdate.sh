#!/bin/bash
# Checks GitHub for a newer commit on the VM's branch and, if there is one, updates the site with
# update.sh (pull, rebuild, restart). The cp-update timer runs it every few minutes; see
# install-autoupdate.sh. Run it by hand any time:  sudo /opt/client-platforms/deploy/vm/autoupdate.sh
#
# - Nothing new: it does nothing (one quiet git fetch).
# - Something new: it updates. The images run their tests while they build, so if a test fails the
#   new version doesn't start and the running site carries on; the failure is in the log, and the
#   next push is tried as normal.
# - Only one runs at a time: if an update is still building, a second one just stops.
# Its log:  journalctl -u cp-update
set -euo pipefail
cd "$(dirname "$0")/../.."

exec 9>"${LOCK:-/run/cp-update.lock}"
if ! flock -n 9; then
  echo "An update is already running; skipping this check."
  exit 0
fi

branch=$(git rev-parse --abbrev-ref HEAD)
git fetch -q origin "$branch"
here=$(git rev-parse HEAD)
there=$(git rev-parse "origin/$branch")
if [ "$here" = "$there" ]; then
  exit 0
fi
if ! git merge-base --is-ancestor "$here" "$there"; then
  echo "The VM's copy has commits GitHub doesn't ($here vs origin/$branch $there): not updating. Sort it out by hand."
  exit 1
fi

echo "Updating ${here:0:7} -> ${there:0:7}:"
git log --oneline "$here..$there" | sed 's/^/  /'
if "$(dirname "$0")/update.sh"; then
  echo "Updated to ${there:0:7}."
else
  echo "The update to ${there:0:7} failed (a test or the build): the site is still running the version before. See above."
  exit 1
fi
