#!/bin/sh
# Installs BoulderMesh as a systemd service running its Docker Compose stack: it starts at boot,
# restarts if it fails, and logs to the journal. Run with sudo. It doesn't start the service.
set -eu
dir=$(cd "$(dirname "$0")/.." && pwd)
user=${SUDO_USER:-$(id -un)}
if [ "$(id -u)" -ne 0 ]; then
    echo "Run it with sudo: sudo $0" >&2
    exit 1
fi
if ! id -nG "$user" | tr ' ' '\n' | grep -qx docker; then
    echo "$user isn't in the docker group, which the service runs as. Add them with: sudo usermod -aG docker $user" >&2
    exit 1
fi
sed -e "s|@DIR@|$dir|g" -e "s|@USER@|$user|g" "$dir/deploy/bouldermesh.service" > /etc/systemd/system/bouldermesh.service
systemctl daemon-reload
echo "Installed /etc/systemd/system/bouldermesh.service, running $dir as $user."
echo "Start it now and at every boot:  sudo systemctl enable --now bouldermesh"
echo "Its log:                         journalctl -u bouldermesh -f"
