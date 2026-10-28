#!/bin/bash
# resident-etc-overlay: make /etc persistently writable on a dm-verity root.
# lower = /etc from the sealed image (factory), upper/work = /var/lib/resident/etc-overlay.
# Consequences: `resident` tracks /etc hashes as before; the image's /etc is the "factory reset"
# (rm -rf the upper dir as root → next boot is pristine); an A/B update changes the lower only,
# so local edits survive updates but may shadow updated defaults (intent log tells you which).
set -euo pipefail
UP=/var/lib/resident/etc-overlay
mkdir -p "$UP/upper" "$UP/work"
chmod 0700 "$UP"

# systemd gave us a transient machine-id (bind-mounted over the empty /etc/machine-id of the ro root).
# Capture it before the overlay hides that bind mount, so the identity survives reboots.
MID=$(cat /etc/machine-id 2>/dev/null || true)

mount -t overlay overlay -o "lowerdir=/etc,upperdir=$UP/upper,workdir=$UP/work,index=off,metacopy=off,xino=off" /etc

if [ -n "$MID" ] && [ "$MID" != "uninitialized" ] && [ ! -s /etc/machine-id ]; then
    printf '%s\n' "$MID" > /etc/machine-id
fi
echo "resident: /etc overlay mounted (upper=$UP/upper)"
