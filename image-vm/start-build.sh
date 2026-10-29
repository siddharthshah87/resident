#!/bin/bash
# Start the image build as a background system job, so closing the terminal can't stop it.
# Usage: sudo ~/resident/image-vm/start-build.sh        Watch: journalctl -fu resident-build  (or tail -f build.log)
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "run it with sudo:  sudo $0" >&2; exit 1; }
DIR=~/resident/image-vm
if systemctl is-active --quiet resident-build; then
  echo "a build is already running (since $(systemctl show resident-build -p ActiveEnterTimestamp --value))."
  echo "watch it with: journalctl -fu resident-build   or   tail -f $DIR/build.log"
  exit 0
fi
systemctl reset-failed resident-build 2>/dev/null || true
systemd-run --unit=resident-build --working-directory="$DIR" \
  --setenv=PATH=~/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
  --setenv=RESIDENT_SSH_PUBKEY=~/.ssh/id_ed25519.pub \
  -p StandardOutput=truncate:"$DIR/build.log" -p StandardError=inherit \
  "$DIR/build.sh" -f
echo "build started in the background — you can close this terminal."
