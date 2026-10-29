#!/bin/bash
# stage-rpi4-firmware.sh: put the Raspberry Pi 4 boot firmware and the pftf UEFI firmware where the rpi4
# profile copies them to the ROOT of the ESP. Run before `./build.sh --profile=rpi4` (CI does).
#
#   mkosi.profiles/rpi4/mkosi.extra/efi/  →  /efi in the build root  →  ESP:/   (mkosi.repart/00-esp.conf CopyFiles=/efi:/)
#
# Lands on the ESP: start4.elf fixup4.dat config.txt RPI_EFI.fd bcm2711-rpi-{4-b,400,cm4}.dtb overlays/
# firmware/ (pftf's Wi-Fi blobs, unused by Debian) Readme.md LICENCE.broadcom. mkosi adds
# EFI/BOOT/BOOTAA64.EFI (systemd-boot), loader/loader.conf and EFI/Linux/resident_<ver>+3.efi.
#
# Licences: RPI_EFI.fd is EDK2 (BSD-2-Clause-Patent); start4.elf, fixup4.dat, the .dtb/.dtbo files are
# Raspberry Pi boot files under Broadcom's redistributable licence (LICENCE.broadcom, fetched below
# because the pftf zip does not include it); firmware/brcm/* is under the Cypress licence in firmware/LICENCE.txt.
# The staged tree is NOT in git (tools/public/.gitignore, tools/export.py DENY): only this script is.
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"

# Pinned: https://github.com/pftf/RPi4/releases/tag/v1.53 (2026-08-31, latest stable when pinned).
PFTF_VERSION=v1.53
PFTF_SHA256=ca9973e2a7a546b3df871cfb7382e656829114b6dfa424f40dc67cc90a217d88
PFTF_URL="https://github.com/pftf/RPi4/releases/download/${PFTF_VERSION}/RPi4_UEFI_Firmware_${PFTF_VERSION}.zip"
# Broadcom licence for the Raspberry Pi boot files, pinned to raspberrypi/firmware tag 1.20260915.
LIC_URL=https://raw.githubusercontent.com/raspberrypi/firmware/1.20260915/boot/LICENCE.broadcom
LIC_SHA256=c7283ff51f863d93a275c66e3b4cb08021a5dd4d8c1e7acc47d872fbe52d3d6b

DEST=mkosi.profiles/rpi4/mkosi.extra/efi
for t in curl unzip sha256sum; do command -v "$t" >/dev/null || { echo "$t not found" >&2; exit 1; }; done

TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
curl -fsSL --retry 3 -o "$TMP/fw.zip" "$PFTF_URL"
echo "$PFTF_SHA256  $TMP/fw.zip" | sha256sum -c --quiet - || { echo "checksum mismatch for $PFTF_URL" >&2; exit 1; }
curl -fsSL --retry 3 -o "$TMP/LICENCE.broadcom" "$LIC_URL"
echo "$LIC_SHA256  $TMP/LICENCE.broadcom" | sha256sum -c --quiet - || { echo "checksum mismatch for $LIC_URL" >&2; exit 1; }

mkdir -p "$TMP/x" && unzip -q "$TMP/fw.zip" -d "$TMP/x"
for f in start4.elf fixup4.dat config.txt RPI_EFI.fd bcm2711-rpi-4-b.dtb overlays/miniuart-bt.dtbo overlays/upstream-pi4.dtbo; do
    [ -f "$TMP/x/$f" ] || { echo "pftf $PFTF_VERSION zip lacks $f" >&2; exit 1; }
done
# armstub=RPI_EFI.fd and the PL011-on-GPIO14/15 overlay are what make this boot chain + console=ttyAMA0 work.
# (config.txt has CRLF line endings.)
tr -d '\r' < "$TMP/x/config.txt" > "$TMP/config.lf"
grep -qx 'armstub=RPI_EFI.fd' "$TMP/config.lf" && grep -qx 'dtoverlay=miniuart-bt' "$TMP/config.lf" \
    || { echo "unexpected config.txt in pftf $PFTF_VERSION" >&2; exit 1; }
install -m 0644 "$TMP/LICENCE.broadcom" "$TMP/x/LICENCE.broadcom"

rm -rf "${DEST:?}"
mkdir -p "$DEST"
cp -r "$TMP/x/." "$DEST/"
chmod -R u=rwX,go=rX "$DEST"
echo "$PFTF_VERSION" > "$DEST/RPI_EFI.version"
echo "staged pftf RPi4 UEFI $PFTF_VERSION into $DEST (→ ESP root):"
(cd "$DEST" && find . -type f | sort | sed 's|^\./|  |')
