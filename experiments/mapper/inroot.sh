#!/usr/bin/env bash
# Runs a command as root inside a tree build.sh made, without Docker: inroot.sh <tree dir> <command...>
# For installing packages into a tree or building in one while Docker can't start containers. Bubblewrap maps this
# user to root, so the tree's files must be this user's (a `cp -a` of a tree Docker made is). /bench is this folder.
set -euo pipefail
T=$(realpath "$1"); shift
HERE=$(cd "$(dirname "$0")" && pwd)
exec bwrap --unshare-user --uid 0 --gid 0 --die-with-parent --bind "$T/usr" /usr --symlink usr/lib /lib \
  --symlink usr/lib64 /lib64 --symlink usr/bin /bin --symlink usr/sbin /sbin --bind "$T/etc" /etc --bind "$T/var" /var \
  --bind "$T/opt" /opt --ro-bind "$(realpath /etc/resolv.conf)" /etc/resolv.conf --proc /proc --dev /dev --tmpfs /tmp \
  --tmpfs /run --ro-bind "$HERE" /bench --chdir /opt --setenv DEBIAN_FRONTEND noninteractive --setenv HOME /root \
  --setenv PATH /usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/usr/local/cuda/bin "$@"
