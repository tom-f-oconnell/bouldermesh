#!/bin/sh
# Pulls a copy of a BoulderMesh server's data to this machine: once the public server harvests by itself, its database
# and meshes exist nowhere else. Run here (hal), not there; nothing connects to this machine:
#     deploy/backup.sh tom@178.105.53.11:     (or a local directory, for a copy of one here)
# The source is the server's checkout (~/bouldermesh), as SSH with the backup key gives it: the public server takes that
# key only for read-only rsync of that folder (rrsync -ro), so the source is host: with nothing after the colon
# (README, "Backups"). BACKUP_KEY names the key (~/.ssh/bouldermesh_backup); BACKUP_SSH replaces the ssh command.
#
# Offloading (backend/offload.py): the server makes room by deleting what this backup holds. What it has marked to go,
# or has deleted (data/offload.list), this copy keeps whatever the server's copy does. After a good copy, a second key,
# the handshake's (BACKUP_HANDSHAKE_KEY, ~/.ssh/bouldermesh_handshake), tells the server when the copy began, so that
# what was marked before then may go, and sends back what the server wants back (deploy/backup-handshake.py;
# BACKUP_HANDSHAKE replaces its command). Without that key, nothing is offloaded. --restore-only only sends back what's
# wanted, which a more frequent timer runs (deploy/bouldermesh-restore.timer).
#
# Into BACKUP_DIR (~/bouldermesh-backups):
#   current/          the server's data/ as it is now, and its .env (which has the editing token and the key its saved
#                     credentials are encrypted with): meshes, projects' photos, masks, rejected photos and records,
#                     fetched videos, the visitors' logs, and data/snapshots/, the databases as the server copied them
#                     safely within the last hour; and what the server offloaded. Not its live databases, caches of web
#                     pages and models, or work folders.
#   db/<date>/        that night's database snapshots, kept KEEP_DAYS (30)
#   changed/<date>/   whatever that night's copy replaced or deleted in current/, kept KEEP_DAYS too, so a mesh deleted
#                     on the server, or a database gone wrong, can still be had back for a while
set -eu
only_restore=
if [ "${1:-}" = --restore-only ]; then
    only_restore=1
    shift
fi
src=${1:-${BACKUP_SOURCE:?usage: deploy/backup.sh [--restore-only] user@host: or a directory}}
dest=${BACKUP_DIR:-$HOME/bouldermesh-backups}
keep=${KEEP_DAYS:-30}
key=${BACKUP_KEY:-$HOME/.ssh/bouldermesh_backup}
hkey=${BACKUP_HANDSHAKE_KEY:-$HOME/.ssh/bouldermesh_handshake}
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
handshake=${BACKUP_HANDSHAKE:-}
if [ -z "$handshake" ] && [ -f "$hkey" ]; then
    case "$src" in *:) handshake="ssh -i $hkey -o IdentitiesOnly=yes -o IdentityAgent=none -o BatchMode=yes ${src%:}" ;; esac
fi
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
# the paths the server offloads, as backend/offload.py's PATH
valid='^(projects/[0-9a-f]{6,32}/(photos|masks|rejected)|cache/videos/[A-Za-z0-9_-]{11}\.[a-z0-9]{2,5}|meshes/[0-9a-f]{6,32}/[A-Za-z0-9_.-]{1,120})$'

send_back() {   # what the server wants back, from this copy, through the handshake
    [ -n "$handshake" ] || return 0
    $handshake wanted | grep -E "$valid" > "$tmp/wanted" || true
    : > "$tmp/have"
    while read -r p; do
        if [ -e "$dest/current/data/$p" ]; then echo "$p" >> "$tmp/have"; fi
    done < "$tmp/wanted"
    [ -s "$tmp/have" ] || return 0
    tar -C "$dest/current/data" -cf - -T "$tmp/have" | $handshake restore
}

if [ -n "$only_restore" ]; then
    send_back
    exit 0
fi
started=$(date +%s)
# what the server has offloaded or marked to: kept here, whatever the server's copy does (rsync's protect rules)
: > "$tmp/protect"
if rsync -a -e "$ssh" "${from}data/offload.list" "$tmp/offload.list" 2>/dev/null; then
    grep -E "$valid" "$tmp/offload.list" | while read -r p; do
        printf 'P /data/%s\nP /data/%s/***\n' "$p" "$p"
    done > "$tmp/protect" || true
fi
# first match wins: what's kept, what's left out (with copies of meshes and textures the server makes again by itself),
# then data/ and .env, then nothing else of the checkout
cat "$tmp/protect" - > "$tmp/filter" <<'RULES'
- /data/**.sqlite
- /data/**.sqlite-*
- /data/cache/http/
- /data/cache/models/
- /data/cache/terrain/
- /data/uploads/
- /data/compute/jobs/
- /data/volunteers/
- /data/tunnel/
- /data/projects/*/work/
- /data/meshes/**/packed/
- /data/meshes/**/smaller/
- /data/restore-inbox/
- *.tmp
+ /.env
+ /data/***
- *
RULES
rsync -a --delete --delete-excluded --backup --backup-dir="$dest/changed/$day" --info=stats1 -e "$ssh" \
    --filter="merge $tmp/filter" "$from" "$dest/current/"
[ -d "$dest/current/data" ] || { echo "no data/ came from $src: is that the server's checkout?" >&2; exit 1; }
for snap in "$dest/current/data/snapshots/"*.backup; do
    [ -e "$snap" ] || continue
    mkdir -p "$dest/db/$day"
    cp -p "$snap" "$dest/db/$day/"
done
find "$dest/db" "$dest/changed" -mindepth 1 -maxdepth 1 -type d -mtime "+$keep" -exec rm -rf {} +
date +%s > "$dest/last-ok"
echo "backed up $src to $dest/current ($(du -sh "$dest/current" | cut -f1)); databases kept in $dest/db/$day"
if [ -n "$handshake" ]; then    # the server may now delete what it marked before this copy began, and gets back what it wants
    $handshake backed-up "$started"
    send_back
else
    echo "no handshake key ($hkey): the server offloads nothing (README, \"Backups\")"
fi
