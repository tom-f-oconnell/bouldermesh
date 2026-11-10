#!/bin/sh
# Ships what the public setup needs to the machine that will run it: the image built here, a snapshot of the
# database with every area stopped (nothing runs by itself there), and the meshes. Run here, where the image was built:
#     deploy/ship.sh you@boulders.example.com [image]
# That machine needs Docker with you in the docker group, zstd, python3, and this repository cloned at ~/bouldermesh
# (REMOTE_DIR to change) with ./data writable by you. The image is the one Compose built here unless named, and
# BM_IMAGE in the machine's .env must name it if it isn't bouldermesh-bouldermesh:latest. An image from deploy/build.sh
# is pushed to its registry first, and as :latest, so that remote machines can pull the build the server runs
# (NO_PUSH=1 skips that). The database there is replaced each time, all but its settings (the admin page's), so holds
# marked or cleanups edited there with the editing token are lost: do those here.
# Running it again is cheap: an image the machine already has isn't sent again, and only changed meshes are.
set -eu
dest=${1:?usage: deploy/ship.sh user@host [image]}
image=${2:-bouldermesh-bouldermesh:latest}
dir=$(cd "$(dirname "$0")/.." && pwd)
data=${DATA_DIR:-$dir/data}
remote=${REMOTE_DIR:-bouldermesh}
snap=$(mktemp --suffix .sqlite)
kept=$(mktemp --suffix .json)
cfg=
trap 'rm -rf "$snap" "$kept" ${cfg:+"$cfg"}' EXIT

# the name deploy/build.sh gave the image, which remote machines pull: pushed before the server gets the image, so that
# it never runs a build they can't have
pushed=$(docker image inspect "$image" --format '{{range .Config.Env}}{{println .}}{{end}}' | sed -n 's/^BOULDERMESH_IMAGE=//p')
if [ -z "$pushed" ]; then
    echo "image $image: not from deploy/build.sh, so not pushed (remote machines run the image the admin page names)"
elif [ -n "${NO_PUSH:-}" ]; then
    echo "image $image: not pushed (NO_PUSH), so remote machines can only pull $pushed if it was pushed before"
else
    latest=${pushed%:*}:latest
    docker tag "$image" "$pushed"
    docker tag "$image" "$latest"
    if [ "${pushed%%/*}" = ghcr.io ] && gh auth token > /dev/null 2>&1; then     # logged in with gh's token, for this only
        cfg=$(mktemp -d)
        gh auth token | DOCKER_CONFIG=$cfg docker login ghcr.io -u "$(gh api user --jq .login)" --password-stdin > /dev/null 2>&1 ||
            { echo "couldn't log in to ghcr.io with gh's token: it needs write:packages (gh auth refresh -s write:packages)" >&2; exit 1; }
    fi
    echo "image $image: pushing it as $pushed and $latest"
    DOCKER_CONFIG=${cfg:-${DOCKER_CONFIG:-$HOME/.docker}} docker push -q "$pushed"
    DOCKER_CONFIG=${cfg:-${DOCKER_CONFIG:-$HOME/.docker}} docker push -q "$latest"
fi

# the settings there, which the copy keeps in place of these
ssh "$dest" "python3 - $remote/data/bouldermesh.sqlite" > "$kept" <<'PY'
import json
import sqlite3
import sys
try:
    rows = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True).execute("SELECT key, value FROM settings").fetchall()
except sqlite3.Error:   # no database there yet
    rows = []
print(json.dumps(rows))
PY
python3 - "$data/bouldermesh.sqlite" "$snap" "$kept" <<'PY'
import json
import sqlite3
import sys
src = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
dst = sqlite3.connect(sys.argv[2])
src.backup(dst)
stopped = dst.execute("UPDATE campaigns SET status='stopped' WHERE status IN ('running', 'stopping')").rowcount
kept = json.load(open(sys.argv[3]))
dst.execute("CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT)")
dst.execute("DELETE FROM settings")
dst.executemany("INSERT INTO settings(key, value) VALUES (?, ?)", kept)
dst.commit()
n = lambda table: dst.execute(f"SELECT count(*) FROM {table}").fetchone()[0]  # noqa: E731
print(f"database: {n('meshes')} meshes, {n('climbs')} climbs, {n('projects')} projects, {stopped} running area(s) stopped "
      f"in the copy, and {len(kept)} setting(s) of the machine's own kept")
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
