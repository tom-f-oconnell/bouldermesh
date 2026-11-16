#!/bin/bash
# Back up the public server now, as the nightly timer does (bouldermesh-backup.service), and show what it's doing until
# it's done, rather than waiting in silence as `systemctl start` does: it starts the service without waiting, follows its
# log, and stops following once it has finished, saying how it went. Ctrl-C stops only the following: the backup goes on.
#
#     deploy/backup-now.sh        (asks for sudo's password, to start the service)
set -u
unit=bouldermesh-backup.service
state() { systemctl show -p ActiveState --value "$unit"; }
if [ "$(state)" = activating ]; then
    echo "a backup is running already: following it"
else
    sudo systemctl start --no-block "$unit" || exit 1
fi
since=$(systemctl show -p ExecMainStartTimestamp --value "$unit")
journalctl -u "$unit" -f -n 50 -o cat ${since:+--since "$since"} &
follow=$!
trap 'kill $follow 2>/dev/null; echo; echo "stopped following: the backup goes on (journalctl -fu $unit)"; exit 130' INT HUP TERM
sleep 2
while [ "$(state)" = activating ]; do     # a oneshot service: activating while it runs, then inactive (or failed)
    sleep 5
done
sleep 2      # its last lines
kill "$follow" 2>/dev/null
result=$(systemctl show -p Result --value "$unit")
if [ "$result" = success ]; then echo "backup done"; else echo "backup FAILED ($result): journalctl -u $unit -n 50"; exit 1; fi
