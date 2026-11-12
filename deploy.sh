#!/usr/bin/env bash
# Deploys what's committed to the public server (README, "Deploying"), then restarts hal's worker if it runs here as a
# system service, so that it runs the worker script as checked out: from this repository, it never updates itself.
#
#     ./deploy.sh            asks first, if a restart would lose work in progress (meshing on a machine, a job asked for)
#     ./deploy.sh --now      restarts at once, whatever's in progress
#     ./deploy.sh --after    waits for what's in progress to finish (taking no new work meanwhile), then restarts
#
# While it waits, it says what for every 30 s, in this machine's time, and restarts by itself once nothing's left. At a
# terminal, n restarts now instead, c (or Ctrl-C) cancels, and it waits for as long as it takes. Without one to ask at,
# it does --after, for at most WAIT_MINUTES (90).
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
if [ -t 1 ]; then ask=$'\e[1;33m' plain=$'\e[0m'; else ask='' plain=''; fi    # what asks you, in bold yellow
cancel() {
    cli deploy-drain --off >/dev/null || true
    echo "not restarted: the new image is pulled, and runs at the next restart"
    exit "${1:-0}"
}

# the images first: the server pulls its own, which changes nothing until the restart
# (set -e doesn't stop at a failure partway through a && list, hence the || exit)
deploy/build.sh && deploy/ship.sh bouldermesh bouldermesh:public || exit 1

# what a restart would cost now: exit 3 if it would lose work in progress (a server too old to say: 1 or 2)
set +e
cli deploy-check
check=$?
set -e
if [ "$check" -eq 3 ]; then
    if [ "$mode" = ask ]; then
        read -r -p "${ask}Restart [n]ow, [a]fter what's in progress finishes (taking no new work meanwhile), or [c]ancel?${plain} " answer
        case "$answer" in
            n*|N*) mode=now ;;
            a*|A*) mode=after ;;
            *) cancel ;;
        esac
    fi
    if [ "$mode" = after ]; then
        cli deploy-drain >/dev/null || true
        trap 'echo; cancel 130' INT HUP TERM    # never leave the server taking no new work
        echo "taking no new work, and restarting once what's in progress has finished"
        deadline=$((SECONDS + ${WAIT_MINUTES:-90} * 60)) late=''
        while true; do
            set +e
            left=$(cli deploy-check --brief 2>&1)
            st=$?
            set -e
            if [ "$st" -eq 2 ]; then    # a server from before deploy-check --brief (Nov 11, 2026), which waits by itself
                cli deploy-wait --max-minutes "${WAIT_MINUTES:-90}" || echo "restarting anyway"
                break
            fi
            echo "$(date +%H:%M:%S) $left"
            [ "$st" -eq 0 ] && break    # otherwise, something's left, or the server couldn't be asked just now
            if [ "$SECONDS" -ge "$deadline" ] && [ -z "$late" ]; then
                [ -t 0 ] || { echo "still going after ${WAIT_MINUTES:-90} min: restarting anyway"; break; }
                echo "${ask}still going after ${WAIT_MINUTES:-90} min: still waiting, to restart once it's done${plain}"$'\a'
                late=1
            fi
            if [ -t 0 ]; then
                key=''
                read -r -s -n 1 -t 30 -p "${ask}restarting once it's done; or [n] now, [c] cancel, any other key: look again${plain} " key || true
                printf '\r\e[K' >&2     # the next line takes its place, and it comes again below that
                case "$key" in
                    n|N) echo "$(date +%H:%M:%S) restarting now, as you said"; mode=now; break ;;
                    c|C) cancel ;;
                esac
            else
                sleep 30
            fi
        done
        trap - INT HUP TERM
    fi
    if [ "$mode" = now ]; then
        cli deploy-drain --now >/dev/null       # the stop then doesn't wait for jobs either
    fi
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
