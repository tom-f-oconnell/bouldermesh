#!/bin/bash
# Moves the server's data/cache (fetched videos, downloaded photos, the web cache, models: ~18 GB on Nov 24, 2026) from
# the Volume onto the server's own (system) disk, keeping its path: /srv/bouldermesh-cache is bind-mounted at
# ./data/cache, so the site, deploy.sh and the backups need no change, and the Volume has that much more room for the rest.
# The data's cap counts it as before; the server also keeps OTHER_DISK_FLOOR_GB (15) free on the system disk, for Docker's
# images and the logs, and waits for room under it as at the cap.
#
#     deploy/cache-to-system-disk.sh       on the server, from the repo's folder
#
# 1. copies data/cache to /srv/bouldermesh-cache while the site runs;
# 2. stops the site, now or once what's in progress has finished (as deploy.sh's [a]), copies what
#    changed meanwhile, puts the old folder aside as data/cache.before-move, and mounts the copy at data/cache (and in
#    /etc/fstab, so it's there after a reboot, after the Volume's own mount);
# 3. starts the site again, checks it sees the mount, and asks before deleting data/cache.before-move.
#
# The empty folder under the mount is made immutable, so that if the mount is ever missing the site can't write its
# cache back onto the Volume unnoticed. Asks before stopping.
set -euo pipefail
cd "$(dirname "$0")/.."
here=$(pwd)
dest=/srv/bouldermesh-cache
mountpoint -q data || { echo "./data isn't a mount point (the Volume): this expects deploy/data-to-volume.sh's layout" >&2; exit 1; }
! mountpoint -q data/cache || { echo "data/cache is already a mount point: nothing to do" >&2; exit 1; }
[ ! -e data/cache.before-move ] || { echo "data/cache.before-move is there already: move it away first" >&2; exit 1; }
need=$(du -s --block-size=1 data/cache | cut -f1); free=$(df --output=avail -B1 / | tail -1)
[ "$free" -gt $(( need + 20 * 2**30 )) ] || { echo "/ has $(( free / 2**30 )) GB free; data/cache needs $(( need / 2**30 )) GB and 20 GB to spare" >&2; exit 1; }

echo "== 1. copying data/cache ($(( need / 2**30 )) GB) to $dest, with the site running"
sudo mkdir -p "$dest" && sudo chown "$(id -u):$(id -g)" "$dest"
nice -n 19 ionice -c3 rsync -aH --info=progress2 data/cache/ "$dest/"

read -r -p "== 2. stop the site, copy what changed and switch to the system disk's copy: [a]fter what's in progress finishes (taking no new work meanwhile, as deploy.sh's [a]), [n]ow, or [c]ancel? " go
case "$go" in
    a*|A*) mode=after ;;
    n*|N*) mode=now ;;
    *) echo "stopped there: run it again to carry on (the copy so far is kept and only topped up)"; exit 0 ;;
esac
cli() { docker compose exec -T bouldermesh python3 cli.py "$@"; }
if [ "$mode" = after ]; then
    cli deploy-drain > /dev/null
    trap 'cli deploy-drain --off > /dev/null; echo; echo "cancelled: taking work again, nothing switched"; exit 130' INT HUP TERM
    echo "taking no new work, and stopping once what's in progress has finished (Ctrl-C cancels)"
    while true; do
        left=$(cli deploy-check --brief 2>&1) && break
        echo "$(date +%H:%M:%S) $left"
        cli deploy-drain > /dev/null 2>&1 || true     # renewed: the drain lasts 5 min unless it is
        sleep 30
    done
    trap - INT HUP TERM
    echo "$(date +%H:%M:%S) nothing in progress that a stop would lose: stopping"
fi
sudo systemctl stop bouldermesh
rsync -aH --delete data/cache/ "$dest/"
mv data/cache data/cache.before-move
mkdir data/cache && sudo chattr +i data/cache
line="$dest $here/data/cache none bind,nofail,x-systemd.requires-mounts-for=$here/data 0 0"
grep -qF "$here/data/cache " /etc/fstab || echo "$line" | sudo tee -a /etc/fstab > /dev/null
sudo systemctl daemon-reload
sudo mount "$here/data/cache"
mountpoint -q data/cache && [ -e data/cache/relevance.sqlite ] || { echo "the mount didn't work: data/cache.before-move has everything; undo with: sudo umount data/cache; sudo chattr -i data/cache; rmdir data/cache; mv data/cache.before-move data/cache; sudo systemctl start bouldermesh" >&2; exit 1; }

echo "== 3. starting the site"
sudo systemctl start bouldermesh
for _ in $(seq 60); do
    docker compose exec -T bouldermesh true 2>/dev/null && break
    sleep 2
done
seen=$(docker compose exec -T bouldermesh python3 -c 'import os; print(os.stat("/data").st_dev != os.stat("/data/cache").st_dev)' 2>/dev/null || echo "?")
[ "$seen" = True ] || { echo "the site doesn't see data/cache as another disk ($seen): leaving data/cache.before-move; see docker compose logs bouldermesh" >&2; exit 1; }
df -h / data | sed 's/^/   /'
read -r -p "== the site runs on the moved cache. Delete data/cache.before-move ($(( need / 2**30 )) GB on the Volume) now? [y/N] " go
if [ "$go" = y ]; then
    rm -rf data/cache.before-move
    df -h data | sed 's/^/   /'
else
    echo "kept: delete it later with rm -rf $here/data/cache.before-move"
fi
echo "done. Then raise the cap on the admin page's Room for data (by about what the system disk adds, ~25 GB)."
