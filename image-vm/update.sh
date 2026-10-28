#!/bin/bash
# update.sh — A/B update of a running resident box via systemd-sysupdate. UNTESTED.
#   ./update.sh <host> <version>          (host = ssh target, e.g. owner@resident or -p2222 localhost)
# Flow: stage out/updates/* into /var/lib/resident/updates → `systemd-sysupdate update` writes the
# UKI (+3 tries) and root/verity into the free slot → reboot → systemd-boot prefers the newest UKI →
# resident-health gates boot-complete.target → bless (strip counter) OR 3 failed tries → old slot.
# The human runs this (policy.toml [approval] "sysupdate"); the agent may only stage artifacts.
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"
HOST=${1:?host}; VER=${2:?version}
UPD=out/updates
ls "$UPD"/resident_"$VER"* >/dev/null

echo "# 1/4 stage artifacts"
ssh "$HOST" 'sudo install -d -m 0755 /var/lib/resident/updates'
tar -C "$UPD" -cf - $(cd "$UPD" && ls resident_"$VER"* SHA256SUMS) | ssh "$HOST" 'sudo tar -C /var/lib/resident/updates -xf -'

echo "# 2/4 what sysupdate sees"
ssh "$HOST" 'sudo systemd-sysupdate list'

echo "# 3/4 update (writes free slot + new UKI with +3 tries; current version is ProtectVersion=)"
ssh "$HOST" "sudo RESIDENT_INTENT=\${RESIDENT_INTENT:-} systemd-sysupdate update $VER"
ssh "$HOST" 'sudo bootctl list | sed -n "1,40p"'

echo "# 4/4 reboot into the new slot (rollback is automatic: 3 failed health checks → previous UKI)"
read -r -p "reboot $HOST now? [y/N] " a; [ "$a" = y ] || exit 0
ssh "$HOST" 'sudo systemctl reboot' || true
echo "# after it is back:  ssh $HOST 'bootctl status | grep -iE \"boot count|selected\"; cat /etc/os-release | grep IMAGE_VERSION; systemctl status resident-health systemd-bless-boot'"
echo "# manual rollback:    ssh $HOST 'sudo systemd-sysupdate vacuum; sudo bootctl set-oneshot <old-entry>'   (or let 3 bad boots do it)"
