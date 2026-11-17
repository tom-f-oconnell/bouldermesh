#!/bin/bash
# Moves the server's data (./data, everything the site keeps) onto an attached Hetzner Volume, keeping its path: the
# Volume's data/ folder is bind-mounted at ./data, so docker-compose.yml, deploy.sh and the backups need no change.
#
#     deploy/data-to-volume.sh /mnt/HC_Volume_<id>       on the server, from the repo's folder
#
# 1. copies ./data onto the Volume while the site runs (most of it: minutes for ~50 GB);
# 2. stops the site (a machine's meshing job carries on on its machine, and is taken up again after), copies what
#    changed meanwhile, puts the old folder aside as data.before-volume, and mounts the Volume's copy at ./data
#    (and in /etc/fstab, so it's there after a reboot);
# 3. starts the site again.
#
# The empty folder under the mount is made immutable, so that if the Volume is ever missing the site fails to start,
# rather than starting with no data and making a fresh database. data.before-volume is kept until you delete it
# (sudo rm -rf data.before-volume, once the site has run well on the Volume for a while). Asks before stopping.
set -euo pipefail
vol=${1:?usage: $0 /mnt/HC_Volume_<id>}
cd "$(dirname "$0")/.."
here=$(pwd)
[ -d data ] && ! mountpoint -q data || { echo "./data is already a mount point: nothing to do" >&2; exit 1; }
mountpoint -q "$vol" || { echo "$vol isn't mounted" >&2; exit 1; }
[ ! -e data.before-volume ] || { echo "data.before-volume is there already: move it away first" >&2; exit 1; }
need=$(du -s --block-size=1 data | cut -f1); free=$(df --output=avail -B1 "$vol" | tail -1)
[ "$free" -gt $(( need + need / 10 )) ] || { echo "the Volume has $(( free / 2**30 )) GB free, ./data needs $(( need / 2**30 )) GB" >&2; exit 1; }

echo "== 1. copying ./data ($(( need / 2**30 )) GB) to $vol/data, with the site running"
sudo mkdir -p "$vol/data" && sudo chown "$(id -u):$(id -g)" "$vol/data"
sudo nice -n 19 ionice -c3 rsync -aHAX --numeric-ids --info=progress2 data/ "$vol/data/"

read -r -p "== 2. stop the site now, copy what changed and switch to the Volume? [y/N] " go
[ "$go" = y ] || { echo "stopped there: run it again to carry on (the copy so far is kept and only topped up)"; exit 0; }
docker compose stop bouldermesh
sudo rsync -aHAX --numeric-ids --delete data/ "$vol/data/"
mv data data.before-volume
mkdir data && sudo chattr +i data
line="$vol/data $here/data none bind,nofail,x-systemd.requires-mounts-for=$vol 0 0"
grep -qF "$here/data " /etc/fstab || echo "$line" | sudo tee -a /etc/fstab > /dev/null
sudo systemctl daemon-reload
sudo mount "$here/data"
mountpoint -q data && [ -e data/bouldermesh.sqlite ] || { echo "the mount didn't work: ./data.before-volume has everything; undo with: sudo umount data; sudo chattr -i data; rmdir data; mv data.before-volume data" >&2; exit 1; }

echo "== 3. starting the site, on the Volume"
docker compose up -d bouldermesh
df -h "$vol" / | sed 's/^/   /'
echo "done. Once it has run well for a while: sudo rm -rf $here/data.before-volume (frees the old copy on the server's own disk)."
