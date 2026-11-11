#!/usr/bin/env bash
# Deploys what's committed to the public server (README, "Deploying"), then restarts hal's worker if it runs here as a
# system service, so that it runs the worker script as checked out: from this repository, it never updates itself.
#
#     ./deploy.sh            asks first, if a restart would lose work in progress (meshing on a machine, a job asked for)
#     ./deploy.sh --now      restarts at once, whatever's in progress
#     ./deploy.sh --after    waits for what's in progress to finish (taking no new work meanwhile), then restarts
#
# Without a terminal to ask at, it does --after. It waits at most WAIT_MINUTES (90), then asks again, or restarts.
set -euo pipefail
cd "$(dirname "$0")"

mode=ask
case "${1:-}" in
    --now) mode=now ;;
    --after) mode=after ;;
    "") ;;
    *) echo "usage: $0 [--now | --after]" >&2; exit 2 ;;
esac
[ "$mode" = ask ] && [ ! -t 0 ] && mode=after
on_server() { ssh bouldermesh "cd bouldermesh && $1"; }
cli() { on_server "docker compose exec -T bouldermesh python3 cli.py $*"; }

# the images first: the server pulls its own, which changes nothing until the restart
# (set -e doesn't stop at a failure partway through a && list, hence the || exit)
deploy/build.sh && deploy/ship.sh bouldermesh bouldermesh:public || exit 1

# what a restart would cost now: exit 3 if it would lose work in progress (a server too old to say: 1 or 2)
set +e
cli deploy-check
check=$?
set -e
if [ "$check" -eq 3 ]; then
    while true; do
        if [ "$mode" = ask ]; then
            read -r -p "Restart [n]ow, [a]fter what's in progress finishes (taking no new work meanwhile), or [c]ancel? " answer
            case "$answer" in
                n*|N*) mode=now ;;
                a*|A*) mode=after ;;
                *) cli deploy-drain --off >/dev/null || true
                   echo "not restarted: the new image is pulled, and runs at the next restart"; exit 0 ;;
            esac
        fi
        if [ "$mode" = now ]; then
            cli deploy-drain --now >/dev/null       # the stop then doesn't wait for jobs either
            break
        fi
        if cli deploy-wait --max-minutes "${WAIT_MINUTES:-90}"; then
            break
        fi
        if [ -t 0 ]; then mode=ask; else echo "restarting anyway"; break; fi
    done
elif [ "$check" -ne 0 ]; then
    echo "couldn't ask the server what's in progress (it may be down): restarting anyway"
fi

on_server 'git pull --ff-only && sudo systemctl restart bouldermesh' || exit 1

# only a system unit (not a user's own, systemctl --user), loaded and running, which takes root to restart
if [ "$(systemctl show -p LoadState --value bouldermesh-worker.service 2>/dev/null)" = loaded ] \
        && systemctl is-active --quiet bouldermesh-worker.service; then
    if [ "$(id -u)" -eq 0 ]; then
        systemctl restart bouldermesh-worker
    elif [ -t 0 ] || sudo -n true 2>/dev/null; then     # a password can be typed, or none is needed
        sudo systemctl restart bouldermesh-worker
    else
        echo "deployed; restart the worker yourself, as sudo needs your password: sudo systemctl restart bouldermesh-worker"
    fi
else
    echo "bouldermesh-worker isn't running as a system service here: not restarting it"
fi
