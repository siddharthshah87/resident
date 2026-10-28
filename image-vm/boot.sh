#!/bin/bash
# boot.sh — boot the built image in qemu (UEFI/OVMF, KVM, serial console). UNTESTED.
#   ./boot.sh [out/resident_<ver>.raw]        ssh: ssh -p 2222 owner@localhost
# Alternative with the same config: `mkosi vm` (mkosi >= 25) / `mkosi qemu` (v20).
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"
IMG=${1:-$(ls -t out/resident*.raw 2>/dev/null | head -1)}
[ -f "${IMG:-}" ] || { echo "no image; run ./build.sh" >&2; exit 1; }
OVMF=${OVMF:-/usr/share/ovmf/OVMF.fd}
[ -f "$OVMF" ] || OVMF=/usr/share/OVMF/OVMF_CODE_4M.fd
KVM=; [ -w /dev/kvm ] && KVM=-enable-kvm || echo "# /dev/kvm not usable (sudo usermod -aG kvm $USER); running without KVM" >&2

# A scratch copy keeps the pristine build; the VM's /var, /etc overlay and update slots land in the copy.
RUN=${RUN_IMG:-out/run.raw}
[ -f "$RUN" ] || cp --reflink=auto "$IMG" "$RUN"
# grow to 16G so repart can expand /var on first boot
truncate -s 16G "$RUN"

exec qemu-system-x86_64 $KVM -cpu host -smp 2 -m 4G \
  -bios "$OVMF" \
  -drive "file=$RUN,format=raw,if=virtio" \
  -netdev user,id=n0,hostfwd=tcp::2222-:22 -device virtio-net-pci,netdev=n0 \
  -device virtio-rng-pci \
  -nographic -serial mon:stdio
