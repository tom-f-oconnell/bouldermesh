#!/usr/bin/env bash
# Deploys what's committed to the public server (README, "Deploying"), then restarts hal's worker if it runs here as a
# system service, so that it runs the worker script as checked out: from this repository, it never updates itself.
set -euo pipefail
cd "$(dirname "$0")"

deploy/build.sh && deploy/ship.sh bouldermesh bouldermesh:public && ssh bouldermesh 'cd bouldermesh && git pull --ff-only && sudo systemctl restart bouldermesh'

# only a system unit (not a user's own, systemctl --user), loaded and running, which takes root to restart
if [ "$(systemctl show -p LoadState --value bouldermesh-worker.service 2>/dev/null)" = loaded ] \
        && systemctl is-active --quiet bouldermesh-worker.service; then
    if [ "$(id -u)" -eq 0 ]; then
        systemctl restart bouldermesh-worker
    else
        sudo systemctl restart bouldermesh-worker
    fi
else
    echo "bouldermesh-worker isn't running as a system service here: not restarting it"
fi
