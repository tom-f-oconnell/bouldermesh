#!/bin/sh
# Pulls a copy of a BoulderMesh server's data to this machine: once the public server harvests by itself, its database
# and meshes exist nowhere else. Run here (hal), not there; nothing connects to this machine:
#     deploy/backup.sh tom@178.105.53.11:     (or a local directory, for a copy of one here)
# The source is the server's checkout (~/bouldermesh), as SSH with the backup key gives it: the public server takes that
# key only for read-only rsync of that folder (rrsync -ro), so the source is host: with nothing after the colon
# (README, "Backups"). BACKUP_KEY names the key (~/.ssh/bouldermesh_backup); BACKUP_SSH replaces the ssh command.
#
# Into BACKUP_DIR (~/bouldermesh-backups):
#   current/          the server's data/ as it is now, and its .env (which has the editing token and the key its saved
#                     credentials are encrypted with): meshes, projects' photos, masks, rejected photos and records,
#                     fetched videos, the visitors' logs, and data/snapshots/, the databases as the server copied them
#                     safely within the last hour. Not its live databases, caches of web pages and models, or work folders.
#   db/<date>/        that night's database snapshots, kept KEEP_DAYS (30)
#   changed/<date>/   whatever that night's copy replaced or deleted in current/, kept KEEP_DAYS too, so a mesh deleted
#                     on the server, or a database gone wrong, can still be had back for a while
set -eu
src=${1:-${BACKUP_SOURCE:?usage: deploy/backup.sh user@host: or a directory}}
dest=${BACKUP_DIR:-$HOME/bouldermesh-backups}
keep=${KEEP_DAYS:-30}
key=${BACKUP_KEY:-$HOME/.ssh/bouldermesh_backup}
day=$(date +%F)
mkdir -p "$dest/current" "$dest/changed" "$dest/db"
chmod 700 "$dest"
case "$src" in
    *:) from=$src ;;            # rrsync's folder itself
    *) from=${src%/}/ ;;        # a folder's contents
esac
# Not through an SSH agent: one holding another key for the same host (as the bouldermesh alias's) offers that first,
# which logs in without the read-only restriction, so "host:" is then the home folder, where there's nothing to copy
ssh=${BACKUP_SSH:-ssh -i $key -o IdentitiesOnly=yes -o IdentityAgent=none -o BatchMode=yes -o ServerAliveInterval=30}
# first match wins: what's left out, then data/ and .env, then nothing else of the checkout
rsync -a --delete --delete-excluded --backup --backup-dir="$dest/changed/$day" --info=stats1 -e "$ssh" --filter='merge -' \
    "$from" "$dest/current/" <<'EOF'
- /data/**.sqlite
- /data/**.sqlite-*
- /data/cache/http/
- /data/cache/models/
- /data/uploads/
- /data/compute/jobs/
- /data/volunteers/
- /data/tunnel/
- /data/projects/*/work/
- *.tmp
+ /.env
+ /data/***
- *
EOF
[ -d "$dest/current/data" ] || { echo "no data/ came from $src: is that the server's checkout?" >&2; exit 1; }
for snap in "$dest/current/data/snapshots/"*.backup; do
    [ -e "$snap" ] || continue
    mkdir -p "$dest/db/$day"
    cp -p "$snap" "$dest/db/$day/"
done
find "$dest/db" "$dest/changed" -mindepth 1 -maxdepth 1 -type d -mtime "+$keep" -exec rm -rf {} +
date +%s > "$dest/last-ok"
echo "backed up $src to $dest/current ($(du -sh "$dest/current" | cut -f1)); databases kept in $dest/db/$day"
