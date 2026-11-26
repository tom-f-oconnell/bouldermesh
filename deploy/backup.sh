#!/bin/sh
# Pulls a copy of a BoulderMesh server's data to this machine: once the public server harvests by itself, its database
# and meshes exist nowhere else. Run here (hal), not there; nothing connects to this machine:
#     deploy/backup.sh tom@178.105.53.11:     (or a local directory, for a copy of one here)
# Or on the server itself, into a Hetzner Storage Box mounted over sshfs (bouldermesh-storagebox.service): the source is
# then the checkout's folder, BACKUP_MOUNT the mount, which must be there (else this copies nothing), and BACKUP_HANDSHAKE
# runs the handshake there without SSH (deploy/backup-storagebox.conf). A second copy (another machine, as above) runs
# without the handshake key: only one copy may tell the server what it holds.
# The source is the server's checkout (~/bouldermesh), as SSH with the backup key gives it: the public server takes that
# key only for read-only rsync of that folder (rrsync -ro), so the source is host: with nothing after the colon
# (README, "Backups"). BACKUP_KEY names the key (~/.ssh/bouldermesh_backup); BACKUP_SSH replaces the ssh command.
#
# Offloading (backend/offload.py): the server makes room by deleting what this backup holds. What it has marked to go,
# or has deleted (data/offload.list), this copy keeps whatever the server's copy does. After a good copy, a second key,
# the handshake's (BACKUP_HANDSHAKE_KEY, ~/.ssh/bouldermesh_handshake), tells the server when the copy began, so that
# what was marked before then may go, and sends back what the server wants back (deploy/backup-handshake.py;
# BACKUP_HANDSHAKE replaces its command). It then sends a manifest of what it holds of everything the server offloaded or
# marked: each file's size and time, and the SHA-1s of the few the server asks for each night; the server deletes nothing
# until a manifest shows this copy holds it, and finds from them what this copy has lost. Without that key, nothing is
# offloaded. --restore-only only sends back what's wanted, which a more frequent timer runs
# (deploy/bouldermesh-restore.timer).
#
# Into BACKUP_DIR (~/bouldermesh-backups):
#   current/          the server's data/ as it is now, and its .env (which has the editing token and the key its saved
#                     credentials are encrypted with): meshes, projects' photos, masks, rejected photos and records,
#                     fetched videos, the visitors' logs, and data/snapshots/, the databases as the server copied them
#                     safely within the last hour (the filter's cache, relevance.sqlite, too); and what the server
#                     offloaded; and its web cache (the photos it downloaded, kept for good, and searches and pages),
#                     so that the sites needn't be asked again; and debug/, the owner's phone's debug files (capture
#                     traces, line-up tests, live hold finding). Not its live databases, models, or work folders.
#   db/<date>/        that night's database snapshots, kept KEEP_DB_DAYS (365)
#   archive/          kept for good, in the server's layout: what the server deleted or replaced that can't easily be had
#                     again (fetched videos, projects' photos, masks and rejected photos, meshes deleted, and the big files
#                     of meshes replaced), moved out of that night's changed/ (deploy/backup-archive.py)
#   changed/<date>/   the rest of what that night's copy replaced or deleted in current/ (records rewritten, web cache
#                     pages), kept KEEP_DAYS (30)
# Each night's report says how much room is left on this disk (on BACKUP_HOST, or this machine's name); under
# WARN_FREE_GB (200) the server tells your phone, and under URGENT_FREE_GB (50), when the copies are about to fail for
# want of room, urgently.
set -eu
only_restore=
if [ "${1:-}" = --restore-only ]; then
    only_restore=1
    shift
fi
src=${1:-${BACKUP_SOURCE:?usage: deploy/backup.sh [--restore-only] user@host: or a directory}}
dest=${BACKUP_DIR:-$HOME/bouldermesh-backups}
keep=${KEEP_DAYS:-30}
keep_db=${KEEP_DB_DAYS:-365}
warn_free=$(( ${WARN_FREE_GB:-200} * 1073741824 ))
urgent_free=$(( ${URGENT_FREE_GB:-50} * 1073741824 ))
key=${BACKUP_KEY:-$HOME/.ssh/bouldermesh_backup}
hkey=${BACKUP_HANDSHAKE_KEY:-$HOME/.ssh/bouldermesh_handshake}
day=$(date +%F)
if [ -n "${BACKUP_MOUNT:-}" ] && ! mountpoint -q "$BACKUP_MOUNT"; then
    # and no report: its room left would be the server's own disk's
    echo "$BACKUP_MOUNT isn't mounted (the Storage Box, bouldermesh-storagebox.service): nothing copied" >&2
    exit 1
fi
mkdir -p "$dest/current" "$dest/changed" "$dest/db" "$dest/archive"
chmod 700 "$dest"
case "$src" in
    *:) from=$src ;;            # rrsync's folder itself
    *) from=${src%/}/ ;;        # a folder's contents
esac
# Not through an SSH agent: one holding another key for the same host (as the bouldermesh alias's) offers that first,
# which logs in without the read-only restriction, so "host:" is then the home folder, where there's nothing to copy.
# Nor through a connection already open to it (ControlMaster in ~/.ssh/config): one deploy.sh opened with that key is
# shared for hours, and the night's copy went through it on Nov 12, 2026, found the home folder, and moved all of
# current/ aside into changed/
nomux="-o ControlMaster=no -o ControlPath=none"
ssh=${BACKUP_SSH:-ssh -i $key -o IdentitiesOnly=yes -o IdentityAgent=none -o BatchMode=yes $nomux -o ServerAliveInterval=30}
handshake=${BACKUP_HANDSHAKE:-}
if [ -z "$handshake" ] && [ -f "$hkey" ]; then
    case "$src" in *:) handshake="ssh -i $hkey -o IdentitiesOnly=yes -o IdentityAgent=none -o BatchMode=yes $nomux ${src%:}" ;; esac
fi
tmp=$(mktemp -d)
started= why= done_ok=
# how the night's copy went, through the handshake, for the admin page (backup-handshake.py's report): sizes of what's
# kept here by night, and on a failure why
report() {
    [ -n "$handshake" ] || return 0
    kept=$(for d in $(ls "$dest/db" "$dest/changed" 2>/dev/null | grep -E '^[0-9]{4}-[0-9]{2}-[0-9]{2}$' | sort -u); do
        printf '{"day":"%s","db":%s,"changed":%s},' "$d" "$(du -sb "$dest/db/$d" 2>/dev/null | cut -f1 || echo 0)" \
            "$(du -sb "$dest/changed/$d" 2>/dev/null | cut -f1 || echo 0)"
    done)
    printf '{"status":"%s","began":%s,"ended":%s,"bytes":%s,"why":"%s","host":"%s","kept":[%s],"archive":%s,"disk_free":%s,"disk_warn":%s,"disk_urgent":%s}' \
        "$1" "${started:-null}" "$(date +%s)" "$(du -sb "$dest/current" 2>/dev/null | cut -f1 || echo 0)" "$(printf '%s' "$2" | tr -d '"\\\n')" \
        "${BACKUP_HOST:-$(hostname)}" "${kept%,}" "$(du -sb "$dest/archive" 2>/dev/null | cut -f1 || echo 0)" \
        "$(df -B1 --output=avail "$dest" 2>/dev/null | tail -1 | tr -d ' ' || echo null)" "$warn_free" "$urgent_free" \
        | $handshake report > /dev/null 2>&1 || echo "couldn't tell the server how the copy went" >&2
}
fail() { why=$1; echo "$1" >&2; exit 1; }
trap 'st=$?; if [ -z "$only_restore" ] && [ -z "$done_ok" ]; then report failed "${why:-it stopped with an error (exit $st)}"; fi; rm -rf "$tmp"' EXIT
# the paths the server offloads, as backend/offload.py's PATH
valid='^(projects/[0-9a-f]{6,32}/(photos|masks|rejected)|cache/videos/[A-Za-z0-9_-]{11}\.[a-z0-9]{2,5}|meshes/[0-9a-f]{6,32}/[A-Za-z0-9_.-]{1,120})$'

send_back() {   # what the server wants back, from this copy (or, for videos it deleted before, archive/), through the handshake
    [ -n "$handshake" ] || return 0
    send_archived
    $handshake wanted | grep -E "$valid" > "$tmp/wanted" || true
    : > "$tmp/have"
    : > "$tmp/have-archived"
    while read -r p; do
        if [ -e "$dest/current/data/$p" ]; then echo "$p" >> "$tmp/have"
        elif [ -f "$dest/archive/data/$p" ]; then echo "$p" >> "$tmp/have-archived"; fi
    done < "$tmp/wanted"
    if [ -s "$tmp/have" ]; then tar -C "$dest/current/data" -cf - -T "$tmp/have" | $handshake restore; fi
    if [ -s "$tmp/have-archived" ]; then tar -C "$dest/archive/data" -cf - -T "$tmp/have-archived" | $handshake restore; fi
}

send_archived() {   # the fetched videos archive/ keeps (deleted on the server before it kept them all), for it to ask back
    [ -d "$dest/archive/data/cache/videos" ] || return 0
    (cd "$dest/archive/data" && find cache/videos -maxdepth 1 -type f -printf '%s\t%T@\t%p\n') \
        | $handshake archived > /dev/null || echo "couldn't tell the server what's archived" >&2
}

send_manifest() {   # what this copy holds of what the server offloaded or marked (backend/offload.py, manifest_of())
    [ -n "$handshake" ] || return 0
    [ -f "$tmp/offload.list" ] || : > "$tmp/offload.list"     # nothing offloaded: a manifest of nothing, saying it's here
    $handshake sample | grep -E '^(projects/[0-9a-f]{6,32}/(photos|masks|rejected)/|cache/videos/|meshes/[0-9a-f]{6,32}/)' \
        | grep -v '\.\.' > "$tmp/sample" || true
    {
        echo "began $started"
        grep -E "$valid" "$tmp/offload.list" | while read -r p; do
            [ -e "$dest/current/data/$p" ] || continue
            (cd "$dest/current/data" && find "$p" -type f -printf 'f\t%s\t%T@\t%p\n')
        done > "$tmp/files"
        cat "$tmp/files"
        while read -r f; do
            if [ -f "$dest/current/data/$f" ]; then
                printf 'h\t%s\t%s\n' "$(sha1sum < "$dest/current/data/$f" | cut -d' ' -f1)" "$f"
            fi
        done < "$tmp/sample"
        echo "end $(wc -l < "$tmp/files" | tr -d ' ')"
    } > "$tmp/manifest"
    $handshake manifest < "$tmp/manifest" > /dev/null || echo "couldn't send the server its manifest" >&2
}

if [ -n "$only_restore" ]; then
    send_back
    # the admin page's Back up now: asked through the handshake, answered here at once with a whole copy
    if [ -n "$handshake" ] && when=$($handshake asked 2>/dev/null) && [ -n "$when" ]; then
        echo "the server asked for a backup at $(date -d "@$when" '+%F %H:%M'): backing up now"
        rm -rf "$tmp"
        trap - EXIT
        exec sh "$0" "$src"
    fi
    exit 0
fi
# one whole copy at a time: the night's, and one the server asked for, don't run together
exec 9> "$dest/.backup.lock"
if ! flock -n 9; then
    echo "a backup is running already: leaving it to finish"
    done_ok=1
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
# then data/, debug/ (the owner's phone's debug files, on the public server's system disk) and .env, then nothing else of the checkout
cat "$tmp/protect" - > "$tmp/filter" <<'RULES'
- /data/**.sqlite
- /data/**.sqlite-*
- /data/cache/models/
- /data/cache/terrain/
- /data/cache/terrarium/
- /data/uploads/
- /data/compute/jobs/
- /data/compute/bm-*/
- /data/volunteers/
- /data/tunnel/
- /data/projects/*/work/
- /data/meshes/**/packed/
- /data/meshes/**/smaller/
- /data/restore-inbox/
- *.tmp
+ /.env
+ /data/***
+ /debug/***
- *
RULES
# data/ there first, before anything here is deleted to match: a copy of the wrong folder would move all of current/ aside
rsync -e "$ssh" --list-only "${from}data/" > /dev/null 2>&1 || fail "no data/ at $src: is that the server's checkout? nothing copied"
# The server is in use while this copies, so files can go between rsync listing them and copying them (exit 24): one
# replaced by a newer version (written under another name, then renamed over it), a photo moved to rejected/, a mesh
# deleted. rsync then leaves this copy's file as it was, so nothing is lost, but it's a night behind. So it goes once
# more straight away, which takes what those became; what still went, of what's kept (meshes, photos, masks, videos),
# is named in the night's report, for the admin page. Any other error stops the night.
copy() {
    rc=0
    # how it's going, every 2 minutes, for whoever follows it (deploy/backup-now.sh, or journalctl -fu bouldermesh-backup)
    # (its sleep stopped with it: one left behind would hold the output open for whoever reads it to its end)
    ( t0=$(date +%s); trap 'kill "$s" 2>/dev/null; exit 0' TERM
      while :; do
        sleep 120 & s=$!; wait "$s"
        echo "still copying: $(( ($(date +%s) - t0) / 60 )) min so far; this copy now $(du -sh "$dest/current" 2>/dev/null | cut -f1)"
      done ) &
    ticker=$!
    rsync -a --delete --delete-excluded --backup --backup-dir="$dest/changed/$day" --info=stats1 -e "$ssh" \
        --filter="merge $tmp/filter" "$from" "$dest/current/" 2> "$tmp/copy.err" || rc=$?
    kill "$ticker" 2>/dev/null || true
    cat "$tmp/copy.err" >&2
}
echo "copying $src into $dest/current: only what changed since the last copy (often 10-30 min)"
copy
if [ "$rc" = 24 ]; then
    echo "some files went on the server while they were copied: copying again, for what they became" >&2
    copy
fi
gone=
case "$rc" in
    0) ;;
    24) gone=$(sed -n 's/^file has vanished: "\(.*\)"$/\1/p' "$tmp/copy.err" | sed 's|^.*/data/|data/|' \
                   | grep -E '^data/(meshes/|projects/[^/]+/(photos|masks|rejected)/|cache/videos/)' | sort -u || true) ;;
    *) fail "the copy stopped (rsync exit $rc): $(grep -v '^ *$' "$tmp/copy.err" | tail -1)" ;;
esac
note=
if [ -n "$gone" ]; then
    n=$(printf '%s\n' "$gone" | wc -l | tr -d ' ')
    note="$n kept file(s) went on the server while they were copied, twice: kept here as the night before had them; the next night's copy follows them: $(printf '%s\n' "$gone" | head -5 | tr '\n' ' ')"
    echo "$note" >&2
fi
[ -d "$dest/current/data" ] || fail "no data/ came from $src: is that the server's checkout?"
for snap in "$dest/current/data/snapshots/"*.backup; do
    [ -e "$snap" ] || continue
    case "$snap" in */relevance.sqlite.backup) continue ;; esac     # a cache: only the latest, in current/
    mkdir -p "$dest/db/$day"
    cp -p "$snap" "$dest/db/$day/"
done
# what can't easily be had again, out of tonight's changed/ into archive/, kept for good; then the rest by age
echo "copied; keeping for good what the server deleted that can't easily be had again (archive/)"
python3 "$(dirname "$0")/backup-archive.py" "$dest" "$day" || echo "couldn't archive tonight's set-aside files: they stay in changed/$day" >&2
find "$dest/changed" -mindepth 1 -maxdepth 1 -type d -mtime "+$keep" -exec rm -rf {} +
find "$dest/db" -mindepth 1 -maxdepth 1 -type d -mtime "+$keep_db" -exec rm -rf {} +
date +%s > "$dest/last-ok"
done_ok=1
echo "backed up $src to $dest/current ($(du -sh "$dest/current" | cut -f1)); databases kept in $dest/db/$day"
if [ -n "$handshake" ]; then    # the server may now delete what it marked before this copy began, and gets back what it wants
    $handshake backed-up "$started"
    send_manifest
    report ok "$note"
    send_back
else
    echo "no handshake key ($hkey): the server offloads nothing (README, \"Backups\")"
fi
