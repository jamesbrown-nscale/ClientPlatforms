#!/bin/bash
# Sets up the VM: Docker, a little swap (image builds run the tests), the repo and its settings.
# Run on the VM, once:
#
#   curl -fsSL https://raw.githubusercontent.com/<owner>/<repo>/main/deploy/vm/setup.sh -o setup.sh   (public repo)
#   or copy it over, then:  sudo REPO=git@github.com:<owner>/<repo>.git bash setup.sh
#
# A private repo needs a read-only deploy key: the script makes one and shows it; add it to the repo
# (Settings, Deploy keys, no write access), then run the script again.
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "Run with sudo." >&2; exit 1; }

REPO=${REPO:?Set REPO to the repository to clone, e.g. git@github.com:<owner>/<repo>.git}
BRANCH=${BRANCH:-main}
DIR=${DIR:-/opt/client-platforms}

echo "==> Docker"
if ! command -v docker >/dev/null; then
  apt-get update -q
  apt-get install -yq ca-certificates curl git
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc
  chmod a+r /etc/apt/keyrings/docker.asc
  . /etc/os-release
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian $VERSION_CODENAME stable" \
    > /etc/apt/sources.list.d/docker.list
  apt-get update -q
  apt-get install -yq docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
fi
systemctl enable --now docker

echo "==> Unattended security updates"
apt-get install -yq unattended-upgrades >/dev/null
dpkg-reconfigure -f noninteractive unattended-upgrades

echo "==> 2 GB swap (building the images is the busiest it gets)"
if ! swapon --show | grep -q /swapfile; then
  fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap /swapfile >/dev/null && swapon /swapfile
  grep -q /swapfile /etc/fstab || echo "/swapfile none swap sw 0 0" >> /etc/fstab
fi

echo "==> The repository"
if [[ "$REPO" == git@* ]]; then
  KEY=/root/.ssh/client-platforms-deploy
  if [ ! -f "$KEY" ]; then
    mkdir -p /root/.ssh && chmod 700 /root/.ssh
    ssh-keygen -q -t ed25519 -N "" -C "client-platforms-vm" -f "$KEY"
  fi
  export GIT_SSH_COMMAND="ssh -i $KEY -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new"
  if ! git ls-remote "$REPO" >/dev/null 2>&1; then
    echo
    echo "Add this read-only deploy key to the repository (Settings, Deploy keys), then run this again:"
    cat "$KEY.pub"
    exit 1
  fi
  git config --global core.sshCommand "$GIT_SSH_COMMAND"
fi
if [ ! -d "$DIR/.git" ]; then
  git clone --branch "$BRANCH" "$REPO" "$DIR"
fi

echo "==> Automatic updates (checks GitHub every 2 minutes)"
bash "$DIR/deploy/vm/install-autoupdate.sh" >/dev/null && echo "    on: pushes to $BRANCH reach the site by themselves (journalctl -u cp-update)"

echo "==> Settings"
if [ ! -f "$DIR/.env" ]; then
  cp "$DIR/deploy/vm/env.example" "$DIR/.env"
  chmod 600 "$DIR/.env"
  # Fill in the secrets that are just random.
  sed -i "s/^AUTH_SECRET=$/AUTH_SECRET=$(openssl rand -hex 32)/; s/^ENROLLMENT_KEY=$/ENROLLMENT_KEY=$(openssl rand -hex 24)/;
          s/^ELEVATION_INGEST_KEYS=$/ELEVATION_INGEST_KEYS=laptops:$(openssl rand -hex 24)/;
          s/^RESTART_INGEST_KEYS=$/RESTART_INGEST_KEYS=laptops:$(openssl rand -hex 24)/;
          s/^ADMIN_PASSWORD=$/ADMIN_PASSWORD=$(openssl rand -base64 24 | tr -d '\/+=')/" "$DIR/.env"
  echo
  echo "Now fill in the rest of $DIR/.env (DOMAIN, ADMIN_EMAIL, OIDC_CLIENT_ID, OIDC_CLIENT_SECRET, ...):"
  echo "  sudo nano $DIR/.env"
  echo "then start it:  sudo $DIR/deploy/vm/update.sh"
else
  echo "    $DIR/.env is already there; start or update with:  sudo $DIR/deploy/vm/update.sh"
fi
