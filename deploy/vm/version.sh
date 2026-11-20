#!/bin/bash
# The site's version, from the commits on its branch: 0.1 was the 34th commit, and every commit
# after it adds one (0.2, 0.3, ... 0.10, 0.11, ...). update.sh passes it to the portal, which shows
# it in the sidebar beside "Client Platforms".
#
# To start a new major version (1.0 = the next commit), set MAJOR=1 and BASE to the commit count
# just before it:  git rev-list --count HEAD
set -euo pipefail
cd "$(dirname "$0")/../.."
MAJOR=0
BASE=33
n=$(git rev-list --count HEAD 2>/dev/null || echo 0)
echo "$MAJOR.$(( n > BASE ? n - BASE : 0 ))"
