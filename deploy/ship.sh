#!/bin/sh
# Ships what the public setup needs to the machine that will run it: the image built here, a snapshot of the
# database with every area stopped (nothing runs by itself there), and the meshes. Run here, where the image was built:
#     deploy/ship.sh you@boulders.example.com [image]
# That machine needs Docker with you in the docker group, zstd, and this repository cloned at ~/bouldermesh
# (REMOTE_DIR to change) with ./data writable by you. The image is the one Compose built here unless named, and
# BM_IMAGE in the machine's .env must name it if it isn't bouldermesh-bouldermesh:latest. The database there is
# replaced each time, so holds marked or cleanups edited there with the editing token are lost (do those here), and so
# are settings changed on its admin page: what the site shows is given in its .env (SITE_SOURCES, SITE_TEXTURES).
# Running it again is cheap: an image the machine already has isn't sent again, and only changed meshes are.
set -eu
dest=${1:?usage: deploy/ship.sh user@host [image]}
image=${2:-bouldermesh-bouldermesh:latest}
dir=$(cd "$(dirname "$0")/.." && pwd)
data=${DATA_DIR:-$dir/data}
remote=${REMOTE_DIR:-bouldermesh}
snap=$(mktemp --suffix .sqlite)
trap 'rm -f "$snap"' EXIT
python3 - "$data/bouldermesh.sqlite" "$snap" <<'PY'
import sqlite3
import sys
src = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
dst = sqlite3.connect(sys.argv[2])
src.backup(dst)
stopped = dst.execute("UPDATE campaigns SET status='stopped' WHERE status IN ('running', 'stopping')").rowcount
dst.commit()
n = lambda table: dst.execute(f"SELECT count(*) FROM {table}").fetchone()[0]  # noqa: E731
print(f"database: {n('meshes')} meshes, {n('climbs')} climbs, {n('projects')} projects, {stopped} running area(s) stopped in the copy")
PY
id=$(docker image inspect "$image" --format '{{.Id}}')
if [ "$(ssh "$dest" "docker image inspect $image --format '{{.Id}}' 2>/dev/null")" = "$id" ]; then
    echo "image $image: $dest already has it"
else
    echo "image $image, as $dest will load it (a few minutes for 2.4 GB)"
    docker save "$image" | zstd | ssh "$dest" 'zstd -d | docker load'
fi
echo "meshes: $(du -sh "$data/meshes" | cut -f1)"
# 23 and 24: some files couldn't be read or vanished while sending, as when a reconstruction here is still writing
# one, or the container wrote it as root; the rest were sent, and the database is still worth sending
rsync -a --info=progress2 "$data/meshes/" "$dest:$remote/data/meshes/" || case $? in
    23|24) echo "warning: not every mesh file could be sent (see above); carrying on" ;;
    *) exit 1 ;;
esac
rsync -a "$snap" "$dest:$remote/data/bouldermesh.sqlite"
echo "done. On $dest:  cd $remote && docker compose up -d"
