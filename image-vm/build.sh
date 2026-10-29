#!/bin/bash
# build.sh — build the resident disk image with mkosi. UNTESTED (written without sudo/mkosi/qemu).
#
# One-time host prep (Ubuntu 24.04, needs sudo):
#   sudo apt install -y mkosi systemd-boot qemu-system-x86 systemd-repart ovmf erofs-utils systemd-ukify pipx
#   sudo usermod -aG kvm owner        # then log out/in (or `newgrp kvm`) so /dev/kvm is usable
#   pipx install git+https://github.com/systemd/mkosi.git   # apt's mkosi 20.2 predates [Build]/[Runtime]/SplitArtifacts=uki,partitions; this config targets v25+
#
# Then:   ./build.sh [--profile=desktop[,docker]] [extra mkosi args]
# Output: out/resident_<ver>.raw (disk), out/resident_<ver>.efi (UKI), out/*.root-<arch>*.raw (slot images;
#         <arch> = x86-64 by default, arm64 with --profile=rpi4: run ./stage-rpi4-firmware.sh first)
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"
RESIDENT_SRC=${RESIDENT_SRC:-$(readlink -f ..)}
VERSION=${RESIDENT_VERSION:-$(date -u +%Y.%m.%d).${RESIDENT_BUILD_N:-1}}

echo "# resident image build $VERSION"
echo "# host prep if not done:  sudo apt install -y mkosi systemd-boot qemu-system-x86 systemd-repart ovmf erofs-utils systemd-ukify pipx && sudo usermod -aG kvm $USER"

command -v mkosi >/dev/null || { echo "mkosi not found (pipx install git+https://github.com/systemd/mkosi.git)" >&2; exit 1; }

# --- permissions: files from a home checkout are often group-writable (umask 002). In an OS image that is a
# security bug, and sshd's StrictModes refuses keys under such directories. Normalise before every build.
chmod -R go-w mkosi.extra
for x in mkosi.profiles/*/mkosi.extra; do [ -d "$x" ] && chmod -R go-w "$x"; done

# --- stage: copy the components that must ship in the image into mkosi.extra -------------------
# (kept out of git-style tracking of this dir: they are copies of ../lib, ../agentd, ../percept, ../browser)
L=mkosi.extra/usr/lib/resident
install -D -m 0755 "$RESIDENT_SRC/lib/agentos.py"          "$L/agentos.py"
install -D -m 0755 "$RESIDENT_SRC/agentd/agentd.py"        "$L/agentd/agentd.py"
install -D -m 0755 "$RESIDENT_SRC/percept/percept.py"      "$L/percept/percept.py"
install -D -m 0755 "$RESIDENT_SRC/browser/browse.mjs"      "$L/browser/browse.mjs"
install -D -m 0755 "$RESIDENT_SRC/browser/browser-mcp.py"  "$L/browser/browser-mcp.py"
install -D -m 0644 intents.json                           mkosi.extra/usr/share/resident/intents.json
install -D -m 0644 README.md                              mkosi.extra/usr/share/doc/resident/README.md
install -D -m 0644 docs/portal-consent.md                 mkosi.extra/usr/share/doc/resident/portal-consent.md
for c in agentd percept browser; do
    [ -f "$RESIDENT_SRC/$c/README.md" ] && install -D -m 0644 "$RESIDENT_SRC/$c/README.md" "mkosi.extra/usr/share/doc/resident/$c.md"
done
# resident-browse wrapper for the image (the NUC one hardcodes nvm paths)
cat > "$L/browser/resident-browse" <<'SH'
#!/bin/sh
exec /usr/bin/node /usr/lib/resident/browser/browse.mjs "$@"
SH
chmod 0755 "$L/browser/resident-browse"
ln -sfn ../lib/resident/browser/resident-browse mkosi.extra/usr/bin/resident-browse
# image version into intents.json so `why` can cite which image seeded the log
python3 - "$VERSION" <<'PY'
import json, sys
p = "mkosi.extra/usr/share/resident/intents.json"
d = json.load(open(p)); d["image_version"] = sys.argv[1]; json.dump(d, open(p, "w"), indent=1)
PY

# --- break-glass key ---------------------------------------------------------------------------
KEY=${RESIDENT_SSH_PUBKEY:-$(ls ~/.ssh/id_*.pub 2>/dev/null | head -1 || true)}
if [ -n "$KEY" ] && [ -f "$KEY" ]; then
    install -D -m 0644 "$KEY" mkosi.extra/etc/ssh/authorized_keys.d/owner
    echo "# break-glass key: $KEY"
else
    echo "# WARNING: no ssh public key found (set RESIDENT_SSH_PUBKEY) — only the serial console will get you in" >&2
fi

# --- build ---------------------------------------------------------------------------------------
# mkosi needs root (or user namespaces) to build; it will sudo itself if needed.
mkosi --image-version="$VERSION" "$@" build

# --- stage update artifacts in the sysupdate naming scheme --------------------------------------
# sysupdate wants resident_<ver>.efi, resident_<ver>_<partuuid>.root-<arch>.raw, ..._<partuuid>.root-<arch>-verity.raw
OUT=out; UPD=out/updates; mkdir -p "$UPD"
IMG=$(ls "$OUT"/resident_"$VERSION".raw 2>/dev/null || ls "$OUT"/resident*.raw | head -1)
if command -v sfdisk >/dev/null && [ -f "$IMG" ]; then
    python3 - "$IMG" "$OUT" "$UPD" "$VERSION" <<'PY'
import json, shutil, subprocess, sys, glob, os, re
img, out, upd, ver = sys.argv[1:]
parts = json.loads(subprocess.check_output(["sfdisk", "-J", img]))["partitiontable"]["partitions"]
by_type = {}
# discoverable-partitions root / root-verity type GUIDs per architecture (the arch is read off mkosi's split files)
TYPES = {"x86-64": ("4f68bce3-e8cd-4db1-96e7-fbcaf984b709", "2c7357ed-ebd2-46d9-aec1-23d437ec2bf5"),
         "arm64":  ("b921b045-1df0-41c3-af44-4c6f280d3fae", "df3300ce-d69f-4c92-978c-9bfb0f38d820")}
arch = next((m.group(1) for f in sorted(glob.glob(f"{out}/*.root-*.raw"))
             if (m := re.search(r"\.root-(x86-64|arm64)(-verity)?\.raw$", f))), "x86-64")
ROOT, VER = TYPES[arch]
for p in parts:
    t = p["type"].lower(); name = p.get("name", "")
    if name.startswith("resident_"):
        by_type[t] = p["uuid"].lower()
pairs = [(f"root-{arch}", ROOT), (f"root-{arch}-verity", VER)]
for role, t in pairs:
    src = glob.glob(f"{out}/*.{role}.raw")
    if src and t in by_type:
        dst = f"{upd}/resident_{ver}_{by_type[t]}.{role}.raw"; shutil.copy2(src[0], dst); print("staged", dst)
uki = glob.glob(f"{out}/*.efi")
if uki:
    dst = f"{upd}/resident_{ver}.efi"; shutil.copy2(uki[0], dst); print("staged", dst)
PY
    (cd "$UPD" && sha256sum resident_"$VERSION"* > SHA256SUMS) || true
else
    echo "# skipping update staging (no sfdisk or no image)" >&2
fi
echo "# done. boot:  ./boot.sh $IMG      update a running box:  ./update.sh <host> $VERSION"
