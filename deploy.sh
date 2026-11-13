#!/usr/bin/env bash
# Deploys what's committed to the public server (README, "Deploying"), then restarts hal's worker if it runs here as a
# system service from this repository's copy of the worker script, which never updates itself. A downloaded copy (as
# deploy/bouldermesh-worker.service runs) updates itself from the server, and every job runs in the server's own image,
# so that needs no restart.
#
#     ./deploy.sh            asks first, if a restart would lose work in progress (meshing on a machine, a job asked for)
#                            (meshing on a machine that connects for work isn't lost: it carries on through the
#                            restart, and its climb takes it up after, unless the new version changes MESH_RULES or
#                            what it's made from)
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
if [ -t 1 ]; then ask=$'\e[1;33m' white=$'\e[1;37m' plain=$'\e[0m'; else ask='' white='' plain=''; fi    # what asks you, in bold yellow (the restart question white, among the coloured list)
# a question, with the legend (what a restart does to each kind of work in progress) below it while it's asked, if
# at a terminal: the caller reads the answer, then clears it with \e[J
below() {
    if [ -n "$legend" ] && [ -t 1 ]; then
        local n=$(($(printf '%s\n' "$legend" | wc -l) + 1))
        printf "%${n}s" '' | tr ' ' '\n'; printf '\e[%dA' "$n"     # room below first, so that it doesn't scroll
        printf '%s\e7\n%s\e8' "$1" "$legend"                       # the legend below, back to the question
    else
        printf '%s' "$1"
    fi
}
legend=''
grey=''; [ -n "$ask" ] && grey=$'\e[90m'    # what's said while it waits, as cli.py's deploy-check --brief --color
# the server's /api/deploy, for this copy of cli.py to show (asked from inside its container, as it only answers there)
deploy_json() {
    ssh bouldermesh 'cd bouldermesh && docker compose exec -T bouldermesh python3 -' <<'PY'
import os, sys, urllib.request
sys.stdout.write(urllib.request.urlopen(f"http://127.0.0.1:{os.environ.get('PORT', '8470')}/api/deploy", timeout=30).read().decode())
PY
}
cancel() {
    cli deploy-drain --off >/dev/null || true
    echo "not restarted: the new image is pulled, and runs at the next restart"
    exit "${1:-0}"
}

# the images first: the server pulls its own, which changes nothing until the restart
# (set -e doesn't stop at a failure partway through a && list, hence the || exit)
export DEPLOY_SH=1      # so that they leave out what they'd tell you to do next by hand, which this does
deploy/build.sh && deploy/ship.sh bouldermesh bouldermesh:public || exit 1

# what a restart would cost now: exit 3 if it would lose work in progress (a server too old to say: 1 or 2)
# (and after a line "@@legend", what a restart does to each kind of work listed: shown below the question while it's asked)
set +e
out=$(deploy_json | python3 cli.py deploy-check --from - ${ask:+--color})
check=$?
set -e
legend=''
if [[ "$out" == *$'\n@@legend\n'* ]]; then
    legend=${out#*$'\n@@legend\n'}
    out=${out%%$'\n@@legend\n'*}
fi
printf '%s\n' "$out"
[ "$check" -eq 3 ] && [ "$mode" = ask ] || { [ -n "$legend" ] && printf '%s\n' "$legend"; }
if [ "$check" -eq 3 ]; then
    if [ "$mode" = ask ]; then
        question="${white}Restart [n]ow, [a]fter what's in progress finishes (taking no new work meanwhile), or [c]ancel?${plain} "
        trap 'printf "\\e[J\\n"; cancel 130' INT HUP TERM      # Ctrl-C (or being killed) clears the legend too
        below "$question"
        read -r answer || { printf '\n'; answer=c; }        # Ctrl-D: cancel, as any other answer
        trap - INT HUP TERM
        printf '\e[J'                                      # and the legend gone again
        case "$answer" in
            n*|N*) mode=now ;;
            a*|A*) mode=after ;;
            *) cancel ;;
        esac
    fi
    if [ "$mode" = after ]; then
        cli deploy-drain >/dev/null || true
        trap 'printf "\\e[J\\n"; cancel 130' INT HUP TERM    # never leave the server taking no new work (nor the legend shown)
        echo "${grey}taking no new work, and restarting once what's in progress has finished${plain}"
        deadline=$((SECONDS + ${WAIT_MINUTES:-90} * 60)) late=''
        while true; do
            set +e
            left=$(deploy_json | python3 cli.py deploy-check --brief --from - ${ask:+--color} 2>&1)
            st=$?
            set -e
            echo "${grey}$(date +%H:%M:%S)${plain} $left"
            cli deploy-drain >/dev/null 2>&1 || true     # still here: the drain lasts 5 min unless renewed (DRAIN_LEASE)
            [ "$st" -eq 0 ] && break    # otherwise, something's left, or the server couldn't be asked just now
            if [ "$SECONDS" -ge "$deadline" ] && [ -z "$late" ]; then
                [ -t 0 ] || { echo "still going after ${WAIT_MINUTES:-90} min: restarting anyway"; break; }
                echo "${ask}still going after ${WAIT_MINUTES:-90} min: still waiting, to restart once it's done${plain}"$'\a'
                late=1
            fi
            if [ -t 0 ]; then
                key=''
                below "${white}restarting once it's done; or [n] now, [c] cancel, any other key: look again${plain} "
                read -r -s -n 1 -t 30 key || true
                printf '\r\e[J'     # it and the legend: the next line takes its place, and they come again below that
                case "$key" in
                    n|N) echo "${grey}$(date +%H:%M:%S) restarting now, as you said${plain}"; mode=now; break ;;
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
want=$(git rev-parse --short HEAD)
for _ in $(seq 30); do      # up to a minute for it to answer again
    live=$(on_server "curl -s http://127.0.0.1:\${PORT:-8470}/api/status" 2>/dev/null | python3 -c 'import json, sys; print(json.load(sys.stdin).get("version") or "")' 2>/dev/null || true)
    [ -n "$live" ] && break
    sleep 2
done
if [ "$live" = "$want" ]; then
    echo "deployed: the server runs $want"
elif [ -n "$live" ]; then
    echo "restarted, but the server says it runs $live, not $want: see its log (docker compose logs bouldermesh, on it)"
else
    echo "restarted, but the server isn't answering yet: see its log (docker compose logs bouldermesh, on it)"
fi

# only a system unit (not a user's own, systemctl --user), loaded and running, which takes root to restart
script=$(systemctl show -p Environment --value bouldermesh-worker.service 2>/dev/null | tr ' ' '\n' | sed -n 's/^SCRIPT=//p')
if [ -n "$script" ] && [ "$(readlink -f "$script")" != "$(readlink -f worker/bouldermesh_worker.py)" ]; then
    :   # hal's worker runs its own copy of the worker script, which updates itself, and jobs run in the server's image
elif [ "$(systemctl show -p LoadState --value bouldermesh-worker.service 2>/dev/null)" = loaded ] \
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
