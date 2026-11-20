#!/bin/bash
# Turns on automatic updates: the cp-update timer runs autoupdate.sh every few minutes, so a push to
# the branch reaches the site on its own. setup.sh runs this; on a VM set up before it existed:
#
#   sudo /opt/client-platforms/deploy/vm/install-autoupdate.sh
#
# Check every N minutes instead of 5:   sudo EVERY=2 ./install-autoupdate.sh
# Pause it:                             sudo systemctl disable --now cp-update.timer
# Turn it back on:                      sudo systemctl enable --now cp-update.timer
# What it did:                          journalctl -u cp-update
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "Run with sudo." >&2; exit 1; }
DIR=$(cd "$(dirname "$0")/../.." && pwd)
EVERY=${EVERY:-5}

cat > /etc/systemd/system/cp-update.service <<EOF
[Unit]
Description=Client Platforms: update the site if GitHub has a newer commit
After=network-online.target docker.service
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=$DIR/deploy/vm/autoupdate.sh
TimeoutStartSec=45min
EOF

cat > /etc/systemd/system/cp-update.timer <<EOF
[Unit]
Description=Client Platforms: check GitHub for updates every $EVERY minutes

[Timer]
OnActiveSec=1min
OnBootSec=3min
OnUnitInactiveSec=${EVERY}min
RandomizedDelaySec=20s

[Install]
WantedBy=timers.target
EOF

chmod +x "$DIR/deploy/vm/autoupdate.sh" "$DIR/deploy/vm/update.sh"
systemctl daemon-reload
systemctl enable --now cp-update.timer
echo "Automatic updates are on: every $EVERY minutes. Log: journalctl -u cp-update"
systemctl list-timers cp-update.timer --no-pager | head -3
